"""Agent 私有目录的挂载与写回：云侧（engine）与端侧（connector）共用同一套语义。

私有目录在每次运行时拷进工作目录的 `agent-home/`（沙箱只允许写工作目录），运行后按文件三方比较写回：
这次运行改了、而私有目录里同一文件在这期间也被别的运行改过，就把这次的版本另存为冲突副本，不覆盖别人的修改。
没有 `file.write` 时只拷入、不写回（只读）。调用方负责按 Agent 加锁。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class HomeMount:
    base: dict[str, tuple[int, int]]   # 挂载时拷进工作目录的版本
    home: dict[str, tuple[int, int]]   # 挂载时 home 里的版本（写回时据此判断期间有没有别的运行改过）


def fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def safe_tag(key: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in key)[:80]


def mount(home: Path, dest: Path) -> HomeMount:
    """把私有目录拷到 dest（工作目录的 agent-home/）。"""
    home.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(home, dest, symlinks=False)
    return HomeMount(base=fingerprint(dest), home=fingerprint(home))


def sync_back(home: Path, dest: Path, before: HomeMount, ctx_id: str = "") -> list[str]:
    """写回并移除 dest；返回产生的冲突副本（相对路径）。"""
    if not dest.is_dir():
        return []
    conflicts: list[str] = []
    after, now = fingerprint(dest), fingerprint(home)
    for rel, fp in after.items():
        if before.base.get(rel) == fp:
            continue  # 这次运行没改
        target = rel
        if now.get(rel) != before.home.get(rel):  # 别的运行在这期间也改过（或删过 / 新建过）
            p = Path(rel)
            target = str(p.with_name(f"{p.stem}.conflict-{safe_tag(ctx_id)[-24:]}{p.suffix}"))
            conflicts.append(target)
        (home / target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dest / rel, home / target)
    for rel in before.base.keys() - after.keys():
        if now.get(rel) == before.home.get(rel):  # 别人没动过才删
            (home / rel).unlink(missing_ok=True)
    shutil.rmtree(dest, ignore_errors=True)
    return conflicts
