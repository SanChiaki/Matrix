"""企业微信驱动：智能机器人的长连接（API 模式 → 长连接）。

出站连 wss://openws.work.weixin.qq.com，不需要公网回调（服务只监听 127.0.0.1 也能用）。接口事实与出处见
research: wecom-aibot#long-connection（订阅、心跳、单连接、被顶掉）、research: wecom-aibot#limits（流式 10 分钟、限频）、
research: wecom-aibot#identity（userid 可能是加密的：只当作稳定标识，靠绑定码认人）。

线程：一个连接线程（连接、订阅、读帧、断线退避重连），一个心跳线程，一个派发线程（把收到的消息交给桥）。
回调不在读帧的线程里跑：桥处理一条消息时可能要等回执，回执也是读帧线程收的。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import queue
import re
import secrets as _secrets
import sys
import threading
import time
from typing import Any
from urllib.parse import unquote

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from .. import compat
from ..models import ATTACHMENT_LIMIT
from .base import Action, Capabilities, Card, Chat, Driver, DriverError, Field, Inbound, InboundFile, Reply

DEFAULT_URL = "wss://openws.work.weixin.qq.com"
ACK_TIMEOUT = 8.0
MAX_MISSED_PONGS = 2
MAX_BACKOFF = 30.0


def decrypt_media(data: bytes, aeskey: str) -> bytes:
    """消息里图片、文件的解密：AES-256-CBC，密钥是 base64 的 aeskey，IV 取密钥前 16 字节，PKCS#7（块长 32）。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = base64.b64decode(aeskey + "=" * (-len(aeskey) % 4))
    if len(key) != 32:
        raise DriverError("aeskey 长度不对")
    if len(data) % 16:
        data += b"\0" * (16 - len(data) % 16)
    d = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    out = d.update(data) + d.finalize()
    pad = out[-1] if out else 0
    if not 1 <= pad <= 32 or out[-pad:] != bytes([pad]) * pad:
        raise DriverError("解密失败")
    return out[:-pad]


