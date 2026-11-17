"""沙箱管理器 `matrix-sandboxd`（docs/cloud-runtime.md 第 3、4 节；阶段 1b 的 `docker` 后端）。

唯一能创建 / 销毁沙箱的组件，也是唯一持有 Docker 权限（docker.sock）的进程。Matrix 服务只经 Unix socket 上的本地 HTTP
向它提请求，请求里只有：执行单元 id、所有者、Agent、代数、配额、一次性启动令牌、逻辑工作目录（由控制面台账给，这里校验
它落在允许的前缀下）。**镜像、运行时、安全选项、网络、挂载模板、命令都写死在这边的配置里**，请求带了别的字段一律拒绝
（`validate_ensure`）。

管理器不存意图：只给容器与卷打上执行单元 id 与代数的标签，上报实际状态；对账由控制面做（`Network.reconcile_sandboxes`）。
调 Docker 用 `docker` 命令行（子进程），不引入新依赖；测试注入假的执行器（`Docker(run=...)`）。

    matrix-sandboxd --config /etc/matrix/sandboxd.json --socket /run/matrix-sandboxd/sandboxd.sock

接口（JSON）：POST /ensure、POST /stop {unit_id}、POST /destroy {unit_id}、GET /status?unit_id=…、GET /list、GET /report。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .checkpoints import unit_rel

LABEL = "dev.matrix"
RUNTIMES = {"runsc": "runsc", "insecure-runc": "runc"}   # 部署方写的档位 → docker 的 --runtime
ID_RE = re.compile(r"^[A-Za-z0-9._@-]{1,80}$")
UNIT_RE = re.compile(r"^(seat|task):[A-Za-z0-9._/-]{1,200}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,400}$")   # 逻辑工作目录：不能有逗号、等号、空白（--mount 的语法），不能有 ..
ENSURE_KEYS = {"unit_id", "owner_id", "agent_id", "generation", "quota", "boot_token", "workdir"}
QUOTA_KEYS = {"cpus", "memory_mb", "pids"}
STATE_DIR = "/state"


class RequestError(ValueError):
    """请求不合法（400）。"""


class Unavailable(RuntimeError):
    """沙箱后端不可用（503）：消息以错误类别开头（ISOLATION / TRANSPORT），控制面按它分类。"""


@dataclass
class DockerConfig:
    """部署方写死的沙箱模板（`--config` 的 JSON 文件；没有的字段用默认值）。"""
    # 部署的名字：容器与卷的名字、标签都带上它，管理器只认自己这个名字下的沙箱。同一个 Docker 上跑几套 Matrix（含验证工具）时
    # 各写各的，否则一套的对账会把另一套的沙箱当成“没有记录”删掉（2026-11-16 本机实测踩到）
    namespace: str = "matrix"
    image: str = "matrix-sandbox:latest"
    runtime: str = "runsc"                       # runsc（默认，gVisor）| insecure-runc（未达到生产隔离）
    network: str = "matrix-sandbox"              # 沙箱专用的 Docker 网络（容器间不互通，见 deploy/compose.sandbox.yml）
    connect_url: str = "ws://matrix:8765/api/sandbox/connect"   # 沙箱客户端连回控制面的地址
    workdir_prefixes: list[str] = field(default_factory=lambda: ["/data/conversations/", "/data/tasks/"])
    user: str = "1000:1000"
    cpus: float = 2.0                            # 默认配额；请求里的配额不能超过 max_*
    memory_mb: int = 2048
    pids: int = 512
    max_cpus: float = 4.0
    max_memory_mb: int = 8192
    max_pids: int = 2048
    tmp_mb: int = 512                            # /tmp 的 tmpfs 上限
    env: dict[str, str] = field(default_factory=dict)   # 沙箱里固定的环境（出网代理、CA）：不能放密钥
    dsh_bin: str = "dsh"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", self.namespace):
            raise ValueError("namespace 只能是小写字母、数字、连字符，至多 31 个字符")
        if self.runtime not in RUNTIMES:
            raise ValueError(f"runtime 只能是 {'、'.join(RUNTIMES)}（写的是 {self.runtime!r}）")
        if self.network in ("host", "none", "") or ":" in self.network:
            raise ValueError("network 不能是 host / none / container:…：沙箱要有自己的网络")
        for p in self.workdir_prefixes:
            if not p.startswith("/") or not p.endswith("/") or ".." in p or p == "/":
                raise ValueError(f"workdir_prefixes 里的 {p!r} 要是以 / 开头和结尾的绝对路径，不能是根目录")
        bad = [k for k in self.env if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) or "KEY" in k.upper()
               or "TOKEN" in k.upper() or "SECRET" in k.upper() or "PASSWORD" in k.upper()]
        if bad:
            raise ValueError(f"env 里不能放这些（像是密钥，或名字不合法）：{bad}")

    @classmethod
    def load(cls, path: str | None) -> "DockerConfig":
        raw: dict[str, Any] = {}
        if path:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        unknown = set(raw) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"配置里有不认识的字段：{sorted(unknown)}")
        return cls(**raw)

    @property
    def production(self) -> bool:
        return self.runtime == "runsc"


def names(unit: str, ns: str = "matrix") -> tuple[str, str, str]:
    """执行单元 → （容器名, 工作目录卷, 状态卷）。名字由部署名与 id 的哈希决定，标签里才有原样的 id。"""
    h = hashlib.sha256(f"{ns}\0{unit}".encode()).hexdigest()[:16]
    return f"{ns}-sbx-{h}", f"{ns}-sbx-{h}-work", f"{ns}-sbx-{h}-state"


def _num(v: Any, lo: float, hi: float, what: str, integer: bool) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and not isinstance(v, int)):
        raise RequestError(f"{what} 要是{'整数' if integer else '数'}")
    if not lo < v <= hi:
        raise RequestError(f"{what} 超出范围（上限 {hi}）")
    return v


def validate_ensure(body: Any, conf: DockerConfig) -> dict[str, Any]:
    """ensure 请求的校验：只认这几个字段；任意镜像、命令、挂载、特权、网络都没有位置，带了就拒绝。"""
    if not isinstance(body, dict):
        raise RequestError("请求要是 JSON 对象")
    extra = set(body) - ENSURE_KEYS
    if extra:
        raise RequestError(f"请求里不能有这些字段：{sorted(extra)}（镜像、命令、挂载、特权都由管理器的配置决定）")
    missing = {"unit_id", "agent_id", "generation", "boot_token", "workdir"} - set(body)
    if missing:
        raise RequestError(f"缺少字段：{sorted(missing)}")
    unit = body["unit_id"]
    if not isinstance(unit, str) or not UNIT_RE.fullmatch(unit):
        raise RequestError("非法的执行单元 id")
    try:
        unit_rel(unit)
    except ValueError as e:
        raise RequestError(str(e)) from e
    for k in ("owner_id", "agent_id"):
        v = body.get(k)
        if not (v is None and k == "owner_id") and (not isinstance(v, str) or not ID_RE.fullmatch(v)):
            raise RequestError(f"非法的 {k}")
    gen = _num(body["generation"], 0, 1e9, "generation", True)
    tok = body["boot_token"]
    if not isinstance(tok, str) or not TOKEN_RE.fullmatch(tok):
        raise RequestError("非法的启动令牌")
    wd = body["workdir"]
    if not isinstance(wd, str) or not PATH_RE.fullmatch(wd) or posixpath.normpath(wd) != wd or ".." in wd.split("/") \
            or not any(wd.startswith(p) and len(wd) > len(p) for p in conf.workdir_prefixes):
        raise RequestError(f"逻辑工作目录不在允许的前缀下（{'、'.join(conf.workdir_prefixes)}）")
    if wd == STATE_DIR or wd.startswith(STATE_DIR + "/") or wd == "/tmp" or wd.startswith("/tmp/"):
        raise RequestError("逻辑工作目录不能和状态目录、/tmp 重叠")
    q = body.get("quota") or {}
    if not isinstance(q, dict) or set(q) - QUOTA_KEYS:
        raise RequestError(f"配额只认 {sorted(QUOTA_KEYS)}")
    quota = {"cpus": _num(q.get("cpus", conf.cpus), 0, conf.max_cpus, "cpus", False),
             "memory_mb": _num(q.get("memory_mb", conf.memory_mb), 63, conf.max_memory_mb, "memory_mb", True),
             "pids": _num(q.get("pids", conf.pids), 15, conf.max_pids, "pids", True)}
    return {"unit_id": unit, "owner_id": body.get("owner_id"), "agent_id": body["agent_id"], "generation": int(gen),
            "boot_token": tok, "workdir": wd, "quota": quota}


def validate_unit(body: Any) -> str:
    if not isinstance(body, dict) or set(body) != {"unit_id"} or not isinstance(body["unit_id"], str) \
            or not UNIT_RE.fullmatch(body["unit_id"]):
        raise RequestError("只要 unit_id")
    try:
        unit_rel(body["unit_id"])
    except ValueError as e:
        raise RequestError(str(e)) from e
    return body["unit_id"]


def security_args(conf: DockerConfig) -> list[str]:
    """容器的安全选项（docs/cloud-runtime.md 4.3）：非 root、去掉全部特权、no-new-privileges、只读根、独立 IPC、有上限的 /tmp。
    insecure-runc 档另外放开 seccomp 与 /proc 遮挡：runc 里不放开，dsh 的内层 bwrap 建不了命名空间，会悄悄退到更弱的沙箱
    （第 11 节补充，matrix-dsh-check 的 sandbox 一项会变红）。gVisor 自己实现这些系统调用，不需要放开。"""
    out = ["--runtime", RUNTIMES[conf.runtime], "--user", conf.user, "--cap-drop", "ALL",
           "--security-opt", "no-new-privileges", "--read-only", "--ipc", "private",
           # /tmp 要能执行：dsh 的原生模块加载器把 .node 拷到 $TMPDIR 再加载（noexec 下 dsh 起不来，实测），Agent 装依赖也要
           "--tmpfs", f"/tmp:rw,exec,nosuid,nodev,size={conf.tmp_mb}m,mode=1777"]
    if conf.runtime == "insecure-runc":
        out += ["--security-opt", "seccomp=unconfined", "--security-opt", "systempaths=unconfined"]
    return out


def run_argv(req: dict[str, Any], conf: DockerConfig, now: float) -> list[str]:
    """起一个沙箱的 `docker run` 参数（纯函数）。挂载只有这个执行单元的两个卷：工作目录挂在逻辑路径（原来的绝对路径）上，
    dsh 状态挂在 /state；不挂宿主目录、docker.sock、数据目录。启动令牌经环境变量给客户端（用过即废）。"""
    unit, q = req["unit_id"], req["quota"]
    name, work, state = names(unit, conf.namespace)
    labels = {"ns": conf.namespace, "unit": unit, "generation": str(req["generation"]), "started": f"{now:.3f}",
              "agent": req["agent_id"], "owner": req.get("owner_id") or ""}
    argv = ["docker", "run", "-d", "--name", name, "--hostname", "sandbox", "--init",
            *security_args(conf),
            "--network", conf.network,
            "--cpus", f"{q['cpus']:g}", "--memory", f"{q['memory_mb']}m", "--memory-swap", f"{q['memory_mb']}m",
            "--pids-limit", str(q["pids"]),
            "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=2",
            # volume-nocopy：空卷挂上来时 Docker 会照搬挂载点（镜像里没有、临时建的、归 root）的属主，把初始化时交给
            # 沙箱用户的属主又改回 root；工作目录卷不要这一步
            "--mount", f"type=volume,src={work},dst={req['workdir']},volume-nocopy",
            "--mount", f"type=volume,src={state},dst={STATE_DIR}",
            "--workdir", req["workdir"]]
    for k, v in labels.items():
        argv += ["--label", f"{LABEL}.{k}={v}"]
    env = {**conf.env, "HOME": STATE_DIR, "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1",
           "MATRIX_BOOT_TOKEN": req["boot_token"]}
    for k, v in env.items():
        argv += ["--env", f"{k}={v}"]
    argv += [conf.image, "python", "-m", "matrix.connector.sandbox", "--url", conf.connect_url,
             "--workdir", req["workdir"], "--state", STATE_DIR, "--dsh-bin", conf.dsh_bin]
    return argv


def init_argv(conf: DockerConfig, work: str) -> list[str]:
    """新建的工作目录卷挂在镜像里不存在的路径上时归 root：用一个只有 CHOWN 特权、不联网、只读根的一次性容器把它交给沙箱用户。"""
    return ["docker", "run", "--rm", "--network", "none", "--runtime", RUNTIMES[conf.runtime], "--user", "0:0",
            "--cap-drop", "ALL", "--cap-add", "CHOWN", "--security-opt", "no-new-privileges", "--read-only",
            "--mount", f"type=volume,src={work},dst=/w", "--entrypoint", "chown", conf.image, conf.user, "/w"]


Runner = Callable[[list[str]], tuple[int, str, str]]


def _subprocess(argv: list[str]) -> tuple[int, str, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)
    return r.returncode, r.stdout, r.stderr


class Manager:
    def __init__(self, conf: DockerConfig, run: Runner = _subprocess, now: Callable[[], float] = time.time):
        self.conf = conf
        self.run = run
        self.now = now
        self._lock = threading.Lock()
        self._runtime_ok: tuple[float, bool, str] | None = None

    def _docker(self, *args: str, ok: tuple[int, ...] = (0,)) -> str:
        rc, out, err = self.run(["docker", *args])
        if rc not in ok:
            raise Unavailable(f"TRANSPORT docker {args[0]} 失败：{(err or out).strip()[-300:]}")
        return out

    def runtime_status(self) -> tuple[bool, str]:
        """配置的运行时在这台机器上有没有（缓存 60 秒）。没有就失败关闭，不退回别的运行时。"""
        cached = self._runtime_ok
        if cached is not None and self.now() - cached[0] < 60:
            return cached[1], cached[2]
        want = RUNTIMES[self.conf.runtime]
        rc, out, err = self.run(["docker", "info", "--format", "{{json .Runtimes}}"])
        if rc != 0:
            ok, why = False, f"连不上 Docker：{(err or out).strip()[-200:]}"
        else:
            try:
                have = set(json.loads(out or "{}"))
            except ValueError:
                have = set()
            ok = want in have
            why = "" if ok else f"Docker 里没有运行时 {want}（有 {'、'.join(sorted(have)) or '无'}）"
        self._runtime_ok = (self.now(), ok, why)
        return ok, why

    def report(self) -> dict[str, Any]:
        ok, why = self.runtime_status()
        rt = self.conf.runtime
        if not ok:
            return {"isolation": "none", "production": False, "quota": False, "volume_quota": False, "runtime": rt,
                    "note": f"docker 后端不可用：{why}。云侧运行一律拒绝（不退回别的方式）"}
        if rt == "runsc":
            note = "docker 后端 + gVisor（runsc）：每个执行单元一个容器，独立内核边界、只读根、非 root、cgroup 配额"
        else:
            note = ("未达到生产隔离：docker 后端用 runc（insecure-runc），容器与宿主共用内核，而且为了 dsh 的内层沙箱放开了 seccomp 与 "
                    "/proc 遮挡；只用于验证与过渡，生产要装 gVisor")
        return {"isolation": "sandbox", "production": rt == "runsc", "quota": True, "volume_quota": False,
                "runtime": rt, "note": note + "；卷没有大小上限（Docker 的 local 卷做不到）"}

    def _inspect(self, name: str) -> dict[str, Any] | None:
        rc, out, _ = self.run(["docker", "container", "inspect", name])
        if rc != 0:
            return None
        try:
            got = json.loads(out)
        except ValueError:
            return None
        return got[0] if isinstance(got, list) and got else None

    def status(self, unit: str) -> dict[str, Any]:
        name, work, _ = names(unit, self.conf.namespace)
        c = self._inspect(name)
        if c is None:
            rc, _, _ = self.run(["docker", "volume", "inspect", work])
            return {"unit_id": unit, "state": "stopped" if rc == 0 else "absent", "generation": 0, "started_at": 0.0}
        labels = (c.get("Config") or {}).get("Labels") or {}
        running = bool((c.get("State") or {}).get("Running"))
        return {"unit_id": unit, "state": "running" if running else "stopped",
                "generation": int(labels.get(f"{LABEL}.generation") or 0),
                "started_at": float(labels.get(f"{LABEL}.started") or 0)}

    def ensure(self, body: Any) -> dict[str, Any]:
        req = validate_ensure(body, self.conf)
        ok, why = self.runtime_status()
        if not ok:
            raise Unavailable(f"ISOLATION: {why}")
        unit = req["unit_id"]
        name, work, state = names(unit, self.conf.namespace)
        with self._lock:
            cur = self.status(unit)
            if cur["state"] == "running" and cur["generation"] == req["generation"]:
                return cur
            self._docker("rm", "-f", name, ok=(0, 1))
            labels = ["--label", f"{LABEL}.ns={self.conf.namespace}", "--label", f"{LABEL}.unit={unit}"]
            if self.run(["docker", "volume", "inspect", work])[0] != 0:
                self._docker("volume", "create", *labels, work)
                rc, out, err = self.run(init_argv(self.conf, work))
                if rc != 0:
                    raise Unavailable(f"TRANSPORT 工作目录卷准备不了：{(err or out).strip()[-300:]}")
            if self.run(["docker", "volume", "inspect", state])[0] != 0:
                self._docker("volume", "create", *labels, state)   # 镜像里 /state 归沙箱用户：空卷挂上去照搬属主
            now = self.now()
            rc, out, err = self.run(run_argv(req, self.conf, now))
            if rc != 0:
                raise Unavailable(f"TRANSPORT 沙箱容器起不来：{(err or out).strip()[-300:]}")
        return {"unit_id": unit, "state": "running", "generation": req["generation"], "started_at": now}

    def stop(self, unit: str) -> None:
        self._docker("rm", "-f", names(unit, self.conf.namespace)[0], ok=(0, 1))

    def destroy(self, unit: str) -> None:
        name, work, state = names(unit, self.conf.namespace)
        self._docker("rm", "-f", name, ok=(0, 1))
        self._docker("volume", "rm", "-f", work, state, ok=(0, 1))

    def list(self) -> list[dict[str, Any]]:
        """这个部署名下带执行单元标签的容器与卷。"""
        out: dict[str, dict[str, Any]] = {}
        fmt = '{{.Label "' + LABEL + '.unit"}}'
        mine = f"label={LABEL}.ns={self.conf.namespace}"   # 只认自己这个部署名下的
        vols = self._docker("volume", "ls", "--filter", mine, "--format", fmt)
        for unit in {u.strip() for u in vols.splitlines() if u.strip()}:
            out[unit] = {"unit_id": unit, "state": "stopped", "generation": 0, "started_at": 0.0}
        cs = self._docker("ps", "-a", "--filter", mine, "--format",
                          fmt + '\t{{.State}}\t{{.Label "' + LABEL + '.generation"}}\t{{.Label "' + LABEL + '.started"}}')
        for line in cs.splitlines():
            parts = line.split("\t")
            if len(parts) != 4 or not parts[0]:
                continue
            try:
                out[parts[0]] = {"unit_id": parts[0], "state": "running" if parts[1] == "running" else "stopped",
                                 "generation": int(parts[2] or 0), "started_at": float(parts[3] or 0)}
            except ValueError:
                continue
        return list(out.values())


# ================= Unix socket 上的本地 HTTP =================
class _Handler(BaseHTTPRequestHandler):
    manager: Manager

    def _send(self, code: int, data: Any) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _call(self, fn: Callable[[], Any]) -> None:
        try:
            self._send(200, fn())
        except RequestError as e:
            self._send(400, {"error": str(e)})
        except Unavailable as e:
            self._send(503, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": f"TRANSPORT {type(e).__name__}: {e}"[:300]})

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        m = self.manager
        if u.path == "/report":
            return self._call(m.report)
        if u.path == "/list":
            return self._call(m.list)
        if u.path == "/status":
            q = parse_qs(u.query)
            return self._call(lambda: m.status(validate_unit({"unit_id": (q.get("unit_id") or [""])[0]})))
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            return self._send(413, {"error": "请求太大"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "不是 JSON"})
        m = self.manager
        if self.path == "/ensure":
            return self._call(lambda: m.ensure(body))
        if self.path == "/stop":
            return self._call(lambda: m.stop(validate_unit(body)) or {"ok": True})
        if self.path == "/destroy":
            return self._call(lambda: m.destroy(validate_unit(body)) or {"ok": True})
        self._send(404, {"error": "not found"})

    def address_string(self) -> str:
        return "unix"

    def log_message(self, fmt: str, *args: Any) -> None:
        if args and str(args[1]) not in ("200",):
            sys.stderr.write(f"[sandboxd] {fmt % args}\n")


_UnixBase: Any = getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)  # Windows 没有 Unix socket：模块照样能导入


class UnixHTTPServer(socketserver.ThreadingMixIn, _UnixBase):
    daemon_threads = True

    def get_request(self):  # type: ignore[override]
        req, _ = super().get_request()
        return req, ("unix", 0)


def serve(manager: Manager, path: str, mode: int = 0o660, group: int | None = None) -> UnixHTTPServer:
    """管理器只在 Linux 上跑（要 Docker）。group：socket 的属组（给 Matrix 服务的用户组，配 0660 只让它连）。"""
    if os.path.exists(path):
        os.unlink(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    handler = type("Handler", (_Handler,), {"manager": manager})
    srv = UnixHTTPServer(path, handler)
    if group is not None:
        os.chown(path, -1, group)   # 进程在这个组里时不需要 CHOWN 特权（compose 里用 group_add）
    os.chmod(path, mode)
    return srv


def main(argv: list[str] | None = None) -> None:
    from . import utf8_stdio
    utf8_stdio()
    p = argparse.ArgumentParser(prog="matrix-sandboxd", description="Matrix 沙箱管理器（唯一持有 Docker 权限）")
    p.add_argument("--config", default=os.environ.get("MATRIX_SANDBOXD_CONFIG"), help="沙箱模板的 JSON 配置")
    p.add_argument("--socket", default=os.environ.get("MATRIX_SANDBOXD_SOCKET", "/run/matrix-sandboxd/sandboxd.sock"))
    p.add_argument("--socket-mode", default="660", help="socket 文件权限（八进制）")
    p.add_argument("--socket-group", type=int, default=None, help="socket 的属组 gid（Matrix 服务的用户组，镜像里是 1000）")
    args = p.parse_args(argv)
    conf = DockerConfig.load(args.config)
    m = Manager(conf)
    rep = m.report()
    print(f"[sandboxd] {json.dumps({**asdict(conf), 'env': sorted(conf.env)}, ensure_ascii=False)}", flush=True)
    print(f"[sandboxd] {rep['note']}", flush=True)
    srv = serve(m, args.socket, int(args.socket_mode, 8), args.socket_group)
    print(f"[sandboxd] 监听 {args.socket}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
