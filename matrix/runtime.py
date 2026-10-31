"""AgentRuntime：Agent 的执行环境抽象。

- DshRuntime：以 DeepSeek Harness headless 为标准运行时。
  权限通过 `--patch` 覆盖层强制执行（禁用对应工具插件 + 沙箱模式）；
  需要网络工具时，用 `insert` 挂载 dsh-mcp-client，连接 matrix/mcp_server.py（stdio MCP）；
  有云端连接器时再挂一个，连接 matrix/mcp_connectors.py（由网络代调用，凭证不进运行时）；
  部署方配了抓取白名单时，有 web.research 的再挂 matrix/web_fetch.py，并关掉 dsh 自带的 web_fetch。
  技能只从工作目录的 skills/ 与 agent-home/skills/ 读，不读部署机器上的默认技能目录。
- ScriptedRuntime：确定性脚本运行时，用于自动化测试与离线演练；网络工具在进程内直接调用。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .mcp_server import TOOLS as NETWORK_TOOLS
from .models import AGENT_HOME, AUTHORITIES, CONNECTOR_PREFIX, CONNECTOR_SERVER, FETCH_SERVER, LIST_TOOLS, MCP_PREFIX, MCP_SERVER, SKILLS
from .web_fetch import DEADLINE_S, FetchPolicy

EventSink = Callable[[dict[str, Any]], None]
ToolHandler = Callable[[str, dict[str, Any]], tuple[str, bool]]  # (name, args) -> (text, is_error)

MCP_SERVER_SCRIPT = Path(__file__).parent / "mcp_server.py"
CONNECTOR_SCRIPT = Path(__file__).parent / "mcp_connectors.py"
FETCH_SCRIPT = Path(__file__).parent / "web_fetch.py"
APPROVAL_PLUGIN = Path(__file__).parent / "dsh_plugins" / "approval.mjs"

# 权限 -> 允许后才启用的 DSH 插件 id
AUTHORITY_PLUGINS: dict[str, list[str]] = {
    "web.research": ["tool-web"],
    "shell.exec": ["tool-bash", "tool-pwsh", "tool-jobs"],  # Windows 上模型用的 shell 是 pwsh
}

# 始终禁用：能在运行期重新启用插件、从而绕过权限覆盖层的工具。
# subagent / workflow / goal 属于 Agent 自己的作用域，不做限制：进程内子 Agent 复用宿主的工具服务
# （被覆盖层禁用的插件不会出现）与沙箱策略，权限不会超出父 Agent。
ALWAYS_DISABLED = [
    "tool-plugin-manager",
    # 0.2.1 起：会话可以切换工作目录。席位与任务的会话按工作目录存放、产物按工作目录收，不让它换
    "tool-working-directory",
]

# dsh-base 覆盖层里 tool-web 的 searchTimeoutMs（DeepSeek 搜索是一次完整的模型请求，给 60s）
TOOL_WEB_SEARCH_TIMEOUT_MS = 60_000

# npm 在 Windows 上生成的 .cmd 包装：... "%_prog%"  "%dp0%\node_modules\<包>\<入口>.js" %*
# 入口是原生程序时没有 "%_prog%"：... "%dp0%\node_modules\<包>\bin\<名字>.exe"   %*
_NPM_SHIM = re.compile(r'"%dp0%\\([^"\r\n]+)"\s+%\*')


def no_window() -> int:
    """Popen 的 creationflags：Windows 上没有控制台时（后台服务用 pythonw 跑），harness 子进程不要各自弹出控制台窗口。
    有控制台时不加，Ctrl+C 照常传给子进程。"""
    if os.name != "nt":
        return 0
    import ctypes
    return 0 if ctypes.windll.kernel32.GetConsoleWindow() else subprocess.CREATE_NO_WINDOW


def argv(cmd: list[str], windows: bool = os.name == "nt") -> list[str]:
    """把命令的可执行文件解析成能直接交给 Popen 的形式。

    Windows 上 npm 装的 CLI（dsh、claude、codex）是 .cmd 包装：CreateProcess 不按 PATHEXT 找它，
    经 cmd.exe 转一道又会改写参数里的引号与特殊字符（--mcp-config 的 JSON、带空格的路径）。
    认得出 npm 包装时直接用 node 跑它指向的脚本，指向原生 .exe 时直接跑那个 .exe；其他系统原样返回。"""
    if not windows or not cmd:
        return cmd
    found = shutil.which(cmd[0])
    if found is None:
        return cmd
    if found.lower().endswith((".cmd", ".bat")):
        try:
            m = _NPM_SHIM.search(Path(found).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            m = None
        if m and m.group(1).lower().endswith((".exe", ".com")):  # 包里带的原生程序（新版 claude-code）：直接跑，不经 node
            return [str(Path(found).parent / m.group(1)), *cmd[1:]]
        if m:
            node = Path(found).parent / "node.exe"
            return [str(node) if node.exists() else (shutil.which("node") or "node"),
                    str(Path(found).parent / m.group(1)), *cmd[1:]]
    return [found, *cmd[1:]]


@dataclass
class ToolBridge:
    """一次执行可用的网络工具：DSH 经 MCP → HTTP 回调；Scripted 直接调用 handler。"""

    url: str            # 网络内部工具端点，如 http://127.0.0.1:8765/internal/tools
    token: str          # 执行上下文令牌：网络据此识别调用方与其项目、权限
    handler: ToolHandler
    timeout_s: float = 1800
    names: list[str] | None = None  # 这次执行能用的网络工具（None = 全部）；MCP 服务只列出这些


@dataclass
class ConnectorBridge:
    """云端连接器：运行时经 mcp_connectors.py → HTTP 回调网络，网络带上凭证转发给外部服务（docs/connectors-and-skills.md 4.2）。
    运行时拿到的只有执行上下文令牌；能用哪些连接器由网络按令牌认定。Scripted 直接调用 handler。"""

    url: str            # 网络内部端点前缀，如 http://127.0.0.1:8765/internal/connectors
    token: str
    list_tools: Callable[[], list[dict[str, Any]]]                    # -> MCP 工具定义（名字是 <连接器>__<工具>）
    call: Callable[[str, dict[str, Any]], tuple[str, bool]]          # (name, args) -> (text, is_error)
    timeout_s: float = 600


@dataclass
class ApprovalBridge:
    """工作区外溢审批：DSH 经应答插件 → HTTP 回调网络；Scripted 直接调用 handler。"""

    url: str            # 网络内部审批端点，如 http://127.0.0.1:8765/internal/approval
    token: str          # 执行上下文令牌（与 ToolBridge 相同）
    handler: Callable[[str, str], str]  # (tool, reason) -> allowed-once | rejected | cancelled | unavailable
    timeout_s: float = 900


@dataclass
class RunRequest:
    agent_id: str
    context_id: str     # 对话 id 或任务 id
    prompt: str
    cwd: Path
    authority: list[str]
    session_id: str | None = None  # 非空表示续接已有会话
    tools: ToolBridge | None = None
    on_event: EventSink | None = None
    timeout_s: float | None = None  # 这一轮最长跑多久；None = 不限制（Agent 设置）
    cancel: threading.Event | None = None  # 人终止这一轮：置位后运行时尽快结束，按 STOPPED 返回
    model: dict[str, str] | None = None  # {provider, model, reasoning_effort?}；None 用运行时默认
    approvals: ApprovalBridge | None = None
    # 每轮下发的只读材料（端侧运行时用；云侧由 _run 直接挂载目录）：
    # [{"path": "project/x.md", "data": <bytes>}]
    materials: list[dict[str, Any]] = field(default_factory=list)
    # 私有目录（端侧运行时用；云侧由 _run 直接挂载）："rw" 挂载并写回，"ro" 只挂载不写回，None 不挂载
    home: str | None = None
    # 端侧设备上用哪个 harness（端侧运行时用）；None = 设备的默认 harness
    harness: str | None = None
    connectors: ConnectorBridge | None = None   # 云端连接器（网络代调用）；None = 这一轮没有
    fetch: FetchPolicy | None = None   # 云侧：部署方的抓取白名单（有 web.research 时挂 web_fetch.py）；None = 用 dsh 自带的
    # 端侧：这一轮允许用的、电脑上那个 AI 工具里配置的服务名。驱动只开这些，其余一律关掉（空 = 全关）
    local_connectors: list[str] = field(default_factory=list)
    disabled_plugins: list[str] = field(default_factory=list)  # dsh：这一轮另外要禁用的插件 id（端侧驱动算出来的）
    # 云侧 dsh：这一轮用平台配的模型供应商时，{"id": 供应商 id, "config": llm-pi-ai 的供应商配置（不含密钥）}；
    # 密钥在 env 里（只加进这个 dsh 进程的环境，不写覆盖层）。端侧运行时从不下发这两项
    llm_provider: dict[str, Any] | None = None
    env: dict[str, str] = field(default_factory=dict, repr=False)


# 运行时错误分类（参照 Raft daemon 的 runtimeErrorDeliveryPolicy）：
# retryable 的错误由网络用同一个会话自动续接一次；其余直接交给人或委托方决定。
ERROR_CLASSES: dict[str, tuple[str, bool]] = {
    "MALFORMED_RESPONSE": ("malformed_tool_call", True),  # 模型写出的工具参数不是合法 JSON
    "EMPTY_RESPONSE": ("empty_response", True),
    "RATE_LIMIT": ("rate_limited", False),
    "TIMEOUT": ("timeout", True),  # 一轮做得太久：会话还在，续接一次收尾（再超时才中断，交给人）
    "TRANSPORT": ("provider_connection", False),
    "SERVER": ("provider_server", False),
    "STOPPED": ("stopped", False),  # 人终止了这一轮：不自动续接，等人决定
}


STOPPED_ERROR = "STOPPED: 人终止了这一轮"


def kill_tree(proc: Any) -> None:
    """结束 harness 进程连同它启动的子进程。Windows 上 proc.kill() 只结束这一个进程：harness 经 .cmd / .bat
    包装启动时结束的只是 cmd.exe，真正的 harness 还在跑、还占着输出管道，这一轮要等它自己退出才结束；
    harness 开的工具进程也留在后台。用 taskkill /T 连同子进程一起结束，失败时退回 proc.kill()。"""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10,
                           creationflags=no_window())
        except (OSError, subprocess.SubprocessError):
            pass
    if proc.poll() is None:
        proc.kill()


def watch_process(proc: Any, timeout_s: float | None, cancel: threading.Event) -> threading.Event:
    """看住一次 harness 进程：超过这一轮的超时（Agent 设置，None 不限制）或人终止（cancel）时结束它。
    返回的事件表示“因超时结束”；因 cancel 结束的由调用方看 cancel 判断。"""
    timed_out = threading.Event()
    deadline = time.monotonic() + timeout_s if timeout_s else None

    def watch() -> None:
        while proc.poll() is None:
            if cancel.wait(0.5) or (deadline is not None and time.monotonic() >= deadline):
                if not cancel.is_set():
                    timed_out.set()
                kill_tree(proc)
                return

    threading.Thread(target=watch, daemon=True).start()
    return timed_out


def classify_error(error: str | None) -> tuple[str, bool]:
    """(错误类别, 是否值得用同一会话自动续接一次)。"""
    for code, cls in ERROR_CLASSES.items():
        if error and code in error:
            return cls
    return ("runtime_error", False)


@dataclass
class RunResult:
    session_id: str
    final_text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    ok: bool = True
    error: str | None = None

    @property
    def error_class(self) -> str | None:
        return None if self.ok else classify_error(self.error)[0]

    @property
    def retryable(self) -> bool:
        return not self.ok and classify_error(self.error)[1]


# 权限在一个运行时上的强制等级（见 docs/device-runtimes.md 第 3 节）：
# - enforced：运行时不提供该能力，或越界会被运行时阻断；
# - advisory：只能靠提示词与网络工具检查，运行时本身拦不住（不进入有效权限）；
# - unavailable：这个运行时没有该能力。
ENFORCED, ADVISORY, UNAVAILABLE = "enforced", "advisory", "unavailable"


@dataclass
class RuntimeCapabilities:
    """一个运行时能强制哪些权限。有效权限 = 自身权限 ∩ 委托授予 ∩ 这里 enforced 的部分。"""
    name: str
    placement: str = "hosted"                       # hosted | device
    enforcement: dict[str, str] = field(default_factory=dict)  # 权限 -> enforced | advisory | unavailable
    session_resume: bool = True
    steer: bool = False                             # 运行中能否追加输入
    approval: str = "interactive"                   # interactive | sandbox-only | none
    sandbox: str = "workspace-write"                # read-only | workspace-write | none
    # 技能怎么送到：native = 运行时自己从工作目录发现；prompt = 网络在提示词里列出名字与路径，正文让它自己读
    skills: str = "native"

    @classmethod
    def all_enforced(cls, name: str, **kw: Any) -> "RuntimeCapabilities":
        return cls(name=name, enforcement={a: ENFORCED for a in AUTHORITIES}, **kw)

    def level(self, authority: str) -> str:
        return self.enforcement.get(authority, UNAVAILABLE)

    def enforces(self, authority: str) -> bool:
        return self.level(authority) == ENFORCED

    def effective(self, authority: list[str]) -> list[str]:
        return [a for a in authority if self.enforces(a)]

    def narrowed(self, authority: list[str]) -> dict[str, str]:
        """被运行时收窄掉的权限与原因（只算 Agent 自己声明过的）。"""
        return {a: f"运行时 {self.name} 无法强制（{self.level(a)}）" for a in authority if not self.enforces(a)}


class AgentRuntime(Protocol):
    name: str

    def capabilities(self) -> RuntimeCapabilities: ...

    def run(self, req: RunRequest) -> RunResult: ...


def disabled_plugins(authority: list[str]) -> list[str]:
    disabled = list(ALWAYS_DISABLED)
    for auth, plugins in AUTHORITY_PLUGINS.items():
        if auth not in authority:
            disabled.extend(plugins)
    return disabled


def permission_mode(authority: list[str]) -> str:
    # 没有 file.write 时用只读沙箱；有则只能写本工作目录（workspaceRoot = cwd）。
    return "workspace-write" if "file.write" in authority else "read-only"


def build_patch_yaml(authority: list[str], tools: ToolBridge | None = None,
                     model: dict[str, str] | None = None, approvals: ApprovalBridge | None = None,
                     skill_dirs: list[str] | None = None, connectors: ConnectorBridge | None = None,
                     extra_disabled: list[str] | None = None, llm_provider: dict[str, Any] | None = None,
                     fetch: FetchPolicy | None = None) -> str:
    """生成 DSH 覆盖层。值用 JSON 书写（JSON 是合法的 YAML flow 语法）。"""
    j = lambda v: json.dumps(v, ensure_ascii=False)  # noqa: E731
    lines = ["# generated by matrix: agent authority -> DSH plugin overlay"]
    # 技能只读网络放进工作目录的那几处：不带默认根，否则部署机器上的 ~/.dsh/skills、~/.agents/skills
    # 与数据目录所在仓库的 .dsh/skills、.agents/skills 会进到每个用户的 Agent（docs/connectors-and-skills.md 4.1）
    lines += ["- id: skill-filesystem", "  config:", "    includeDefaultRoots: false",
              f"    customSkillDirs: {j(list(skill_dirs or []))}"]
    if model and model.get("provider") and model.get("model"):
        lines += ["- id: agent-default-model", "  config:",
                  f"    provider: {j(model['provider'])}", f"    model: {j(model['model'])}"]
        if model.get("reasoning_effort"):
            lines.append(f"    reasoningEffort: {j(model['reasoning_effort'])}")
    if llm_provider:
        # 平台配的供应商：dsh 对这段配置是整段替换（不和 ~/.dsh 里的合并），所以只写这一轮用的那一个；
        # 里面只有密钥的环境变量名（research: deepseek-harness#model-providers）
        lines += ["- id: llm-pi-ai", "  config:", "    providers:",
                  f"      {j(llm_provider['id'])}: {j(llm_provider['config'])}"]
    if tools is not None:
        lines += [
            "- insert:",
            f"    - id: mcp-{MCP_SERVER}",
            "      name: '@deepseek-ai/dsh-mcp-client'",
            "      config:",
            f"        serverName: {MCP_SERVER}",
            "        transport: stdio",
            f"        command: {j(sys.executable)}",
            f"        args: [{j(str(MCP_SERVER_SCRIPT))}]",
            "        env:",
            f"          MATRIX_TOOL_URL: {j(tools.url)}",
            f"          MATRIX_TOKEN: {j(tools.token)}",
            f"          MATRIX_TOOL_TIMEOUT: {j(str(int(tools.timeout_s)))}",
            *([f"          MATRIX_TOOLS: {j(','.join(tools.names))}"] if tools.names is not None else []),
            f"        toolCallTimeoutMs: {int(tools.timeout_s * 1000) + 60_000}",
            "        failOnStartupError: true",
        ]
    if connectors is not None:
        lines += [
            "- insert:",
            f"    - id: mcp-{CONNECTOR_SERVER}",
            "      name: '@deepseek-ai/dsh-mcp-client'",
            "      config:",
            f"        serverName: {CONNECTOR_SERVER}",
            "        transport: stdio",
            f"        command: {j(sys.executable)}",
            f"        args: [{j(str(CONNECTOR_SCRIPT))}]",
            "        env:",
            f"          MATRIX_CONNECTOR_URL: {j(connectors.url)}",
            f"          MATRIX_TOKEN: {j(connectors.token)}",
            f"          MATRIX_TOOL_TIMEOUT: {j(str(int(connectors.timeout_s)))}",
            f"        toolCallTimeoutMs: {int(connectors.timeout_s * 1000) + 60_000}",
            "        failOnStartupError: false",   # 外部服务连不上不该让整轮起不来
        ]
    if fetch is not None and "web.research" in authority:
        lines += [
            "- insert:",
            f"    - id: mcp-{FETCH_SERVER}",
            "      name: '@deepseek-ai/dsh-mcp-client'",
            "      config:",
            f"        serverName: {FETCH_SERVER}",
            "        transport: stdio",
            f"        command: {j(sys.executable)}",
            f"        args: [{j(str(FETCH_SCRIPT))}]",
            "        env:",
            f"          MATRIX_FETCH_POLICY: {j(fetch.to_env())}",
            f"        toolCallTimeoutMs: {int(DEADLINE_S * 1000) + 30_000}",
            "        failOnStartupError: false",
        ]
        if fetch.replace_builtin:
            # 关掉自带的 web_fetch，只留 web_search：两个都在时模型常挑自带的那个，在内网地址上碰壁。
            # dsh 对一行的 config 是整段替换，dsh-base 给这一行配的 searchTimeoutMs 要照抄过来
            lines += ["- id: tool-web", "  config:", "    search: true", "    fetch: false",
                      f"    searchTimeoutMs: {TOOL_WEB_SEARCH_TIMEOUT_MS}"]
    if approvals is not None:
        # 审批应答者：沙箱升权（写工作目录以外）的申请转给网络，由 Agent 所有者决定
        lines += [
            "- insert:",
            "    - id: matrix-approval",
            f"      name: {j(str(APPROVAL_PLUGIN))}",
            "      config:",
            f"        url: {j(approvals.url)}",
            f"        token: {j(approvals.token)}",
            f"        timeoutMs: {int(approvals.timeout_s * 1000)}",
        ]
    for pid in dict.fromkeys(disabled_plugins(authority) + list(extra_disabled or [])):
        lines += [f"- id: {pid}", "  disabled: true"]
    return "\n".join(lines) + "\n"


def _accumulate(ev: dict[str, Any], tool_calls: list[dict[str, Any]], usage: dict[str, int]) -> None:
    t = ev.get("type")
    if t == "tool_call":
        tool_calls.append({"call_id": ev.get("callId"), "tool": ev.get("tool"), "input": ev.get("input"),
                           "status": None, "result": None})
    elif t == "tool_result":
        for c in reversed(tool_calls):
            if c["call_id"] == ev.get("callId"):
                c["status"] = ev.get("status")
                c["result"] = _clip(ev.get("result"), 1200)
                break
    elif t == "status" and ev.get("usage"):
        for k, v in ev["usage"].items():
            if isinstance(v, int):
                usage[k] = usage.get(k, 0) + v


def _clip(v: Any, n: int) -> Any:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"…(+{len(v) - n} chars)"
    if not isinstance(v, str) and v is not None:
        s = json.dumps(v, ensure_ascii=False)
        return s[:n] + ("…" if len(s) > n else "")
    return v


class DshRuntime:
    name = "dsh"

    def capabilities(self) -> RuntimeCapabilities:
        """dsh 的权限由 --patch 覆盖层禁用插件 + 沙箱强制执行；审批经 dsh 插件转给网络。"""
        return RuntimeCapabilities.all_enforced("dsh", approval="interactive", sandbox="workspace-write")

    def __init__(self, dsh_bin: str = "dsh", profile: str = "headless"):
        self.dsh_bin = dsh_bin
        self.profile = profile
        self._default_model: dict[str, str] | None = None

    def default_model(self) -> dict[str, str] | None:
        """没给 Agent 配模型时 dsh 实际用哪个：读一次组合后的配置（profile 层也算进去）。"""
        if self._default_model is None:
            self._default_model = {}
            try:
                out = subprocess.run(argv([self.dsh_bin, self.profile, "--dump-config"]), capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=30,
                                     creationflags=no_window()).stdout
                block = out.split("- id: agent-default-model", 1)[1].split("\n- ", 1)[0]
                for key, dst in (("provider", "provider"), ("model", "model"), ("reasoningEffort", "reasoning_effort")):
                    m = re.search(rf"^\s+{key}: ['\"]?([^'\"\n]+)", block, re.M)
                    if m:
                        self._default_model[dst] = m.group(1).strip()
            except (OSError, IndexError, subprocess.SubprocessError):
                pass
        return self._default_model or None

    def command(self, req: RunRequest, patch_path: Path) -> list[str]:
        cmd = [self.dsh_bin, self.profile, "--patch", str(patch_path), "--json"]
        if req.session_id:
            cmd += ["--session-id", req.session_id]
        cmd.append("-")  # 文本从 stdin 读入
        return cmd

    def run(self, req: RunRequest) -> RunResult:
        req.cwd.mkdir(parents=True, exist_ok=True)
        patch_path = req.cwd / ".dsh-authority.yml"
        skill_dirs = [str((req.cwd / SKILLS).resolve()), str((req.cwd / AGENT_HOME / SKILLS).resolve())]
        connectors = req.connectors if "connector.use" in req.authority else None
        patch_path.write_text(build_patch_yaml(req.authority, req.tools, req.model, req.approvals, skill_dirs=skill_dirs,
                                               connectors=connectors, extra_disabled=req.disabled_plugins,
                                               llm_provider=req.llm_provider, fetch=req.fetch),
                              encoding="utf-8")
        env = dict(os.environ, **req.env, DSH_PERMISSION_MODE=permission_mode(req.authority))
        cmd = self.command(req, patch_path)

        tool_calls: list[dict[str, Any]] = []
        usage: dict[str, int] = {}
        session_id = req.session_id or ""
        final_text = ""
        texts: list[str] = []
        stderr_tail: list[str] = []

        proc = subprocess.Popen(
            argv(cmd), cwd=req.cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=no_window(),
        )

        def drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_tail.append(line.rstrip())
                del stderr_tail[:-30]

        t_err = threading.Thread(target=drain_stderr, daemon=True)
        t_err.start()
        cancel = req.cancel or threading.Event()
        timed_out = watch_process(proc, req.timeout_s, cancel)
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(req.prompt)
            proc.stdin.close()
            with open(req.cwd / "dsh-events.jsonl", "a", encoding="utf-8") as log:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    log.write(line + "\n")
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = ev.get("type")
                    if t == "session":
                        session_id = ev.get("sessionId") or session_id
                    elif t == "final":
                        final_text = ev.get("text") or ""
                    elif t == "text":
                        texts.append(ev.get("text") or "")
                    _accumulate(ev, tool_calls, usage)
                    if req.on_event:
                        req.on_event(ev)
            proc.wait()
        finally:
            if proc.poll() is None:
                kill_tree(proc)
        t_err.join(timeout=2)

        if not final_text and texts:
            final_text = texts[-1]
        if cancel.is_set():
            return RunResult(session_id, final_text, tool_calls, usage, ok=False, error=STOPPED_ERROR)
        if timed_out.is_set():
            return RunResult(session_id, final_text, tool_calls, usage, ok=False,
                             error=f"TIMEOUT: 超过 {int(req.timeout_s or 0)} 秒，网络终止了这次运行")
        if proc.returncode != 0:
            err = f"dsh exited {proc.returncode}: " + " | ".join(stderr_tail[-5:])
            return RunResult(session_id, final_text, tool_calls, usage, ok=False, error=err)
        return RunResult(session_id, final_text, tool_calls, usage)


# ---------------- ScriptedRuntime ----------------

class ScriptCall:
    """脚本 handler 的入参。`tool()` 调用网络工具（等价于 DSH 里的 MCP 工具调用），`use()` 模拟普通工具。"""

    def __init__(self, req: RunRequest, session_id: str, turn: int):
        self.agent_id = req.agent_id
        self.context_id = req.context_id
        self.prompt = req.prompt
        self.session_id = session_id
        self.resumed = bool(req.session_id)
        self.authority = list(req.authority)
        self.cwd = req.cwd
        self.turn = turn                 # 同一会话中的第几次调用（从 1 开始）
        self.timeout_s = req.timeout_s   # 这一轮的运行超时（None = 不限制）
        self.stopped = req.cancel or threading.Event()  # 人终止了这一轮（脚本里可以等它）
        self.tool_names = [MCP_PREFIX + t["name"] for t in NETWORK_TOOLS
                           if req.tools.names is None or t["name"] in req.tools.names] if req.tools else []
        if req.tools:  # 项目插件服务的工具：和 mcp_server 一样在“列工具”时向网络取
            text, is_error = req.tools.handler(LIST_TOOLS, {})
            try:
                extra = json.loads(text) if not is_error else []
            except ValueError:
                extra = []
            self.tool_names += [MCP_PREFIX + t["name"] for t in extra if isinstance(t, dict) and t.get("name")]
        self._req = req
        self.events: list[dict[str, Any]] = []

    @property
    def can_delegate(self) -> bool:
        """挂载了协作工具（delegate 等）；只有项目档案工具时为 False。"""
        return MCP_PREFIX + "delegate" in self.tool_names

    def _emit(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)
        if self._req.on_event:
            self._req.on_event(ev)

    def say(self, text: str) -> None:
        """模拟运行中途输出的文本（DSH 的 text 事件）。"""
        self._emit({"type": "text", "text": text})

    def use(self, tool: str, input: dict[str, Any] | None = None, result: Any = "ok") -> None:
        cid = f"call-{len(self.events)}"
        self._emit({"type": "tool_call", "callId": cid, "tool": tool, "input": input or {}})
        self._emit({"type": "tool_result", "callId": cid, "status": "completed", "result": result})

    def tool(self, name: str, /, **args: Any) -> str:
        """调用网络工具；返回工具结果文本。无网络工具时抛 RuntimeError（与 DSH 中工具不存在一致）。"""
        if self._req.tools is None or MCP_PREFIX + name not in self.tool_names:
            raise RuntimeError(f"tool {name} not available")
        cid = f"call-{len(self.events)}"
        full = MCP_PREFIX + name
        self._emit({"type": "tool_call", "callId": cid, "tool": full, "input": args})
        text, is_error = self._req.tools.handler(name, args)
        self._emit({"type": "tool_result", "callId": cid, "status": "failed" if is_error else "completed",
                    "result": text})
        return text

    @property
    def skills(self) -> list[str]:
        """这一轮工作目录 skills/ 里的技能名（网络合并好的）。"""
        d = self.cwd / SKILLS
        return sorted(p.name for p in d.iterdir() if (p / "SKILL.md").is_file()) if d.is_dir() else []

    def connector_tools(self) -> list[str]:
        """这一轮能用的云端连接器工具（不含前缀）；没有连接器返回空。"""
        c = self._req.connectors
        if c is None or "connector.use" not in self._req.authority:
            return []
        return [t["name"] for t in c.list_tools()]

    def connector(self, name: str, /, **args: Any) -> str:
        """调用一个云端连接器工具（等价于 DSH 里 mcp__connectors__<name>）。没有时抛 RuntimeError。"""
        c = self._req.connectors
        if c is None or "connector.use" not in self._req.authority:
            raise RuntimeError(f"connector tool {name} not available")
        cid = f"call-{len(self.events)}"
        full = CONNECTOR_PREFIX + name
        self._emit({"type": "tool_call", "callId": cid, "tool": full, "input": args})
        text, is_error = c.call(name, args)
        self._emit({"type": "tool_result", "callId": cid, "status": "failed" if is_error else "completed",
                    "result": text})
        return text

    def escalate(self, reason: str, tool: str = "bash") -> str:
        """模拟沙箱升权申请（dsh 的 approval/request）：返回 allowed-once / rejected / cancelled / unavailable。"""
        if self._req.approvals is None:
            return "unavailable"
        return self._req.approvals.handler(tool, reason)

    def fail(self, error: str) -> None:
        """模拟运行时失败（例如 dsh 的 MALFORMED_RESPONSE）：本次运行以 error 结束，会话保留。"""
        raise ScriptFailure(error)


class ScriptFailure(Exception):
    pass


ScriptHandler = Callable[[ScriptCall], str]


class ScriptedRuntime:
    """按 handler 返回固定输出；同样生成 session id、事件流和工具调用证据。"""

    name = "scripted"

    def __init__(self, handler: ScriptHandler, capabilities: RuntimeCapabilities | None = None):
        self.handler = handler
        # 默认与 dsh 一样全部可强制；测试里可以传入收窄的能力表，模拟端侧 harness
        self._capabilities = capabilities or RuntimeCapabilities.all_enforced("scripted")
        self.calls: list[ScriptCall] = []
        self._turns: dict[str, int] = {}
        self._lock = threading.Lock()

    def capabilities(self) -> RuntimeCapabilities:
        return self._capabilities

    def run(self, req: RunRequest) -> RunResult:
        with self._lock:
            sid = req.session_id or f"scripted-{uuid.uuid4().hex[:10]}"
            self._turns[sid] = self._turns.get(sid, 0) + 1
            call = ScriptCall(req, sid, self._turns[sid])
            self.calls.append(call)
        call._emit({"type": "session", "sessionId": sid, "cwd": str(req.cwd)})
        tool_calls: list[dict[str, Any]] = []
        usage: dict[str, int] = {}
        try:
            final = self.handler(call)
        except Exception as e:  # noqa: BLE001 —— 脚本错误等价于运行时失败
            for ev in call.events:
                _accumulate(ev, tool_calls, usage)
            err = str(e) if isinstance(e, ScriptFailure) else f"script error: {e!r}"
            return RunResult(sid, "", tool_calls, usage, ok=False, error=err)
        call._emit({"type": "final", "text": final})
        for ev in call.events:
            _accumulate(ev, tool_calls, usage)
        if call.stopped.is_set():
            return RunResult(sid, final, tool_calls, usage, ok=False, error=STOPPED_ERROR)
        return RunResult(sid, final, tool_calls, usage)
