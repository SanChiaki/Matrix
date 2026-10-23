"""连接器主循环：出站连服务器，收 `run.request`，分派给本机驱动。

一条连接可以承载多个运行（席位 / 任务并行）；每个运行一个线程。工具与审批请求经本机桥进来，
这里把它们转成 `tool.call` / `approval.request` 并等回执（按 `call_id` 配对）。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import shutil
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

import base64

from .. import homedir
from ..workdir import safe_rel
from ..device import (APPROVAL_REQUEST, CANCEL, FILE_FETCH, HELLO, HELLO_OK, PROTOCOL, RUN_EVENT, RUN_REQUEST,
                      RUN_RESULT, TOOL_CALL, DeviceError)
from ..models import AGENT_HOME, ATTACHMENTS, PROJECT_FILES, SKILLS
from .bridge import LocalBridge
from .drivers.base import Driver, RunOutcome, RunSpec

HOMES = ".homes"   # 私有目录在根目录下的位置：<root>/.homes/<agent>/。工作目录引用不能以 . 开头，碰不到这里


class _Run:
    """一次运行在本机的上下文：绑定桥令牌、回执等待表与取消标记。"""

    def __init__(self, connector: "Connector", run_id: str):
        self.connector = connector
        self.run_id = run_id
        self.cancel = threading.Event()
        self.workdir: Path | None = None
        self.home: str | None = None      # run.request 的 home："rw" | "ro" | None

    def emit(self, event: dict[str, Any]) -> None:
        self.connector.send({"type": RUN_EVENT, "run_id": self.run_id, "event": event})

    def call_tool(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        reply = self.connector.round_trip({"type": TOOL_CALL, "run_id": self.run_id,
                                           "call_id": self.connector.next_call_id(), "name": name, "args": args})
        return str(reply.get("text", "")), bool(reply.get("is_error"))

    def ask_approval(self, tool: str, reason: str) -> str:
        reply = self.connector.round_trip({"type": APPROVAL_REQUEST, "run_id": self.run_id,
                                           "call_id": self.connector.next_call_id(),
                                           "tool": tool, "reason": reason})
        return str(reply.get("outcome", "unavailable"))


class Connector:
    """一台设备一个连接器；可以同时提供多个 harness（驱动），第一个是默认。
    `run.request.harness` 选驱动；本机没有就直接回失败，不落到别的 harness 上（能力不同，权限会算错）。"""

    def __init__(self, drivers: Driver | list[Driver], workroot: Path, bridge: LocalBridge | None = None):
        drivers = list(drivers) if isinstance(drivers, (list, tuple)) else [drivers]
        if not drivers:
            raise DeviceError("至少要有一个驱动")
        self.drivers: dict[str, Driver] = {d.name: d for d in drivers}
        self.driver = drivers[0]  # 默认 harness
        self.workroot = Path(workroot).expanduser()
        self.bridge = bridge or LocalBridge()
        self._ws: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._send_lock = threading.Lock()
        self._pending: dict[str, threading.Event] = {}
        self._replies: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, _Run] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._home_locks: dict[str, threading.Lock] = {}  # 同一 Agent 的并行运行：挂载与写回按 Agent 串行

    # ---- 生命周期 ----
    def connect(self, url: str, token: str, *, hello_timeout: float = 20.0) -> None:
        """阻塞地连上并握手；失败抛 DeviceError。"""
        from websockets.sync.client import connect as ws_connect

        sep = "&" if "?" in url else "?"
        try:
            ws = ws_connect(f"{url}{sep}token={token}", open_timeout=hello_timeout, max_size=None)
        except Exception as e:  # noqa: BLE001
            raise DeviceError(f"无法连接服务器：{e}") from e
        first = json.loads(ws.recv(timeout=hello_timeout))
        if first.get("type") != HELLO:
            ws.close()
            raise DeviceError(f"握手失败：期望 {HELLO}，收到 {first.get('type')!r}")
        ws.send(json.dumps(self.hello_message(), ensure_ascii=False))
        self._ws = ws

    def hello_message(self) -> dict[str, Any]:
        """握手回执：默认 harness 放在 runtime / capabilities（旧服务器只认这两个），全部放在 runtimes。"""
        def caps_of(d: Driver) -> dict[str, Any]:
            c = d.capabilities()
            return {"name": c.name, "placement": c.placement, "enforcement": c.enforcement,
                    "session_resume": c.session_resume, "steer": c.steer,
                    "approval": c.approval, "sandbox": c.sandbox, "skills": c.skills}

        def services_of(d: Driver) -> list[str]:
            """这个 AI 工具里用户配置的服务：只报名字（电脑上的连接器），配置与凭证留在本机。"""
            get = getattr(d, "services", None)
            try:
                return list(get()) if callable(get) else []
            except Exception:  # noqa: BLE001 —— 读不出来就当没有，不影响上线
                return []
        return {"type": HELLO_OK, "protocol": PROTOCOL, "runtime": self.driver.name,
                "capabilities": caps_of(self.driver),
                "runtimes": [{"runtime": name, "capabilities": caps_of(d), "connectors": services_of(d)}
                             for name, d in self.drivers.items()]}

    def serve_forever(self) -> None:
        """收到消息就处理；连接断开时抛 DeviceError（由外层按退避重连）。"""
        assert self._ws is not None
        try:
            for raw in self._ws:
                msg = json.loads(raw)
                t = msg.get("type")
                if t == RUN_REQUEST:
                    self._start(msg)
                elif t == CANCEL:
                    run = self._runs.get(str(msg.get("run_id", "")))
                    if run is not None:
                        run.cancel.set()
                elif msg.get("type") == FILE_FETCH:
                    self._fetch(msg)
                elif "reply_to" in msg:      # 工具 / 审批 / 文件读取的回执
                    self._deliver(msg)
        except Exception as e:  # noqa: BLE001 —— 断线
            raise DeviceError(str(e)) from e

    def close(self) -> None:
        try:
            if self._ws is not None:
                self._ws.close()
        finally:
            self.bridge.close()

    # ---- 发送 ----
    def send(self, payload: dict[str, Any]) -> None:
        with self._send_lock:
            if self._ws is None:
                raise DeviceError("not connected")
            try:
                self._ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                raise DeviceError(str(e)) from e

    def next_call_id(self) -> str:
        return f"c{uuid.uuid4().hex[:12]}"

    def round_trip(self, payload: dict[str, Any], timeout_s: float = 3600) -> dict[str, Any]:
        cid = str(payload["call_id"])
        done = threading.Event()
        self._pending[cid] = done
        try:
            self.send(payload)
            if not done.wait(timeout_s):
                return {"error": "timeout"}
            return self._replies.pop(cid, {})
        finally:
            self._pending.pop(cid, None)

    def _fetch(self, msg: dict[str, Any]) -> None:
        """服务器要读工作目录里的一个文件（publish_file）。只允许工作目录内的相对路径。"""
        reply: dict[str, Any] = {"reply_to": msg.get("call_id"), "run_id": ""}
        try:
            root = self._root_for_fetch(msg)
            path = self._safe_path(root, str(msg.get("path", "")))
            for top in (AGENT_HOME, PROJECT_FILES, SKILLS, ".claude"):  # 私有目录不经过网络；下发的副本不能当成产物回传
                if (root / top).resolve() in (path, *path.parents):
                    raise DeviceError(f"不能发布 {top}/ 里的文件")
            reply["data"] = base64.b64encode(path.read_bytes()).decode()
        except Exception as e:  # noqa: BLE001 —— 越界 / 不存在都回错误
            reply["error"] = str(e)
        self.send(reply)

    def _root_for_fetch(self, msg: dict[str, Any]) -> Path:
        """file.fetch 只属于某一次运行：按 run_id 找它的工作目录，找不到就不允许。"""
        run = self._runs.get(str(msg.get("run_id", "")))
        if run is None or run.workdir is None:
            raise DeviceError("这次运行不在这台连接器上")
        return run.workdir

    @staticmethod
    def _safe_path(root: Path, rel: str) -> Path:
        if safe_rel(rel) is None:
            raise DeviceError(f"非法的路径：{rel!r}")
        p = (root / rel).resolve()
        if root.resolve() not in p.parents:
            raise DeviceError(f"非法的路径：{rel!r}")
        if not p.is_file():
            raise DeviceError(f"文件不存在：{rel}")
        return p

    def _deliver(self, msg: dict[str, Any]) -> None:
        cid = str(msg.get("reply_to", ""))
        self._replies[cid] = msg
        done = self._pending.get(cid)
        if done is not None:
            done.set()

    # ---- 一次运行 ----
    def _start(self, msg: dict[str, Any]) -> None:
        run_id = str(msg.get("run_id", ""))
        run = _Run(self, run_id)
        self._runs[run_id] = run
        self.bridge.register(run_id, run.call_tool, run.ask_approval)
        workdir = self.workdir_of(str(msg.get("workdir_ref", "")))
        self._write_materials(workdir, list(msg.get("materials") or []))
        spec = RunSpec(
            run_id=run_id, agent_id=str(msg.get("agent_id", "")), context_id=str(msg.get("context_id", "")),
            prompt=str(msg.get("prompt", "")), workdir=workdir,
            authority=list(msg.get("authority") or []), tool_names=list(msg.get("tool_names") or []),
            session_id=msg.get("session_id"), model=msg.get("model"),
            harness=str(msg["harness"]) if msg.get("harness") else None,
            timeout_s=_limit(msg.get("timeout_s")),
            connectors=[str(x) for x in msg["connectors"] if isinstance(x, str)]
            if isinstance(msg.get("connectors"), list) else None)
        run.workdir = workdir
        run.home = msg.get("home") if msg.get("home") in ("rw", "ro") else None
        threading.Thread(target=self._execute, args=(run, spec), daemon=True).start()

    @staticmethod
    def _write_materials(workdir: Path, materials: list[dict[str, Any]]) -> None:
        """每轮下发的只读材料：先清掉上一次的，再写。路径来自网络，仍然拒绝越界。"""
        for top in ("group", PROJECT_FILES, ATTACHMENTS, SKILLS, "materials"):  # group/：旧版下发的
            shutil.rmtree(workdir / top, ignore_errors=True)
        for item in materials:
            rel = str(item.get("path", ""))
            if safe_rel(rel) is None or Path(rel).parts[0] == AGENT_HOME:
                raise DeviceError(f"非法的材料路径：{rel!r}")
            dest = workdir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(base64.b64decode(item.get("data") or ""))

    def workdir_of(self, ref: str) -> Path:
        """`workdir_ref` 是本机工作目录的相对引用（`<agent>/<context>`），拼到用户配置的根目录下。
        必须拒绝绝对路径与 `..`：这个字符串来自网络，不能让它写到根目录以外。"""
        raw = ref.split("/")
        # 以 . 开头的段一律拒绝（`.` 本身除外）：私有目录在 <root>/.homes/ 下，工作目录不能落进去
        if not ref or any(p in ("..", "") or (p.startswith(".") and p != ".") or safe_rel(p) is None
                          or "\\" in p for p in raw):
            raise DeviceError(f"非法的工作目录引用：{ref!r}")
        parts = [p for p in raw if p != "."]
        if not parts:
            raise DeviceError(f"非法的工作目录引用：{ref!r}")
        return self.workroot.joinpath(*parts)

    def home_of(self, agent_id: str) -> Path:
        """Agent 的私有目录：留在本机，跨对话、跨任务保留，网络不读（docs/device-runtimes.md 第 4 节）。"""
        if not agent_id or agent_id.startswith(".") or any(c in agent_id for c in "/\\") or safe_rel(agent_id) is None:
            raise DeviceError(f"非法的 Agent id：{agent_id!r}")
        return self.workroot / HOMES / agent_id

    def _home_lock(self, agent_id: str) -> threading.Lock:
        with self._lock:
            return self._home_locks.setdefault(agent_id, threading.Lock())

    def _mount_home(self, run: _Run, spec: RunSpec) -> homedir.HomeMount | None:
        """按 run.request 的 home 挂载私有目录；不挂载时清掉上一次可能残留的副本。"""
        dest = spec.workdir / AGENT_HOME
        if run.home is None:
            shutil.rmtree(dest, ignore_errors=True)
            return None
        with self._home_lock(spec.agent_id):
            return homedir.mount(self.home_of(spec.agent_id), dest)

    def _unmount_home(self, run: _Run, spec: RunSpec, before: homedir.HomeMount | None) -> None:
        """rw：三方合并写回；ro：丢弃这次运行里的改动。"""
        dest = spec.workdir / AGENT_HOME
        with self._home_lock(spec.agent_id):
            if before is not None and run.home == "rw":
                homedir.sync_back(self.home_of(spec.agent_id), dest, before, spec.context_id)
            shutil.rmtree(dest, ignore_errors=True)

    def _execute(self, run: _Run, spec: RunSpec) -> None:
        before = None
        try:
            driver = self.drivers.get(spec.harness) if spec.harness else self.driver
            if driver is None:
                outcome = RunOutcome(ok=False, error=f"DRIVER 这台设备上没有 {spec.harness}"
                                                     f"（有 {'、'.join(self.drivers)}）")
            else:
                spec.workdir.mkdir(parents=True, exist_ok=True)
                before = self._mount_home(run, spec)
                outcome = driver.run(spec, run, run.cancel)
        except Exception as e:  # noqa: BLE001 —— 驱动异常等价于运行时失败
            outcome = RunOutcome(ok=False, error=f"runtime error: {e!r}")
        finally:
            try:
                self._unmount_home(run, spec, before)
            except Exception as e:  # noqa: BLE001 —— 写回失败不能吞掉这一轮的结果，只在本机提示
                print(f"私有目录写回失败（{spec.agent_id}）：{e}", file=sys.stderr, flush=True)
            self.bridge.unregister(run.run_id)
            self._runs.pop(run.run_id, None)
        self.send({"type": RUN_RESULT, "run_id": run.run_id, "session_id": outcome.session_id,
                   "final_text": outcome.final_text, "tool_calls": outcome.tool_calls,
                   "usage": outcome.usage, "ok": outcome.ok, "error": outcome.error})


def _limit(v: Any) -> float | None:
    """run.request 里的超时：正数；没有 / 不合法 = 不限制（旧服务端不发这个字段）。"""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None
