"""IM 桥：IM 驱动和网络之间的唯一通道（docs/reference/im.md，不变量 25）。

规则都在这里写一次，对每个驱动都成立：
- 只认绑定的人：只有经绑定码核验过的 sender 在单聊里发的消息进入网络，以那个人的身份进他的助理对话；
  别人的消息、群里的消息只回一句固定的话，内容不进网络，不启动工作。个人机器人只能绑主人。
- 只推给本人：推出去的都先经 Network.event_view(…, 那个人) 过滤，只推到他自己的单聊；卡片按钮只认他本人。
- 密钥只进保管库：机器人的密钥不出现在任何返回值、事件里；绑定码只存哈希，用一次、有期限、错误次数有上限。

各渠道的历史分开、不互相镜像（参照 OpenAI dots，research: openai-dots#channels-identity）：一条助理消息推不推、推到哪，
看它前面最近的那条人的消息——经 IM 发来的回到那个 IM；网页里发的不推；中间没有人说话（定时、早报、委托回报唤醒的）
推到这个人开了推送的绑定。只有主人能指挥（research: openai-dots#who-can-direct）。

加锁顺序：桥的锁在外、网络的锁在内（收消息时拿着桥的锁调 send_message，消费事件时拿着桥的锁读 store），
保证一条 IM 消息登记好“回到哪里”之前，事件线程看不到它引出的回复。
"""

from __future__ import annotations

import hashlib
import queue
import re
import secrets
import sys
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..models import ImBinding, ImChannel, new_id
from ..vault import VaultError
from .base import Action, Button, Card, Chat, Driver, DriverError, Inbound, Reply
from .registry import describe, load_drivers

if TYPE_CHECKING:
    from ..engine import Network

CODE_TTL = 600                     # 绑定码有效期（秒）
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
MAX_FAILS = 5                      # 同一个 sender 10 分钟内最多试错几次；个人机器人上错满就作废待用的码
FAIL_WINDOW = 600
DEDUPE = 2000                      # 记住最近多少条 msg_id
PROGRESS_INTERVAL = 5.0            # 刷新进度的最小间隔（秒）
TTL_MARGIN = 30.0                  # 回复时限到之前多久先收尾
TICK = 1.0

GROUP_TEXT = "我只在单聊里工作：请私聊我。"
UNBOUND_TEXT = "还没有绑定。在 Matrix 的「助理与定时 → 在 IM 里找助理」拿一个绑定码，在这里发给我。"
WORKING_TEXT = "收到，正在处理…"
LATER_TEXT = "还在做，做好了发给你。"
NO_REPLY_TEXT = "处理完了，这一轮没有新的回复。"

_COMMAND = re.compile(r"^\s*(发送|放弃)\s*(\d{1,4})\s*$")
_CODE_LIKE = re.compile(r"^\s*[A-Za-z0-9]{6,12}\s*$")


def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def split_text(text: str, limit: int) -> list[str]:
    """按 UTF-8 字节切段，不切断字符；尽量在换行处断开。"""
    out: list[str] = []
    rest = text
    while len(rest.encode()) > limit:
        cut = len(rest.encode()[:limit].decode(errors="ignore"))
        nl = rest.rfind("\n", 0, cut)
        if nl > cut // 2:
            cut = nl + 1
        out.append(rest[:cut])
        rest = rest[cut:]
    out.append(rest)
    return [x for x in out if x.strip()] or [""]


@dataclass
class _Code:
    channel_id: str
    user_id: str
    expires: float


@dataclass
class _Pending:
    """一条经 IM 发来、还没收尾的回复。"""

    message_id: str                      # 网络里的那条人的消息
    conv_id: str
    binding_id: str
    channel_id: str
    chat: Chat
    reply: Reply
    started: float
    last_update: float = 0.0
    seen: bool = False                   # 事件线程已看到那条人的消息
    thinking: bool = False               # 之后对话进入过 thinking
    done: bool = False


