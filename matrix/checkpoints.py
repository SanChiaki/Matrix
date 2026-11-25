"""云侧执行面的内容存储与检查点（docs/cloud-runtime.md 5.2、10.2；阶段 1a）。

- `ContentStore`：按内容寻址的本机目录 `<root>/<sha 前两位>/<sha>`。接口只有“有没有、在哪、放进去、分块收、清理”，
  以后换成对象存储（docs/control-plane.md 4.5）只换这一个类。
- `Uploads`：沙箱客户端经 `blob.put` 分块交上来的内容。内容来自不可信的执行面：每份有大小上限、每轮有总量上限，
  收齐后按 sha256 校验才放进内容存储，对不上就丢掉。
- `CheckpointStore`：每个执行单元（席位 `seat:<对话>/<Agent>`、委托 `task:<任务>`）的最新检查点：产物清单（路径、sha、大小、
  内容存没存）与 dsh 会话包（不透明的块，只存最新两份）。卷只是缓存，检查点才是准。
- `pack_session` / `unpack_session`：一个 dsh 会话的两处文件打成包、在另一个 HOME 里解开（控制面迁旧会话、沙箱客户端都用）。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import workdir as wd

SHA_RE = re.compile(r"[0-9a-f]{64}")
CHECKPOINT_LIMIT = 1_000_000_000      # 一个执行单元的检查点里最多存多少字节的产物，超出的只记清单
BLOB_MAX = 1_100_000_000              # 沙箱交上来的单份内容最大多少字节
RUN_UPLOAD_MAX = 2_500_000_000        # 一轮最多交上来多少字节（产物 + 私有目录改动 + 会话包）
SESSION_MAX = 200_000_000             # 会话包最大多少字节
MANIFEST_MAX = 20_000                 # 产物清单最多多少项
SESSIONS_KEPT = 2                     # 会话包只存最新两份


def unit_rel(unit: str) -> Path:
    """执行单元 → 相对路径：seat:c/a → seats/c/a，task:t → tasks/t。id 只能是普通的名字，不能带 .. 或绝对路径。"""
    kind, _, ref = unit.partition(":")
    if kind not in ("seat", "task") or not ref or wd.safe_rel(ref) is None or any(
            p in ("", ".", "..") or p.startswith(".") for p in ref.split("/")):
        raise ValueError(f"非法的执行单元：{unit!r}")
    return Path("seats" if kind == "seat" else "tasks", *ref.split("/"))


def sha_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ContentStore:
    """按内容寻址的本机目录。"""

    def __init__(self, root: Path):
        self.root = root

    def path(self, sha: str) -> Path:
        if not SHA_RE.fullmatch(sha):
            raise ValueError(f"非法的 sha：{sha!r}")
        return self.root / sha[:2] / sha

    def has(self, sha: str, size: int | None = None) -> bool:
        try:
            p = self.path(sha)
        except ValueError:
            return False
        return p.is_file() and (size is None or p.stat().st_size == size)

    def _place(self, tmp: Path, sha: str) -> None:
        dest = self.path(sha)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_file():
            tmp.unlink(missing_ok=True)
        else:
            os.replace(tmp, dest)

    def put_bytes(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        if not self.has(sha):
            tmp = self.root / ".incoming" / f"{uuid.uuid4().hex}"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            self._place(tmp, sha)
        return sha

    def put_file(self, src: Path) -> tuple[str, int]:
        """拷一份文件进来（不移动原文件）。返回（sha, 大小）。"""
        tmp = self.root / ".incoming" / f"{uuid.uuid4().hex}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        h, n = hashlib.sha256(), 0
        with src.open("rb") as f, tmp.open("wb") as out:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
                out.write(chunk)
                n += len(chunk)
        sha = h.hexdigest()
        self._place(tmp, sha)
        return sha, n

    def gc(self, keep: set[str], older_than_s: float = 3600) -> int:
        """删掉没人引用、而且放进来超过 older_than_s 秒的内容（刚交上来、还没记进检查点的不删）。返回删了几份。"""
        n, cutoff = 0, time.time() - older_than_s
        for d in self.root.iterdir() if self.root.is_dir() else []:
            if not d.is_dir() or d.name.startswith("."):
                continue
            for f in d.iterdir():
                if f.name not in keep and SHA_RE.fullmatch(f.name) and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    n += 1
        inc = self.root / ".incoming"
        for f in inc.iterdir() if inc.is_dir() else []:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        return n


class Uploads:
    """沙箱客户端分块交上来的内容（`blob.put`）。按“一条连接上的一轮”记账，轮次结束丢掉没收齐的。"""

    def __init__(self, store: ContentStore):
        self.store = store
        self._lock = threading.Lock()
        self._runs: dict[str, int] = {}                    # 轮次键 → 这一轮已经收了多少字节
        self._parts: dict[tuple[str, str], tuple[Path, int, Any]] = {}  # (轮次键, sha) → (临时文件, 已收字节, sha 计算器)

    def put(self, key: str, sha: str, offset: Any, data: bytes, total: Any) -> str | None:
        """收一块；返回错误说明，None 表示收下了。收齐时校验 sha256 并放进内容存储。"""
        if not SHA_RE.fullmatch(sha) or not isinstance(total, int) or isinstance(total, bool) or not 0 <= total <= BLOB_MAX:
            return "非法的内容（sha 或大小）"
        if not isinstance(offset, int) or isinstance(offset, bool):
            return "非法的偏移"
        with self._lock:
            used = self._runs.get(key, 0) + len(data)
            if used > RUN_UPLOAD_MAX:
                return "这一轮交上来的内容超过上限"
            self._runs[key] = used
            if total == 0:
                if data or hashlib.sha256(b"").hexdigest() != sha:
                    return "内容和 sha256 对不上"
                self.store.put_bytes(b"")
                return None
            cur = self._parts.get((key, sha))
            if cur is None:
                if offset != 0:
                    return "偏移不对"
                tmp = self.store.root / ".incoming" / f"{uuid.uuid4().hex}"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                cur = (tmp, 0, hashlib.sha256())
            tmp, got, h = cur
            if offset != got or got + len(data) > total:
                return "偏移不对或超过声明的大小"
            with tmp.open("ab") as f:
                f.write(data)
            h.update(data)
            got += len(data)
            if got < total:
                self._parts[(key, sha)] = (tmp, got, h)
                return None
            self._parts.pop((key, sha), None)
        if h.hexdigest() != sha:
            tmp.unlink(missing_ok=True)
            return "内容和 sha256 对不上"
        self.store._place(tmp, sha)
        return None

    def end(self, key: str) -> None:
        """一轮结束：丢掉没收齐的。"""
        with self._lock:
            self._runs.pop(key, None)
            for k in [k for k in self._parts if k[0] == key]:
                self._parts.pop(k)[0].unlink(missing_ok=True)


@dataclass
class Checkpoint:
    id: str = ""
    at: float = 0.0
    files: list[dict[str, Any]] = field(default_factory=list)     # [{path, sha, size, stored}]
    sessions: list[dict[str, Any]] = field(default_factory=list)  # 最新的在前：[{sha, size, session_id, at}]
    skipped: list[str] = field(default_factory=list)              # 导入旧工作目录时超过上限、只记了清单的

    def stored(self) -> list[dict[str, Any]]:
        return [f for f in self.files if f.get("stored")]

    def session(self, session_id: str | None = None) -> dict[str, Any] | None:
        for s in self.sessions:
            if session_id is None or s.get("session_id") == session_id:
                return s
        return None


def manifest_id(files: list[dict[str, Any]]) -> str:
    """检查点 id：产物（路径、sha）的指纹。沙箱客户端按同样的算法记下自己手上是哪一份。"""
    raw = json.dumps(sorted([f["path"], f["sha"]] for f in files), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def clean_manifest(items: Any) -> list[dict[str, Any]]:
    """沙箱报来的产物清单：只认合法的相对路径、sha、大小；不能落进网络放进去的只读副本与私有目录。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for it in items if isinstance(items, list) else []:
        if len(out) >= MANIFEST_MAX:
            break
        if not isinstance(it, dict):
            continue
        path, sha, size = str(it.get("path", "")), str(it.get("sha", "")), it.get("size")
        if not path or wd.safe_rel(path) is None or "\\" in path or path in seen or not SHA_RE.fullmatch(sha) \
                or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            continue
        if Path(path).parts[0] in wd.TOP_SKIP or path in wd.RUNTIME_FILES:
            continue
        seen.add(path)
        out.append({"path": Path(path).as_posix(), "sha": sha, "size": size, "stored": bool(it.get("stored"))})
    return out


