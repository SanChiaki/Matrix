"""连接器主循环：出站连服务器，收 `run.request`，分派给本机驱动。

一条连接可以承载多个运行（席位 / 任务并行）；每个运行一个线程。工具与审批请求经本机桥进来，
这里把它们转成 `tool.call` / `approval.request` 并等回执（按 `call_id` 配对）。
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import os
import re
import shutil
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

import base64

from .. import homedir
from ..workdir import safe_rel
from ..device import (APPROVAL_REQUEST, FEATURE_MATERIALS_CACHE, MATERIAL_FETCH, QUESTION_REQUEST, CANCEL, FILE_FETCH, HELLO, HELLO_OK, PROTOCOL, RUN_EVENT, RUN_REQUEST,
                      RUN_RESULT, TOOL_CALL, DeviceError)
from ..models import AGENT_HOME, ATTACHMENTS, PLUGIN_MOUNT, PROJECT_FILES, RELEASES, SKILLS
from .bridge import LocalBridge
from .drivers.base import Driver, RunOutcome, RunSpec

HOMES = ".homes"   # 私有目录在根目录下的位置：<root>/.homes/<agent>/。工作目录引用不能以 . 开头，碰不到这里


SHA_RE = re.compile(r"[0-9a-f]{64}")
MANIFEST_MAX = 5000                  # 一轮最多多少份材料
MATERIAL_MAX = 500_000_000           # 单份材料最大多少字节（服务端的附件上限是 20 MB，这里只防异常值）
CACHE_LIMIT = 2_000_000_000          # 本机材料缓存的上限（字节），超了按最近使用时间清最旧的


class MaterialsError(Exception):
    pass


class _Run:
    """一次运行在本机的上下文：绑定桥令牌、回执等待表与取消标记。"""

    def __init__(self, connector: "Connector", run_id: str):
        self.connector = connector
        self.run_id = run_id
        self.cancel = threading.Event()
        self.workdir: Path | None = None
        self.home: str | None = None      # run.request 的 home："rw" | "ro" | None
        self.manifest: list[dict[str, Any]] | None = None  # run.request 的 manifest：材料清单，运行前按它从本机缓存补齐

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

    def ask_question(self, questions: list[dict[str, Any]]) -> dict[str, Any]:
        reply = self.connector.round_trip({"type": QUESTION_REQUEST, "run_id": self.run_id,
                                           "call_id": self.connector.next_call_id(), "questions": questions})
        if isinstance(reply.get("answers"), list):
            return {"answers": reply["answers"]}
        return {"error": str(reply.get("error") or "没有得到回答")}


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
                    "approval": c.approval, "sandbox": c.sandbox, "skills": c.skills,
                    "questions": c.questions, "goals": c.goals}

        def services_of(d: Driver) -> list[str]:
            """这个 AI 工具里用户配置的服务：只报名字（电脑上的连接器），配置与凭证留在本机。"""
            get = getattr(d, "services", None)
            try:
                return list(get()) if callable(get) else []
            except Exception:  # noqa: BLE001 —— 读不出来就当没有，不影响上线
                return []
        return {"type": HELLO_OK, "protocol": PROTOCOL, "runtime": self.driver.name,
                "features": [FEATURE_MATERIALS_CACHE],
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
        self.bridge.register(run_id, run.call_tool, run.ask_approval, run.ask_question)
        workdir = self.workdir_of(str(msg.get("workdir_ref", "")))
        if isinstance(msg.get("manifest"), list):  # 新服务端：只给清单，运行线程里按缓存补齐（要等回执，不能占着接收循环）
            run.manifest = list(msg["manifest"])
        else:
            self._write_materials(workdir, list(msg.get("materials") or []))
        spec = RunSpec(
            run_id=run_id, agent_id=str(msg.get("agent_id", "")), context_id=str(msg.get("context_id", "")),
            prompt=str(msg.get("prompt", "")), workdir=workdir,
            authority=list(msg.get("authority") or []), tool_names=list(msg.get("tool_names") or []),
            session_id=msg.get("session_id"), model=msg.get("model"),
            harness=str(msg["harness"]) if msg.get("harness") else None,
            timeout_s=_limit(msg.get("timeout_s")),
            connectors=[str(x) for x in msg["connectors"] if isinstance(x, str)]
            if isinstance(msg.get("connectors"), list) else None,
            questions=msg.get("questions") is True)
        run.workdir = workdir
        run.home = msg.get("home") if msg.get("home") in ("rw", "ro") else None
        threading.Thread(target=self._execute, args=(run, spec), daemon=True).start()

    @staticmethod
    def _clear_materials(workdir: Path) -> None:
        """上一轮下发的只读副本先清掉（改过的也不保留）。"""
        for top in ("group", PROJECT_FILES, ATTACHMENTS, SKILLS, RELEASES, PLUGIN_MOUNT, "materials"):  # group/：旧版下发的
            shutil.rmtree(workdir / top, ignore_errors=True)

    @staticmethod
    def _material_path(workdir: Path, rel: str) -> Path:
        """材料路径来自网络：只认工作目录里的相对路径，私有目录除外。"""
        if safe_rel(rel) is None or Path(rel).parts[0] == AGENT_HOME:
            raise DeviceError(f"非法的材料路径：{rel!r}")
        return workdir / rel

    # ---- 材料：清单 + 本机缓存（只传缺的） ----
    # 服务端每轮只给清单 {path, sha, size}；内容按 sha256 存在 <工作根目录>/.cache/materials/<sha>，
    # 没有的才用 material.fetch 分块取，取完校验 sha 再放进缓存。没变的档案文件、附件第二轮起一个字节都不传。
    # 工作目录引用不能以 . 开头（workdir_of），所以缓存不会落进任何工作目录。
    @property
    def cache_dir(self) -> Path:
        return self.workroot / ".cache" / "materials"

    def _sync_materials(self, run: _Run, workdir: Path, manifest: list[dict[str, Any]]) -> dict[str, int]:
        """按清单把材料放进工作目录：缓存里有的直接拷，没有的取。返回 {fetched, cached, bytes}（取了几份、几份命中、传了多少字节）。"""
        if len(manifest) > MANIFEST_MAX:
            raise DeviceError(f"材料清单太长：{len(manifest)} 份")
        items: list[tuple[Path, str, int]] = []
        for it in manifest:
            sha, size = str((it or {}).get("sha", "")), (it or {}).get("size")
            if not SHA_RE.fullmatch(sha) or not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MATERIAL_MAX:
                raise DeviceError(f"非法的材料：{it!r}"[:200])
            items.append((self._material_path(workdir, str(it.get("path", ""))), sha, size))
        self._clear_materials(workdir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        stats = {"fetched": 0, "cached": 0, "bytes": 0}
        for dest, sha, size in items:
            src = self.cache_dir / sha
            if src.is_file() and src.stat().st_size == size:
                stats["cached"] += 1
                os.utime(src)  # 最近用过：清理缓存时最后才清它
            else:
                stats["bytes"] += self._fetch_material(run, sha, size)
                stats["fetched"] += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)  # 拷贝而不是硬链接：Agent 改了工作目录里的副本，缓存不跟着坏
        self._prune_cache(keep={sha for _, sha, _ in items})
        return stats

    def _fetch_material(self, run: _Run, sha: str, size: int) -> int:
        """向服务器分块取一份材料，校验 sha256 后放进缓存。返回传了多少字节。"""
        tmp = self.cache_dir / f".{sha}.{uuid.uuid4().hex[:8]}"
        h, off = hashlib.sha256(), 0
        try:
            with tmp.open("wb") as f:
                while True:
                    reply = self.round_trip({"type": MATERIAL_FETCH, "run_id": run.run_id, "call_id": self.next_call_id(),
                                             "sha": sha, "offset": off}, timeout_s=120)
                    if reply.get("error"):
                        raise DeviceError(f"取材料失败：{reply['error']}")
                    chunk = base64.b64decode(reply.get("data") or "")
                    f.write(chunk)
                    h.update(chunk)
                    off += len(chunk)
                    if not chunk or off >= int(reply.get("total", 0)):
                        break
            if off != size or h.hexdigest() != sha:
                raise DeviceError("取到的材料和清单对不上（大小或 sha256 不一致）")
            os.replace(tmp, self.cache_dir / sha)
        finally:
            tmp.unlink(missing_ok=True)
        return off

    def _prune_cache(self, keep: set[str]) -> None:
        """缓存超过上限时按最近使用时间清掉最旧的（这一轮刚用的不清）。"""
        try:
            files = [(f.stat().st_mtime, f.stat().st_size, f) for f in self.cache_dir.iterdir()
                     if f.is_file() and SHA_RE.fullmatch(f.name)]
        except OSError:
            return
        total = sum(size for _, size, _ in files)
        for _, size, f in sorted(files, key=lambda x: x[0]):
            if total <= CACHE_LIMIT:
                break
            if f.name in keep:
                continue
            f.unlink(missing_ok=True)
            total -= size

    @staticmethod
    def _write_materials(workdir: Path, materials: list[dict[str, Any]]) -> None:
        """旧服务端：每轮整份下发的只读材料。先清掉上一次的，再写。路径来自网络，仍然拒绝越界。"""
        Connector._clear_materials(workdir)
        for item in materials:
            rel = str(item.get("path", ""))
            Connector._material_path(workdir, rel)
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
                if run.manifest is not None:
                    try:
                        self._sync_materials(run, spec.workdir, run.manifest)
                    except Exception as e:  # noqa: BLE001 —— 材料没齐：不跑这一轮（Agent 会以为档案就是空的）
                        raise MaterialsError(str(e)) from e
                before = self._mount_home(run, spec)
                outcome = driver.run(spec, run, run.cancel)
        except MaterialsError as e:  # 连接断了或内容对不上：按传输错误处理，任务回到等待、上线后重来
            outcome = RunOutcome(ok=False, error=f"TRANSPORT 材料没取到：{e}")
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
