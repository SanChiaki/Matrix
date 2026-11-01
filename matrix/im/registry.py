"""可用的 IM 驱动：内置的，加上装在同一个环境里、经 entry point「matrix.im_drivers」注册的外部驱动包。

外部包在 pyproject.toml 里写：

    [project.entry-points."matrix.im_drivers"]
    acme = "acme_im.driver:AcmeDriver"

名字和内置的重名时以内置为准（外部包不能顶替企业微信驱动）。加载失败的驱动跳过并记下原因，不影响服务启动。
"""

from __future__ import annotations

from importlib.metadata import entry_points

from .base import Driver
from .wecom import WecomDriver

GROUP = "matrix.im_drivers"
BUILTIN: dict[str, type[Driver]] = {WecomDriver.kind: WecomDriver}


def load_drivers() -> tuple[dict[str, type[Driver]], dict[str, str]]:
    """返回（kind -> 驱动类，加载失败的 entry point -> 原因）。"""
    out: dict[str, type[Driver]] = dict(BUILTIN)
    errors: dict[str, str] = {}
    for ep in entry_points(group=GROUP):
        try:
            cls = ep.load()
        except Exception as e:  # 外部包写错了：跳过，不拖垮服务
            errors[ep.name] = f"{type(e).__name__}: {e}"
            continue
        if not (isinstance(cls, type) and issubclass(cls, Driver)) or not cls.kind:
            errors[ep.name] = "不是带 kind 的 matrix.im.base.Driver 子类"
            continue
        if cls.kind in BUILTIN:
            errors[ep.name] = f"kind「{cls.kind}」和内置驱动重名"
            continue
        out.setdefault(cls.kind, cls)
    return out, errors


def describe(cls: type[Driver]) -> dict:
    """给界面的驱动说明：要填哪些字段、有什么能力。"""
    c = cls.caps
    return {"kind": cls.kind, "title": cls.title,
            "fields": [{"key": f.key, "label": f.label, "secret": f.secret, "help": f.help, "required": f.required}
                       for f in cls.fields],
            "caps": {"stream": c.stream, "edit": c.edit, "cards": c.cards, "proactive": c.proactive}}
