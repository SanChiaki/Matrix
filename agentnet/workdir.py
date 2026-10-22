"""工作目录里的“产物”：执行者自己写出来的文件，不含网络与运行时放进去的东西。

委托交付时把执行者的产物快照放进委托方工作目录的 deliveries/<task_id>/（委托产物只给直接委托方，不变量 7、9）；
人（Agent 的所有者）也按同一口径浏览、下载席位与任务的工作目录。只用标准库。"""
from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from .models import AGENT_HOME, PROJECT_FILES

DELIVERIES = "deliveries"  # 委托产物在委托方工作目录里的位置：deliveries/<task_id>/
# 网络、运行时写进工作目录的文件：不是执行者的产物
RUNTIME_FILES = {".dsh-authority.yml", "dsh-events.jsonl", "DELEGATION.json"}
# 顶层：只读副本、私有目录、共享知识、别人交来的产物、临时文件
TOP_SKIP = {PROJECT_FILES, AGENT_HOME, "shared", DELIVERIES, "tmp"}
# 任何一层：缓存与依赖目录
ANY_SKIP = {"__pycache__", ".venv", "venv", "node_modules", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
MAX_FILES = 500
MAX_BYTES = 5_000_000


def outputs(root: Path, top: bool = True) -> list[Path]:
    """root 下的产物（相对路径，排好序）。不跟随符号链接。"""
    out: list[Path] = []
    if not root.is_dir():
        return out

    def walk(d: Path, rel: Path) -> None:
        for p in sorted(d.iterdir(), key=lambda x: x.name):
            if p.is_symlink():
                continue
            if p.is_dir():
                if p.name in ANY_SKIP or (top and rel == Path() and p.name in TOP_SKIP):
                    continue
                walk(p, rel / p.name)
            elif p.is_file() and not (rel == Path() and p.name in RUNTIME_FILES):
                out.append(rel / p.name)
    walk(root, Path())
    return out


def listing(root: Path) -> list[dict]:
    return [{"path": r.as_posix(), "size": (root / r).stat().st_size, "updated_at": (root / r).stat().st_mtime}
            for r in outputs(root)][:MAX_FILES]


def safe_rel(rel: str) -> Path | None:
    """来自网络或模型的相对路径；绝对路径、带根或盘符的、含 .. 的一律 None。
    两种系统的写法都拒绝：Windows 上 "/etc/x"、"C:x" 不算 is_absolute()，拼上去却会落到工作目录以外。"""
    if not rel or ".." in PurePosixPath(rel).parts or ".." in PureWindowsPath(rel).parts \
            or PurePosixPath(rel).anchor or PureWindowsPath(rel).anchor:
        return None
    return Path(rel)


def resolve(root: Path, rel: str) -> Path | None:
    """产物里的一个文件；越界、符号链接、不是产物的一律 None。"""
    rel = rel.strip().lstrip("/")
    if not rel:
        return None
    path = Path(rel)
    return root / path if path in set(outputs(root)) else None


def snapshot(src: Path, dst: Path) -> tuple[list[str], int]:
    """把 src 的产物整体拷到 dst（先清空 dst）。返回（拷贝的相对路径，因为超出上限没拷的个数）。"""
    shutil.rmtree(dst, ignore_errors=True)
    files = outputs(src)
    copied: list[str] = []
    total = 0
    for rel in files:
        size = (src / rel).stat().st_size
        if len(copied) >= MAX_FILES or total + size > MAX_BYTES:
            continue
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, dst / rel)
        copied.append(rel.as_posix())
        total += size
    return copied, len(files) - len(copied)


def pack(d: Path) -> bytes:
    """把一个目录的产物打成 zip（发布多文件交付物用）。目录里的内容按“产物”口径过滤，但不跳过顶层的保留名。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in outputs(d, top=False):
            z.write(d / rel, (Path(d.name) / rel).as_posix())
    return buf.getvalue()
