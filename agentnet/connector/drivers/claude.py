"""Claude Code 驱动：本机 `claude -p` + stream-json。

能力映射（第 3 节）：

- `web.research` / `file.write` / `shell.exec`：工具白名单。对应工具不在白名单里，Claude Code 不会调用；
  这是 harness 自己执行的限制。
- `delegate`：由网络工具列表决定（MCP 只挂上 grant 里有的工具），与 harness 无关。
- 审批：`--permission-mode bypassPermissions` + 白名单。没有逐次审批上行（`approval: none`），
  所以工作目录以外的写入不会被转给所有者——这也是为什么本驱动声明的 `sandbox` 是 `none`。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import sys

from ...runtime import MCP_SERVER_SCRIPT, ENFORCED, UNAVAILABLE, RuntimeCapabilities, argv, no_window
from .base import RunOutcome, RunSpec

# 宿主环境里这些变量会让子进程里的 claude 改用宿主进程的凭据 / 网关，而不是用户自己的登录，
# 表现为 "Not logged in"。连接器要跑的是"用户的 Claude Code"，所以启动前清掉它们。
HOST_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
            "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
            "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_MESSAGING_SOCKET", "ANTHROPIC_BASE_URL")


def clean_env(env: dict[str, str] | None = None) -> dict[str, str]:
    out = dict(os.environ if env is None else env)
    for k in HOST_ENV:
        out.pop(k, None)
    return out


# 权限 -> Claude Code 工具。只要有一个工具，该权限就是 enforced（工具不在白名单里就调不到）。
CLAUDE_TOOLS = {
    "web.research": ["WebSearch", "WebFetch"],
    "file.write": ["Write", "Edit", "NotebookEdit"],
    "shell.exec": ["Bash"],
}


class ClaudeDriver:
    name = "claude-code"

    def __init__(self, claude_bin: str = "claude", bridge_url: str = ""):
        self.claude_bin = claude_bin
        self.bridge_url = bridge_url.rstrip("/")

    def capabilities(self) -> RuntimeCapabilities:
        enforcement = {a: (ENFORCED if a in CLAUDE_TOOLS else UNAVAILABLE) for a in
                       ("web.research", "file.write", "shell.exec")}
        enforcement["delegate"] = ENFORCED  # MCP 工具由网络决定挂不挂
        # 没有审批上行：越过工作目录的写不会被转给所有者
        return RuntimeCapabilities(name="claude-code", placement="device", enforcement=enforcement,
                                   session_resume=True, steer=False, approval="none", sandbox="none")

    def available(self) -> bool:
        return shutil.which(self.claude_bin) is not None

    def allowed_tools(self, authority: list[str]) -> list[str]:
        out = []
        for a, tools in CLAUDE_TOOLS.items():
            if a in authority:
                out.extend(tools)
        return out

    def command(self, spec: RunSpec) -> list[str]:
        cmd = [self.claude_bin, "-p", "--output-format", "stream-json", "--verbose",
               "--dangerously-skip-permissions"]
        allowed = self.allowed_tools(spec.authority)
        if allowed:
            cmd += ["--allowedTools", ",".join(allowed)]
        cmd += ["--disallowedTools", "Bash(git push *)"]  # 网络里的 Agent 不直接推远端
        if spec.model and spec.model.get("model"):
            cmd += ["--model", spec.model["model"]]
        if spec.session_id:
            cmd += ["--resume", spec.session_id]
        if spec.tool_names and self.bridge_url:
            cmd += ["--mcp-config", json.dumps({"mcpServers": {"agentnet": {
                "type": "stdio", "command": os.environ.get("AGENTNET_MCP_CMD", sys.executable),
                "args": [str(MCP_SERVER_SCRIPT)],
                "env": {"AGENTNET_TOOL_URL": f"{self.bridge_url}/internal/tools",
                        "AGENTNET_TOKEN": spec.run_id,
                        "AGENTNET_TOOLS": ",".join(spec.tool_names)}}}})]
        return cmd

    def run(self, spec: RunSpec, ctx, cancel: threading.Event) -> RunOutcome:
        spec.workdir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(argv(self.command(spec)), cwd=Path(spec.workdir), env=clean_env(),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", creationflags=no_window())
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(spec.prompt)
        proc.stdin.close()
        session_id, final = spec.session_id or "", ""
        tools: list[dict] = []
        result_error = ""
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                session_id = ev.get("session_id") or session_id
            elif t == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "text":
                        ctx.emit({"type": "text", "text": block.get("text", "")})
                    elif block.get("type") == "tool_use":
                        tools.append({"name": block.get("name"), "status": "ok"})
            elif t == "result":
                session_id = ev.get("session_id") or session_id
                final = ev.get("result") or final
                if ev.get("is_error") or ev.get("subtype") not in (None, "success"):
                    result_error = ev.get("result") or ev.get("subtype") or "claude reported an error"
        code = proc.wait()
        err = (proc.stderr.read() if proc.stderr else "")[-2000:]
        if code != 0 or result_error:
            # 退出码之外，harness 自己的 result 事件通常说清了原因（如未登录）；空 stderr 时尤其重要
            detail = err.strip() or result_error or "（没有输出）"
            return RunOutcome(session_id=session_id, tool_calls=tools, ok=False,
                              error=f"SERVER claude exited {code}: {detail}")
        return RunOutcome(session_id=session_id, final_text=final, tool_calls=tools)