class _Outbox:
    """一个渠道的发送队列：按顺序发，按会话限频；进度刷新可以丢，正式消息等到能发为止。"""

    def __init__(self, bridge: ImBridge, channel_id: str, driver: Driver):
        self.bridge, self.channel_id, self.driver = bridge, channel_id, driver
        self.q: queue.Queue[tuple[str, Callable[[], None], bool, str | None] | None] = queue.Queue()
        self.sent: dict[str, deque[float]] = {}
        self.thread = threading.Thread(target=self._loop, name=f"matrix-im-{channel_id}", daemon=True)
        self.thread.start()

    def put(self, chat_id: str, fn: Callable[[], None], droppable: bool = False, binding_id: str | None = None) -> None:
        self.q.put((chat_id, fn, droppable, binding_id))

    def close(self) -> None:
        self.q.put(None)

    def idle(self) -> bool:
        return self.q.unfinished_tasks == 0

    def _loop(self) -> None:
        while True:
            item = self.q.get()
            try:
                if item is None:
                    return
                chat_id, fn, droppable, binding_id = item
                if not self._admit(chat_id, droppable):
                    continue
                try:
                    fn()
                except Exception as e:  # 驱动发不出去：记在绑定上，不影响别的消息
                    self.bridge._send_failed(binding_id, e)
            finally:
                self.q.task_done()

    def _admit(self, chat_id: str, droppable: bool) -> bool:
        rate = max(1, self.driver.caps.rate_per_minute)
        stamps = self.sent.setdefault(chat_id, deque())
        while True:
            now = time.time()
            while stamps and now - stamps[0] >= 60:
                stamps.popleft()
            if len(stamps) < rate:
                stamps.append(now)
                return True
            if droppable:
                return False
            time.sleep(min(5.0, 60 - (now - stamps[0]) + 0.05))


class _ChannelSink:
    """交给驱动的 Sink：把回调带上渠道 id 转给桥。"""

    def __init__(self, bridge: ImBridge, channel_id: str):
        self.bridge, self.channel_id = bridge, channel_id

    def on_message(self, msg: Inbound) -> None:
        self.bridge._inbound(self.channel_id, msg)

    def on_action(self, action: Action) -> str | None:
        return self.bridge._action(self.channel_id, action)

    def on_status(self, status: str, error: str = "") -> None:
        self.bridge._status(self.channel_id, status, error)


