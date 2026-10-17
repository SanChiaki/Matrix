"""端侧驱动接口（docs/device-runtimes.md 第 2.3 节）。

一个驱动只做一件事：把一次 `run.request` 落成本机 harness 的一次执行，边跑边报告过程，
需要网络工具或审批时通过 `RunCtx` 回到连接器。驱动不解析 URL、不持有令牌、不自己批准。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ...runtime import RuntimeCapabilities


@dataclass
class RunSpec:
    """连接器从 `run.request` 解析出来的这一轮。名称与协议字段一一对应。"""

    run_id: str
    agent_id: str
    context_id: str
    prompt: str
    workdir: Path                     # 本机解出来的工作目录（由 workdir_ref + 根目录拼成）
    authority: list[str]
    tool_names: list[str]
    session_id: str | None = None
    model: dict[str, str] | None = None


@dataclass
class RunOutcome:
    session_id: str = ""
    final_text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    ok: bool = True
    error: str | None = None


class RunCtx(Protocol):
    """驱动在运行期间能用的三件事。"""

    def emit(self, event: dict[str, Any]) -> None: ...          # 过程事件（run.event）
    def call_tool(self, name: str, args: dict[str, Any]) -> tuple[str, bool]: ...  # run.request.tool_names 之内
    def ask_approval(self, tool: str, reason: str) -> str: ...  # allowed-once | rejected | cancelled | unavailable


class Driver(Protocol):
    name: str

    def capabilities(self) -> RuntimeCapabilities: ...
    def available(self) -> bool: ...                              # harness 是否装在本机
    def run(self, spec: RunSpec, ctx: RunCtx, cancel: threading.Event) -> RunOutcome: ...


class DriverError(RuntimeError):
    pass


def event_text(ev: dict[str, Any]) -> str | None:
    """把各家的事件压成一个可读的进度行；取不到返回 None。"""
    if ev.get("type") in ("text", "assistant_text"):
        return ev.get("text")
    return None


__all__ = ["Driver", "DriverError", "RunCtx", "RunOutcome", "RunSpec", "Callable", "event_text"]
