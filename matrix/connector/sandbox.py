"""云侧沙箱客户端（docs/cloud-runtime.md 第 4、5 节，阶段 1a）：沙箱里的 1 号进程，复用端侧连接器的代码与协议。

和端侧连接器（`client.Connector`）的差别只在这一层：

- 一个沙箱只服务一个执行单元：工作目录是固定的那一个（挂在原来的绝对路径上），材料缓存放在这个单元的 dsh 状态目录里；
- 私有目录由网络持有（run.request.home 为 net-rw / net-ro）：可挂的部分作为材料下发到 agent-home/，轮次结束只报改过、删过的文件；
- 每轮结束交检查点：按 workdir.outputs 的口径报产物清单（有变化的才交内容）、当前 dsh 会话打成包交上去；
- 沙箱重建后第一轮按 run.request.checkpoint 恢复产物与会话；
- 云端连接器经这条连接（connector.call）由网络代调用；DELEGATION.json、shared/、deliveries/ 也是材料。

身份：启动时从 stdin 读一次性启动令牌，连上后服务器在握手里给执行单元凭证；断线后凭它重连几次，连不上就退出
（客户端退出就是沙箱退出，控制面按“沙箱没了”处理）。

    python -m matrix.connector.sandbox --url ws://127.0.0.1:8765/api/sandbox/connect \\
        --workdir <工作目录的绝对路径> --state <状态目录> [--dsh-bin dsh]   # 启动令牌从 stdin 读一行
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .. import workdir as wd
from ..checkpoints import CHECKPOINT_LIMIT, MANIFEST_MAX, SHA_RE, manifest_id, pack_session, unpack_session
from ..device import BLOB_PUT, FEATURE_MATERIALS_CACHE, FEATURE_SANDBOX, DeviceError
from ..homedir import fingerprint
from ..models import AGENT_HOME
from ..runtime import dsh_session_files
from .bridge import LocalBridge
from .client import Connector, _Run
from .drivers.base import Driver, RunOutcome, RunSpec

CHUNK = 1 << 20
MARKER = ".matrix-checkpoint"     # 状态目录里：这个沙箱手上的产物是哪一份检查点
NET_HOME = ("net-rw", "net-ro")
HOME_FILE_MAX = 100_000_000       # 私有目录里单个文件最多交多少字节（服务端再校验一次）


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


class SandboxConnector(Connector):
    HOME_MODES = ("rw", "ro", *NET_HOME)
    CLEARED = (*Connector.CLEARED, AGENT_HOME, wd.DELIVERIES, "shared", "DELEGATION.json")

    def __init__(self, drivers: Driver | list[Driver], workdir: Path, state: Path, bridge: LocalBridge | None = None):
        super().__init__(drivers, state, bridge=bridge)
        self.unit_workdir = Path(workdir)
        self.state = Path(state)
        self._shas: dict[tuple[str, int, int], str] = {}

    def hello_message(self) -> dict[str, Any]:
        msg = super().hello_message()
        msg["features"] = [FEATURE_MATERIALS_CACHE, FEATURE_SANDBOX]
        return msg

    # ---- 一个沙箱一个执行单元 ----
    def workdir_of(self, ref: str) -> Path:
        return self.unit_workdir

    @property
    def cache_dir(self) -> Path:
        return self.state / "cache" / "materials"

    def _root_for_fetch(self, msg: dict[str, Any]) -> Path:
        return self.unit_workdir   # 沙箱里只有这一个执行单元：人浏览检查点之外的大文件时也能取

    def _material_path(self, workdir: Path, rel: str) -> Path:
        """材料路径来自网络：只认工作目录里的相对路径；私有目录（网络持有时）也是材料。"""
        if safe(rel) is None:
            raise DeviceError(f"非法的材料路径：{rel!r}")
        return workdir / rel

    def _fetch(self, msg: dict[str, Any]) -> None:
        """服务器读工作目录里的文件；dir=zip 时目录打成 zip。私有目录与下发的只读副本不能当产物回传。"""
        reply: dict[str, Any] = {"reply_to": msg.get("call_id"), "run_id": ""}
        try:
            root = self.unit_workdir.resolve()
            rel = str(msg.get("path", ""))
            if safe(rel) is None:
                raise DeviceError(f"非法的路径：{rel!r}")
            p = (root / rel).resolve()
            if root not in p.parents:
                raise DeviceError(f"非法的路径：{rel!r}")
            for top in (AGENT_HOME, "project", "skills", ".claude"):
                if (root / top).resolve() in (p, *p.parents):
                    raise DeviceError(f"不能发布 {top}/ 里的文件")
            if p.is_dir() and msg.get("dir") == "zip":
                reply["data"], reply["zip"] = base64.b64encode(wd.pack(p)).decode(), True
            elif p.is_file():
                reply["data"] = base64.b64encode(p.read_bytes()).decode()
            else:
                raise DeviceError(f"文件不存在：{rel}")
        except Exception as e:  # noqa: BLE001
            reply["error"] = str(e)
        self.send(reply)

    def _spec_extra(self, msg: dict[str, Any]) -> dict[str, Any]:
        return {"cloud": {k: msg[k] for k in ("llm_provider", "env", "fetch", "cloud_connectors") if k in msg}}

    def _bridge_extra(self, run: _Run) -> tuple:
        return ((run.list_connectors, run.call_connector),) if run.msg.get("cloud_connectors") else ()

    # ---- 私有目录：网络持有，材料进、改动出 ----
    def _mount_home(self, run: _Run, spec: RunSpec):
        if run.home in NET_HOME:
            d = spec.workdir / AGENT_HOME
            d.mkdir(parents=True, exist_ok=True)
            return fingerprint(d)   # 材料已经放好：记下这一刻的样子，轮次结束比对
        return super()._mount_home(run, spec)

    def _unmount_home(self, run: _Run, spec: RunSpec, before) -> None:
        if run.home not in NET_HOME:
            return super()._unmount_home(run, spec, before)
        d = spec.workdir / AGENT_HOME
        try:
            if run.home == "net-rw" and isinstance(before, dict) and d.is_dir():
                after = fingerprint(d)   # 不跟随符号链接
                written = []
                for rel, fp in sorted(after.items()):
                    if before.get(rel) == fp:
                        continue
                    p = d / rel
                    if p.stat().st_size > HOME_FILE_MAX:
                        continue
                    sha, size = self._upload(run, p)
                    written.append({"path": rel, "sha": sha, "size": size})
                deleted = sorted(set(before) - set(after))
                run.extra["home_changes"] = {"written": written, "deleted": deleted}
        finally:
            shutil.rmtree(d, ignore_errors=True)

    # ---- 检查点 ----
    def _upload(self, run: _Run, path: Path) -> tuple[str, int]:
        """把一个文件分块交给服务器（blob.put）。返回（sha, 大小）；服务器拒收时抛 DeviceError。"""
        sha, size = _sha(path), path.stat().st_size
        off = 0
        with path.open("rb") as f:
            while True:
                chunk = f.read(CHUNK)
                reply = self.round_trip({"type": BLOB_PUT, "run_id": run.run_id, "call_id": self.next_call_id(),
                                         "sha": sha, "offset": off, "total": size,
                                         "data": base64.b64encode(chunk).decode()}, timeout_s=300)
                if reply.get("error") or not reply.get("ok"):
                    raise DeviceError(f"服务器没收下：{reply.get('error') or '没有回执'}")
                off += len(chunk)
                if off >= size:
                    return sha, size

    def _sha_cached(self, p: Path) -> str:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        sha = self._shas.get(key)
        if sha is None:
            if len(self._shas) > 50_000:
                self._shas.clear()
            sha = self._shas[key] = _sha(p)
        return sha

    def _prepare(self, run: _Run, spec: RunSpec) -> None:
        """沙箱是新建的（手上的检查点和网络记的不一样）：从检查点恢复产物；会话不在就从会话包恢复。"""
        ck = run.msg.get("checkpoint")
        if not isinstance(ck, dict):
            return
        marker = self.state / MARKER
        have = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
        if ck.get("id") and have != ck.get("id"):
            for f in ck.get("files") or []:
                rel, sha, size = str(f.get("path", "")), str(f.get("sha", "")), f.get("size")
                if safe(rel) is None or not SHA_RE.fullmatch(sha) or not isinstance(size, int):
                    continue
                dest = spec.workdir / rel
                if dest.is_file() and not dest.is_symlink() and self._sha_cached(dest) == sha:
                    continue
                self._get(run, sha, size)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.cache_dir / sha, dest)
            marker.write_text(str(ck["id"]), encoding="utf-8")
        sess, sid = ck.get("session"), spec.session_id
        if isinstance(sess, dict) and sid and sess.get("session_id") == sid \
                and dsh_session_files(self.state, spec.workdir, sid)[0] is None \
                and SHA_RE.fullmatch(str(sess.get("sha", ""))) and isinstance(sess.get("size"), int):
            self._get(run, sess["sha"], sess["size"])
            unpack_session((self.cache_dir / sess["sha"]).read_bytes(), self.state)

    def _get(self, run: _Run, sha: str, size: int) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        src = self.cache_dir / sha
        if not (src.is_file() and src.stat().st_size == size):
            self._fetch_material(run, sha, size)

    def _finish(self, run: _Run, spec: RunSpec, outcome: RunOutcome | None) -> None:
        """交检查点：产物清单（内容有变化的才交）、当前会话的包。"""
        ck = run.msg.get("checkpoint")
        if not isinstance(ck, dict):
            return
        prev = {str(f.get("path")): str(f.get("sha")) for f in ck.get("files") or [] if isinstance(f, dict)}
        limit = ck.get("limit") if isinstance(ck.get("limit"), int) else CHECKPOINT_LIMIT
        files, total = [], 0
        for rel in wd.outputs(spec.workdir)[:MANIFEST_MAX]:
            p = spec.workdir / rel
            size, sha = p.stat().st_size, self._sha_cached(p)
            stored = total + size <= limit
            if stored and prev.get(rel.as_posix()) != sha:
                try:
                    self._upload(run, p)
                except DeviceError:
                    stored = False
            if stored:
                total += size
            files.append({"path": rel.as_posix(), "sha": sha, "size": size, "stored": stored})
        run.extra["outputs"] = files
        (self.state / MARKER).write_text(manifest_id(files), encoding="utf-8")
        sid = outcome.session_id if outcome is not None else ""
        if sid:
            data = pack_session(self.state, spec.workdir, sid)
            if data is not None:
                tmp = self.state / f".session-{uuid.uuid4().hex[:8]}.tgz"
                try:
                    tmp.write_bytes(data)
                    sha, size = self._upload(run, tmp)
                    run.extra["session_pack"] = {"sha": sha, "size": size, "session_id": sid}
                finally:
                    tmp.unlink(missing_ok=True)


def safe(rel: str) -> Path | None:
    """来自网络的相对路径：不能越界（缓存与状态目录不在工作目录里，以 . 开头的名字没有危险）。"""
    if wd.safe_rel(rel) is None or "\\" in rel or any(p in ("", ".", "..") for p in rel.split("/")):
        return None
    return Path(rel)


def main(argv: list[str] | None = None) -> None:
    from .. import utf8_stdio
    from .drivers.dsh import DshDriver
    utf8_stdio()
    p = argparse.ArgumentParser(prog="matrix-sandbox-client", description="云侧沙箱客户端（由控制面起，不要手动运行）")
    p.add_argument("--url", required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--dsh-bin", default="dsh")
    args = p.parse_args(argv)
    # 一次性启动令牌：local 后端经 stdin 给；docker 后端经环境变量 MATRIX_BOOT_TOKEN 给（读完就从环境里拿掉，用过即废）
    token = os.environ.pop("MATRIX_BOOT_TOKEN", "") or sys.stdin.readline().strip()
    os.environ["HOME"] = args.state
    bridge = LocalBridge()
    driver = DshDriver(args.dsh_bin, bridge.url, state_dir=Path(args.state))
    credential, attempt = "", 0
    try:
        while True:
            conn = SandboxConnector([driver], Path(args.workdir), Path(args.state), bridge=bridge)
            try:
                if credential:
                    conn.connect(f"{args.url}?credential={credential}", "")
                else:
                    conn.connect(args.url, token)
                    credential = str(conn.server_hello.get("credential") or "")
                attempt = 0
                print(f"沙箱客户端已连上：{conn.server_hello.get('unit')}", flush=True)
                conn.serve_forever()
            except DeviceError as e:
                attempt += 1
                if not credential or attempt > 3:   # 连不回去：退出就是沙箱退出
                    print(f"沙箱客户端退出：{e}", file=sys.stderr, flush=True)
                    return
                time.sleep(2 * attempt)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
