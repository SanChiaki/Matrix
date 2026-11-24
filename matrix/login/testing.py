"""驱动一致性检查：外部驱动包在自己的测试里跑 `assert_driver(MyDriver)`（docs/login-driver-authoring.md）。

只查结构（声明齐不齐、该实现的方法实现了没有），不连真的身份系统；行为在驱动自己的测试里对着假的身份系统测。
"""

from __future__ import annotations

import re

from .base import Driver


def check_driver(cls: type) -> list[str]:
    """返回发现的问题；空列表 = 通过。"""
    if not (isinstance(cls, type) and issubclass(cls, Driver)):
        return ["不是 matrix.login.base.Driver 的子类"]
    problems: list[str] = []
    if not isinstance(cls.kind, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", cls.kind):
        problems.append("kind 要是 2–32 位小写字母、数字、-、_，字母开头")
    if not isinstance(cls.title, str) or not cls.title.strip():
        problems.append("title 不能为空")
    if not isinstance(cls.state_param, str) or not cls.state_param:
        problems.append("state_param 不能为空（回调里哪个参数带着 flow.state）")
    for name in ("authorize_url", "identify"):
        if getattr(cls, name) is getattr(Driver, name):
            problems.append(f"没有实现 {name}()")
    return problems


def assert_driver(cls: type) -> None:
    problems = check_driver(cls)
    assert not problems, "；".join(problems)
