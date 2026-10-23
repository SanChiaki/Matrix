"""项目插件：包、清单、记录与部署方目录（docs/project-plugins.md）。

一个插件包是一个目录（上传时是 zip）::

    sales-pipeline/
      plugin.json          清单：标签、表、申请的能力、触发
      page/                页面代码（只在浏览器沙箱里跑，网络不执行）
      skills/<名字>/       打法：标准技能包，装上后排在项目技能之后

本模块只做文件与格式层面的事（解析、校验、安装、记录读写），不判断权限；权限与可见性在 engine。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import skills as skillfs
from .models import (PLUGIN_AGENT_CAPS, PLUGIN_FIELDS, PLUGIN_FILES, PLUGIN_LIMIT, PLUGIN_PAGE_CAPS, PLUGIN_RECORD_LIMIT,
                     PLUGIN_TABLES, PLUGIN_TABS, PLUGIN_TRIGGERS, new_id)
from .workdir import safe_rel

MANIFEST = "plugin.json"
PAGE_DIR = "page"
SKILLS_DIR = "skills"
KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
VERSION = re.compile(r"^\d+(\.\d+){0,3}([-+][0-9A-Za-z.-]+)?$")
FIELD_TYPES = ("text", "longtext", "number", "date", "select", "bool", "agent", "file")
TRIGGER_TYPES = ("webhook", "schedule", "record")
TEXT_MAX, LONGTEXT_MAX = 500, 8000


class PluginError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    type: str
    options: tuple[str, ...] = ()
    required: bool = False


@dataclass(frozen=True)
class Table:
    key: str
    title: str
    fields: tuple[Field, ...]

    def field(self, key: str) -> Field | None:
        return next((f for f in self.fields if f.key == key), None)


@dataclass(frozen=True)
class Tab:
    key: str
    label: str
    entry: str            # 相对插件包根目录，必须在 page/ 下


@dataclass(frozen=True)
class Trigger:
    key: str
    type: str             # webhook | schedule | record
    to: str               # 被 @ 的成员（Agent 名称）
    text: str             # 消息模板：{字段} 换成记录的值
    table: str | None = None
    cron: str | None = None
    when_field: str | None = None    # record：某字段变成某值时
    when_equals: str | None = None
    on_create: bool = False          # record：新增记录时

    @property
    def label(self) -> str:
        """给人看的名字（群聊说明里用，不露出 key）。"""
        kind = {"webhook": "外部回调", "schedule": "定时", "record": "记录变化"}[self.type]
        return f"{kind} → @{self.to}"


@dataclass(frozen=True)
class Manifest:
    name: str
    version: str
    title: str
    description: str
    tabs: tuple[Tab, ...]
    tables: tuple[Table, ...]
    page_permissions: tuple[str, ...]
    agent_tools: tuple[str, ...]
    triggers: tuple[Trigger, ...]

    def table(self, key: str) -> Table | None:
        return next((t for t in self.tables if t.key == key), None)

    def trigger(self, key: str) -> Trigger | None:
        return next((t for t in self.triggers if t.key == key), None)

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name, "version": self.version, "title": self.title, "description": self.description,
            "tabs": [{"key": t.key, "label": t.label} for t in self.tabs],
            "tables": [{"key": t.key, "title": t.title,
                        "fields": [{"key": f.key, "label": f.label, "type": f.type, "options": list(f.options),
                                    "required": f.required} for f in t.fields]} for t in self.tables],
            "page_permissions": [{"key": p, "label": PLUGIN_PAGE_CAPS[p]} for p in self.page_permissions],
            "agent_tools": list(self.agent_tools),
            "triggers": [{"key": t.key, "type": t.type, "to": t.to, "text": t.text, "table": t.table, "cron": t.cron,
                          "when": ({"field": t.when_field, "equals": t.when_equals} if t.when_field else
                                   {"event": "create"} if t.on_create else None)} for t in self.triggers],
        }


# ================= 清单 =================
def _s(v: Any, what: str, limit: int = 200, required: bool = True) -> str:
    v = " ".join(str(v if v is not None else "").split())
    if required and not v:
        raise PluginError(f"{what} 不能为空")
    if len(v) > limit:
        raise PluginError(f"{what} 最长 {limit} 个字符")
    return v


def _key(v: Any, what: str) -> str:
    v = str(v or "").strip()
    if not KEY.match(v):
        raise PluginError(f"{what} 无效：{v!r}（小写字母开头，小写字母、数字、下划线，最长 40）")
    return v


def parse_manifest(data: Any) -> Manifest:
    if not isinstance(data, dict):
        raise PluginError("plugin.json 顶层必须是对象")
    try:
        name = skillfs.check_name(str(data.get("name") or ""))
    except skillfs.SkillError as e:
        raise PluginError(str(e).replace("技能名", "插件名")) from e
    version = str(data.get("version") or "").strip()
    if not VERSION.match(version):
        raise PluginError(f"version 无效：{version!r}（如 1.0.0）")
    title = _s(data.get("title") or name, "title", 40)
    description = _s(data.get("description"), "description", 500)

    tables: list[Table] = []
    raw_tables = data.get("tables") or {}
    if not isinstance(raw_tables, dict) or len(raw_tables) > PLUGIN_TABLES:
        raise PluginError(f"tables 必须是对象，最多 {PLUGIN_TABLES} 张表")
    for tk, t in raw_tables.items():
        tk = _key(tk, "表名")
        if not isinstance(t, dict) or not isinstance(t.get("fields"), list) or not t["fields"]:
            raise PluginError(f"表 {tk} 要有 fields 列表")
        if len(t["fields"]) > PLUGIN_FIELDS:
            raise PluginError(f"表 {tk} 最多 {PLUGIN_FIELDS} 个字段")
        fs: list[Field] = []
        for i, f in enumerate(t["fields"]):
            if not isinstance(f, dict):
                raise PluginError(f"表 {tk} 的 fields[{i}] 必须是对象")
            fk = _key(f.get("key"), f"表 {tk} 的字段名")
            if any(x.key == fk for x in fs) or fk in ("id",):
                raise PluginError(f"表 {tk} 的字段 {fk} 重复或是保留字")
            ftype = str(f.get("type") or "text")
            if ftype not in FIELD_TYPES:
                raise PluginError(f"表 {tk} 的字段 {fk} 类型无效：{ftype}（可选 {'/'.join(FIELD_TYPES)}）")
            opts = tuple(_s(o, f"字段 {fk} 的选项", 60) for o in (f.get("options") or []))
            if ftype == "select" and not opts:
                raise PluginError(f"表 {tk} 的字段 {fk} 是 select，要有 options")
            if len(opts) > 50 or len(set(opts)) != len(opts):
                raise PluginError(f"表 {tk} 的字段 {fk} 选项最多 50 个且不能重复")
            fs.append(Field(fk, _s(f.get("label") or fk, f"字段 {fk} 的 label", 40), ftype, opts,
                            bool(f.get("required"))))
        tables.append(Table(tk, _s(t.get("title") or tk, f"表 {tk} 的 title", 40), tuple(fs)))

    tabs: list[Tab] = []
    raw_tabs = data.get("tabs") or []
    if not isinstance(raw_tabs, list) or len(raw_tabs) > PLUGIN_TABS:
        raise PluginError(f"tabs 必须是列表，最多 {PLUGIN_TABS} 个")
    for i, t in enumerate(raw_tabs):
        if not isinstance(t, dict):
            raise PluginError(f"tabs[{i}] 必须是对象")
        key = _key(t.get("key"), f"tabs[{i}].key")
        entry = str(t.get("entry") or "").strip()
        rel = safe_rel(entry)
        if rel is None or rel.parts[0] != PAGE_DIR or len(rel.parts) < 2 or rel.suffix.lower() not in (".html", ".htm"):
            raise PluginError(f"tabs[{i}].entry 要是 {PAGE_DIR}/ 下的 .html 文件：{entry!r}")
        if any(x.key == key for x in tabs):
            raise PluginError(f"标签 {key} 重复")
        tabs.append(Tab(key, _s(t.get("label"), f"tabs[{i}].label", 20), rel.as_posix()))

    perms = _caps(data.get("page_permissions"), PLUGIN_PAGE_CAPS, "page_permissions")
    agent = _caps(data.get("agent_tools"), PLUGIN_AGENT_CAPS, "agent_tools")
    if any(p.startswith("records.") for p in perms + agent) and not tables:
        raise PluginError("申请了记录能力，但清单里没有表")

    triggers: list[Trigger] = []
    raw_trig = data.get("triggers") or []
    if not isinstance(raw_trig, list) or len(raw_trig) > PLUGIN_TRIGGERS:
        raise PluginError(f"triggers 必须是列表，最多 {PLUGIN_TRIGGERS} 个")
    by_key = {t.key: t for t in tables}
    for i, t in enumerate(raw_trig):
        if not isinstance(t, dict):
            raise PluginError(f"triggers[{i}] 必须是对象")
        key = _key(t.get("key"), f"triggers[{i}].key")
        if any(x.key == key for x in triggers):
            raise PluginError(f"触发 {key} 重复")
        ttype = str(t.get("type") or "")
        if ttype not in TRIGGER_TYPES:
            raise PluginError(f"触发 {key} 的 type 无效：{ttype!r}（可选 {'/'.join(TRIGGER_TYPES)}）")
        notify = t.get("notify") or {}
        if not isinstance(notify, dict):
            raise PluginError(f"触发 {key} 的 notify 必须是对象")
        to = _s(str(notify.get("to") or "").lstrip("@"), f"触发 {key} 的 notify.to", 80)
        text = _s(notify.get("text"), f"触发 {key} 的 notify.text", 1000)
        table = t.get("table")
        if table is not None:
            table = _key(table, f"触发 {key} 的 table")
            if table not in by_key:
                raise PluginError(f"触发 {key} 的表 {table} 不在清单里")
        cron = when_field = when_equals = None
        on_create = False
        if ttype == "schedule":
            cron = str(t.get("cron") or "").strip()
            parse_cron(cron)
        if ttype == "record":
            if table is None:
                raise PluginError(f"触发 {key} 是记录变化，要写 table")
            when = t.get("when") or {}
            if not isinstance(when, dict):
                raise PluginError(f"触发 {key} 的 when 必须是对象")
            if when.get("event") == "create":
                on_create = True
            else:
                when_field = _key(when.get("field"), f"触发 {key} 的 when.field")
                if by_key[table].field(when_field) is None:
                    raise PluginError(f"触发 {key} 的字段 {when_field} 不在表 {table} 里")
                when_equals = _s(when.get("equals"), f"触发 {key} 的 when.equals", 200)
        triggers.append(Trigger(key, ttype, to, text, table, cron, when_field, when_equals, on_create))
    if not tabs and not tables and not triggers:
        raise PluginError("插件至少要有一个标签、一张表或一个触发")
    return Manifest(name, version, title, description, tuple(tabs), tuple(tables), perms, agent, tuple(triggers))


def _caps(v: Any, allowed: Any, what: str) -> tuple[str, ...]:
    v = [str(x).strip() for x in (v or [])] if isinstance(v, list) or v is None else None
    if v is None:
        raise PluginError(f"{what} 必须是列表")
    bad = [x for x in v if x not in allowed]
    if bad:
        raise PluginError(f"{what} 里有不认识的能力：{'、'.join(bad)}")
    return tuple(dict.fromkeys(v))


def read_manifest(d: Path) -> Manifest:
    try:
        return parse_manifest(json.loads((d / MANIFEST).read_text(encoding="utf-8")))
    except OSError as e:
        raise PluginError(f"没有 {MANIFEST}") from e
    except json.JSONDecodeError as e:
        raise PluginError(f"{MANIFEST} 不是合法的 JSON：{e}") from e


def schema_changes(old: Manifest, new: Manifest) -> list[str]:
    """升级时会丢掉或改变含义的结构变化：删表、删字段、改字段类型、删 select 选项。新增的不算。"""
    out = []
    for t in old.tables:
        nt = new.table(t.key)
        if nt is None:
            out.append(f"删除表 {t.key}")
            continue
        for f in t.fields:
            nf = nt.field(f.key)
            if nf is None:
                out.append(f"删除字段 {t.key}.{f.key}")
            elif nf.type != f.type:
                out.append(f"字段 {t.key}.{f.key} 类型 {f.type} → {nf.type}")
            elif set(f.options) - set(nf.options):
                out.append(f"字段 {t.key}.{f.key} 去掉选项 {'、'.join(sorted(set(f.options) - set(nf.options)))}")
    return out


# ================= 包 =================
def unzip(data: bytes) -> list[tuple[str, bytes]]:
    import io
    import zipfile
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise PluginError("不是有效的 zip 文件") from e
    out, total = [], 0
    for m in z.infolist():
        if m.is_dir():
            continue
        total += m.file_size
        if total > PLUGIN_LIMIT or len(out) >= PLUGIN_FILES:
            raise PluginError(f"插件包超过 {PLUGIN_LIMIT // 1_000_000} MB 或 {PLUGIN_FILES} 个文件")
        out.append((m.filename, z.read(m)))
    return out


def package_files(entries: list[tuple[str, bytes]]) -> dict[Path, bytes]:
    """[(路径, 内容)] → {相对包根目录的路径: 内容}。包可以多一层顶层目录；`__MACOSX`、`.DS_Store` 忽略，点开头或越界的路径拒绝。"""
    entries = [(p.replace("\\", "/").lstrip("/"), b) for p, b in entries]
    entries = [(p, b) for p, b in entries if not any(part in ("__MACOSX", ".DS_Store") for part in p.split("/"))]
    heads = [p.split("/") for p, _ in entries if p.split("/")[-1] == MANIFEST]
    if not heads:
        raise PluginError(f"插件包里没有 {MANIFEST}")
    prefix = min(heads, key=len)[:-1]
    if len(prefix) > 1:
        raise PluginError(f"{MANIFEST} 要在插件包的根目录（或唯一的顶层文件夹）里")
    files: dict[Path, bytes] = {}
    for p, b in entries:
        parts = p.split("/")
        if parts[:len(prefix)] != prefix:
            raise PluginError("插件包里只能有一个插件")
        rel = safe_rel("/".join(parts[len(prefix):]))
        if rel is None or any(part.startswith(".") for part in rel.parts):
            raise PluginError(f"非法的文件名：{p!r}")
        files[rel] = b
    if len(files) > PLUGIN_FILES or sum(len(b) for b in files.values()) > PLUGIN_LIMIT:
        raise PluginError(f"插件包超过 {PLUGIN_LIMIT // 1_000_000} MB 或 {PLUGIN_FILES} 个文件")
    return files


def dir_files(src: Path) -> dict[Path, bytes]:
    """部署方目录里的插件（一个文件夹）读成同样的形状。"""
    out = {}
    for f in sorted(src.rglob("*")):
        if f.is_file() and not f.is_symlink():
            rel = f.relative_to(src)
            if any(part.startswith(".") or part == "__MACOSX" for part in rel.parts):
                continue
            out[rel] = f.read_bytes()
    if len(out) > PLUGIN_FILES or sum(len(b) for b in out.values()) > PLUGIN_LIMIT:
        raise PluginError(f"插件 {src.name} 超过 {PLUGIN_LIMIT // 1_000_000} MB 或 {PLUGIN_FILES} 个文件")
    return out


def check_package(files: dict[Path, bytes]) -> Manifest:
    head = files.get(Path(MANIFEST))
    if head is None:
        raise PluginError(f"插件包里没有 {MANIFEST}")
    try:
        m = parse_manifest(json.loads(head.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PluginError(f"{MANIFEST} 不是合法的 UTF-8 JSON：{e}") from e
    for t in m.tabs:
        if Path(t.entry) not in files:
            raise PluginError(f"标签 {t.key} 的页面 {t.entry} 不在包里")
    for rel, b in files.items():  # 打法：skills/<名字>/SKILL.md 要是合法的技能
        if rel.parts[0] == SKILLS_DIR and len(rel.parts) == 3 and rel.name == skillfs.SKILL_FILE:
            name, desc, _ = skillfs.parse(b.decode("utf-8", errors="replace"))
            if name != rel.parts[1]:
                raise PluginError(f"打法 {rel.parts[1]} 的 SKILL.md 里 name 要和目录名一致")
            try:
                skillfs.check_name(name)
            except skillfs.SkillError as e:
                raise PluginError(str(e)) from e
            if not desc.strip():
                raise PluginError(f"打法 {name} 的 SKILL.md 要有 description")
    return m


def install(root: Path, files: dict[Path, bytes]) -> Manifest:
    """校验后装进 root/<名字>/（同名整个替换）。返回清单。"""
    m = check_package(files)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".upload-{m.name}"
    shutil.rmtree(tmp, ignore_errors=True)
    for rel, b in files.items():
        (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp / rel).write_bytes(b)
    shutil.rmtree(root / m.name, ignore_errors=True)
    tmp.rename(root / m.name)
    return m


def page_file(pkg: Path, rel: str) -> Path | None:
    """沙箱页面要的静态文件：只能是 page/ 下的。"""
    p = safe_rel(rel)
    if p is None or not p.parts or p.parts[0] != PAGE_DIR or any(x.startswith(".") for x in p.parts):
        return None
    f = pkg / p
    return f if f.is_file() and not f.is_symlink() else None


# ================= 记录 =================
def clean_fields(table: Table, values: Any, partial: bool, check: Any = None) -> dict[str, Any]:
    """按清单校验一条记录的字段。partial：修改时只校验给出的字段。check(field, value) 让调用方
    再校验 agent / file 这类要看项目状态的值（返回规范化后的值或抛 PluginError）。"""
    if not isinstance(values, dict):
        raise PluginError("fields 必须是对象")
    unknown = [k for k in values if table.field(str(k)) is None]
    if unknown:
        raise PluginError(f"表 {table.key} 没有字段：{'、'.join(map(str, unknown))}")
    out: dict[str, Any] = {}
    for f in table.fields:
        if f.key not in values:
            if f.required and not partial:
                raise PluginError(f"字段 {f.label}（{f.key}）必填")
            continue
        v = values[f.key]
        if v is None or v == "":
            if f.required:
                raise PluginError(f"字段 {f.label}（{f.key}）必填")
            out[f.key] = None
            continue
        out[f.key] = _value(f, v)
        if check is not None and f.type in ("agent", "file"):
            out[f.key] = check(f, out[f.key])
    if len(json.dumps(out, ensure_ascii=False).encode()) > PLUGIN_RECORD_LIMIT:
        raise PluginError(f"一条记录最多 {PLUGIN_RECORD_LIMIT // 1000} KB")
    return out


def _value(f: Field, v: Any) -> Any:
    if f.type in ("text", "agent", "file"):
        v = " ".join(str(v).split())
        if len(v) > TEXT_MAX:
            raise PluginError(f"字段 {f.key} 最长 {TEXT_MAX} 个字符")
        return v
    if f.type == "longtext":
        v = str(v).strip()
        if len(v) > LONGTEXT_MAX:
            raise PluginError(f"字段 {f.key} 最长 {LONGTEXT_MAX} 个字符")
        return v
    if f.type == "number":
        if isinstance(v, bool):
            raise PluginError(f"字段 {f.key} 要是数字")
        try:
            n = float(v)
        except (TypeError, ValueError) as e:
            raise PluginError(f"字段 {f.key} 要是数字") from e
        if n != n or n in (float("inf"), float("-inf")):
            raise PluginError(f"字段 {f.key} 要是有限的数字")
        return int(n) if n.is_integer() else n
    if f.type == "date":
        v = str(v).strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?$", v):
            raise PluginError(f"字段 {f.key} 要是日期，如 2026-10-23")
        return v
    if f.type == "bool":
        if isinstance(v, bool):
            return v
        if str(v).lower() in ("true", "1", "yes", "是"):
            return True
        if str(v).lower() in ("false", "0", "no", "否"):
            return False
        raise PluginError(f"字段 {f.key} 要是 true / false")
    v = str(v).strip()  # select
    if v not in f.options:
        raise PluginError(f"字段 {f.key} 只能是：{'、'.join(f.options)}")
    return v


class Records:
    """一个插件的一张表：`plugin-data/<插件>/<表>.json`，整表读写（上限 PLUGIN_RECORDS 条）。调用方加锁。"""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def save(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def new(fields_: dict[str, Any], by: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        return {"id": new_id("rec"), "fields": fields_, "created_at": now, "created_by": by,
                "updated_at": now, "updated_by": by}


def search(rows: list[dict[str, Any]], query: str = "", where: dict[str, Any] | None = None,
           limit: int = 200) -> list[dict[str, Any]]:
    q = query.strip().lower()
    out = []
    for r in reversed(rows):  # 新的在前
        f = r.get("fields") or {}
        if where and any(str(f.get(k)) != str(v) for k, v in where.items()):
            continue
        if q and q not in json.dumps(f, ensure_ascii=False).lower():
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def render_text(template: str, values: dict[str, Any]) -> str:
    """消息模板：{字段} 换成值；没有的字段换成空。不支持表达式。"""
    return re.sub(r"\{([a-z][a-z0-9_]{0,39})\}", lambda m: " ".join(str(values.get(m.group(1)) or "").split())[:200],
                  template)


# ================= 定时 =================
def parse_cron(expr: str) -> tuple[set[int], ...]:
    """五段 cron（分 时 日 月 周，本地时间）：支持 * 、数字、a-b、a,b、*/n、a-b/n。周日是 0 或 7。"""
    parts = expr.split()
    if len(parts) != 5:
        raise PluginError(f"cron 要有五段（分 时 日 月 周）：{expr!r}")
    bounds = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
    out = []
    for part, (lo, hi) in zip(parts, bounds):
        vals: set[int] = set()
        for item in part.split(","):
            rng, _, step = item.partition("/")
            try:
                st = int(step) if step else 1
                if rng == "*":
                    a, b = lo, hi
                elif "-" in rng:
                    a, b = (int(x) for x in rng.split("-", 1))
                else:
                    a = b = int(rng)
            except ValueError as e:
                raise PluginError(f"cron 无效：{expr!r}") from e
            if st < 1 or a < lo or b > hi or a > b:
                raise PluginError(f"cron 越界：{expr!r}")
            vals.update(range(a, b + 1, st))
        out.append(vals)
    if 7 in out[4]:
        out[4].add(0)
    if len(out[0]) > 6:
        raise PluginError("定时最小间隔 10 分钟：分钟这一段最多 6 个值")
    return tuple(out)


def cron_due(expr: str, t: time.struct_time) -> bool:
    m, h, dom, mon, dow = parse_cron(expr)
    wd = (t.tm_wday + 1) % 7  # struct_time 周一是 0；cron 周日是 0
    day_ok = (t.tm_mday in dom or wd in dow) if (len(dom) < 31 and len(dow) < 8) else (t.tm_mday in dom and wd in dow)
    return t.tm_min in m and t.tm_hour in h and t.tm_mon in mon and day_ok


# ================= 部署方目录 =================
@dataclass(frozen=True)
class CatalogPlugin:
    dir: Path
    manifest: Manifest


@dataclass
class PluginCatalog:
    """部署方维护的 `plugins.json`：`{"plugins": ["相对这个文件的目录", ...]}`，按修改时间热加载，写坏了沿用上一份。
    目录里的插件本身改了（版本号变）也会被看到：每次文件或任一插件的清单变了就重读。"""

    path: Path | None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stamp: tuple[float, ...] | None = None
    _items: list[CatalogPlugin] = field(default_factory=list)
    error: str | None = None

    def _dirs(self) -> list[Path]:
        assert self.path is not None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
            raise PluginError("顶层必须是 {\"plugins\": [\"目录\", ...]}")
        return [(self.path.parent / str(p)).resolve() for p in data["plugins"]]

    def _refresh(self) -> None:
        if self.path is None:
            return
        try:
            stamp = [self.path.stat().st_mtime]
            dirs = self._dirs()
            stamp += [(d / MANIFEST).stat().st_mtime if (d / MANIFEST).is_file() else 0.0 for d in dirs]
        except FileNotFoundError:
            self._stamp, self._items, self.error = None, [], None
            return
        except (OSError, ValueError) as e:
            self.error = str(e)
            return
        if tuple(stamp) == self._stamp:
            return
        self._stamp = tuple(stamp)
        try:
            items = []
            for d in dirs:
                m = check_package(dir_files(d))
                if any(x.manifest.name == m.name for x in items):
                    raise PluginError(f"插件名重复：{m.name}")
                items.append(CatalogPlugin(d, m))
            self._items, self.error = items, None
        except (OSError, ValueError) as e:
            self.error = str(e)
            print(f"[matrix] 插件目录 {self.path} 无效，沿用上一份：{e}", file=sys.stderr)

    def items(self) -> list[CatalogPlugin]:
        with self._lock:
            self._refresh()
            return list(self._items)

    def get(self, name: str) -> CatalogPlugin | None:
        return next((x for x in self.items() if x.manifest.name == name), None)
