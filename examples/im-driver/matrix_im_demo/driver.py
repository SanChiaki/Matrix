"""演示 IM 的驱动：给企业内部 IM 照着改的样板（接入指南见 docs/im-driver-authoring.md）。

“演示 IM”是虚构的：一条出站 WebSocket，帧是 JSON，协议写在 devserver.py 开头。接你们的 IM 时：
- 换掉标着「协议」的几个方法（登录、解析消息、发送、改消息）；
- 照抄线程结构：连接线程（连接、登录、读帧、断线退避重连）+ 心跳线程 + 派发线程（把消息交给桥）。
  回调不在读帧的线程里跑：桥处理一条消息时会让驱动发消息并等回执，回执也是读帧线程收的；
- 按你们 IM 的能力改 caps。这个样板能改已发的消息（edit）、不能发卡片（cards）：
  桥会先发“收到，正在处理…”再改成正式回复；草稿的“发送 / 放弃”用文字指令。

谁的话能进网络、回复推到哪、按钮认谁，都由桥决定（不变量 25），驱动不做这些判断。
"""

from __future__ import annotations

import json
import os
import queue
import re
import secrets as _secrets
import sys
import threading
import time
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from matrix.im.base import Capabilities, Chat, Driver, DriverError, Field, Inbound, Reply

DEFAULT_URL = "ws://127.0.0.1:8790"     # devserver.py 的默认地址
ACK_TIMEOUT = 8.0                       # 等回执的上限（秒）
MAX_BACKOFF = 30.0                      # 断线重连：1、2、4……秒，封顶 30 秒
MAX_MISSED_PINGS = 2                    # 连续几次心跳没回执就当连接已死


class DemoReply(Reply):
    """能改消息的 IM：第一次 update 发一条新消息，之后改它；finish 改成正式回复（没 update 过就直接回一条）。"""

    def __init__(self, driver: DemoDriver, handle: Any, chat: Chat):
        super().__init__(driver, handle, chat)
        self.message_id: str | None = None
        self.shown = ""                 # 现在消息上的文字：一样就不再改，省下限频的额度

    def update(self, text: str) -> None:
        d: DemoDriver = self.driver  # type: ignore[assignment]
        if text == self.shown:
            return
        self.shown = text
        if self.message_id is None:
            self.message_id = d._send_text(self.chat, text, reply_to=self.handle)
        else:
            d._edit(self.message_id, text)

    def finish(self, text: str) -> None:
        if self.message_id is None:
            self.driver.respond(self.handle, self.chat, text)
        elif text != self.shown:
            self.driver._edit(self.message_id, text)  # type: ignore[attr-defined]


