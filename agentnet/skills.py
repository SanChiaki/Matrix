"""技能：可复用的做法（docs/connectors-and-skills.md）。

一个技能是一个目录 `<名字>/SKILL.md`（frontmatter 写 name 与 description，正文是说明），可以带模板、脚本。
三处来源，名字相同时按顺序前者优先：

1. 项目技能：项目档案的一部分，`projects/<id>/skills/`，只经人的操作或经人批准的提议写入（不变量 17）；
2. 连接器自带的技能：随“我的连接器”来；
3. 我的技能：人维护的技能库，`users/<uid>/skills/`，勾给自己的 Agent；
4. Agent 自己攒的：私有目录的 `skills/`。网络不读（不变量 8），只把它作为运行时的第二个技能根挂上，
   前三者合并到工作目录的 `skills/` 后排在它前面，重名时运行时取前者。

本模块只做文件层面的事，不判断权限。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import SKILL_LIMIT, SKILL_NAME_MAX, SKILL_NAME_RE
from .workdir import safe_rel

SKILL_FILE = "SKILL.md"
DESCRIPTION_MAX = 500
_NAME = re.compile(SKILL_NAME_RE)


class SkillError(ValueError):
    pass


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > SKILL_NAME_MAX or not _NAME.match(name):
        raise SkillError(f"技能名无效：{name!r}（小写英文、数字与连字符，如 weekly-report，最长 {SKILL_NAME_MAX}）")
    return name


def render(name: str, description: str, body: str) -> str:
    """生成 SKILL.md。值用 JSON 字符串写（是合法的 YAML 双引号字符串），中文与冒号都不用另外转义。"""
    desc = " ".join((description or "").split())[:DESCRIPTION_MAX]
    if not desc:
        raise SkillError("技能要有一句描述：什么时候用它")
    return (f"---\nname: {json.dumps(check_name(name), ensure_ascii=False)}\n"
            f"description: {json.dumps(desc, ensure_ascii=False)}\n---\n\n{(body or '').strip()}\n")


def parse(text: str) -> tuple[str, str, str]:
    """(name, description, body)。只认最简单的 frontmatter：`key: value` 一行一个。"""
    name = description = ""
    body = text
    if text.startswith("---"):
        head, sep, rest = text[3:].partition("\n---")
        if sep:
            body = rest.lstrip("-").lstrip("\n")
            for line in head.splitlines():
                k, _, v = line.partition(":")
                v = v.strip()
                if v.startswith('"'):
                    try:
                        v = json.loads(v)
                    except json.JSONDecodeError:
                        v = v.strip('"')
                elif v.startswith("'") and v.endswith("'"):
                    v = v[1:-1].replace("''", "'")
                if k.strip() == "name":
                    name = str(v)
                elif k.strip() == "description":
                    description = str(v)
    return name, description, body.strip()


def size_of(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


@dataclass
class SkillInfo:
    name: str
    description: str
    size: int
    updated_at: float
    files: list[str]

    def public(self) -> dict[str, object]:
        return {"name": self.name, "description": self.description, "size": self.size,
                "updated_at": self.updated_at, "files": self.files}


def info(d: Path) -> SkillInfo | None:
    f = d / SKILL_FILE
    if not f.is_file():
        return None
    _, description, _ = parse(f.read_text(encoding="utf-8", errors="replace"))
    files = sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file() and p != f)  # 附带文件，不含 SKILL.md
    mtime = max((p.stat().st_mtime for p in d.rglob("*") if p.is_file()), default=0.0)
    return SkillInfo(d.name, description, size_of(d), mtime, files)


def listing(root: Path) -> list[SkillInfo]:
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and _NAME.match(d.name):
            i = info(d)
            if i is not None:
                out.append(i)
    return out


def read(root: Path, name: str) -> tuple[str, str, str]:
    f = root / check_name(name) / SKILL_FILE
    if not f.is_file():
        raise SkillError(f"没有技能 {name}")
    return parse(f.read_text(encoding="utf-8", errors="replace"))


def write(root: Path, name: str, description: str, body: str) -> Path:
    """新建或改写 SKILL.md（同目录的其他文件保留）。"""
    d = root / check_name(name)
    text = render(name, description, body)
    extra = size_of(d) - ((d / SKILL_FILE).stat().st_size if (d / SKILL_FILE).is_file() else 0) if d.is_dir() else 0
    if len(text.encode()) + extra > SKILL_LIMIT:
        raise SkillError(f"技能 {name} 超过 {SKILL_LIMIT // 1000} KB")
    d.mkdir(parents=True, exist_ok=True)
    (d / SKILL_FILE).write_text(text, encoding="utf-8")
    return d


def add_file(root: Path, name: str, rel: str, data: bytes) -> None:
    """往技能目录里加一个附带文件（模板、脚本）。SKILL.md 只能经 write 改。"""
    d = root / check_name(name)
    if not (d / SKILL_FILE).is_file():
        raise SkillError(f"没有技能 {name}")
    path = safe_rel((rel or "").strip())
    if path is None or path.as_posix() == SKILL_FILE or any(part.startswith(".") for part in path.parts):
        raise SkillError(f"非法的文件名：{rel!r}")
    dest = d / path
    old = dest.stat().st_size if dest.is_file() else 0
    if size_of(d) - old + len(data) > SKILL_LIMIT:
        raise SkillError(f"技能 {name} 超过 {SKILL_LIMIT // 1000} KB")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def remove(root: Path, name: str) -> bool:
    d = root / check_name(name)
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


Layer = tuple[str, Path, Iterable[str] | None]   # (来源, 技能根目录, 只取这些名字；None = 全部)


def resolve(layers: list[Layer]) -> tuple[dict[str, tuple[str, Path]], list[tuple[str, str]]]:
    """按顺序合并几层技能：返回 ({名字: (来源, 目录)}, [(被盖住的名字, 来源)])。前面的层优先。"""
    chosen: dict[str, tuple[str, Path]] = {}
    shadowed: list[tuple[str, str]] = []
    for source, root, names in layers:
        wanted = None if names is None else set(names)
        for i in listing(root):
            if wanted is not None and i.name not in wanted:
                continue
            if i.name in chosen:
                shadowed.append((i.name, source))
            else:
                chosen[i.name] = (source, root / i.name)
    return chosen, shadowed


def mount(dest: Path, chosen: dict[str, tuple[str, Path]]) -> None:
    """把选中的技能拷进 dest（先清空）。只读副本：运行里的改动随后丢弃。"""
    shutil.rmtree(dest, ignore_errors=True)
    for name, (_, src) in chosen.items():
        shutil.copytree(src, dest / name, symlinks=False)
