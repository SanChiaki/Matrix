"""蓝图（docs/blueprints.md）：项目去掉数据、人和凭证之后的配方。本模块只做格式、占位符、可移植检查与存储；
发布、建项目、可见性在 engine「蓝图」一节。

一个蓝图是一个目录（上传、导出时打成 zip）::

    blueprint.json          清单
    README.md               给人看的说明
    brief.md                纲要模板
    skills/<名字>/SKILL.md   项目技能
    files/...               模板文件（建项目时放进项目档案）
    plugins/<名字>/...       打包进来的插件包
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import plugins as pluginfs
from . import skills as skillfs
from .models import AUTHORITIES, new_id
from .plugin_sources import package_hash
from .workdir import safe_rel

MANIFEST = "blueprint.json"
README = "README.md"
BRIEF = "brief.md"
SKILLS = "skills"
FILES = "files"
PLUGINS = "plugins"
LIMIT = 10_000_000               # 一个蓝图包最大字节数（含打包的插件）
FILES_MAX = 1000
ROLES_MAX = 20
KEEP_VERSIONS = 3
PLACEHOLDER = re.compile(r"\{(param|role):([a-z][a-z0-9_]{0,39})\}")
CHECKPOINT_KINDS = {"accept": "人验收", "approve": "人审批", "human_step": "只有人能做的一步", "notify": "代表人发消息"}
ACCESS = ("read", "write", "use")
TEXT_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".html", ".htm", ".css", ".js", ".py", ".toml", ".ini",
                 ".sh", ".sql", ".xml", ".svg")


class BlueprintError(ValueError):
    pass


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    example: str = ""
    required: bool = False


@dataclass(frozen=True)
class Role:
    key: str
    name: str
    description: str
    capabilities: tuple[str, ...] = ()
    authority: tuple[str, ...] = ()
    model: str | None = None


@dataclass(frozen=True)
class Slot:
    slot: str
    connector: str
    access: str
    roles: tuple[str, ...]
    purpose: str


@dataclass(frozen=True)
class PluginRef:
    name: str
    version: str
    source: str                          # catalog | bundled
    settings: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Checkpoint:
    at: str
    kind: str
    note: str
    role: str | None = None
    plugin: str | None = None


@dataclass(frozen=True)
class Blueprint:
    name: str
    version: str
    title: str
    description: str
    params: tuple[Param, ...] = ()
    roles: tuple[Role, ...] = ()
    connectors: tuple[Slot, ...] = ()
    plugins: tuple[PluginRef, ...] = ()
    checkpoints: tuple[Checkpoint, ...] = ()
    files: tuple[str, ...] = ()

    def role(self, key: str) -> Role | None:
        return next((r for r in self.roles if r.key == key), None)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name, "version": self.version, "title": self.title, "description": self.description,
            "params": [{"key": p.key, "label": p.label, "example": p.example, "required": p.required} for p in self.params],
            "roles": [{"key": r.key, "name": r.name, "description": r.description, "capabilities": list(r.capabilities),
                       "authority": list(r.authority), **({"model": r.model} if r.model else {})} for r in self.roles],
            "connectors": [{"slot": c.slot, "connector": c.connector, "access": c.access, "roles": list(c.roles),
                            "purpose": c.purpose} for c in self.connectors],
            "plugins": [{"name": p.name, "version": p.version, "source": p.source, "settings": dict(p.settings)}
                        for p in self.plugins],
            "checkpoints": [{"at": c.at, "kind": c.kind, "note": c.note, **({"role": c.role} if c.role else {}),
                             **({"plugin": c.plugin} if c.plugin else {})} for c in self.checkpoints],
            "files": list(self.files),
        }


def _s(v: Any, what: str, limit: int = 200, required: bool = True) -> str:
    v = " ".join(str(v if v is not None else "").split())
    if required and not v:
        raise BlueprintError(f"{what} 不能为空")
    if len(v) > limit:
        raise BlueprintError(f"{what} 最长 {limit} 个字符")
    return v


def _key(v: Any, what: str) -> str:
    v = str(v or "").strip()
    if not pluginfs.KEY.match(v):
        raise BlueprintError(f"{what} 无效：{v!r}（小写字母开头，小写字母、数字、下划线，最长 40）")
    return v


def _list(v: Any, what: str, limit: int) -> list[Any]:
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > limit:
        raise BlueprintError(f"{what} 必须是列表，最多 {limit} 项")
    return v


def parse(data: Any) -> Blueprint:
    if not isinstance(data, dict):
        raise BlueprintError(f"{MANIFEST} 顶层必须是对象")
    try:
        name = skillfs.check_name(str(data.get("name") or ""))
    except skillfs.SkillError as e:
        raise BlueprintError(str(e).replace("技能名", "蓝图名")) from e
    version = str(data.get("version") or "").strip()
    if not pluginfs.VERSION.match(version):
        raise BlueprintError(f"version 无效：{version!r}（如 1.0.0）")
    params: list[Param] = []
    for i, p in enumerate(_list(data.get("params"), "params", 30)):
        if not isinstance(p, dict):
            raise BlueprintError(f"params[{i}] 必须是对象")
        k = _key(p.get("key"), f"params[{i}].key")
        if any(x.key == k for x in params):
            raise BlueprintError(f"参数 {k} 重复")
        params.append(Param(k, _s(p.get("label") or k, f"参数 {k} 的 label", 60),
                            _s(p.get("example"), f"参数 {k} 的 example", 200, False), bool(p.get("required"))))
    roles: list[Role] = []
    for i, r in enumerate(_list(data.get("roles"), "roles", ROLES_MAX)):
        if not isinstance(r, dict):
            raise BlueprintError(f"roles[{i}] 必须是对象")
        k = _key(r.get("key"), f"roles[{i}].key")
        if any(x.key == k for x in roles):
            raise BlueprintError(f"岗位 {k} 重复")
        auth = tuple(dict.fromkeys(str(a) for a in _list(r.get("authority"), f"岗位 {k} 的 authority", 10)))
        bad = [a for a in auth if a not in AUTHORITIES]
        if bad:
            raise BlueprintError(f"岗位 {k} 的权限不认识：{'、'.join(bad)}")
        roles.append(Role(k, _s(r.get("name") or k, f"岗位 {k} 的 name", 40),
                          _s(r.get("description"), f"岗位 {k} 的 description", 1000, False),
                          tuple(_s(c, f"岗位 {k} 的能力", 40) for c in _list(r.get("capabilities"), f"岗位 {k} 的 capabilities", 20)),
                          auth, (str(r["model"]).strip()[:64] or None) if r.get("model") else None))
    role_keys = {r.key for r in roles}
    slots: list[Slot] = []
    for i, c in enumerate(_list(data.get("connectors"), "connectors", 20)):
        if not isinstance(c, dict):
            raise BlueprintError(f"connectors[{i}] 必须是对象")
        slot = _key(c.get("slot"), f"connectors[{i}].slot")
        access = str(c.get("access") or "use")
        if access not in ACCESS:
            raise BlueprintError(f"连接器槽位 {slot} 的 access 要是 {'/'.join(ACCESS)}")
        rs = tuple(str(x) for x in _list(c.get("roles"), f"连接器槽位 {slot} 的 roles", ROLES_MAX))
        if set(rs) - role_keys:
            raise BlueprintError(f"连接器槽位 {slot} 引用了没有的岗位：{'、'.join(sorted(set(rs) - role_keys))}")
        slots.append(Slot(slot, _s(c.get("connector"), f"连接器槽位 {slot} 的 connector", 80), access, rs,
                          _s(c.get("purpose"), f"连接器槽位 {slot} 的 purpose", 300, False)))
    plugins: list[PluginRef] = []
    for i, p in enumerate(_list(data.get("plugins"), "plugins", 10)):
        if not isinstance(p, dict):
            raise BlueprintError(f"plugins[{i}] 必须是对象")
        pname = str(p.get("name") or "")
        try:
            skillfs.check_name(pname)
        except skillfs.SkillError as e:
            raise BlueprintError(f"plugins[{i}].name 无效：{pname!r}") from e
        source = str(p.get("source") or "catalog")
        if source not in ("catalog", "bundled"):
            raise BlueprintError(f"插件 {pname} 的 source 要是 catalog 或 bundled")
        settings = p.get("settings") or {}
        if not isinstance(settings, dict) or len(settings) > 20:
            raise BlueprintError(f"插件 {pname} 的 settings 要是对象，最多 20 项")
        plugins.append(PluginRef(pname, str(p.get("version") or "").strip(), source,
                                 tuple((_key(k, f"插件 {pname} 的设置名"), _s(v, f"插件 {pname} 的设置 {k}", 200, False))
                                       for k, v in settings.items())))
    checkpoints: list[Checkpoint] = []
    for i, c in enumerate(_list(data.get("checkpoints"), "checkpoints", 30)):
        if not isinstance(c, dict):
            raise BlueprintError(f"checkpoints[{i}] 必须是对象")
        kind = str(c.get("kind") or "")
        if kind not in CHECKPOINT_KINDS:
            raise BlueprintError(f"checkpoints[{i}].kind 要是 {'/'.join(CHECKPOINT_KINDS)}")
        role = str(c["role"]) if c.get("role") else None
        if role is not None and role not in role_keys:
            raise BlueprintError(f"checkpoints[{i}] 引用了没有的岗位 {role}")
        checkpoints.append(Checkpoint(_s(c.get("at"), f"checkpoints[{i}].at", 40), kind,
                                      _s(c.get("note"), f"checkpoints[{i}].note", 300, False), role,
                                      str(c["plugin"]) if c.get("plugin") else None))
    files = []
    for i, f in enumerate(_list(data.get("files"), "files", 200)):
        rel = safe_rel(str(f))
        if rel is None or rel.parts[0] != FILES or len(rel.parts) < 2:
            raise BlueprintError(f"files[{i}] 要是 {FILES}/ 下的路径：{f!r}")
        files.append(rel.as_posix())
    return Blueprint(name, version, _s(data.get("title") or name, "title", 40), _s(data.get("description"), "description", 500),
                     tuple(params), tuple(roles), tuple(slots), tuple(plugins), tuple(checkpoints), tuple(dict.fromkeys(files)))


def plugin_files(files: dict[Path, bytes], name: str) -> dict[Path, bytes]:
    """打包进来的插件：plugins/<名字>/ 下的文件，路径相对插件包根目录。"""
    return {Path(*rel.parts[2:]): b for rel, b in files.items()
            if len(rel.parts) > 2 and rel.parts[0] == PLUGINS and rel.parts[1] == name}


def skill_names(files: dict[Path, bytes]) -> list[str]:
    return sorted({rel.parts[1] for rel in files if len(rel.parts) == 3 and rel.parts[0] == SKILLS
                   and rel.name == skillfs.SKILL_FILE})


def text(files: dict[Path, bytes], rel: str) -> str:
    b = files.get(Path(rel))
    return b.decode("utf-8", errors="replace") if b is not None else ""


def check_package(files: dict[Path, bytes]) -> Blueprint:
    """校验整个蓝图包：清单、技能、模板文件、打包的插件、占位符都对得上。"""
    if len(files) > FILES_MAX or sum(len(b) for b in files.values()) > LIMIT:
        raise BlueprintError(f"蓝图超过 {LIMIT // 1_000_000} MB 或 {FILES_MAX} 个文件")
    head = files.get(Path(MANIFEST))
    if head is None:
        raise BlueprintError(f"蓝图里没有 {MANIFEST}")
    try:
        bp = parse(json.loads(head.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BlueprintError(f"{MANIFEST} 不是合法的 UTF-8 JSON：{e}") from e
    for rel in files:
        if rel.parts[0] not in (MANIFEST, README, BRIEF, SKILLS, FILES, PLUGINS):
            raise BlueprintError(f"蓝图里不认识的文件：{rel.as_posix()}（只能有 {MANIFEST}、{README}、{BRIEF}、{SKILLS}/、{FILES}/、{PLUGINS}/）")
    for f in bp.files:
        if Path(f) not in files:
            raise BlueprintError(f"清单里的模板文件 {f} 不在包里")
    for name in skill_names(files):
        sk_name, desc, _ = skillfs.parse(text(files, f"{SKILLS}/{name}/{skillfs.SKILL_FILE}"))
        if sk_name != name or not desc.strip():
            raise BlueprintError(f"技能 {name} 的 SKILL.md 要有和目录同名的 name 与 description")
    bundled = {p.name for p in bp.plugins if p.source == "bundled"}
    for p in bp.plugins:
        if p.source != "bundled":
            continue
        sub = plugin_files(files, p.name)
        try:
            m = pluginfs.check_package(sub)
        except pluginfs.PluginError as e:
            raise BlueprintError(f"打包的插件 {p.name} 无效：{e}") from e
        if m.name != p.name:
            raise BlueprintError(f"plugins/{p.name}/ 里的插件叫 {m.name}")
    extra = {rel.parts[1] for rel in files if rel.parts[0] == PLUGINS and len(rel.parts) > 2} - bundled
    if extra:
        raise BlueprintError(f"{PLUGINS}/ 下有清单里没写的插件：{'、'.join(sorted(extra))}")
    keys = {"param": {p.key for p in bp.params}, "role": {r.key for r in bp.roles}}
    texts = [(rel.as_posix(), b) for rel, b in files.items() if rel.parts[0] in (BRIEF, SKILLS, FILES)
             and rel.suffix.lower() in TEXT_SUFFIXES]
    texts += [(f"插件 {p.name} 的设置", "\n".join(v for _, v in p.settings).encode()) for p in bp.plugins]
    for where, b in texts:
        for kind, k in PLACEHOLDER.findall(b.decode("utf-8", errors="replace")):
            if k not in keys[kind]:
                raise BlueprintError(f"{where} 里的 {{{kind}:{k}}} 在清单里没有")
    return bp


def fill(s: str, params: dict[str, str], roles: dict[str, str]) -> str:
    """占位符换成建项目时的值：{param:x} → 填的值，{role:x} → 担任这个岗位的成员名（多个用顿号）。"""
    return PLACEHOLDER.sub(lambda m: (params if m.group(1) == "param" else roles).get(m.group(2), m.group(0)), s)


def fill_bytes(rel: Path, b: bytes, params: dict[str, str], roles: dict[str, str]) -> bytes:
    if rel.suffix.lower() not in TEXT_SUFFIXES:
        return b
    try:
        return fill(b.decode("utf-8"), params, roles).encode("utf-8")
    except UnicodeDecodeError:
        return b


# ================= 发布时的可移植检查 =================
FACT_PATTERNS = (
    (re.compile(r"\b20\d\d-\d\d-\d\d\b"), "日期"),
    (re.compile(r"[¥￥$]\s?\d|\d+(?:\.\d+)?\s?(?:万元|元|美元)"), "金额"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "邮箱"),
)


def portability(files: dict[Path, bytes], names: list[str], project_files: list[str]) -> list[str]:
    """蓝图里像是写死了人、引用了没带上的文件、带了项目事实的地方。只提示，主持人确认后照样能发布。"""
    out: list[str] = []
    included = {rel.as_posix()[len(FILES) + 1:] for rel in files if rel.parts[0] == FILES}
    for rel, b in sorted(files.items(), key=lambda x: x[0].as_posix()):
        if rel.parts[0] not in (BRIEF, SKILLS, FILES) or rel.suffix.lower() not in TEXT_SUFFIXES:
            continue
        s = b.decode("utf-8", errors="replace")
        where = rel.as_posix()
        hit = sorted({n for n in names if n and n in s})
        if hit:
            out.append(f"{where} 里还写着成员名「{'、'.join(hit)}」：要换成岗位就写 {{role:岗位标识}}")
        missing = sorted({f for f in re.findall(r"project/([^\s)`'\"]+)", s) if f in project_files and f not in included})
        if missing:
            out.append(f"{where} 引用了没带上的项目文件：{'、'.join(missing)}")
        for pat, what in FACT_PATTERNS:
            m = pat.search(s)
            if m:
                out.append(f"{where} 里有像是项目事实的{what}「{m.group(0)}」：蓝图不带数据，确认这是方法不是事实")
                break
    return out


def dir_files(src: Path) -> dict[Path, bytes]:
    """一个蓝图目录读成 {相对路径: 内容}（不跟符号链接，跳过点开头的）。"""
    out: dict[Path, bytes] = {}
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src)
        if f.is_file() and not f.is_symlink() and not any(x.startswith(".") or x == "__pycache__" for x in rel.parts):
            out[rel] = f.read_bytes()
            if len(out) > FILES_MAX or sum(len(b) for b in out.values()) > LIMIT:
                raise BlueprintError(f"蓝图超过 {LIMIT // 1_000_000} MB 或 {FILES_MAX} 个文件")
    return out


def package_files(entries: list[tuple[str, bytes]]) -> dict[Path, bytes]:
    """上传的 zip / 文件夹：可以多一层顶层目录（blueprint.json 所在的那层是根）。"""
    entries = [(p.replace("\\", "/").lstrip("/"), b) for p, b in entries]
    entries = [(p, b) for p, b in entries if not any(x in ("__MACOSX", ".DS_Store", "__pycache__") for x in p.split("/"))]
    heads = [p.split("/") for p, _ in entries if p.split("/")[-1] == MANIFEST]
    if not heads:
        raise BlueprintError(f"蓝图里没有 {MANIFEST}")
    prefix = min(heads, key=len)[:-1]
    if len(prefix) > 1:
        raise BlueprintError(f"{MANIFEST} 要在根目录（或唯一的顶层文件夹）里")
    out: dict[Path, bytes] = {}
    for p, b in entries:
        parts = p.split("/")
        if parts[:len(prefix)] != prefix:
            raise BlueprintError("包里只能有一个蓝图")
        rel = safe_rel("/".join(parts[len(prefix):]))
        if rel is None or any(x.startswith(".") for x in rel.parts):
            raise BlueprintError(f"非法的文件名：{p!r}")
        out[rel] = b
    if len(out) > FILES_MAX or sum(len(b) for b in out.values()) > LIMIT:
        raise BlueprintError(f"蓝图超过 {LIMIT // 1_000_000} MB 或 {FILES_MAX} 个文件")
    return out


# ================= 存储 =================
@dataclass
class BlueprintStore:
    """`<data>/blueprints/<id>/meta.json` + `v<N>/`（一份蓝图的每个版本，保留最近 3 个）。

    meta：id、name、title、description、version（清单里的）、rev（第几版）、source（project | catalog）、project_id、
    readers（用户 id）、public、published_by、published_at、status（active | withdrawn）、uses、sha256。"""

    root: Path
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def dir(self) -> Path:
        return self.root / "blueprints"

    def _write(self, meta: dict[str, Any]) -> None:
        d = self.dir / meta["id"]
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "meta.tmp"
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(d / "meta.json")

    def get(self, bid: str) -> dict[str, Any] | None:
        if not re.match(r"^bp-[0-9a-f]{8}$", bid or ""):
            return None
        try:
            return json.loads((self.dir / bid / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def list(self) -> list[dict[str, Any]]:
        out = []
        if self.dir.is_dir():
            for d in self.dir.iterdir():
                m = self.get(d.name)
                if m is not None:
                    out.append(m)
        return sorted(out, key=lambda m: -m.get("published_at", 0))

    def files(self, bid: str, rev: int | None = None) -> dict[Path, bytes]:
        meta = self.get(bid)
        if meta is None:
            raise BlueprintError(f"unknown blueprint {bid}")
        return dir_files(self.dir / bid / f"v{rev or meta['rev']}")

    def save(self, files: dict[Path, bytes], meta: dict[str, Any], by: str | None) -> dict[str, Any]:
        """新建（meta 里没有 id）或给已有的切一个新版本。"""
        bp = check_package(files)
        with self._lock:
            old = self.get(meta.get("id", "")) if meta.get("id") else None
            rev = (old or {}).get("rev", 0) + 1
            meta = {**(old or {}), **meta, "id": (old or {}).get("id") or new_id("bp"), "name": bp.name, "title": bp.title,
                    "description": bp.description, "version": bp.version, "rev": rev, "sha256": package_hash(files),
                    "published_by": by, "published_at": time.time(), "status": "active",
                    "uses": (old or {}).get("uses", 0)}
            d = self.dir / meta["id"]
            tmp = d / f".v{rev}"
            shutil.rmtree(tmp, ignore_errors=True)
            for rel, b in files.items():
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                (tmp / rel).write_bytes(b)
            tmp.rename(d / f"v{rev}")
            for old_rev in range(1, rev - KEEP_VERSIONS + 1):
                shutil.rmtree(d / f"v{old_rev}", ignore_errors=True)
            self._write(meta)
        return meta

    def update(self, bid: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            meta = self.get(bid)
            if meta is None:
                raise BlueprintError(f"unknown blueprint {bid}")
            meta.update(fields)
            self._write(meta)
        return meta

    def remove(self, bid: str) -> None:
        with self._lock:
            if self.get(bid) is None:
                raise BlueprintError(f"unknown blueprint {bid}")
            shutil.rmtree(self.dir / bid, ignore_errors=True)
