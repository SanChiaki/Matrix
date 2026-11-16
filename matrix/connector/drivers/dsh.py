"""dsh 驱动：在本机跑 DSH headless（与云侧同一个运行时，但工作目录在本机）。

复用 `matrix.runtime.DshRuntime`：权限仍然由 `--patch` 覆盖层 + 沙箱强制执行；
区别只在于 MCP 工具端点与审批端点指向连接器本地的小桥（见 connector/bridge.py），
由桥经出站连接把 `tool.call` / `approval.request` 送回网络。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from pathlib import Path

from ...models import CONNECTOR_SERVER, MCP_SERVER
from ...runtime import (ApprovalBridge, ConnectorBridge, DshRuntime, QuestionBridge, RunRequest, RuntimeCapabilities, ToolBridge, argv,
                        no_window)
from ...web_fetch import parse_policy
from .base import RunOutcome, RunSpec


class DshDriver:
    name = "dsh"

    def __init__(self, dsh_bin: str = "dsh", bridge_url: str = "", profile: str = "headless",
                 state_dir: Path | None = None):
        """state_dir：云侧沙箱客户端里用（docs/cloud-runtime.md 阶段 1a）——这个执行单元的 dsh 状态目录当 HOME，
        不插审批应答者（云侧不审批，不变量 14）；外面已经是沙箱，不再套 Matrix 的外层。端侧不传。"""
        self.dsh_bin = dsh_bin
        self.bridge_url = bridge_url.rstrip("/")
        self.state_dir = state_dir
        # 端侧：不套外层沙箱，审批照旧上行
        self._runtime = DshRuntime(dsh_bin, profile, hosted=state_dir is not None, isolation="off" if state_dir else None)

    def capabilities(self) -> RuntimeCapabilities:
        if self.state_dir is not None:
            return RuntimeCapabilities.all_enforced("dsh", approval="sandbox-only", sandbox="workspace-write")
        # 与云侧同一套覆盖层与沙箱；审批经连接器上行
        return RuntimeCapabilities.all_enforced("dsh", placement="device",
                                                approval="interactive", sandbox="workspace-write")

    def available(self) -> bool:
        return shutil.which(self.dsh_bin) is not None

    def mcp_plugins(self) -> dict[str, str]:
        """用户自己在 dsh 里配置的 MCP 服务：{服务名: 插件 id}。读组合后的配置（--dump-config），不读凭证。"""
        try:
            out = subprocess.run(argv([self.dsh_bin, self._runtime.profile, "--dump-config"]), capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=30,
                                 creationflags=no_window()).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        found: dict[str, str] = {}
        for block in re.split(r"\n(?=- )", "\n" + out):
            if "dsh-mcp-client" not in block:
                continue
            pid = re.search(r"^-\s+id:\s*['\"]?([^'\"\n]+)", block.strip(), re.M) or \
                re.search(r"^\s+- id:\s*['\"]?([^'\"\n]+)", block, re.M)
            server = re.search(r"^\s+serverName:\s*['\"]?([^'\"\n]+)", block, re.M)
            if pid and server and server.group(1).strip() not in (MCP_SERVER, CONNECTOR_SERVER):
                found[server.group(1).strip()] = pid.group(1).strip()
        return found

    def services(self) -> list[str]:
        return [] if self.state_dir is not None else list(self.mcp_plugins())

    def run(self, spec: RunSpec, ctx, cancel: threading.Event) -> RunOutcome:
        if not self.bridge_url:
            return RunOutcome(ok=False, error="DRIVER bridge not started: cannot relay network tools")
        tools = ToolBridge(url=f"{self.bridge_url}/internal/tools", token=spec.run_id,
                           handler=ctx.call_tool, timeout_s=1800,
                           names=list(spec.tool_names) or None) if spec.tool_names else None
        approvals = ApprovalBridge(url=f"{self.bridge_url}/internal/approval", token=spec.run_id,
                                   handler=ctx.ask_approval, timeout_s=3600)
        # 提问：等多久由网络决定（席位里等人、委托里先等委托方），这里给一个足够长的上限
        questions = QuestionBridge(url=f"{self.bridge_url}/internal/question", token=spec.run_id,
                                   handler=ctx.ask_question, timeout_s=4 * 3600) if spec.questions else None
        # 电脑上的连接器：网络没允许的 MCP 服务，在覆盖层里按插件 id 关掉（只开这一轮允许的）
        disabled = [pid for name, pid in self.mcp_plugins().items() if name not in spec.connectors] \
            if spec.connectors is not None else []
        if self.state_dir is not None:
            return self._run_cloud(spec, ctx, cancel, tools, questions)
        req = RunRequest(agent_id=spec.agent_id, context_id=spec.context_id, prompt=spec.prompt,
                         cwd=Path(spec.workdir), authority=list(spec.authority),
                         session_id=spec.session_id, tools=tools, on_event=ctx.emit,
                         timeout_s=spec.timeout_s, model=spec.model, approvals=approvals, cancel=cancel,
                         disabled_plugins=disabled, questions=questions)
        # 超时与取消（人终止）都由 DshRuntime 结束进程；返回值里保留 session，下一轮可以续接，不会丢上下文。
        res = self._runtime.run(req)
        return RunOutcome(session_id=res.session_id, final_text=res.final_text, tool_calls=res.tool_calls,
                          usage=res.usage, ok=res.ok, error=res.error)

    def _run_cloud(self, spec: RunSpec, ctx, cancel: threading.Event, tools, questions) -> RunOutcome:
        """云侧沙箱里的一轮：模型供应商、抓取白名单、云端连接器都来自 run.request（网络算好的），没有审批应答者。"""
        cloud = spec.cloud
        connectors = ConnectorBridge(url=f"{self.bridge_url}/internal/connectors", token=spec.run_id,
                                     list_tools=ctx.list_connectors, call=ctx.call_connector) \
            if cloud.get("cloud_connectors") else None
        fetch = None
        if isinstance(cloud.get("fetch"), dict):
            try:
                fetch = parse_policy(cloud["fetch"])
            except ValueError:
                fetch = None
        env = {str(k): str(v) for k, v in (cloud.get("env") or {}).items()} if isinstance(cloud.get("env"), dict) else {}
        req = RunRequest(agent_id=spec.agent_id, context_id=spec.context_id, prompt=spec.prompt,
                         cwd=Path(spec.workdir), authority=list(spec.authority), session_id=spec.session_id,
                         tools=tools, on_event=ctx.emit, timeout_s=spec.timeout_s, model=spec.model, cancel=cancel,
                         questions=questions, connectors=connectors, fetch=fetch,
                         llm_provider=cloud.get("llm_provider") if isinstance(cloud.get("llm_provider"), dict) else None,
                         env=env, state_dir=self.state_dir, events_file=False)
        res = self._runtime.run(req)
        return RunOutcome(session_id=res.session_id, final_text=res.final_text, tool_calls=res.tool_calls,
                          usage=res.usage, ok=res.ok, error=res.error)