class ImBridge:
    def __init__(self, net: Network, drivers: dict[str, type[Driver]] | None = None):
        self.net = net
        if drivers is None:
            self.drivers, self.driver_errors = load_drivers()
        else:
            self.drivers, self.driver_errors = dict(drivers), {}
        self._lock = threading.RLock()
        self._links: dict[str, Driver] = {}
        self._outboxes: dict[str, _Outbox] = {}
        self._codes: dict[str, _Code] = {}
        self._fails: dict[tuple[str, str], list[float]] = {}
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._via: OrderedDict[str, str] = OrderedDict()       # 人的消息 id -> 绑定 id（经 IM 发来的）
        self._pending: dict[str, _Pending] = {}                # 人的消息 id -> 还没收尾的回复
        self._short: dict[str, dict[int, str]] = {}            # 用户 -> 文字指令里的编号 -> 草稿 id
        self._running = False
        self._seq = 0
        self._consumer: threading.Thread | None = None

    # ================= 生命周期 =================
    def start(self) -> None:
        """开始消费事件、连上所有启用的渠道。只在服务进程里调用一次（一个机器人只能有一条连接）。"""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._seq = self.net.bus.last_seq
            channels = [c for c in self.net.store.im_channels.values() if c.enabled]
        self._consumer = threading.Thread(target=self._consume, name="matrix-im-events", daemon=True)
        self._consumer.start()
        for c in channels:
            self._start_channel(c.id)

    def stop(self) -> None:
        with self._lock:
            self._running = False
            ids = list(self._links)
        for cid in ids:
            self._stop_channel(cid)

    def flush(self, timeout: float = 10) -> None:
        """测试用：等网络静止、事件处理完、发送队列清空。"""
        deadline = time.time() + timeout
        stable = 0
        while time.time() < deadline:
            self.net.wait_quiet(max(0.1, deadline - time.time()))
            with self._lock:
                boxes = list(self._outboxes.values())
            if self._seq >= self.net.bus.last_seq and all(b.idle() for b in boxes):
                stable += 1
                if stable >= 3:
                    return
            else:
                stable = 0
            time.sleep(0.02)
        raise TimeoutError("IM 桥没有在时限内处理完")

    def _start_channel(self, channel_id: str) -> None:
        with self._lock:
            if not self._running or channel_id in self._links:
                return
            c = self.net.store.im_channels.get(channel_id)
            if c is None or not c.enabled:
                return
            cls = self.drivers.get(c.kind)
            if cls is None:
                self._set_status(c, "error", f"这个部署没有装「{c.kind}」驱动")
                return
            try:
                secret_values = self.net.vault.get(_vault_name(c.id))
            except VaultError:
                self._set_status(c, "error", "保管密钥换过了：重新填一次密钥")
                return
            driver = cls(dict(c.config), secret_values, _ChannelSink(self, c.id))
            self._links[c.id] = driver
            self._outboxes[c.id] = _Outbox(self, c.id, driver)
            self._set_status(c, "connecting", "")
        try:
            driver.start()
        except Exception as e:
            self._status(channel_id, "error", f"{type(e).__name__}: {e}")

    def _stop_channel(self, channel_id: str) -> None:
        with self._lock:
            driver = self._links.pop(channel_id, None)
            box = self._outboxes.pop(channel_id, None)
            for k in [k for k, p in self._pending.items() if p.channel_id == channel_id]:
                self._pending.pop(k)
        if box is not None:
            box.close()
        if driver is not None:
            try:
                driver.stop()
            except Exception:
                pass
        with self._lock:
            c = self.net.store.im_channels.get(channel_id)
            if c is not None and c.status not in ("kicked", "auth_failed", "error"):
                self._set_status(c, "stopped", "")

    def _restart(self, channel_id: str) -> None:
        self._stop_channel(channel_id)
        self._start_channel(channel_id)

    # ================= 驱动报上来的事 =================
    def _status(self, channel_id: str, status: str, error: str = "") -> None:
        kicked = False
        with self._lock:
            c = self.net.store.im_channels.get(channel_id)
            if c is None or channel_id not in self._links:
                return
            if status == "stopped" and c.status in ("kicked", "auth_failed", "error"):
                return
            self._set_status(c, status, error)
            kicked = status in ("kicked", "auth_failed")
        if kicked:  # 被顶掉、凭证不对：停下，等人处理（不自动重连，免得两个实例互相踢）
            threading.Thread(target=self._stop_channel, args=(channel_id,), daemon=True).start()

    def _set_status(self, c: ImChannel, status: str, error: str) -> None:
        """调用方持有桥的锁。"""
        if (c.status, c.error) == (status, error[:300]):
            return
        c.status, c.error = status, error[:300]
        self.net.store.save()
        self._emit(c)

    def _emit(self, c: ImChannel) -> None:
        users = {c.owner_id} if c.owner_id else {b.user_id for b in self.net.store.im_bindings.values()
                                                 if b.channel_id == c.id}
        for u in users:
            if u:
                self.net.bus.emit("im.updated", user_id=u, channel_id=c.id)

    def _respond(self, channel_id: str, inbound: Inbound, text: str) -> None:
        with self._lock:
            driver, box = self._links.get(channel_id), self._outboxes.get(channel_id)
        if driver is not None and box is not None:
            box.put(inbound.chat.id, lambda: driver.respond(inbound.handle, inbound.chat, text))

    def _inbound(self, channel_id: str, msg: Inbound) -> None:
        """不变量 25：只有绑定的人在单聊里说的话进入网络。"""
        b = self._admit_inbound(channel_id, msg)
        if b is not None:
            self._deliver_inbound(channel_id, b.id, msg)

    def _admit_inbound(self, channel_id: str, msg: Inbound) -> ImBinding | None:
        """把关：去重、群聊、没绑定（试绑定码）、文字指令。返回要进网络的那个人的绑定。"""
        with self._lock:
            key = (channel_id, msg.msg_id)
            if key in self._seen:
                return
            self._seen[key] = None
            while len(self._seen) > DEDUPE:
                self._seen.popitem(last=False)
            c = self.net.store.im_channels.get(channel_id)
            if c is None:
                return
            if msg.chat.kind != "single":
                self._respond(channel_id, msg, GROUP_TEXT)
                return
            b = self._binding_of(channel_id, msg.sender)
            if b is None:
                text = msg.text.strip()
                reply = self._try_bind(c, msg.sender, msg.chat, text) if _CODE_LIKE.match(text) else UNBOUND_TEXT
                self._respond(channel_id, msg, reply)
                return
            m = _COMMAND.match(msg.text)
            if m and not msg.files:
                self._respond(channel_id, msg, self._draft_command(b, m.group(1), int(m.group(2))))
                return None
            return b

    def _deliver_inbound(self, channel_id: str, binding_id: str, msg: Inbound) -> None:
        """附件在锁外下载（可能慢）；发消息与登记“回到哪里”在同一把锁里，事件线程不会先看到回复。"""
        from ..engine import NetworkError
        with self._lock:
            b = self.net.store.im_bindings.get(binding_id)
            driver = self._links.get(channel_id)
        if b is None or driver is None:
            return
        home = self.net.home_of(b.user_id)
        if home is None:
            self._respond(channel_id, msg, "你还没有可以私聊的助理：先在 Matrix 里设一个。")
            return
        try:
            files = []
            for f in msg.files:
                data = f.fetch()  # 先下载：有的 IM 下载后才知道文件名
                files.append(self.net.upload_attachment(home.id, f.name, data, f.mime, actor=b.user_id))
        except NetworkError as e:
            self._respond(channel_id, msg, f"没收下附件：{e}")
            return
        except Exception as e:  # 下载、解密失败
            self._respond(channel_id, msg, f"没收下附件：{type(e).__name__}")
            return
        text = msg.text.strip()
        if msg.quote.strip():
            quoted = "\n".join(f"> {line}" for line in msg.quote.strip().splitlines())
            text = f"{quoted}\n\n{text}" if text else quoted
        with self._lock:
            box = self._outboxes.get(channel_id)
            if box is None or self.net.store.im_bindings.get(binding_id) is not b:
                return
            try:
                sent = self.net.send_message(home.id, text, actor=b.user_id, attachments=files,
                                             via=f"来自{driver.title or driver.kind}")
            except NetworkError as e:
                self._respond(channel_id, msg, f"没发出去：{e}")
                return
            self._register(channel_id, b, msg, sent.id, home.id, driver, box)

    def _register(self, channel_id: str, b: ImBinding, msg: Inbound, message_id: str, conv_id: str,
                  driver: Driver, box: _Outbox) -> None:
        """调用方持有桥的锁。"""
        sent_id = message_id
        self._via[sent_id] = b.id
        while len(self._via) > DEDUPE:
            self._via.popitem(last=False)
        reply = driver.open_reply(msg.handle, msg.chat)
        now = time.time()
        p = _Pending(message_id=sent_id, conv_id=conv_id, binding_id=b.id, channel_id=channel_id, chat=msg.chat,
                     reply=reply, started=now, last_update=now)
        self._pending[sent_id] = p
        if driver.caps.stream or driver.caps.edit:
            box.put(msg.chat.id, lambda: reply.update(WORKING_TEXT), droppable=True, binding_id=b.id)
        b.last_inbound_at, b.chat_id = now, msg.chat.id
        self.net.store.save()

    # ---- 绑定 ----
    def _binding_of(self, channel_id: str, sender: str) -> ImBinding | None:
        return next((b for b in self.net.store.im_bindings.values()
                     if b.channel_id == channel_id and b.sender == sender), None)

    def _try_bind(self, c: ImChannel, sender: str, chat: Chat, text: str) -> str:
        """调用方持有桥的锁。"""
        now = time.time()
        fails = [t for t in self._fails.get((c.id, sender), []) if now - t < FAIL_WINDOW]
        self._fails[(c.id, sender)] = fails
        if len(fails) >= MAX_FAILS:
            return "绑定码错的次数太多了，10 分钟后再试。"
        code = self._codes.get(_hash(text))
        if code is None or code.channel_id != c.id or code.expires < now:
            fails.append(now)
            if c.owner_id and sum(len(v) for (cid, _), v in self._fails.items() if cid == c.id) >= MAX_FAILS:
                for h in [h for h, x in self._codes.items() if x.channel_id == c.id]:
                    self._codes.pop(h)  # 个人机器人上有人一直在猜：待用的码作废，主人重新拿
            return "绑定码不对或已过期：在 Matrix 里重新拿一个。"
        self._codes.pop(_hash(text))
        if c.owner_id and code.user_id != c.owner_id:
            return "绑定码不对或已过期：在 Matrix 里重新拿一个。"
        for old in [x for x in self.net.store.im_bindings.values()
                    if x.channel_id == c.id and x.user_id == code.user_id]:
            self.net.store.im_bindings.pop(old.id)
        b = ImBinding(id=new_id("imb"), channel_id=c.id, user_id=code.user_id, sender=sender, chat_id=chat.id,
                      last_inbound_at=now)
        self.net.store.im_bindings[b.id] = b
        self._fails.pop((c.id, sender), None)
        self.net.store.save()
        self.net.bus.emit("im.updated", user_id=b.user_id, channel_id=c.id)
        a = self.net.assistant_of(b.user_id)
        who = a.name if a else "你的助理"
        return f"绑定好了。以后在这里说的话会交给 {who}，它的回复也会发到这里。"

    def issue_code(self, channel_id: str, actor: str) -> dict[str, Any]:
        """给这个人一个一次性绑定码（只显示这一次）。个人机器人只给主人。"""
        with self._lock:
            c = self._visible(channel_id, actor)
            if not c.enabled:
                from ..engine import NetworkError
                raise NetworkError("这个机器人已停用")
            for h in [h for h, x in self._codes.items() if x.channel_id == c.id and x.user_id == actor]:
                self._codes.pop(h)
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
            self._codes[_hash(code)] = _Code(c.id, actor, time.time() + CODE_TTL)
            return {"code": code, "expires_in": CODE_TTL}

    # ---- 草稿：卡片按钮与文字指令 ----
    def _short_of(self, user_id: str, draft_id: str) -> int:
        m = self._short.setdefault(user_id, {})
        for n, d in m.items():
            if d == draft_id:
                return n
        n = max(m, default=0) + 1
        m[n] = draft_id
        return n

    def _draft_command(self, b: ImBinding, verb: str, n: int) -> str:
        draft_id = self._short.get(b.user_id, {}).get(n)
        if draft_id is None:
            return f"没有编号 {n} 的草稿。"
        return self._decide_draft(b, draft_id, "send" if verb == "发送" else "discard")

    def _decide_draft(self, b: ImBinding, draft_id: str, action: str) -> str:
        from ..engine import NetworkError
        try:
            if action == "send":
                self.net.send_draft(draft_id, actor=b.user_id)
                d = self.net.store.drafts.get(draft_id)
                g = self.net.store.projects.get(d.project_id) if d else None
                return f"已发到「{g.name if g else '项目'}」群聊。"
            self.net.discard_draft(draft_id, actor=b.user_id)
            return "这条草稿不发了。"
        except NetworkError as e:
            msg = str(e)
            return "这条草稿不存在。" if msg.startswith("unknown ") else msg

    def _action(self, channel_id: str, action: Action) -> str | None:
        """卡片按钮：只认绑定的那个人自己点的（不变量 25）。"""
        with self._lock:
            b = self._binding_of(channel_id, action.sender)
            if b is None or action.chat.kind != "single":
                return "这张卡片不是给你的。"
            parts = action.key.split(":")
            if len(parts) != 3 or parts[0] != "draft" or parts[2] not in ("send", "discard"):
                return None
            return self._decide_draft(b, parts[1], parts[2])

    # ================= 网络里发生的事 → IM =================
    def _consume(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    return
                seq = self._seq
            events = self.net.bus.wait(seq, TICK)
            for ev in events:
                try:
                    with self._lock:
                        self._handle(ev.type, ev.data)
                except Exception as e:  # 一条事件处理出错不能让整个桥停下
                    print(f"IM 桥处理 {ev.type} 出错：{type(e).__name__}: {e}", file=sys.stderr)
                finally:
                    with self._lock:
                        self._seq = ev.seq
            with self._lock:
                self._tick()
            if self.net.bus.closed:
                return

    def _handle(self, ev_type: str, data: dict[str, Any]) -> None:
        """调用方持有桥的锁。"""
        if not self.net.store.im_bindings:
            return
        if ev_type == "message.added":
            self._on_message(data)
        elif ev_type == "run.progress":
            self._on_progress(data)
        elif ev_type == "conversation.updated":
            self._on_conversation(data)
        elif ev_type == "held.updated":
            h = data.get("held") or {}
            if h.get("status") == "pending" and data.get("user_id"):
                self._notice(data["user_id"], ev_type, data,
                             f"{h.get('agent_name') or '你的 Agent'} 有一条输出用到了别处的资料，等你确认再给别人看：打开 Matrix 处理。")
        elif ev_type == "approval.updated":
            ap = data.get("approval") or {}
            owner = ap.get("owner_id")
            if ap.get("status") == "pending" and owner:
                agent = self.net.store.agents.get(ap.get("agent_id") or "")
                name = agent.name if agent else "你的 Agent"
                what = f"想用你的连接器（{ap.get('tool')}）" if ap.get("kind") == "connector" else \
                    f"想在工作目录以外操作（{ap.get('tool')}）"
                self._notice(owner, ev_type, data, f"{name} {what}，等你批准：打开 Matrix 处理。")

    def _home_conv(self, conv_id: str) -> Any:
        c = self.net.store.conversations.get(conv_id or "")
        if c is None or c.kind != "dm" or not c.owner_id or not self.net._is_home(c):
            return None
        return c

    def _on_message(self, data: dict[str, Any]) -> None:
        conv = self._home_conv(data.get("conversation_id", ""))
        if conv is None:
            return
        mid = (data.get("message") or {}).get("id", "")
        p = self._pending.get(mid)
        if p is not None:
            p.seen = True
            return
        view = self.net.event_view("message.added", data, conv.owner_id)
        if view is None:
            return
        m = view["message"]
        if m["role"] == "event" and m.get("draft_id"):
            self._on_draft(conv, mid, m["draft_id"])
            return
        if m["role"] not in ("agent", "system") or not m.get("text", "").strip():
            return
        pending, bindings = self._route(conv, mid)
        if pending is not None:
            self._finish(pending, m["text"])
        for b in bindings:
            self._push(b, m["text"])

    def _route(self, conv: Any, mid: str) -> tuple[_Pending | None, list[ImBinding]]:
        """这条助理消息回到哪里：看它前面最近的那条人的消息（或上一条助理消息）。"""
        idx = next((i for i in range(len(conv.messages) - 1, -1, -1) if conv.messages[i].id == mid), None)
        if idx is None:
            return None, []
        for prev in reversed(conv.messages[:idx]):
            if prev.role == "user":
                bid = self._via.get(prev.id)
                if bid is None:
                    return None, []  # 网页里发的：不推
                p = self._pending.get(prev.id)
                if p is not None and not p.done:
                    return p, []
                b = self.net.store.im_bindings.get(bid)
                return None, [b] if b is not None else []
            if prev.role == "agent":
                break
        return None, self._push_targets(conv.owner_id)

    def _push_targets(self, user_id: str) -> list[ImBinding]:
        return [b for b in self.net.store.im_bindings.values() if b.user_id == user_id and b.push]

    def _active(self, user_id: str) -> list[_Pending]:
        return [p for p in self._pending.values() if not p.done
                and (b := self.net.store.im_bindings.get(p.binding_id)) is not None and b.user_id == user_id]

    def _notice(self, user_id: str, ev_type: str, data: dict[str, Any], text: str) -> None:
        """等你处理的事：推到正在说话的那个 IM，以及开了推送的绑定。"""
        if self.net.event_view(ev_type, data, user_id) is None:
            return
        targets = {p.binding_id for p in self._active(user_id)} | {b.id for b in self._push_targets(user_id)}
        for bid in targets:
            b = self.net.store.im_bindings.get(bid)
            if b is not None:
                self._push(b, text)

    def _on_draft(self, conv: Any, mid: str, draft_id: str) -> None:
        d = self.net.store.drafts.get(draft_id)
        if d is None or d.owner_id != conv.owner_id or d.status != "pending":
            return
        pending, bindings = self._route(conv, mid)
        targets = ([self.net.store.im_bindings[pending.binding_id]] if pending is not None
                   and pending.binding_id in self.net.store.im_bindings else []) + bindings
        if not targets:
            return
        view = self.net.draft_view(d)
        n = self._short_of(d.owner_id, d.id)
        at = f"会 @ {'、'.join(view['mentions'])}" if view["mentions"] else "没有 @ 任何成员"
        card = Card(title=f"{view['agent_name']} 起草了一条发往「{view['project_name']}」群聊的消息",
                    body=f"{d.text[:1500]}\n\n（{at}；以你的名义发出）",
                    buttons=(Button(f"draft:{d.id}:send", "发送", f"发送 {n}"),
                             Button(f"draft:{d.id}:discard", "放弃", f"放弃 {n}")))
        for b in targets:
            self._send(b, lambda drv, chat, card=card: drv.push_card(chat, card), card.as_text())

    def _on_progress(self, data: dict[str, Any]) -> None:
        step = data.get("step") or {}
        ctx = str(data.get("context_id") or "")
        if step.get("kind") != "text" or "/" not in ctx:
            return
        conv_id = ctx.partition("/")[0]
        for p in self._pending.values():
            if p.conv_id != conv_id or p.done:
                continue
            driver, box = self._links.get(p.channel_id), self._outboxes.get(p.channel_id)
            b = self.net.store.im_bindings.get(p.binding_id)
            if driver is None or box is None or b is None or not (driver.caps.stream or driver.caps.edit):
                continue
            if self.net.event_view("run.progress", data, b.user_id) is None:
                continue
            now = time.time()
            if now - p.last_update < max(PROGRESS_INTERVAL, driver.caps.min_update_interval):
                continue
            p.last_update = now
            text = split_text(f"{WORKING_TEXT}\n\n{str(step.get('text', '')).strip()}", driver.caps.max_text_bytes)[0]
            box.put(p.chat.id, lambda r=p.reply, t=text: r.update(t), droppable=True, binding_id=p.binding_id)

    def _on_conversation(self, data: dict[str, Any]) -> None:
        conv_id = (data.get("conversation") or {}).get("id", "")
        status = (data.get("conversation") or {}).get("status")
        for p in list(self._pending.values()):
            if p.conv_id != conv_id or p.done or not p.seen:
                continue
            if status == "thinking":
                p.thinking = True
            elif p.thinking and status in ("idle", "waiting"):
                self._finish(p, NO_REPLY_TEXT)

    def _tick(self) -> None:
        now = time.time()
        for k, p in list(self._pending.items()):
            if p.done:
                if now - p.started > 3600:
                    self._pending.pop(k, None)
                continue
            driver = self._links.get(p.channel_id)
            ttl = driver.caps.reply_ttl if driver else None
            if ttl and now - p.started > ttl - TTL_MARGIN:
                self._finish(p, LATER_TEXT)  # 之后的回复走主动推送

    # ---- 发出去 ----
    def _finish(self, p: _Pending, text: str) -> None:
        p.done = True
        driver, box = self._links.get(p.channel_id), self._outboxes.get(p.channel_id)
        b = self.net.store.im_bindings.get(p.binding_id)
        if driver is None or box is None or b is None:
            return
        parts = split_text(text, driver.caps.max_text_bytes)
        if not driver.caps.proactive and len(parts) > 1:
            parts = [split_text(text, driver.caps.max_text_bytes - 60)[0] + "\n\n（太长了，其余在 Matrix 里看）"]
        box.put(p.chat.id, lambda r=p.reply, t=parts[0]: r.finish(t), binding_id=b.id)
        for rest in parts[1:]:
            box.put(p.chat.id, lambda t=rest: driver.push(p.chat, t), binding_id=b.id)

    def _push(self, b: ImBinding, text: str) -> None:
        self._send(b, None, text)

    def _send(self, b: ImBinding, fn: Callable[[Driver, Chat], None] | None, text: str) -> None:
        """主动推送：只推到这个人自己的单聊（不变量 25）。fn 为空时推文字，按长度分段。"""
        driver, box = self._links.get(b.channel_id), self._outboxes.get(b.channel_id)
        if driver is None or box is None or not b.chat_id:
            return
        if not driver.caps.proactive:
            self._send_failed(b.id, DriverError(f"{driver.title or driver.kind} 不能主动发消息：只在你发消息后的回复里说话"))
            return
        chat = Chat("single", b.chat_id)
        if fn is not None:
            box.put(chat.id, lambda: fn(driver, chat), binding_id=b.id)
            return
        for part in split_text(text, driver.caps.max_text_bytes):
            box.put(chat.id, lambda t=part: driver.push(chat, t), binding_id=b.id)

    def _send_failed(self, binding_id: str | None, e: Exception) -> None:
        with self._lock:
            b = self.net.store.im_bindings.get(binding_id or "")
            if b is None:
                return
            err = (str(e) or type(e).__name__)[:300]
            if b.error != err:
                b.error = err
                self.net.store.save()
                self.net.bus.emit("im.updated", user_id=b.user_id, channel_id=b.channel_id)

    # ================= 给 REST 用：渠道与绑定 =================
    def _visible(self, channel_id: str, actor: str | None, manage: bool = False) -> ImChannel:
        """调用方持有桥的锁。个人机器人只给主人；部署方的人人看得到（才能绑定），改只给管理员。其余按不存在（404）。"""
        from ..engine import NetworkError
        c = self.net.store.im_channels.get(channel_id)
        ok = c is not None and (actor is None or (c.owner_id == actor if c.owner_id else
                                                  (self.net.is_admin(actor) or (not manage and c.enabled))))
        if not ok:
            raise NetworkError(f"unknown im channel {channel_id}")
        return c  # type: ignore[return-value]

    def _clean(self, kind: str, values: dict[str, Any], old: ImChannel | None = None) -> tuple[dict[str, str], dict[str, str]]:
        from ..engine import NetworkError
        cls = self.drivers.get(kind)
        if cls is None:
            raise NetworkError(f"没有「{kind}」这种 IM")
        config, secret_values = dict(old.config) if old else {}, {}
        have = self.net.vault.get(_vault_name(old.id)) if old else {}
        for f in cls.fields:
            v = str(values.get(f.key, "") or "").strip()
            if len(v) > 2000:
                raise NetworkError(f"{f.label} 太长")
            if f.secret:
                if v:
                    secret_values[f.key] = v
                elif f.key in have:
                    secret_values[f.key] = have[f.key]  # 没填 = 沿用原来的
                elif f.required:
                    raise NetworkError(f"要填 {f.label}")
            else:
                if v or f.key not in config:
                    config[f.key] = v
                if f.required and not config.get(f.key):
                    raise NetworkError(f"要填 {f.label}")
        return config, secret_values

    def channel_view(self, c: ImChannel, actor: str | None) -> dict[str, Any]:
        """调用方持有桥的锁。没有密钥：只告诉设没设。"""
        cls = self.drivers.get(c.kind)
        try:
            have = self.net.vault.get(_vault_name(c.id))
        except VaultError:
            have = {}
        mine = next((b for b in self.net.store.im_bindings.values()
                     if b.channel_id == c.id and b.user_id == actor), None)
        out = {"id": c.id, "kind": c.kind, "title": c.title or (cls.title if cls else c.kind),
               "driver_title": cls.title if cls else c.kind, "shared": c.owner_id is None,
               "config": dict(c.config), "secrets_set": sorted(k for k, v in have.items() if v),
               "enabled": c.enabled, "status": c.status, "error": c.error, "created_at": c.created_at,
               "binding": None}
        if mine is not None:
            out["binding"] = {"id": mine.id, "push": mine.push, "error": mine.error,
                              "last_inbound_at": mine.last_inbound_at, "created_at": mine.created_at}
        if c.owner_id is None and actor is not None and self.net.is_admin(actor):
            out["bound"] = sum(1 for b in self.net.store.im_bindings.values() if b.channel_id == c.id)
        return out

    def drivers_view(self) -> list[dict[str, Any]]:
        return [describe(cls) for cls in self.drivers.values()]

    def my_channels(self, actor: str) -> dict[str, Any]:
        with self._lock:
            mine = [self.channel_view(c, actor) for c in self.net.store.im_channels.values() if c.owner_id == actor]
            shared = [self.channel_view(c, actor) for c in self.net.store.im_channels.values()
                      if c.owner_id is None and c.enabled]
        return {"drivers": self.drivers_view(), "personal": mine, "shared": shared}

    def admin_channels(self, actor: str) -> dict[str, Any]:
        self._admin(actor)
        with self._lock:
            return {"drivers": self.drivers_view(), "driver_errors": dict(self.driver_errors),
                    "channels": [self.channel_view(c, actor) for c in self.net.store.im_channels.values()
                                 if c.owner_id is None]}

    def _admin(self, actor: str | None) -> None:
        self.net._check(actor is None or self.net.is_admin(actor), "admin")

    def create_channel(self, kind: str, values: dict[str, Any], actor: str, title: str = "",
                       shared: bool = False) -> dict[str, Any]:
        """个人机器人：建好就给主人一个绑定码。部署方机器人只有管理员能建。"""
        from ..engine import NetworkError
        if shared:
            self._admin(actor)
        with self._lock:
            if not shared and any(c.owner_id == actor and c.kind == kind for c in self.net.store.im_channels.values()):
                raise NetworkError("你已经接了一个这种 IM 的机器人：先删掉原来的")
            config, secret_values = self._clean(kind, values)
            c = ImChannel(id=new_id("im"), kind=kind, owner_id=None if shared else actor, title=title.strip()[:60],
                          config=config)
            self.net.vault.put(_vault_name(c.id), secret_values)
            self.net.store.im_channels[c.id] = c
            self.net.store.save()
            self._emit(c)
        self._start_channel(c.id)
        with self._lock:
            out = {"channel": self.channel_view(c, actor)}
        if not shared:
            out.update(self.issue_code(c.id, actor))
        return out

    def update_channel(self, channel_id: str, actor: str, values: dict[str, Any] | None = None,
                       title: str | None = None, enabled: bool | None = None) -> dict[str, Any]:
        with self._lock:
            c = self._visible(channel_id, actor, manage=True)
            if c.owner_id is None:
                self._admin(actor)
            if values:
                config, secret_values = self._clean(c.kind, values, c)
                c.config = config
                self.net.vault.put(_vault_name(c.id), secret_values)
            if title is not None:
                c.title = title.strip()[:60]
            if enabled is not None:
                c.enabled = enabled
            self.net.store.save()
            self._emit(c)
        if enabled is False:
            self._stop_channel(channel_id)
        elif values or enabled:
            self._restart(channel_id)
        with self._lock:
            return self.channel_view(c, actor)

    def reconnect(self, channel_id: str, actor: str) -> dict[str, Any]:
        with self._lock:
            c = self._visible(channel_id, actor, manage=True)
            if c.owner_id is None:
                self._admin(actor)
        self._restart(channel_id)
        with self._lock:
            return self.channel_view(c, actor)

    def delete_channel(self, channel_id: str, actor: str) -> None:
        with self._lock:
            c = self._visible(channel_id, actor, manage=True)
            if c.owner_id is None:
                self._admin(actor)
        self._stop_channel(channel_id)
        with self._lock:
            self._emit(c)
            for b in [b for b in self.net.store.im_bindings.values() if b.channel_id == c.id]:
                self.net.store.im_bindings.pop(b.id)
            for h in [h for h, x in self._codes.items() if x.channel_id == c.id]:
                self._codes.pop(h)
            self.net.store.im_channels.pop(c.id, None)
            self.net.vault.delete(_vault_name(c.id))
            self.net.store.save()

    def _own_binding(self, binding_id: str, actor: str) -> ImBinding:
        from ..engine import NetworkError
        b = self.net.store.im_bindings.get(binding_id)
        if b is None or b.user_id != actor:
            raise NetworkError(f"unknown im binding {binding_id}")
        return b

    def update_binding(self, binding_id: str, actor: str, push: bool | None = None) -> None:
        with self._lock:
            b = self._own_binding(binding_id, actor)
            if push is not None:
                b.push = push
            b.error = ""
            self.net.store.save()
        self.net.bus.emit("im.updated", user_id=actor, channel_id=b.channel_id)

    def delete_binding(self, binding_id: str, actor: str) -> None:
        with self._lock:
            b = self._own_binding(binding_id, actor)
            self.net.store.im_bindings.pop(b.id)
            for k in [k for k, p in self._pending.items() if p.binding_id == b.id]:
                self._pending.pop(k)
            self.net.store.save()
        self.net.bus.emit("im.updated", user_id=actor, channel_id=b.channel_id)


def _vault_name(channel_id: str) -> str:
    return f"im-{channel_id}"
