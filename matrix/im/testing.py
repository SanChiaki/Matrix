"""驱动一致性检查：外部驱动包在自己的测试里跑 `assert_driver(MyDriver)`（docs/im-driver-authoring.md）。

只查结构（声明齐不齐、该实现的方法实现了没有），不连真的 IM；行为用 fake.py 的写法在自己的测试里模拟。
"""

from __future__ import annotations

import re

from .base import Capabilities, Driver, Field


def check_driver(cls: type) -> list[str]:
    """返回发现的问题；空列表 = 通过。"""
    problems: list[str] = []
    if not (isinstance(cls, type) and issubclass(cls, Driver)):
        return ["不是 matrix.im.base.Driver 的子类"]
    if not isinstance(cls.kind, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", cls.kind):
        problems.append("kind 要是 2–32 位小写字母、数字、-、_，字母开头")
    if not isinstance(cls.title, str) or not cls.title.strip():
        problems.append("title 不能为空（界面与消息上的“来自 …”用它）")
    if not all(isinstance(f, Field) for f in cls.fields):
        problems.append("fields 里要都是 Field")
    keys = [f.key for f in cls.fields if isinstance(f, Field)]
    if len(keys) != len(set(keys)):
        problems.append("fields 的 key 重复")
    if not isinstance(cls.caps, Capabilities):
        problems.append("caps 要是 Capabilities")
    else:
        c = cls.caps
        if c.max_text_bytes < 200:
            problems.append("caps.max_text_bytes 太小（至少 200）")
        if c.rate_per_minute < 1:
            problems.append("caps.rate_per_minute 至少 1")
        if c.reply_ttl is not None and c.reply_ttl <= 60:
            problems.append("caps.reply_ttl 要大于 60 秒（桥在时限前 30 秒收尾）")
    required = ["start", "stop", "respond"] + (["push"] if isinstance(cls.caps, Capabilities) and cls.caps.proactive else [])
    for name in required:
        if getattr(cls, name) is getattr(Driver, name):
            problems.append(f"没有实现 {name}()")
    if isinstance(cls.caps, Capabilities) and (cls.caps.stream or cls.caps.edit) and cls.open_reply is Driver.open_reply:
        problems.append("caps 声明了 stream / edit，但没有覆盖 open_reply()")
    if isinstance(cls.caps, Capabilities) and cls.caps.cards and cls.push_card is Driver.push_card:
        problems.append("caps 声明了 cards，但没有覆盖 push_card()")
    try:
        cls({}, {}, _NullSink())
    except Exception as e:
        problems.append(f"不能用空配置构造（构造函数里不要连网）：{type(e).__name__}: {e}")
    return problems


def assert_driver(cls: type) -> None:
    problems = check_driver(cls)
    assert not problems, "；".join(problems)


class _NullSink:
    def on_message(self, msg: object) -> None: ...

    def on_action(self, action: object) -> None:
        return None

    def on_status(self, status: str, error: str = "") -> None: ...
