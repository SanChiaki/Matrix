"""dsh 驱动：在本机跑 DSH headless（与云侧同一个运行时，但工作目录在本机）。

复用 `agentnet.runtime.DshRuntime`：权限仍然由 `--patch` 覆盖层 + 沙箱强制执行；
区别只在于 MCP 工具端点与审批端点指向连接器本地的小桥（见 connector/bridge.py），
由桥经出站连接把 `tool.call` / `approval.request` 送回网络。
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

from ...runtime import DshRuntime, RunRequest, RuntimeCapabilities, ToolBridge, ApprovalBridge
from .base import RunOutcome, RunSpec


class DshDriver:
    name = "dsh"

    def __init__(self, dsh_bin: str = "dsh", bridge_url: str = "", profile: str = "headless"):
        self.dsh_bin = dsh_bin
        self.bridge_url = bridge_url.rstrip("/")
        self._runtime = DshRuntime(dsh_bin, profile)

    def capabilities(self) -> RuntimeCapabilities:
        # 与云侧同一套覆盖层与沙箱；审批经连接器上行
        return RuntimeCapabilities.all_enforced("dsh", placement="device",
                                                approval="interactive", sandbox="workspace-write")

    def available(self) -> bool:
        return shutil.which(self.dsh_bin) is not None

    def run(self, spec: RunSpec, ctx, cancel: threading.Event) -> RunOutcome:
        if not self.bridge_url:
            return RunOutcome(ok=False, error="DRIVER bridge not started: cannot relay network tools")
        tools = ToolBridge(url=f"{self.bridge_url}/internal/tools", token=spec.run_id,
                           handler=ctx.call_tool, timeout_s=1800,
                           names=list(spec.tool_names) or None) if spec.tool_names else None
        approvals = ApprovalBridge(url=f"{self.bridge_url}/internal/approval", token=spec.run_id,
                                   handler=ctx.ask_approval, timeout_s=3600)
        req = RunRequest(agent_id=spec.agent_id, context_id=spec.context_id, prompt=spec.prompt,
                         cwd=Path(spec.workdir), authority=list(spec.authority),
                         session_id=spec.session_id, tools=tools, on_event=ctx.emit,
                         timeout_s=3600, model=spec.model, approvals=approvals)
        # 取消：DshRuntime 自己按 timeout 杀进程；连接器收到 cancel 时只能等这一轮结束
        # （没有跨进程句柄）。返回值里保留 session，下一轮可以续接，不会丢上下文。
        res = self._runtime.run(req)
        return RunOutcome(session_id=res.session_id, final_text=res.final_text, tool_calls=res.tool_calls,
                          usage=res.usage, ok=res.ok, error=res.error)