def encrypt_media(data: bytes, aeskey: str) -> bytes:
    """测试用：decrypt_media 的逆过程。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = base64.b64decode(aeskey + "=" * (-len(aeskey) % 4))
    pad = 32 - len(data) % 32
    e = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return e.update(data + bytes([pad]) * pad) + e.finalize()


def _sniff(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG"):
        return "png", "image/png"
    if data.startswith(b"GIF8"):
        return "gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return "jpg", "image/jpeg"


def _disposition_name(header: str) -> str:
    m = re.search(r"filename\*=UTF-8''([^;\s]+)", header, re.I) or re.search(r'filename="?([^";]+)"?', header, re.I)
    return unquote(m.group(1)).strip() if m else ""


def _req_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{_secrets.token_hex(4)}"


class WecomReply(Reply):
    """流式回复：第一次用某个 stream id 新建一条消息，之后同一个 id 刷新，finish 收尾（10 分钟内）。"""

    def __init__(self, driver: WecomDriver, handle: Any, chat: Chat):
        super().__init__(driver, handle, chat)
        self.stream_id = _secrets.token_hex(8)

    def update(self, text: str) -> None:
        self.driver._stream(self.handle, self.stream_id, text, finish=False)  # type: ignore[attr-defined]

    def finish(self, text: str) -> None:
        self.driver._stream(self.handle, self.stream_id, text, finish=True)  # type: ignore[attr-defined]


class WecomDriver(Driver):
    kind = "wecom"
    title = "企业微信"
    fields = (
        Field("bot_id", "Bot ID", help="企业微信工作台 → 智能机器人 → 创建机器人 → API 模式，选「长连接」后得到"),
        Field("secret", "Secret", secret=True, help="和 Bot ID 一起生成；只保存在 Matrix 的加密保管库里"),
    )
    caps = Capabilities(stream=True, edit=False, cards=True, proactive=True, max_text_bytes=20000, reply_ttl=600,
                        min_update_interval=1.0, rate_per_minute=30)
    heartbeat = 30.0                     # 秒；测试里改小

    def __init__(self, config: dict[str, str], secrets: dict[str, str], sink: Any):
        super().__init__(config, secrets, sink)
        # 私有化部署的企业微信地址不同：部署方用 MATRIX_WECOM_URL 指定（不给用户填，免得网络被当跳板连到任意地址）
        self.url = compat.env("WECOM_URL", DEFAULT_URL) or DEFAULT_URL
        self._stop = threading.Event()
        self._ws: ClientConnection | None = None
        self._send_lock = threading.Lock()
        self._acks: dict[str, tuple[threading.Event, list[dict[str, Any]]]] = {}
        self._acks_lock = threading.Lock()
        self._inbox: queue.Queue[Any] = queue.Queue()
        self._missed = 0
        self._kicked = False
        self._threads: list[threading.Thread] = []

    # ================= 生命周期 =================
    def start(self) -> None:
        self._stop.clear()
        for target, name in ((self._run, "conn"), (self._dispatch, "dispatch")):
            t = threading.Thread(target=target, name=f"matrix-wecom-{name}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._inbox.put(None)
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            self.sink.on_status("connecting", "" if attempt == 0 else "连接断了，正在重连")
            opened = False
            try:
                with connect(self.url, open_timeout=10, ping_interval=None, close_timeout=3,
                             max_size=8 * 2 ** 20) as ws:
                    opened = True
                    result = self._session(ws)
            except Exception as e:
                if opened:  # 关连接时出的错：当作断线
                    result = "dropped"
                else:
                    attempt += 1
                    self.sink.on_status("error", f"连不上企业微信：{type(e).__name__}")
                    if self._stop.wait(min(MAX_BACKOFF, 2 ** (attempt - 1))):
                        return
                    continue
            if result == "auth_failed":
                return
            if result == "online":
                attempt = 0
            if self._kicked:
                self.sink.on_status("kicked", "这个机器人在别处连上了（同一个机器人只能有一条连接）")
                return
            if self._stop.is_set():
                return
            attempt += 1
            if self._stop.wait(min(MAX_BACKOFF, 2 ** (attempt - 1))):
                return

    def _session(self, ws: ClientConnection) -> str:
        """一条连接从订阅到断开。返回 auth_failed（订阅被拒）、online（订阅成功过）或 dropped（没订阅上就断了）。"""
        self._ws, self._missed = ws, 0
        result = "dropped"
        try:
            ok, err = self._subscribe(ws)
            if not ok:
                self.sink.on_status("auth_failed", err)
                return "auth_failed"
            result = "online"
            self.sink.on_status("online")
            beat = threading.Thread(target=self._heartbeat, args=(ws,), name="matrix-wecom-beat", daemon=True)
            beat.start()
            for raw in ws:
                self._frame(raw)
        except ConnectionClosed:
            pass
        except Exception as e:
            self.sink.on_status("error", f"{type(e).__name__}: {e}")
        finally:
            self._ws = None
            self._fail_acks("连接断了")
        return result

    def _subscribe(self, ws: ClientConnection) -> tuple[bool, str]:
        rid = _req_id("aibot_subscribe")
        ws.send(json.dumps({"cmd": "aibot_subscribe", "headers": {"req_id": rid},
                            "body": {"bot_id": self.config.get("bot_id", ""), "secret": self.secrets.get("secret", "")}}))
        deadline = time.time() + 10
        while time.time() < deadline:
            frame = json.loads(ws.recv(timeout=max(0.1, deadline - time.time())))
            if (frame.get("headers") or {}).get("req_id") == rid:
                if frame.get("errcode") in (0, None):
                    return True, ""
                return False, f"企业微信拒绝了：{frame.get('errmsg') or frame.get('errcode')}（检查 Bot ID、Secret 与长连接权限）"
        return False, "企业微信没有回应订阅"

    def _heartbeat(self, ws: ClientConnection) -> None:
        while not self._stop.wait(self.heartbeat):
            if self._ws is not ws:
                return
            if self._missed >= MAX_MISSED_PONGS:  # 连续收不到回执：当作断了，交给重连
                try:
                    ws.close()
                except Exception:
                    pass
                return
            self._missed += 1
            try:
                self._send({"cmd": "ping", "headers": {"req_id": _req_id("ping")}})
            except Exception:
                return

    # ================= 收 =================
    def _frame(self, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            return
        cmd = frame.get("cmd")
        rid = (frame.get("headers") or {}).get("req_id", "")
        if cmd in ("aibot_msg_callback", "aibot_event_callback"):
            body = frame.get("body") or {}
            ev = body.get("event") if isinstance(body.get("event"), dict) else {}
            if ev.get("eventtype") == "disconnected_event":  # 新连接把这条顶掉了：不重连
                self._kicked = True
                return
            self._inbox.put((cmd, rid, body))
            return
        if rid.startswith("ping"):
            self._missed = 0
            return
        with self._acks_lock:
            waiter = self._acks.get(rid)
        if waiter is not None:
            waiter[1].append(frame)
            waiter[0].set()

    def _dispatch(self) -> None:
        while True:
            item = self._inbox.get()
            if item is None or self._stop.is_set():
                return
            cmd, rid, body = item
            try:
                if cmd == "aibot_msg_callback":
                    msg = self.parse_message(body, rid)
                    if msg is not None:
                        self.sink.on_message(msg)
                else:
                    self._on_event(body, rid)
            except Exception as e:  # 一条消息出错不影响后面的
                print(f"企业微信驱动处理消息出错：{type(e).__name__}: {e}", file=sys.stderr)

    def parse_message(self, body: dict[str, Any], rid: str) -> Inbound | None:
        """企业微信的消息体 → Inbound。不是普通用户发的（compat_info.source）不收。"""
        if (body.get("compat_info") or {}).get("source"):
            return None
        sender = str((body.get("from") or {}).get("userid") or "")
        if not sender:
            return None
        group = body.get("chattype") == "group"
        chat = Chat("group", str(body.get("chatid") or "")) if group else Chat("single", sender)
        texts, files = self._content(body)
        text = "\n".join(texts).strip()
        if group:
            text = re.sub(r"^@\S+\s*", "", text)
        quote = body.get("quote") or {}
        qtexts, _ = self._content(quote) if quote else ([], [])
        return Inbound(msg_id=str(body.get("msgid") or rid), chat=chat, sender=sender, text=text,
                       quote="\n".join(qtexts), files=files, handle=rid)

    def _content(self, body: dict[str, Any]) -> tuple[list[str], list[InboundFile]]:
        kind = body.get("msgtype")
        texts: list[str] = []
        files: list[InboundFile] = []
        if kind == "text":
            texts.append(str((body.get("text") or {}).get("content", "")))
        elif kind == "voice":  # 企业微信已经转成文字
            texts.append(str((body.get("voice") or {}).get("content", "")))
        elif kind == "mixed":
            for it in (body.get("mixed") or {}).get("msg_item") or []:
                if it.get("msgtype") == "text":
                    texts.append(str((it.get("text") or {}).get("content", "")))
                elif it.get("msgtype") == "image":
                    files.append(self._file(it.get("image") or {}, "image"))
        elif kind in ("image", "file", "video"):
            files.append(self._file(body.get(kind) or {}, kind))
        return texts, files

    def _file(self, item: dict[str, Any], kind: str) -> InboundFile:
        url, aeskey = str(item.get("url", "")), str(item.get("aeskey", ""))
        default = {"image": "图片", "video": "视频"}.get(kind, "文件")
        f = InboundFile(name=str(item.get("filename") or default), mime="", fetch=lambda: b"")

        def fetch() -> bytes:
            from ..mcp_client import fetch_public
            data, disposition = fetch_public(url, ATTACHMENT_LIMIT + 64)
            if aeskey:
                data = decrypt_media(data, aeskey)
            name = _disposition_name(disposition)
            if kind == "image" and not name:
                ext, mime = _sniff(data)
                f.name, f.mime = f"{default}.{ext}", mime
            elif name:
                f.name = name
                f.mime = mimetypes.guess_type(name)[0] or ""
            return data

        f.fetch = fetch
        return f

    def _on_event(self, body: dict[str, Any], rid: str) -> None:
        ev = body.get("event") or {}
        if ev.get("eventtype") != "template_card_event":
            return
        card = ev.get("template_card_event") if isinstance(ev.get("template_card_event"), dict) else ev
        sender = str((body.get("from") or {}).get("userid") or "")
        group = body.get("chattype") == "group"
        chat = Chat("group", str(body.get("chatid") or "")) if group else Chat("single", sender)
        key = str(card.get("event_key") or "")
        result = self.sink.on_action(Action(chat=chat, sender=sender, key=key, handle=rid))
        if result is not None:  # 5 秒内更新卡片：按钮换成处理结果
            self._request("aibot_respond_update_msg", {
                "response_type": "update_template_card",
                "template_card": {"card_type": "button_interaction", "task_id": str(card.get("task_id") or ""),
                                  "main_title": {"title": result[:26]},
                                  "button_list": [{"text": "已处理", "style": 2, "key": "done"}]}}, rid=rid)

    # ================= 发 =================
    def _send(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise DriverError("企业微信没连上")
        with self._send_lock:
            ws.send(json.dumps(frame, ensure_ascii=False))

    def _request(self, cmd: str, body: dict[str, Any], rid: str | None = None) -> dict[str, Any]:
        """发一帧并等回执；同一个 req_id 一次只有一帧在等（调用方——桥的发送队列——本来就是一条一条发）。"""
        rid = rid or _req_id(cmd)
        ev: threading.Event = threading.Event()
        box: list[dict[str, Any]] = []
        with self._acks_lock:
            self._acks[rid] = (ev, box)
        try:
            self._send({"cmd": cmd, "headers": {"req_id": rid}, "body": body})
            if not ev.wait(ACK_TIMEOUT):
                raise DriverError("企业微信没有回执")
            if not box:
                raise DriverError("连接断了")
            ack = box[0]
            if ack.get("errcode") not in (0, None):
                raise DriverError(f"企业微信拒绝了：{ack.get('errmsg') or ack.get('errcode')}")
            return ack
        finally:
            with self._acks_lock:
                if self._acks.get(rid, (None,))[0] is ev:
                    self._acks.pop(rid, None)

    def _fail_acks(self, _reason: str) -> None:
        with self._acks_lock:
            waiters = list(self._acks.values())
        for ev, _ in waiters:
            ev.set()

    def _stream(self, rid: str, stream_id: str, text: str, finish: bool) -> None:
        self._request("aibot_respond_msg", {"msgtype": "stream",
                                            "stream": {"id": stream_id, "finish": finish, "content": text}}, rid=rid)

    def open_reply(self, handle: Any, chat: Chat) -> Reply:
        return WecomReply(self, handle, chat)

    def respond(self, handle: Any, chat: Chat, text: str) -> None:
        self._stream(handle, _secrets.token_hex(8), text, finish=True)

    def push(self, chat: Chat, text: str) -> None:
        self._request("aibot_send_msg", {"chatid": chat.id, "chat_type": 1 if chat.kind == "single" else 2,
                                         "msgtype": "markdown", "markdown": {"content": text}})

    def push_card(self, chat: Chat, card: Card) -> None:
        """正文先发一条 markdown（卡片上的字数很有限），再发一张只有标题和按钮的卡片。"""
        self.push(chat, f"**{card.title}**\n{card.body}")
        self._request("aibot_send_msg", {
            "chatid": chat.id, "chat_type": 1 if chat.kind == "single" else 2, "msgtype": "template_card",
            "template_card": {"card_type": "button_interaction", "task_id": f"matrix_{_secrets.token_hex(8)}",
                              "main_title": {"title": card.title[:26]},
                              "button_list": [{"text": b.label, "style": 1 if i == 0 else 2, "key": b.key}
                                              for i, b in enumerate(card.buttons)]}})
