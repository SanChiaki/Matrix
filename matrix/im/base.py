"""IM 驱动接口：一个驱动只管和某个 IM 收发消息，规则（只认绑定的人、推给谁、推什么）都在 bridge.py。

内置驱动：企业微信（wecom.py）。企业内部 IM 写成独立的 Python 包，用 entry point「matrix.im_drivers」注册一个
Driver 子类即可（见 docs/im-driver-authoring.md 与 registry.py）。驱动之间的差异用 Capabilities 声明，
桥按能力降级：不能流式就只发最后一条，不能发卡片就用文字指令，不能主动推送就只在回复里说话。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class DriverError(Exception):
    """驱动出错（发不出去、凭证不对……）。桥把它记成渠道或绑定的状态，不往上抛。"""


@dataclass(frozen=True)
class Field:
    """接入时要填的一项：界面据此生成表单。secret 的值只进保管库（vault），不回显。"""

    key: str
    label: str
    secret: bool = False
    help: str = ""
    required: bool = True


@dataclass(frozen=True)
class Capabilities:
    stream: bool = False                 # 一条回复能边做边刷新（先占位、最后收尾）
    edit: bool = False                   # 发出的消息能改（没有 stream 时用它模拟刷新）
    cards: bool = False                  # 能发带按钮的卡片，点击经 Sink.on_action 回来
    proactive: bool = True               # 能不经回复、主动给某个单聊发消息
    max_text_bytes: int = 4000           # 一条消息正文的上限（UTF-8 字节）
    reply_ttl: float | None = None       # 一条回复从开始到收尾的时限（秒），None = 不限
    min_update_interval: float = 1.0     # 刷新回复的最小间隔（秒）
    rate_per_minute: int = 20            # 每个会话每分钟最多发几条（含刷新）


@dataclass(frozen=True)
class Chat:
    kind: str                            # single | group
    id: str                              # 单聊：对方在这个 IM 里的标识；群聊：群标识


@dataclass
class InboundFile:
    name: str                            # fetch() 之后可以由驱动改成真实文件名（有的 IM 下载后才知道）
    mime: str
    fetch: Callable[[], bytes]           # 下载（并解密）；只在桥决定收下这条消息之后才调用


@dataclass
class Inbound:
    """IM 里收到的一条消息，驱动把各家的格式统一成这样。"""

    msg_id: str                          # 去重用：IM 可能重推
    chat: Chat
    sender: str                          # 发消息的人在这个 IM（这个机器人）下的稳定标识
    text: str = ""                       # 已去掉开头的 @机器人
    quote: str = ""                      # 引用的那条消息的文字
    files: list[InboundFile] = field(default_factory=list)
    handle: Any = None                   # 驱动自己的回复凭据（如企业微信的 req_id），桥原样交回 open_reply


@dataclass
class Action:
    """卡片按钮被点了。"""

    chat: Chat
    sender: str
    key: str                             # 按钮的 key（Button.key）
    handle: Any = None


@dataclass(frozen=True)
class Button:
    key: str
    label: str
    command: str                         # 不能发卡片时，让人回复这句话代替点按钮


@dataclass(frozen=True)
class Card:
    title: str
    body: str
    buttons: tuple[Button, ...] = ()

    def as_text(self) -> str:
        hint = "，".join(f"回复「{b.command}」{b.label}" for b in self.buttons)
        return f"**{self.title}**\n{self.body}" + (f"\n\n{hint}" if hint else "")


class Sink(Protocol):
    """驱动往上报的三件事。都可能在驱动自己的线程里被调用，桥负责线程安全。"""

    def on_message(self, msg: Inbound) -> None: ...

    def on_action(self, action: Action) -> str | None:
        """返回值：按钮点完后卡片上要显示的结果（能改卡片的驱动用它更新卡片）。"""
        ...

    def on_status(self, status: str, error: str = "") -> None:
        """status：connecting | online | auth_failed | kicked（被别处的连接顶掉，不自动重连）| error | stopped。"""
        ...


class Reply:
    """一条正在写的回复。默认实现：不刷新，收尾时发一条（或在回复里说一句）。"""

    def __init__(self, driver: Driver, handle: Any, chat: Chat):
        self.driver, self.handle, self.chat = driver, handle, chat

    def update(self, text: str) -> None:
        """刷新进度。不能流式也不能改消息的驱动什么都不做。"""

    def finish(self, text: str) -> None:
        self.driver.respond(self.handle, self.chat, text)


class Driver:
    """IM 驱动的基类。子类声明 kind / title / fields / caps，实现 start / stop / respond / push；
    能流式或能改消息的覆盖 open_reply，能发卡片的覆盖 push_card。"""

    kind: str = ""
    title: str = ""                      # 界面上的名字，也是消息上的“来自 …”
    fields: tuple[Field, ...] = ()
    caps: Capabilities = Capabilities()

    def __init__(self, config: dict[str, str], secrets: dict[str, str], sink: Sink):
        self.config, self.secrets, self.sink = config, secrets, sink

    def start(self) -> None:
        """开始连接，立即返回；连接、心跳、重连在驱动自己的线程里，状态经 sink.on_status 报。"""
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def respond(self, handle: Any, chat: Chat, text: str) -> None:
        """回复收到的那条消息（不流式）。"""
        raise NotImplementedError

    def push(self, chat: Chat, text: str) -> None:
        """主动发一条（caps.proactive 为 False 的驱动不会被调用）。"""
        raise NotImplementedError

    def open_reply(self, handle: Any, chat: Chat) -> Reply:
        return Reply(self, handle, chat)

    def push_card(self, chat: Chat, card: Card) -> None:
        self.push(chat, card.as_text())
