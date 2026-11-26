"""演示用的外部驱动：把一个虚构的设备端 Agent（`demo-agent` 命令）接进 matrix-connect。

照 docs/device-driver-authoring.md 改成你们自己的 Agent：换掉标着「协议」的三处，改 `name`、包名与 entry point。

虚构 Agent 的协议（「协议」，你们的 Agent 换成它自己的调用方式，命令行、本机 HTTP 都行）：

    demo-agent run --workdir <目录>
      标准输入：一个 JSON  {"prompt": "...", "session_id": "上一轮的会话 id，可以没有"}
      标准输出：每行一个 JSON
        {"type": "text", "text": "..."}          过程中的一段输出（可以有多行）
        {"type": "tool", "name": "..."}          它用了一个自己的工具（只用于展示）
        {"type": "done", "text": "最终答复", "session_id": "..."}
      退出码非 0 = 失败，标准错误里是原因
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading

from matrix.connector.drivers.base import RunCtx, RunOutcome, RunSpec, guard
from matrix.runtime import UNAVAILABLE, RuntimeCapabilities, argv, no_window


class DemoAgentDriver:
    # harness 名：Matrix 服务器要用 MATRIX_HARNESSES=demo-agent=演示助手 认它，否则配对时会被过滤掉
    name = "demo-agent"

    def __init__(self, *, bin_path: str | None = None, bridge_url: str = ""):
        """matrix-connect 按这两个关键字参数构造外部驱动。bin_path 来自 `--bin`，没给时按名字在 PATH 里找；
        bridge_url 是本机网络工具桥（MCP）的地址，你们的 Agent 能接 MCP 时把它交过去（这个样板不用）。"""
        self.bin = bin_path or "demo-agent"
        self.bridge_url = bridge_url

    def capabilities(self) -> RuntimeCapabilities:
        """如实报告能强制什么。做不到的权限写 unavailable：网络会把绑在这里的 Agent 的有效权限收窄掉，
        而不是假装它被限制住了（docs/device-runtimes.md 第 3 节）。"""
        return RuntimeCapabilities(
            name=self.name, placement="device",
            enforcement={"web.research": UNAVAILABLE, "file.write": UNAVAILABLE, "shell.exec": UNAVAILABLE,
                         "delegate": UNAVAILABLE, "connector.use": UNAVAILABLE},
            session_resume=True, steer=False, approval="none", sandbox="none", skills="prompt", questions=False)

    def available(self) -> bool:
        """本机装没装这个 Agent。没装时连接器不报告它，配对时也不会出现。"""
        return shutil.which(self.bin) is not None

    def services(self) -> list[str]:
        """这个 Agent 里用户配置的外部服务名（相当于“电脑上的连接器”）；没有这个概念就返回空。"""
        return []

    def command(self, spec: RunSpec) -> list[str]:  # 「协议」1：怎么拉起一次运行
        return [self.bin, "run", "--workdir", str(spec.workdir)]

    def request(self, spec: RunSpec) -> str:  # 「协议」2：这一轮交给它什么
        return json.dumps({"prompt": spec.prompt, "session_id": spec.session_id}, ensure_ascii=False)

    def run(self, spec: RunSpec, ctx: RunCtx, cancel: threading.Event) -> RunOutcome:
        spec.workdir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(argv(self.command(spec)), cwd=spec.workdir, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", creationflags=no_window())
        assert proc.stdin is not None and proc.stdout is not None
        timed_out = guard(proc, spec.timeout_s, cancel)  # 超时与人终止时结束它，驱动不用自己管
        proc.stdin.write(self.request(spec))
        proc.stdin.close()
        final, session_id, tools = "", spec.session_id or "", []
        for line in proc.stdout:  # 「协议」3：怎么读它的输出
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "text" and ev.get("text"):
                ctx.emit({"type": "text", "text": ev["text"]})   # 过程实时显示在 Matrix 里
            elif t == "tool":
                tools.append({"name": str(ev.get("name") or "tool"), "status": "ok"})
            elif t == "done":
                final = str(ev.get("text") or "")
                session_id = str(ev.get("session_id") or session_id)
        code = proc.wait()
        err = (proc.stderr.read() if proc.stderr else "")[-2000:]
        if timed_out.is_set():
            return RunOutcome(session_id=session_id, tool_calls=tools, ok=False,
                              error=f"TIMEOUT: 超过 {int(spec.timeout_s or 0)} 秒，连接器终止了这次运行")
        if code != 0:
            return RunOutcome(session_id=session_id, tool_calls=tools, ok=False,
                              error=f"SERVER {self.name} exited {code}: {err}")
        return RunOutcome(session_id=session_id, final_text=final, tool_calls=tools)