class CheckpointStore:
    """`<root>/<seats|tasks>/<id>/checkpoint.json`，内容在 `ContentStore` 里。"""

    def __init__(self, root: Path, content: ContentStore, limit: int = CHECKPOINT_LIMIT):
        self.root = root
        self.content = content
        self.limit = limit
        self._lock = threading.Lock()

    def _file(self, unit: str) -> Path:
        return self.root / unit_rel(unit) / "checkpoint.json"

    def load(self, unit: str) -> Checkpoint | None:
        p = self._file(unit)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return Checkpoint(**{k: raw[k] for k in Checkpoint.__dataclass_fields__ if k in raw})

    def _save(self, unit: str, ck: Checkpoint) -> None:
        p = self._file(unit)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(ck), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)

    def save_outputs(self, unit: str, items: Any) -> Checkpoint:
        """记下一轮结束时的产物清单。说“存了”的内容必须真在内容存储里、而且总量不超过上限，否则改记成只有清单。"""
        files, total = clean_manifest(items), 0
        for f in files:
            if f["stored"] and self.content.has(f["sha"], f["size"]) and total + f["size"] <= self.limit:
                total += f["size"]
            else:
                f["stored"] = False
        with self._lock:
            ck = self.load(unit) or Checkpoint()
            ck.files, ck.id, ck.at = files, manifest_id(files), time.time()
            self._save(unit, ck)
        return ck

    def add_session(self, unit: str, sha: str, size: int, session_id: str) -> None:
        """会话包：不透明的块，只存最新两份。"""
        if not self.content.has(sha, size) or size > SESSION_MAX:
            return
        with self._lock:
            ck = self.load(unit) or Checkpoint()
            ck.sessions = [{"sha": sha, "size": size, "session_id": session_id, "at": time.time()}] + \
                [s for s in ck.sessions if s.get("sha") != sha][:SESSIONS_KEPT - 1]
            self._save(unit, ck)

    def import_dir(self, unit: str, src: Path) -> Checkpoint:
        """从旧的工作目录生成第一份检查点（按产物口径；原目录不动）。超过上限的只记清单，列进 skipped。"""
        files, total, skipped = [], 0, []
        for rel in wd.outputs(src)[:MANIFEST_MAX]:
            p = src / rel
            size = p.stat().st_size
            if total + size <= self.limit:
                sha, size = self.content.put_file(p)
                total += size
                files.append({"path": rel.as_posix(), "sha": sha, "size": size, "stored": True})
            else:
                files.append({"path": rel.as_posix(), "sha": sha_of(p), "size": size, "stored": False})
                skipped.append(rel.as_posix())
        with self._lock:
            ck = self.load(unit) or Checkpoint()
            ck.files, ck.id, ck.at, ck.skipped = files, manifest_id(files), time.time(), skipped
            self._save(unit, ck)
        return ck

    def remove(self, unit: str) -> None:
        shutil.rmtree(self._file(unit).parent, ignore_errors=True)

    def refs(self) -> set[str]:
        """全部检查点引用的内容（清理内容存储时保留）。"""
        out: set[str] = set()
        for p in self.root.rglob("checkpoint.json") if self.root.is_dir() else []:
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out |= {f.get("sha", "") for f in raw.get("files", []) if f.get("stored")}
            out |= {s.get("sha", "") for s in raw.get("sessions", [])}
        return out


