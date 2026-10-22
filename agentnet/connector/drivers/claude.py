"""Claude Code 驱动：本机 `claude -p` + stream-json。

能力映射（第 3 节）：

- `web.research` / `file.write` / `shell.exec`：工具白名单。对应工具不在白名单里，Claude Code 不会调用；
  这是 harness 自己执行的限制。
- `delegate`：由网络工具列表决定（MCP 只挂上 grant 里有的工具），与 harness 无关。
- 审批：`--permission-mode bypassPermissions` + 白名单。没有逐次审批上行（`approval: none`），
  所以工作目录以外的写入不会被转给所有者——这也是为什么本驱动声明的 `sandbox` 是 `none`。
- `connector.use`：`--strict-mcp-config`，只加载网络这一轮允许的服务。允许的那几个的配置从用户自己的
  `~/.claude.json`（用户级 `mcpServers`）原样取出，写进工作目录以外的临时文件（0600），跑完删掉。
- 技能：网络下发的 `skills/` 与私有目录的 `agent-home/skills/` 拷进工作目录的 `.claude/skills/`
  （前者优先），跑完删掉。用户自己 `~/.claude/skills/` 里的技能照常生效：那是所有者自己机器上的能力。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import sys

from ...models import AGENT_HOME, SKILLS
from ...runtime import MCP_SERVER_SCRIPT, ENFORCED, UNAVAILABLE, RuntimeCapabilities, argv, no_window
from .base import RunOutcome, RunSpec, guard

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

    def __init__(self, claude_bin: str = "claude", bridge_url: str = "", config_path: Path | None = None):
        self.claude_bin = claude_bin
        self.bridge_url = bridge_url.rstrip("/")
        self.config_path = config_path or Path.home() / ".claude.json"  # 用户级 MCP 服务在这里

    def capabilities(self) -> RuntimeCapabilities:
        enforcement = {a: (ENFORCED if a in CLAUDE_TOOLS else UNAVAILABLE) for a in
                       ("web.research", "file.write", "shell.exec")}
        enforcement["delegate"] = ENFORCED  # MCP 工具由网络决定挂不挂
        enforcement["connector.use"] = ENFORCED  # --strict-mcp-config：只加载网络允许的服务
        # 没有审批上行：越过工作目录的写不会被转给所有者
        return RuntimeCapabilities(name="claude-code", placement="device", enforcement=enforcement,
                                   session_resume=True, steer=False, approval="none", sandbox="none")

    def available(self) -> bool:
        return shutil.which(self.claude_bin) is not None

    def user_servers(self) -> dict[str, dict]:
        """用户在 Claude Code 里配置的用户级 MCP 服务（名字 → 配置）。配置只在本机用，不上报。"""
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        return {str(k): v for k, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {}

    def services(self) -> list[str]:
        return list(self.user_servers())

    def mcp_servers(self, spec: RunSpec) -> dict[str, dict]:
        """这一轮要加载的 MCP 服务：网络工具 + 网络允许的、用户自己配置的服务。"""
        servers: dict[str, dict] = {}
        if spec.tool_names and self.bridge_url:
            servers["agentnet"] = {
                "type": "stdio", "command": os.environ.get("AGENTNET_MCP_CMD", sys.executable),
                "args": [str(MCP_SERVER_SCRIPT)],
                "env": {"AGENTNET_TOOL_URL": f"{self.bridge_url}/internal/tools",
                        "AGENTNET_TOKEN": spec.run_id,
                        "AGENTNET_TOOLS": ",".join(spec.tool_names)}}
        if spec.connectors:
            mine = self.user_servers()
            servers.update({n: mine[n] for n in spec.connectors if n in mine and n != "agentnet"})
        return servers

    def allowed_tools(self, authority: list[str]) -> list[str]:
        out = []
        for a, tools in CLAUDE_TOOLS.items():
            if a in authority:
                out.extend(tools)
        return out

    def command(self, spec: RunSpec, mcp_config: Path | None = None) -> list[str]:
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
        if mcp_config is not None:
            cmd += ["--mcp-config", str(mcp_config)]
        elif spec.tool_names and self.bridge_url:
            cmd += ["--mcp-config", json.dumps({"mcpServers": self.mcp_servers(spec)})]
        if spec.connectors is not None:  # 只加载上面给的；用户配置的其余服务这一轮不开
            cmd.append("--strict-mcp-config")
        return cmd

    @staticmethod
    def mount_skills(workdir: Path) -> Path | None:
        """skills/（网络合并好的）与 agent-home/skills/ 拷进 .claude/skills/，前者优先。返回挂上的目录。"""
        dest = workdir / ".claude" / SKILLS
        shutil.rmtree(dest, ignore_errors=True)
        roots = [workdir / SKILLS, workdir / AGENT_HOME / SKILLS]
        names = [(r, d) for r in roots if r.is_dir() for d in sorted(r.iterdir())
                 if d.is_dir() and (d / "SKILL.md").is_file()]
        if not names:
            return None
        for _, d in names:
            if not (dest / d.name).exists():
                shutil.copytree(d, dest / d.name, symlinks=False)
        return dest

    def run(self, spec: RunSpec, ctx, cancel: threading.Event) -> RunOutcome:
        spec.workdir.mkdir(parents=True, exist_ok=True)
        skills = self.mount_skills(Path(spec.workdir))
        config: Path | None = None
        if spec.connectors is not None:
            # 用户服务的配置可能带密钥：写在工作目录以外、只有自己能读，跑完就删
            fd, name = tempfile.mkstemp(prefix="matrix-mcp-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": self.mcp_servers(spec)}, f)
            config = Path(name)
        try:
            return self._run(spec, ctx, cancel, config)
        finally:
            if config is not None:
                config.unlink(missing_ok=True)
            if skills is not None:
                shutil.rmtree(skills, ignore_errors=True)

    def _run(self, spec: RunSpec, ctx, cancel: threading.Event, config: Path | None) -> RunOutcome:
        proc = subprocess.Popen(argv(self.command(spec, config)), cwd=Path(spec.workdir), env=clean_env(),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", creationflags=no_window())
        assert proc.stdin is not None and proc.stdout is not None
        timed_out = guard(proc, spec.timeout_s, cancel)
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
        if timed_out.is_set():
            return RunOutcome(session_id=session_id, tool_calls=tools, ok=False,
                              error=f"TIMEOUT: 超过 {int(spec.timeout_s or 0)} 秒，连接器终止了这次运行")
        if code != 0 or result_error:
            # 退出码之外，harness 自己的 result 事件通常说清了原因（如未登录）；空 stderr 时尤其重要
            detail = err.strip() or result_error or "（没有输出）"
            return RunOutcome(session_id=session_id, tool_calls=tools, ok=False,
                              error=f"SERVER claude exited {code}: {detail}")
        return RunOutcome(session_id=session_id, final_text=final, tool_calls=tools)
