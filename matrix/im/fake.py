"""内存里的假 IM 驱动：测试用，也是写驱动时的参考（最少要实现哪些方法、怎么报状态）。

收到的消息由测试调 say() / click() 送进来；发出去的都记在 sent 里：
("respond" | "push" | "update" | "finish" | "card" | "card_update", 会话 id, 文字)。
"""

from __future__ import annotations

import itertools
import threading
from typing import Any

from .base import Action, Capabilities, Card, Chat, Driver, Field, Inbound, InboundFile, Reply

_ids = itertools.count(1)


class FakeReply(Reply):
    def __init__(self, driver: FakeDriver, handle: Any, chat: Chat):
        super().__init__(driver, handle, chat)
        self.done = False

    def update(self, text: str) -> None:
        assert not self.done, "收尾之后不能再刷新"
        self.driver._out("update", self.chat, text)

    def finish(self, text: str) -> None:
        assert not self.done, "一条回复只能收尾一次"
        self.done = True
        self.driver._out("finish", self.chat, text)


class FakeDriver(Driver):
    kind = "fake"
    title = "测试 IM"
    fields = (Field("bot", "机器人"), Field("token", "密钥", secret=True))
    caps = Capabilities(stream=True, cards=True, proactive=True, max_text_bytes=200, reply_ttl=600,
                        min_update_interval=0, rate_per_minute=1000)

    instances: list[FakeDriver] = []

    def __init__(self, config: dict[str, str], secrets: dict[str, str], sink: Any):
        super().__init__(config, secrets, sink)
        self.sent: list[tuple[str, str, str]] = []
        self.running = False
        self.fail_push = False
        self._lock = threading.Lock()
        FakeDriver.instances.append(self)

    # ---- 驱动接口 ----
    def start(self) -> None:
        self.running = True
        if self.secrets.get("token") == "bad":
            self.sink.on_status("auth_failed", "密钥不对")
            return
        self.sink.on_status("online")

    def stop(self) -> None:
        self.running = False
        self.sink.on_status("stopped")

    def respond(self, handle: Any, chat: Chat, text: str) -> None:
        self._out("respond", chat, text)

    def push(self, chat: Chat, text: str) -> None:
        if self.fail_push:
            from .base import DriverError
            raise DriverError("对方还没在这个会话里说过话")
        self._out("push", chat, text)

    def open_reply(self, handle: Any, chat: Chat) -> Reply:
        return FakeReply(self, handle, chat) if self.caps.stream else Reply(self, handle, chat)

    def push_card(self, chat: Chat, card: Card) -> None:
        if not self.caps.cards:
            return super().push_card(chat, card)
        self._out("card", chat, f"{card.title}|{card.body}|" + ",".join(b.key for b in card.buttons))

    # ---- 测试用 ----
    def _out(self, kind: str, chat: Chat, text: str) -> None:
        with self._lock:
            self.sent.append((kind, chat.id, text))

    def say(self, sender: str, text: str = "", group: str | None = None, msg_id: str | None = None,
            files: list[tuple[str, str, bytes]] | None = None, quote: str = "") -> None:
        chat = Chat("group", group) if group else Chat("single", sender)
        self.sink.on_message(Inbound(msg_id=msg_id or f"m{next(_ids)}", chat=chat, sender=sender, text=text,
                                     quote=quote,
                                     files=[InboundFile(n, m, (lambda d=d: d)) for n, m, d in files or []],
                                     handle=f"h{next(_ids)}"))

    def click(self, sender: str, key: str) -> str | None:
        result = self.sink.on_action(Action(chat=Chat("single", sender), sender=sender, key=key))
        if result is not None:
            self._out("card_update", Chat("single", sender), result)
        return result

    def kick(self) -> None:
        self.sink.on_status("kicked", "这个机器人在别处连上了")

    def texts(self, *kinds: str) -> list[str]:
        with self._lock:
            return [t for k, _, t in self.sent if not kinds or k in kinds]


class PlainFakeDriver(FakeDriver):
    """什么都不会的 IM：不能流式、不能改、不能发卡片，也不能主动推送。"""

    kind = "fake-plain"
    title = "简易 IM"
    caps = Capabilities(stream=False, edit=False, cards=False, proactive=False, max_text_bytes=200,
                        min_update_interval=0, rate_per_minute=1000)
