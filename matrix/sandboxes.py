"""云侧执行面：每个执行单元一个沙箱（docs/cloud-runtime.md 第 4、7 节；这里是阶段 1a）。

- `SandboxProvider`：后端接口。`ensure(spec)` 幂等（按执行单元 id 与代数），`stop` 停沙箱留目录，`destroy` 连目录删，
  `status` / `list` 报实际状态（控制面对账用）。`SandboxSpec` 只有执行单元 id、所有者、Agent、代数、配额与一次性启动令牌：
  没有路径、命令、挂载参数，这些由后端按固定模板生成（逻辑工作目录由控制面按执行单元定下，永远不变）。
- 后端：`MemorySandboxes`（测试用，进程内，沙箱里的“harness”是传进来的运行时，通常是 ScriptedRuntime）、
  `LocalSandboxes`（本机子进程 + 外层 bwrap，单机没有 Docker 时的过渡形态，也是端到端验证手段）。
  Docker / Kubernetes 后端以后在同一个接口后面。
- `SandboxHub`：控制面这边的身份：一次性启动令牌换执行单元凭证，一条沙箱连接只绑一个执行单元（不变量 10）。
- `SandboxRuntime`：引擎眼里的云侧运行时。要这个执行单元的沙箱、等它的客户端连上，再把这一轮交给它的 `DeviceRuntime`
  （与端侧同一套协议：run.request 带材料清单、material.fetch、tool.call、question.request、run.event、run.result）。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Protocol

from . import sandbox as bw
from .checkpoints import unit_rel
from .device import (CANCEL, FILE_FETCH, HELLO, PROTOCOL, RUN_REQUEST, DeviceRuntime, MemoryLink,
                     capabilities_from_hello)
from .runtime import (ConnectorBridge, QuestionBridge, RunRequest, RunResult, RuntimeCapabilities, ToolBridge,
                      outer_ro)

BOOT_TTL_S = 120          # 启动令牌多久不用就作废
CONNECT_TIMEOUT_S = 60    # 起沙箱后等客户端连上的时间


class SandboxError(RuntimeError):
    """起不来沙箱；消息以错误类别开头（ISOLATION / TRANSPORT），引擎按它分类。"""


@dataclass(frozen=True)
class Quota:
    """配额（docs/cloud-runtime.md 4.3 的默认值）。local 后端做不到，照实在能力报告里写。"""
    cpus: float = 2.0
    memory_mb: int = 2048
    pids: int = 512
    disk_gb: int = 10


@dataclass(frozen=True)
class SandboxSpec:
    unit_id: str                 # seat:<对话>/<Agent> | task:<任务>
    owner_id: str | None
    agent_id: str
    generation: int
    boot_token: str = field(repr=False)
    quota: Quota = Quota()


@dataclass
class SandboxStatus:
    unit_id: str
    state: str                   # running | stopped | absent
    generation: int = 0
    started_at: float = 0.0
    address: str = ""            # 沙箱在它网络里的地址（docker 后端）：出口网关据此核对请求来源


class SandboxProvider(Protocol):
    name: str

    def bind(self, host: "SandboxHost") -> None: ...
    def ensure(self, spec: SandboxSpec) -> SandboxStatus: ...   # 幂等：同一单元同一代数已在运行就直接返回
    def stop(self, unit_id: str) -> None: ...                   # 停沙箱，目录保留
    def destroy(self, unit_id: str) -> None: ...                # 连目录一起删
    def status(self, unit_id: str) -> SandboxStatus: ...
    def list(self) -> list[SandboxStatus]: ...                  # 实际有的（运行中与已停），对账用
    def report(self) -> dict[str, Any]: ...                     # 能力报告：{isolation, note, quota}


@dataclass
class SandboxHost:
    """控制面交给后端的东西：身份中心、客户端连回来的地址、执行单元的逻辑工作目录、执行面目录树的根。"""
    hub: "SandboxHub"
    connect_url: str
    workdir_of: Callable[[str], Path]
    root: Path
    runtime: Any = None          # 内存后端：沙箱里扮演 harness 的运行时
    dsh_bin: str = "dsh"
    forbidden: tuple[Path, ...] = ()   # 不能只读挂进沙箱的目录（数据目录、执行面目录树）


# ================= 身份：一次性启动令牌 → 执行单元凭证 =================
@dataclass
class Grant:
    unit: str
    generation: int
    credential: str = field(repr=False)


class SandboxHub:
    """启动令牌与凭证只存 sha256、只在内存。一个执行单元同一时刻只认一条连接、一份凭证；重建时旧的一起作废。"""

    def __init__(self, uploads: Any = None):
        self.uploads = uploads
        self._lock = threading.Condition()
        self._boot: dict[str, tuple[str, int, float]] = {}     # sha256(令牌) → (单元, 代数, 过期时间)
        self._creds: dict[str, tuple[str, int]] = {}           # sha256(凭证) → (单元, 代数)
        self._live: dict[str, DeviceRuntime] = {}              # 单元 → 当前连接

    @staticmethod
    def _h(v: str) -> str:
        return hashlib.sha256(v.encode()).hexdigest()

    def issue(self, unit: str, generation: int) -> str:
        """给新建的沙箱发启动令牌；这个单元以前的令牌、凭证、连接一起作废。"""
        token = secrets.token_urlsafe(24)
        self.revoke(unit)
        with self._lock:
            self._boot[self._h(token)] = (unit, generation, time.time() + BOOT_TTL_S)
        return token

    def redeem(self, token: str) -> Grant | None:
        """用启动令牌换凭证：只能用一次，过期不认。"""
        with self._lock:
            got = self._boot.pop(self._h(token), None) if token else None
            if got is None or got[2] < time.time():
                return None
            cred = secrets.token_urlsafe(24)
            self._creds[self._h(cred)] = (got[0], got[1])
            return Grant(got[0], got[1], cred)

    def by_credential(self, credential: str) -> Grant | None:
        """断线重连：凭执行单元凭证认回原来的单元（只认当前代数）。"""
        with self._lock:
            got = self._creds.get(self._h(credential)) if credential else None
            return Grant(got[0], got[1], credential) if got else None

    def accept(self, grant: Grant, link: Any, hello: dict[str, Any]) -> DeviceRuntime:
        """握手完成：这条连接从此只代表 grant.unit（只接受派给它的运行，只取它这一轮的材料）。"""
        rt = DeviceRuntime(link, capabilities_from_hello(hello), unit=grant.unit, uploads=self.uploads)
        rt.generation = grant.generation
        with self._lock:
            old = self._live.get(grant.unit)
            self._live[grant.unit] = rt
            self._lock.notify_all()
        if old is not None and old is not rt:
            old.abort_all()
        return rt

    def detach(self, unit: str, rt: DeviceRuntime | None = None) -> None:
        """连接断了（或沙箱没了）：在途的轮次立刻以传输错误结束。"""
        with self._lock:
            cur = self._live.get(unit)
            if rt is None or cur is rt:
                self._live.pop(unit, None)
            gone = rt or cur
        if gone is not None:
            gone.abort_all()

    def revoke(self, unit: str) -> None:
        with self._lock:
            self._boot = {k: v for k, v in self._boot.items() if v[0] != unit}
            self._creds = {k: v for k, v in self._creds.items() if v[0] != unit}
        self.detach(unit)

    def live(self, unit: str) -> DeviceRuntime | None:
        with self._lock:
            return self._live.get(unit)

    def wait(self, unit: str, generation: int, timeout_s: float) -> DeviceRuntime | None:
        deadline = time.monotonic() + timeout_s
        with self._lock:
            while True:
                rt = self._live.get(unit)
                if rt is not None and rt.generation == generation:
                    return rt
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._lock.wait(min(left, 0.5))


def hello_message(grant: Grant) -> dict[str, Any]:
    """服务器发给沙箱客户端的握手：带上执行单元与凭证（凭证只在客户端内存里，用来断线重连）。"""
    return {"type": HELLO, "protocol": PROTOCOL, "unit": grant.unit, "credential": grant.credential}


# ================= 沙箱里扮演 harness 的运行时（内存后端） =================
class RuntimeDriver:
    """把一个 AgentRuntime（测试里是 ScriptedRuntime）包成连接器驱动：在沙箱客户端里跑，网络工具、提问、
    云端连接器都经连接回到网络，和真实沙箱里的 dsh 驱动走同一条路。"""

    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.name = getattr(runtime, "name", "runtime")

    def capabilities(self) -> RuntimeCapabilities:
        return self.runtime.capabilities()

    def available(self) -> bool:
        return True

    def services(self) -> list[str]:
        return []

    def run(self, spec: Any, ctx: Any, cancel: threading.Event) -> Any:
        from .connector.drivers.base import RunOutcome
        tools = ToolBridge(url="", token=spec.run_id, handler=ctx.call_tool,
                           names=list(spec.tool_names) or None) if spec.tool_names else None
        questions = QuestionBridge(url="", token=spec.run_id, handler=ctx.ask_question) if spec.questions else None
        connectors = ConnectorBridge(url="", token=spec.run_id, list_tools=ctx.list_connectors,
                                     call=ctx.call_connector) if spec.cloud.get("cloud_connectors") else None
        res = self.runtime.run(RunRequest(agent_id=spec.agent_id, context_id=spec.context_id, prompt=spec.prompt,
                                          cwd=Path(spec.workdir), authority=list(spec.authority),
                                          session_id=spec.session_id, tools=tools, on_event=ctx.emit,
                                          timeout_s=spec.timeout_s, model=spec.model, cancel=cancel,
                                          questions=questions, connectors=connectors))
        return RunOutcome(session_id=res.session_id, final_text=res.final_text, tool_calls=res.tool_calls,
                          usage=res.usage, ok=res.ok, error=res.error)


def unit_dirs(root: Path, unit: str) -> tuple[Path, Path, Path]:
    """执行面目录树里一个执行单元的（单元目录, 工作目录, dsh 状态目录）。不在数据目录的 conversations/、tasks/ 里。"""
    base = root / "units" / unit_rel(unit)
    return base, base / "work", base / "state"


class MemorySandboxes:
    """测试用后端：不起任何进程。沙箱客户端（真实的 SandboxConnector 代码）在进程里，经内存链路连到身份中心。
    没有 bwrap，工作目录就是执行面目录树里的物理位置（看不到“原来的绝对路径”，脚本运行时不在乎）。"""

    name = "memory"

    def __init__(self) -> None:
        self.host: SandboxHost | None = None
        self._boxes: dict[str, dict[str, Any]] = {}     # 单元 → {generation, started_at, alive, conn, rt}
        self._lock = threading.Lock()
        self.started: list[str] = []                     # 起过的沙箱（测试看）

    def bind(self, host: SandboxHost) -> None:
        self.host = host

    def report(self) -> dict[str, Any]:
        return {"isolation": "none", "note": "测试用的内存后端：没有任何隔离", "quota": False}

    def ensure(self, spec: SandboxSpec) -> SandboxStatus:
        from .connector.sandbox import SandboxConnector
        assert self.host is not None
        with self._lock:
            box = self._boxes.get(spec.unit_id)
        if box is not None and box["alive"] and box["generation"] == spec.generation:
            return self.status(spec.unit_id)
        self.stop(spec.unit_id)
        _, work, state = unit_dirs(self.host.root, spec.unit_id)
        work.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        conn = SandboxConnector([RuntimeDriver(self.host.runtime)], work, state)
        hub = self.host.hub
        grant = hub.redeem(spec.boot_token)
        if grant is None:
            raise SandboxError("TRANSPORT 启动令牌无效")
        box = {"generation": spec.generation, "started_at": time.time(), "alive": True, "conn": conn}
        server: dict[str, DeviceRuntime] = {}

        def down(msg: dict[str, Any]) -> None:   # 服务器 → 客户端
            if box["alive"]:
                conn.dispatch(msg)

        def up(msg: dict[str, Any]) -> None:     # 客户端 → 服务器；回执交回客户端
            if not box["alive"]:
                raise_closed()
            reply = server["rt"].on_message(msg)
            if reply is not None:
                conn.dispatch(reply)

        conn.send = up  # type: ignore[method-assign]
        conn.server_hello = hello_message(grant)
        server["rt"] = hub.accept(grant, MemoryLink(serve=down), conn.hello_message())
        box["rt"] = server["rt"]
        with self._lock:
            self._boxes[spec.unit_id] = box
            self.started.append(spec.unit_id)
        return self.status(spec.unit_id)

    def crash(self, unit: str) -> None:
        """测试用：沙箱进程没了（客户端退出）。"""
        with self._lock:
            box = self._boxes.get(unit)
        if box is not None and box["alive"]:
            box["alive"] = False
            self.host.hub.detach(unit, box.get("rt"))  # type: ignore[union-attr]

    def stop(self, unit_id: str) -> None:
        self.crash(unit_id)
        with self._lock:
            box = self._boxes.get(unit_id)
            if box is not None:
                box["conn"].bridge.close()

    def destroy(self, unit_id: str) -> None:
        self.stop(unit_id)
        with self._lock:
            self._boxes.pop(unit_id, None)
        if self.host is not None:
            shutil.rmtree(unit_dirs(self.host.root, unit_id)[0], ignore_errors=True)

    def status(self, unit_id: str) -> SandboxStatus:
        with self._lock:
            box = self._boxes.get(unit_id)
        if box is None:
            return SandboxStatus(unit_id, "absent")
        return SandboxStatus(unit_id, "running" if box["alive"] else "stopped", box["generation"], box["started_at"])

    def list(self) -> list[SandboxStatus]:
        with self._lock:
            units = list(self._boxes)
        return [self.status(u) for u in units]

    def adopt(self, unit: str, generation: int, started_at: float = 0.0) -> None:
        """测试用：一个控制面不认识的沙箱（上一个控制面进程留下的、或代数过期的）。"""
        with self._lock:
            self._boxes[unit] = {"generation": generation, "started_at": started_at, "alive": True,
                                 "conn": _Nothing(), "rt": None}


class _Nothing:
    class bridge:  # noqa: N801
        @staticmethod
        def close() -> None: ...


def raise_closed() -> None:
    from .device import DeviceError
    raise DeviceError("沙箱已经停了")


# ================= local 后端：本机子进程 + 外层 bwrap =================
SANDBOX_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy",
               "http_proxy", "no_proxy", "ALL_PROXY", "all_proxy", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
               "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "PIP_CERT", "GIT_SSL_CAINFO")


def sandbox_env(state: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """沙箱客户端进程的环境：只带运行要的（路径、语言、代理、CA）和模型供应商的密钥变量（`*_API_KEY`，阶段 2 由出口网关换掉）。
    不带 MATRIX_* 等部署方变量，HOME 是这个单元的状态目录。"""
    base = dict(os.environ if base is None else base)
    env = {k: v for k, v in base.items() if k in SANDBOX_ENV}
    env.update({k: v for k, v in base.items() if k.endswith("_API_KEY")
                and not k.startswith(("MATRIX_", "AGENTNET_"))})
    import matrix
    env.update(HOME=str(state), TMPDIR="/tmp", PYTHONPATH=str(Path(matrix.__file__).resolve().parent.parent),
               PYTHONUNBUFFERED="1")
    env.setdefault("LANG", "C.UTF-8")
    return env


def _within(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def local_argv(host: SandboxHost, unit: str) -> tuple[list[str], dict[str, str], Path]:
    """local 后端起一个沙箱的命令：外层 bwrap 里只读挂系统目录、Python 环境与 Matrix 代码、dsh 与 node、CA 证书；
    可写的只有这个单元的工作目录（bind 到它的逻辑路径，也就是原来的绝对路径：dsh 的会话绑死了它）和 dsh 状态目录。
    返回（命令, 环境, 逻辑工作目录）。"""
    _, work, state = unit_dirs(host.root, unit)
    logical = host.workdir_of(unit)
    env = sandbox_env(state)
    exe = shutil.which(host.dsh_bin, path=env.get("PATH")) or host.dsh_bin
    ro = [p for p in outer_ro(exe, env)
          if not any(_within(p, x) or _within(x, p) for x in (logical, state, work, *host.forbidden))]
    client = [sys.executable, "-m", "matrix.connector.sandbox", "--url", host.connect_url,
              "--workdir", str(logical), "--state", str(state), "--dsh-bin", exe]
    cmd = bw.wrap(client, ro=ro, rw=[state], network=True, nested=True, binds=[(work, logical)], chdir=logical)
    return cmd, env, logical


class LocalSandboxes:
    """local 后端：沙箱客户端作为本机子进程，套外层 bwrap（Linux）。没有 bwrap 时起不来（不悄悄退回不隔离的子进程）。
    做不到 cgroup 配额（进程数、内存、磁盘总量），也不隔离网络与内核：能力报告里照实写。"""

    uses_gateway = True   # 模型经出口网关；但共用宿主网络，做不到强制断网（能力报告里写明）

    name = "local"

    def __init__(self) -> None:
        self.host: SandboxHost | None = None
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._jobs: queue.Queue = queue.Queue()
        # 子进程由一个常驻线程起：bwrap 的 --die-with-parent 跟的是起它的那个线程，轮次线程退出不能带走沙箱
        threading.Thread(target=self._launcher, name="matrix-sandbox-launcher", daemon=True).start()

    def bind(self, host: SandboxHost) -> None:
        self.host = host

    def report(self) -> dict[str, Any]:
        ok, why = bw.available("nested")
        if not ok:
            return {"isolation": "none", "note": f"local 后端起不来：{why}", "quota": False}
        return {"isolation": "sandbox", "quota": False, "net_enforced": False,
                "note": "local 后端：每个执行单元一个本机子进程，外层 bubblewrap 里只有这个单元的工作目录与 dsh 状态；"
                        "模型经出口网关（密钥不进沙箱）；没有 cgroup 配额（进程数、内存、磁盘），网络与内核和服务器共用，"
                        "做不到强制断网（Agent 的命令不理代理就能直接出网）"}

    def _launcher(self) -> None:
        while True:
            argv, env, cwd, log, box = self._jobs.get()
            try:
                with open(log, "ab") as out:
                    box["proc"] = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out, stderr=out, env=env,
                                                   cwd=cwd, start_new_session=True)
            except OSError as e:
                box["error"] = str(e)
            box["done"].set()

    def ensure(self, spec: SandboxSpec) -> SandboxStatus:
        assert self.host is not None
        st = self.status(spec.unit_id)
        if st.state == "running" and st.generation == spec.generation:
            return st
        self.stop(spec.unit_id)
        ok, why = bw.available("nested")
        if not ok:
            raise SandboxError(f"ISOLATION: local 后端要 bubblewrap（外层里再起 dsh 的 bwrap）：{why}")
        base, work, state = unit_dirs(self.host.root, spec.unit_id)
        work.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        argv, env, _ = local_argv(self.host, spec.unit_id)
        box: dict[str, Any] = {"done": threading.Event()}
        self._jobs.put((argv, env, str(state), base / "sandbox.log", box))
        box["done"].wait(30)
        proc = box.get("proc")
        if proc is None:
            raise SandboxError(f"TRANSPORT 沙箱起不来：{box.get('error') or '超时'}")
        try:
            assert proc.stdin is not None
            proc.stdin.write((spec.boot_token + "\n").encode())
            proc.stdin.close()
        except OSError:
            pass
        started = time.time()
        (base / "sandbox.json").write_text(json.dumps({"pid": proc.pid, "generation": spec.generation,
                                                       "started_at": started}), encoding="utf-8")
        with self._lock:
            self._procs[spec.unit_id] = proc
        hub = self.host.hub

        def watch() -> None:   # 客户端退出就是沙箱退出
            proc.wait()
            rt = hub.live(spec.unit_id)
            if rt is not None and rt.generation == spec.generation:
                hub.detach(spec.unit_id, rt)
        threading.Thread(target=watch, daemon=True).start()
        return SandboxStatus(spec.unit_id, "running", spec.generation, started)

    def _meta(self, unit: str) -> dict[str, Any] | None:
        assert self.host is not None
        try:
            return json.loads((unit_dirs(self.host.root, unit)[0] / "sandbox.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return b"matrix.connector.sandbox" in cmdline and ") Z " not in stat

    def stop(self, unit_id: str) -> None:
        with self._lock:
            proc = self._procs.pop(unit_id, None)
        meta = self._meta(unit_id)
        pid = proc.pid if proc is not None else (meta or {}).get("pid")
        if isinstance(pid, int) and self._alive(pid):
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass
            for _ in range(50):
                if not self._alive(pid):
                    break
                time.sleep(0.1)
            else:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
        if proc is not None:
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
        if self.host is not None:
            self.host.hub.detach(unit_id)

    def destroy(self, unit_id: str) -> None:
        self.stop(unit_id)
        assert self.host is not None
        shutil.rmtree(unit_dirs(self.host.root, unit_id)[0], ignore_errors=True)

    def status(self, unit_id: str) -> SandboxStatus:
        meta = self._meta(unit_id)
        if meta is None:
            return SandboxStatus(unit_id, "absent")
        alive = isinstance(meta.get("pid"), int) and self._alive(meta["pid"])
        return SandboxStatus(unit_id, "running" if alive else "stopped", int(meta.get("generation", 0)),
                             float(meta.get("started_at", 0)))

    def list(self) -> list[SandboxStatus]:
        assert self.host is not None
        out = []
        units = self.host.root / "units"
        for f in units.rglob("sandbox.json") if units.is_dir() else []:
            rel = f.parent.relative_to(units).parts
            if len(rel) >= 2 and rel[0] in ("seats", "tasks"):
                out.append(self.status(f"{'seat' if rel[0] == 'seats' else 'task'}:{'/'.join(rel[1:])}"))
        return out


# ================= docker 后端：经沙箱管理器（matrix-sandboxd） =================
class DockerSandboxes:
    """docker 后端（阶段 1b）：每个执行单元一个容器（生产用 gVisor）。Matrix 服务不碰 Docker，只经 Unix socket 上的本地 HTTP
    向沙箱管理器提请求；请求里只有执行单元 id、所有者、Agent、代数、配额、启动令牌、逻辑工作目录（管理器校验前缀）。
    镜像、运行时、安全选项、网络、挂载模板都在管理器的配置里。"""

    uses_gateway = True

    name = "docker"

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.host: SandboxHost | None = None
        self._report: tuple[float, dict[str, Any]] | None = None

    def bind(self, host: SandboxHost) -> None:
        self.host = host

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 180) -> Any:
        import http.client
        import socket

        class Conn(http.client.HTTPConnection):
            def connect(conn) -> None:  # noqa: N805
                conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.sock.settimeout(timeout)
                conn.sock.connect(self.socket_path)
        c = Conn("sandboxd", timeout=timeout)
        try:
            raw = json.dumps(body).encode() if body is not None else None
            c.request(method, path, body=raw, headers={"Content-Type": "application/json"} if raw else {})
            r = c.getresponse()
            data = json.loads(r.read() or b"null")
        except (OSError, ValueError) as e:
            raise SandboxError(f"TRANSPORT 连不上沙箱管理器（{self.socket_path}）：{e}") from e
        finally:
            c.close()
        if r.status != 200:
            msg = str((data or {}).get("error") if isinstance(data, dict) else data)
            raise SandboxError(msg if msg.startswith(("ISOLATION", "TRANSPORT")) else f"TRANSPORT 沙箱管理器拒绝：{msg}")
        return data

    def report(self) -> dict[str, Any]:
        cached = self._report
        if cached is not None and time.time() - cached[0] < 30:
            return cached[1]
        try:
            rep = self._call("GET", "/report", timeout=30)
        except SandboxError as e:
            rep = {"isolation": "none", "production": False, "quota": False, "note": f"docker 后端不可用：{e}"}
        self._report = (time.time(), rep)
        return rep

    def ensure(self, spec: SandboxSpec) -> SandboxStatus:
        assert self.host is not None
        q = spec.quota
        got = self._call("POST", "/ensure", {
            "unit_id": spec.unit_id, "owner_id": spec.owner_id, "agent_id": spec.agent_id,
            "generation": spec.generation, "boot_token": spec.boot_token,
            "workdir": str(self.host.workdir_of(spec.unit_id)),
            "quota": {"cpus": q.cpus, "memory_mb": q.memory_mb, "pids": q.pids}})
        return SandboxStatus(**got)

    def stop(self, unit_id: str) -> None:
        self._call("POST", "/stop", {"unit_id": unit_id})
        if self.host is not None:
            self.host.hub.detach(unit_id)

    def destroy(self, unit_id: str) -> None:
        self._call("POST", "/destroy", {"unit_id": unit_id})
        if self.host is not None:
            self.host.hub.detach(unit_id)

    def status(self, unit_id: str) -> SandboxStatus:
        from urllib.parse import quote
        return SandboxStatus(**self._call("GET", f"/status?unit_id={quote(unit_id)}", timeout=30))

    def list(self) -> list[SandboxStatus]:
        return [SandboxStatus(**x) for x in self._call("GET", "/list", timeout=60)]


# ================= 引擎眼里的云侧运行时 =================
class SandboxRuntime:
    """把一轮交给执行单元的沙箱。`connect(unit)` 由引擎给：要沙箱（幂等）并等客户端连上，返回那条连接的 DeviceRuntime。"""

    name = "sandbox"

    def __init__(self, base: Any, provider: SandboxProvider, connect: Callable[[str], DeviceRuntime],
                 log: Callable[[str, dict[str, Any]], None] | None = None):
        self.base = base
        self.provider = provider
        self._connect = connect
        self._log = log

    def capabilities(self) -> RuntimeCapabilities:
        get = getattr(self.base, "capabilities", None)
        caps = get() if callable(get) else RuntimeCapabilities.all_enforced(getattr(self.base, "name", "runtime"))
        rep = self.provider.report()
        return replace(caps, placement="hosted", approval="sandbox-only", isolation=rep["isolation"],
                       isolation_note=rep["note"])

    def run(self, req: RunRequest) -> RunResult:
        if req.unit is None:
            return RunResult(req.session_id or "", "", ok=False, error="runtime error: 沙箱方式的一轮没有执行单元")
        try:
            dev = self._connect(req.unit)
        except SandboxError as e:
            return RunResult(req.session_id or "", "", ok=False, error=str(e))
        if self._log is not None and req.on_event is not None:  # dsh 的事件存在控制面这边（不在沙箱的工作目录里）
            inner, unit, log = req.on_event, req.unit, self._log

            def on_event(ev: dict[str, Any]) -> None:
                log(unit, ev)
                inner(ev)
            req = replace(req, on_event=on_event)
        return dev.run(req)


def sandbox_config(env: dict[str, str] | None = None) -> dict[str, Any]:
    """部署方开关（docs/cloud-runtime.md、deploy/README.md）：MATRIX_CLOUD_SANDBOX=off（默认）| local | docker（MATRIX_SANDBOXD_SOCKET）；
    MATRIX_SANDBOX_MAX / MATRIX_SANDBOX_PER_USER（并发上限）、MATRIX_SANDBOX_IDLE（秒）、MATRIX_SANDBOX_RETAIN（秒）、
    MATRIX_CHECKPOINT_LIMIT（字节）、MATRIX_SANDBOX_ROOT（执行面目录树）。"""
    from . import compat
    get = (lambda k: (env or {}).get(f"MATRIX_{k}")) if env is not None else compat.env

    def num(key: str, default: float) -> float:
        try:
            v = float(get(key) or default)
            return v if v > 0 else default
        except ValueError:
            return default
    return {"backend": (get("CLOUD_SANDBOX") or "off").strip().lower(),
            "max": int(num("SANDBOX_MAX", default_max())), "per_user": int(num("SANDBOX_PER_USER", 4)),
            "idle_s": num("SANDBOX_IDLE", 600), "retain_s": num("SANDBOX_RETAIN", 7 * 86400),
            "checkpoint_limit": int(num("CHECKPOINT_LIMIT", 1_000_000_000)), "root": get("SANDBOX_ROOT") or "",
            # 出口网关（阶段 2a）：监听在哪（docker 后端要是沙箱网络能到的地址）、包镜像源（逗号分隔的主机，有 shell.exec 时放行）
            "gateway_listen": get("GATEWAY_LISTEN") or "", "gateway_mirrors": get("GATEWAY_MIRRORS") or ""}


def default_max() -> int:
    """全局并发默认按 (宿主内存 − 2 GB) / 512 MB 估（docs/cloud-runtime.md 4.3），至少 2。"""
    try:
        mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 8
    return max(2, int((mem - (2 << 30)) // (512 << 20)))


def provider_named(name: str) -> SandboxProvider | None:
    if name in ("", "off", "none", "process"):
        return None
    if name == "local":
        return LocalSandboxes()
    if name == "memory":
        return MemorySandboxes()
    if name == "docker":
        from . import compat
        return DockerSandboxes(compat.env("SANDBOXD_SOCKET") or "/run/matrix-sandboxd/sandboxd.sock")
    raise ValueError(f"MATRIX_CLOUD_SANDBOX 的值 {name!r} 认不出（off / local / docker）")


__all__ = ["CANCEL", "FILE_FETCH", "RUN_REQUEST", "Grant", "LocalSandboxes", "MemorySandboxes", "Quota",
           "RuntimeDriver", "SandboxError", "SandboxHost", "SandboxHub", "SandboxProvider", "SandboxRuntime",
           "SandboxSpec", "SandboxStatus", "hello_message", "local_argv", "provider_named", "sandbox_config",
           "sandbox_env", "unit_dirs"]
