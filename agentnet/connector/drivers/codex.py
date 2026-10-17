"""Codex CLI 驱动：本机 `codex exec --json`。

能力映射（第 3 节）：

- `file.write` / `shell.exec`：由 `-s read-only | workspace-write` 沙箱强制。Codex 的 shell 是核心能力，
  关不掉；只能靠沙箱把写入限制在工作目录。所以有 `file.write` 时是 `workspace-write`，没有时是只读。
- `web.research`：Codex 的联网开关不在 exec 的参数里（走 config / 运行时），本驱动按 `advisory` 处理，
  也就是**不进入有效权限**。要放开需要在 `~/.codex/config.toml` 里自行开启，这属于所有者自己的环境决定。
- 审批：`-s` 沙箱拒绝即失败，没有逐次上行（`approval: none`）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path

from ...runtime import ADVISORY, ENFORCED, RuntimeCapabilities
from .base import RunOutcome, RunSpec


class CodexDriver:
    name = "codex"

    def __init__(self, codex_bin: str = "codex"):
        self.codex_bin = codex_bin

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            name="codex", placement="device",
            enforcement={"web.research": ADVISORY, "file.write": ENFORCED, "shell.exec": ENFORCED,
                         "delegate": ENFORCED},
            session_resume=True, steer=False, approval="none", sandbox="workspace-write")

    def available(self) -> bool:
        return shutil.which(self.codex_bin) is not None

    def sandbox(self, authority: list[str]) -> str:
        return "workspace-write" if "file.write" in authority else "read-only"

    def command(self, spec: RunSpec) -> list[str]:
        cmd = [self.codex_bin, "exec", "--json", "--skip-git-repo-check",
               "--sandbox", self.sandbox(spec.authority), "--cd", str(spec.workdir)]
        if spec.model and spec.model.get("model"):
            cmd += ["--model", spec.model["model"]]
        if spec.session_id:
            # 续接：`codex exec resume <id>` 是子命令，提示词从 stdin 进
            return [self.codex_bin, "exec", "resume", spec.session_id, "--json", "--skip-git-repo-check",
                    "--sandbox", self.sandbox(spec.authority), "-c",
                    f'projects."{spec.workdir}".trust_level="trusted"']
        cmd.append("-")  # 提示词从 stdin 读
        return cmd

    def run(self, spec: RunSpec, ctx, cancel: threading.Event) -> RunOutcome:
        spec.workdir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(self.command(spec), cwd=Path(spec.workdir), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(spec.prompt)
        proc.stdin.close()
        final, session_id, tools = "", spec.session_id or "", []
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type") or ev.get("event")
            if t in ("session.created", "session_configured", "thread.started"):
                session_id = ev.get("session_id") or ev.get("thread_id") or session_id
            elif t in ("item.completed", "item_updated"):
                item = ev.get("item") or {}
                if item.get("type") == "agent_message":
                    text = item.get("text") or ""
                    if text:
                        final = text
                        ctx.emit({"type": "text", "text": text})
                elif item.get("type") == "command_execution":
                    tools.append({"name": "bash", "status": "ok"})
            elif t == "turn.completed":
                final = ev.get("last_agent_message") or final
        code = proc.wait()
        err = (proc.stderr.read() if proc.stderr else "")[-2000:]
        if code != 0:
            return RunOutcome(session_id=session_id, tool_calls=tools, ok=False,
                              error=f"SERVER codex exited {code}: {err}")
        return RunOutcome(session_id=session_id, final_text=final, tool_calls=tools)