class DemoDriver(Driver):
    kind = "demo-im"
    title = "演示 IM"
    fields = (
        Field("app_id", "App ID", help="在演示 IM 的开放平台建一个机器人应用后得到"),
        Field("app_secret", "App Secret", secret=True, help="和 App ID 一起生成；只保存在 Matrix 的加密保管库里"),
    )
    caps = Capabilities(stream=False, edit=True, cards=False, proactive=True, max_text_bytes=4000,
                        reply_ttl=None, min_update_interval=2.0, rate_per_minute=20)
    heartbeat = 30.0                    # 秒；测试里改小

    def __init__(self, config: dict[str, str], secrets: dict[str, str], sink: Any):
        super().__init__(config, secrets, sink)
        # 地址只由部署方经环境变量给，不做成 fields：用户可填的地址会让网络被当跳板连到任意内网地址
        self.url = os.environ.get("DEMO_IM_URL") or DEFAULT_URL
        self._stop = threading.Event()
        self._ws: ClientConnection | None = None
        self._send_lock = threading.Lock()
        self._acks: dict[str, tuple[threading.Event, list[dict[str, Any]]]] = {}
        self._acks_lock = threading.Lock()
        self._inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._missed = 0
        self._kicked = False

    # ================= 生命周期 =================
    def start(self) -> None:
        self._stop.clear()
        self._kicked = False
        for target, name in ((self._run, "conn"), (self._dispatch, "dispatch")):
            threading.Thread(target=target, name=f"demo-im-{name}", daemon=True).start()

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
            try:
                ws = connect(self.url, open_timeout=10, ping_interval=None, close_timeout=3)
            except Exception as e:
                attempt += 1
                self.sink.on_status("error", f"连不上演示 IM：{type(e).__name__}")
                if self._stop.wait(min(MAX_BACKOFF, 2 ** (attempt - 1))):
                    return
                continue
            self._ws, self._missed = ws, 0
            try:
                ok, err = self._login(ws)
                if not ok:
                    self.sink.on_status("auth_failed", err)  # 凭证不对：别重试，等人改了再点“重新连接”
                    return
                attempt = 0
                self.sink.on_status("online")
                threading.Thread(target=self._heartbeat, args=(ws,), name="demo-im-beat", daemon=True).start()
                for raw in ws:
                    self._frame(raw)
            except ConnectionClosed:
                pass
            except Exception as e:
                self.sink.on_status("error", f"{type(e).__name__}: {e}")
            finally:
                self._ws = None
                self._fail_acks("连接断了")
                try:
                    ws.close()
                except Exception:
                    pass
            if self._kicked:  # 同一个应用在别处登录了：不重连，免得两个实例互相顶
                self.sink.on_status("kicked", "这个机器人在别处连上了（同一个机器人只能有一条连接）")
                return
            if self._stop.is_set():
                return
            attempt += 1
            if self._stop.wait(min(MAX_BACKOFF, 2 ** (attempt - 1))):
                return

    def _heartbeat(self, ws: ClientConnection) -> None:
        while not self._stop.wait(self.heartbeat):
            if self._ws is not ws:
                return
            if self._missed >= MAX_MISSED_PINGS:
                try:
                    ws.close()  # 交给连接线程重连
                except Exception:
                    pass
                return
            self._missed += 1
            try:
                self._raw_send({"op": "ping", "id": _rid("ping")})
            except Exception:
                return

    # ================= 协议：登录 =================
    def _login(self, ws: ClientConnection) -> tuple[bool, str]:
        rid = _rid("auth")
        ws.send(json.dumps({"op": "auth", "id": rid, "app_id": self.config.get("app_id", ""),
                            "app_secret": self.secrets.get("app_secret", "")}))
        deadline = time.time() + 10
        while time.time() < deadline:
            frame = json.loads(ws.recv(timeout=max(0.1, deadline - time.time())))
            if frame.get("op") == "ack" and frame.get("id") == rid:
                return (True, "") if frame.get("ok") else (False, f"演示 IM 拒绝了：{frame.get('error') or '凭证不对'}")
        return False, "演示 IM 没有回应登录"

    # ================= 收 =================
    def _frame(self, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            return
        op = frame.get("op")
        if op == "message":
            self._inbox.put(frame)
        elif op == "kicked":
            self._kicked = True
        elif op == "ack":
            rid = str(frame.get("id", ""))
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
            frame = self._inbox.get()
            if frame is None or self._stop.is_set():
                return
            try:
                msg = self.parse_message(frame)
                if msg is not None:
                    self.sink.on_message(msg)
            except Exception as e:  # 一条消息出错不影响后面的
                print(f"演示 IM 驱动处理消息出错：{type(e).__name__}: {e}", file=sys.stderr)

    def parse_message(self, frame: dict[str, Any]) -> Inbound | None:
        """协议：演示 IM 的消息帧 → Inbound。sender 要是这个人在这个 IM 下稳定不变的标识（绑定就绑它）。"""
        chat = frame.get("chat") or {}
        sender = str(frame.get("from") or "")
        if not sender or chat.get("type") not in ("single", "group") or not chat.get("id"):
            return None
        text = str(frame.get("text") or "")
        if chat["type"] == "group":
            text = re.sub(r"^@\S+\s*", "", text)  # 群里 @ 机器人时开头带着 @名字
        return Inbound(msg_id=str(frame.get("msg_id") or ""), chat=Chat(chat["type"], str(chat["id"])),
                       sender=sender, text=text.strip(), handle=frame.get("msg_id"))

    # ================= 发 =================
    def respond(self, handle: Any, chat: Chat, text: str) -> None:
        self._send_text(chat, text, reply_to=handle)

    def push(self, chat: Chat, text: str) -> None:
        self._send_text(chat, text)

    def open_reply(self, handle: Any, chat: Chat) -> Reply:
        return DemoReply(self, handle, chat)

    def _send_text(self, chat: Chat, text: str, reply_to: Any = None) -> str:
        """协议：发一条消息，返回它的 message_id（改消息时用）。"""
        body: dict[str, Any] = {"chat": {"type": chat.kind, "id": chat.id}, "text": text}
        if reply_to:
            body["reply_to"] = reply_to
        return str(self._request("send", body).get("message_id") or "")

    def _edit(self, message_id: str, text: str) -> None:
        """协议：改一条已经发出的消息。"""
        self._request("edit", {"message_id": message_id, "text": text})

    def _request(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        """发一帧并等回执；失败抛 DriverError（桥把它记在机器人或绑定上）。"""
        rid = _rid(op)
        ev, box = threading.Event(), []
        with self._acks_lock:
            self._acks[rid] = (ev, box)
        try:
            self._raw_send({"op": op, "id": rid, **body})
            if not ev.wait(ACK_TIMEOUT):
                raise DriverError("演示 IM 没有回执")
            ack = box[0]
            if ack.get("_failed") or not ack.get("ok"):
                raise DriverError(str(ack.get("error") or "发送失败"))
            return ack
        finally:
            with self._acks_lock:
                self._acks.pop(rid, None)

    def _raw_send(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise DriverError("还没连上演示 IM")
        with self._send_lock:
            ws.send(json.dumps(frame, ensure_ascii=False))

    def _fail_acks(self, why: str) -> None:
        with self._acks_lock:
            waiters = list(self._acks.values())
        for ev, box in waiters:
            box.append({"_failed": True, "error": why})
            ev.set()


def _rid(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{_secrets.token_hex(4)}"
