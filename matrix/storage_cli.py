"""存储迁移的命令（M7 / M8，docs/reference/storage.md）。服务停着时跑。

    matrix migrate state [--replace]       state.json → 数据库（库空时新版本首次启动也会自动做），打印对账报告
    matrix migrate verify [--against F]     库里的状态和原来的 state.json（默认 state.json.imported）按内容比对
    matrix export-state [--out F] [--force] 库 → state.json（回退到旧版本之前做）
    matrix backup-db --out F                SQLite 在线备份（服务开着也行）；PostgreSQL 用 pg_dump

库的位置都来自 --store 或 MATRIX_STORE（sqlite / sqlite:<路径> / postgresql://…）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import compat
from . import db as dbmod
from .store import DbBackend, Store, reconcile, write_durable


def _args(prog: str, argv: list[str], extra: Any = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog=prog)
    p.add_argument("--data", default=None, help="数据目录；默认 MATRIX_DATA，没有就用 .matrix")
    p.add_argument("--store", default=None, help="库地址；默认 MATRIX_STORE（sqlite / sqlite:<路径> / postgresql://…）")
    if extra:
        extra(p)
    a = p.parse_args(argv)
    a.data = Path(a.data or compat.data_dir()).resolve()
    a.store = a.store if a.store is not None else compat.env("STORE")
    if not dbmod.resolve_url(a.store, a.data):
        raise SystemExit("没有指定数据库：设 MATRIX_STORE 或加 --store（例如 --store sqlite）")
    return a


def _show(rep: dict[str, Any]) -> None:
    for kind, c in rep["kinds"].items():
        if c["state"] or c["db"]:
            mark = "" if c["state"] == c["db"] else "  ← 不一致"
            print(f"  {kind:<18} state.json {c['state']:>8}   库 {c['db']:>8}{mark}")
    print(f"  {'messages':<18} state.json {rep['messages']['state']:>8}   库 {rep['messages']['db']:>8}")
    for cid in rep["conversations_mismatched"][:20]:
        print(f"  对话 {cid} 的消息数不一致")
    print(("对账一致：" if rep["ok"] else "对账不一致：") + rep["summary"])


def migrate_state(argv: list[str]) -> int:
    a = _args("matrix migrate state", argv, lambda p: p.add_argument(
        "--replace", action="store_true", help="库里已有数据时先清空，再从 state.json 重新导入（回退后又要升级时）"))
    url = dbmod.resolve_url(a.store, a.data)
    assert url is not None
    if not (a.data / "state.json").exists() and not (a.data / "state.json.bak").exists():
        raise SystemExit(f"{a.data} 里没有 state.json（已经导入过的话是 state.json.imported）")
    if a.replace:
        probe = dbmod.Database(url)
        probe.init_schema()
        if not probe.is_empty():
            with probe.transaction():
                for t in ("entity", "message", "event", "run"):
                    probe.execute(f"DELETE FROM {t}")
            print(f"已清空 {dbmod.describe(url)}")
        probe.close()
    s = Store(a.data, seed=False, url=a.store)
    rep = s.backend.import_report if isinstance(s.backend, DbBackend) else None
    s.close()
    if rep is None:
        raise SystemExit("没有导入：库里已经有数据（要重新导入加 --replace）")
    _show(rep)
    return 0 if rep["ok"] else 1


def _normalized(path: Path) -> dict[str, Any]:
    """一份 state.json 经 Store._load 读进来再写出去的样子（兼容逻辑、重启语义都走一遍），用来和库比。"""
    with tempfile.TemporaryDirectory() as d:
        shutil.copy2(path, Path(d) / "state.json")
        s = Store(Path(d), seed=False, url="json")
        with s.lock:
            return json.loads(json.dumps(s.snapshot(), ensure_ascii=False, default=str))


def _strip_volatile(state: dict[str, Any]) -> dict[str, Any]:
    """两次读进来必然不同的字段：旧明文登录换成哈希时记的“最近用过”是读的那一刻。"""
    for s in (state.get("sessions") or {}).values():
        s.pop("last_seen", None)
    return state


def verify(argv: list[str]) -> int:
    a = _args("matrix migrate verify", argv, lambda p: p.add_argument(
        "--against", default=None, help="和哪份 state.json 比；默认数据目录里的 state.json.imported"))
    src = Path(a.against) if a.against else a.data / "state.json.imported"
    if not src.is_file():
        raise SystemExit(f"找不到 {src}")
    want = _strip_volatile(_normalized(src))
    s = Store(a.data, seed=False, url=a.store)
    with s.lock:
        got = _strip_volatile(json.loads(json.dumps(s.snapshot(), ensure_ascii=False, default=str)))
        rep = reconcile(want, s.backend.db) if isinstance(s.backend, DbBackend) else None
    s.close()
    if rep is not None:
        _show(rep)
    diffs = []
    for key in sorted(set(want) | set(got)):
        w, g = want.get(key), got.get(key)
        if isinstance(w, list) and isinstance(g, list):
            wi = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in w}
            gi = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in g}
            if wi != gi:
                diffs.append(f"{key}：{len(wi - gi)} 个只在 state.json、{len(gi - wi)} 个只在库里（或内容不同）")
        elif json.dumps(w, sort_keys=True) != json.dumps(g, sort_keys=True):
            diffs.append(f"{key}：内容不同")
    if diffs:
        print("按内容比对：有差别（服务导入后又跑过的话，差别是正常的新数据）")
        for d in diffs:
            print(f"  {d}")
        return 1
    print(f"按内容比对：库里的状态和 {src.name} 完全一致")
    return 0


def export_state(argv: list[str]) -> int:
    def extra(p: argparse.ArgumentParser) -> None:
        p.add_argument("--out", default=None, help="写到哪；默认数据目录里的 state.json")
        p.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    a = _args("matrix export-state", argv, extra)
    out = Path(a.out).resolve() if a.out else a.data / "state.json"
    if out.exists() and not a.force:
        raise SystemExit(f"{out} 已经存在；确认要覆盖加 --force")
    if out == a.data / "state.json":
        # Store 打开库时看到数据目录里有 state.json 会拒绝：先写到旁边，关掉库再改名
        tmp = a.data / "state.json.export"
    else:
        tmp = out
    if out.exists() and out == a.data / "state.json":
        os.replace(out, a.data / "state.json.before-export")
    s = Store(a.data, seed=False, url=a.store)
    with s.lock:
        text = json.dumps(s.snapshot(), ensure_ascii=False, indent=1)
        n_msgs = sum(len(c.messages) for c in s.conversations.values())
        n_obj = sum(len(getattr(s, attr)) for attr in ("agents", "projects", "conversations", "tasks", "users"))
    s.close()
    write_durable(tmp, text)
    if tmp != out:
        os.replace(tmp, out)
    print(f"已导出到 {out}（{n_obj} 个 Agent / 项目 / 对话 / 委托 / 用户，{n_msgs} 条消息）。"
          "回退：停服务、去掉 MATRIX_STORE（或换回旧镜像）再启动。注意：旧版本认不出哈希过的登录，所有人要重新登录一次")
    return 0


def backup_db(argv: list[str]) -> int:
    a = _args("matrix backup-db", argv, lambda p: p.add_argument("--out", required=True, help="备份文件"))
    url = dbmod.resolve_url(a.store, a.data)
    assert url is not None
    d = dbmod.Database(url)
    try:
        d.backup_to(Path(a.out).resolve())
    finally:
        d.close()
    print(f"已备份到 {a.out}")
    return 0


def main(cmd: str, argv: list[str]) -> None:
    fn = {"state": migrate_state, "verify": verify, "export-state": export_state, "backup-db": backup_db}[cmd]
    try:
        code = fn(argv)
    except (RuntimeError, dbmod.DatabaseError) as e:
        print(str(e), file=sys.stderr)
        code = 2
    raise SystemExit(code)