# ---------------- dsh 会话包 ----------------
SESSION_PREFIXES = (".dsh/sessions/", ".dsh/storages/session_projcache/sessions/")


def pack_session(home: Path, cwd: Path, session_id: str) -> bytes | None:
    """一个 dsh 会话的两处文件（会话目录去掉 session.lock；索引）打成 tar.gz，路径相对 HOME。找不到会话返回 None。"""
    from .runtime import dsh_session_files
    d, index = dsh_session_files(home, cwd, session_id)
    if d is None:
        return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for f in sorted(d.rglob("*")):
            if f.is_file() and not f.is_symlink() and f.name != "session.lock":
                t.add(f, arcname=f.relative_to(home).as_posix(), recursive=False)
        if index.is_file():
            t.add(index, arcname=index.relative_to(home).as_posix(), recursive=False)
    return buf.getvalue()


def unpack_session(data: bytes, home: Path) -> bool:
    """在 home 里解开会话包。只认两处会话文件里的普通文件，别的（链接、越界路径）一律跳过。"""
    ok = False
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        for m in t.getmembers():
            name = m.name
            if not m.isfile() or wd.safe_rel(name) is None or not name.startswith(SESSION_PREFIXES) \
                    or any(p in ("", ".", "..") for p in name.split("/")):
                continue
            dest = home / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = t.extractfile(m)
            if src is None:
                continue
            dest.write_bytes(src.read())
            ok = True
    return ok
