"""发布物的快照（docs/project-publishing.md，不变量 23）。

一份发布物的每个版本固定成 `releases/<id>/v<N>/`：

    skills/<名字>/...   技能包：发布时那一版的技能（项目技能或插件自带的技能）
    files/<文件名>      交付件：发布时那一版的档案文件
    page/...            交付件带插件视图时：发布时那一版的插件页面代码（人看过的代码才在读者浏览器里跑）
    manifest.json       这个版本的说明：标题、内容清单、视图、发布时间

快照先写进临时目录再改名，读者与订阅者看到的永远是一个完整的版本。这里只有文件操作，谁能发布、谁能看在 engine 里判断。
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .models import RELEASE_VERSIONS_KEPT

SKILLS_DIR = "skills"
FILES_DIR = "files"
PAGE_DIR = "page"
MANIFEST = "manifest.json"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ReleaseError(ValueError):
    pass


def check_name(name: str) -> str:
    name = str(name or "").strip()
    if not NAME_RE.match(name) or len(name) > 64:
        raise ReleaseError(f"发布物的名字只能是小写字母、数字和 -，不以 - 开头结尾，最长 64：{name!r}")
    return name


def version_dir(root: Path, version: int) -> Path:
    return root / f"v{version}"


def snapshot(root: Path, version: int, skills: dict[str, Path], files: dict[str, Path], page: Path | None,
             meta: dict[str, Any]) -> Path:
    """把这一版的内容拷成快照。skills：名字 → 技能目录；files：文件名 → 档案里的文件；page：插件包的 page/。"""
    final = version_dir(root, version)
    tmp = root / f".v{version}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        for name, src in skills.items():
            shutil.copytree(src, tmp / SKILLS_DIR / name, symlinks=False,
                            ignore=shutil.ignore_patterns(".*"))
        if files:
            (tmp / FILES_DIR).mkdir()
            for name, src in files.items():
                shutil.copy2(src, tmp / FILES_DIR / name)
        if page is not None and page.is_dir():
            shutil.copytree(page, tmp / PAGE_DIR, symlinks=False, ignore=shutil.ignore_patterns(".*"))
        (tmp / MANIFEST).write_text(json.dumps({**meta, "version": version, "snapshot_at": time.time()},
                                               ensure_ascii=False, indent=1), encoding="utf-8")
        shutil.rmtree(final, ignore_errors=True)
        tmp.rename(final)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    prune(root, version)
    return final


def prune(root: Path, current: int, keep: int = RELEASE_VERSIONS_KEPT) -> None:
    """只保留最近几个版本：读者手上的帧令牌绑定版本，刚切版本时上一个版本还要能取到页面。"""
    for d in root.iterdir() if root.is_dir() else []:
        m = re.fullmatch(r"v(\d+)", d.name)
        if m and int(m.group(1)) <= current - keep:
            shutil.rmtree(d, ignore_errors=True)


def listing(d: Path) -> list[dict[str, Any]]:
    """一个版本里的文件（交付件）：名字与大小。"""
    f = d / FILES_DIR
    return [{"name": p.name, "size": p.stat().st_size} for p in sorted(f.iterdir()) if p.is_file()] if f.is_dir() else []


def manifest(d: Path) -> dict[str, Any]:
    try:
        return json.loads((d / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def safe_file(d: Path, sub: str, rel: str) -> Path | None:
    """快照里 sub/ 下的一个文件；越界、点开头、符号链接一律当不存在。"""
    parts = [x for x in str(rel or "").replace("\\", "/").split("/") if x not in ("", ".")]
    if not parts or any(x == ".." or x.startswith(".") for x in parts):
        return None
    f = d / sub / Path(*parts)
    return f if f.is_file() and not f.is_symlink() else None
