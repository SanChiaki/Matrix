"""旧数据迁移与回退（docs/cloud-runtime.md 第 10 节）：

    matrix migrate workdirs [--data <目录>]                  # 给每个席位、每个委托的现有工作目录建第一份检查点（可重复、可中断）
    matrix migrate export [--data <目录>]                    # 回退：把各执行单元的最新检查点写回原来的工作目录，会话放回能续接的位置
    matrix migrate purge-legacy [--data <目录>] [--yes]      # 清理原工作目录与旧的 dsh 状态（不带 --yes 只列出会删什么）

在服务停着的时候跑（都会改 state.json）。只处理云侧 Agent 的执行单元；端侧的工作目录在用户电脑上，不归这里管。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import workdir as wd
from .checkpoints import sha_of, unpack_session
from .models import Seat, Task, seat_id
from .runtime import dsh_session_files


@dataclass
class Report:
    done: list[str] = field(default_factory=list)       # 这一次处理了的执行单元
    skipped: list[str] = field(default_factory=list)    # 已经处理过 / 不用处理的
    over_limit: dict[str, list[str]] = field(default_factory=dict)  # 超过检查点上限、只记了清单的文件（原处还在）
    problems: dict[str, str] = field(default_factory=dict)  # 没处理的与原因
    removed: list[str] = field(default_factory=list)     # purge：删了（或将要删）的路径

    def show(self, title: str) -> None:
        print(f"{title}：处理 {len(self.done)} 个执行单元，跳过 {len(self.skipped)} 个")
        for u, files in self.over_limit.items():
            print(f"  ! {u}：{len(files)} 个文件超过检查点上限，只记了清单、留在原处：{'、'.join(files[:10])}"
                  + ("…" if len(files) > 10 else ""))
        for u, why in self.problems.items():
            print(f"  ✗ {u}：{why}")
        for p in self.removed:
            print(f"  - {p}")


def units(net: Any) -> Iterator[tuple[str, str, Seat | Task, Any]]:
    """云侧 Agent 的全部执行单元（含已结束、不会再运行的委托）：（kind, id, holder, agent）。"""
    with net._lock:
        items: list[tuple[str, str, Seat | Task, Any]] = []
        for conv in net.store.conversations.values():
            for agent_id, seat in conv.seats.items():
                a = net.store.agents.get(agent_id)
                if a is not None and not a.device_runtime:
                    items.append(("seat", seat_id(conv.id, agent_id), seat, a))
        for t in net.store.tasks.values():
            a = net.store.agents.get(t.delegatee_id or "")
            if a is not None and not a.device_runtime:
                items.append(("task", t.id, t, a))
    yield from items


def workdirs(net: Any) -> Report:
    """给每个执行单元建第一份检查点（产物、别人交来的 deliveries/、dsh 会话）。已经导入过的跳过；原目录不动。"""
    r = Report()
    for kind, cid, holder, agent in units(net):
        unit = f"{kind}:{cid}"
        rec = net.store.sandboxes.get(unit)
        if rec is not None and rec.imported:
            r.skipped.append(unit)
            continue
        try:
            net._sandbox_import(kind, cid, holder, agent)   # 和第一次在沙箱方式下运行时的按需导入是同一段代码
        except OSError as e:
            r.problems[unit] = f"读不了原目录：{e}"
            continue
        ck = net.checkpoints.load(unit)
        if ck is not None and ck.skipped:
            r.over_limit[unit] = list(ck.skipped)
        r.done.append(unit)
    return r


def export(net: Any) -> Report:
    """回退（10.4）：最新检查点里的产物写回原来的工作目录（覆盖同名文件，不删别的），别人交来的产物写回委托方的 deliveries/，
    dsh 会话包解到这个执行单元的 dsh 状态目录（旧方式的 HOME），之后关掉开关旧方式能续接。只记了清单的文件导不出来，列出来。"""
    r = Report()
    for kind, cid, holder, agent in units(net):
        unit = f"{kind}:{cid}"
        rec = net.store.sandboxes.get(unit)
        ck = net.checkpoints.load(unit)
        if ck is None or rec is None:
            r.skipped.append(unit)
            continue
        dest = Path(rec.workdir)
        missing = []
        for f in ck.files:
            if not f["stored"] or not net.content.has(f["sha"], f["size"]):
                missing.append(f["path"])
                continue
            p = dest / f["path"]
            if p.is_file() and not p.is_symlink() and sha_of(p) == f["sha"]:
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(net.content.path(f["sha"]), p)
        with net._lock:   # 交给这个单元的委托产物：放回它的 deliveries/<任务>/
            given = [t for t in net.store.tasks.values() if t.result and t.result.get("files_ckpt")
                     and net._delivered_to(t) == unit]
        for t in given:
            for f in t.result["files_ckpt"]:
                if net.content.has(f["sha"], f["size"]):
                    p = dest / wd.DELIVERIES / t.id / f["path"]
                    p.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(net.content.path(f["sha"]), p)
        sess = ck.session(holder.dsh_session_id) if holder.dsh_session_id else None
        if sess is not None and net.content.has(sess["sha"], sess["size"]):
            home = net.store.unit_state_dir(kind, cid)
            home.mkdir(parents=True, exist_ok=True)
            unpack_session(net.content.path(sess["sha"]).read_bytes(), home)
        elif holder.dsh_session_id:
            r.problems[unit] = f"检查点里没有会话 {holder.dsh_session_id} 的包：旧方式下会换新会话"
        if missing:
            r.over_limit[unit] = missing
        with net._lock:
            rec.imported, rec.exported = False, True   # 再打开沙箱方式时从原目录重新导入
            net.store.save()
        r.done.append(unit)
    return r


def purge(net: Any, yes: bool) -> Report:
    """清理原工作目录与旧的 dsh 状态（阶段 0 的状态目录、共用 ~/.dsh 里这个会话的两处文件）。只删导入过、检查点完整
    （没有只记清单的文件、内容都在）、而且原目录里的产物和检查点一致的；不带 yes 只列出来。执行过就只能从备份恢复。"""
    r = Report()
    for kind, cid, holder, agent in units(net):
        unit = f"{kind}:{cid}"
        rec = net.store.sandboxes.get(unit)
        ck = net.checkpoints.load(unit)
        if rec is None or not rec.imported or ck is None:
            r.skipped.append(unit)
            continue
        if ck.skipped or any(not f["stored"] or not net.content.has(f["sha"], f["size"]) for f in ck.files):
            r.problems[unit] = "检查点不完整（有只记了清单、或内容不在的文件），不删"
            continue
        old = Path(rec.workdir)
        stored = {f["path"]: f["sha"] for f in ck.files}
        if old.is_dir():
            # 原目录里检查点没有的、或者内容不同而且比检查点新的：旧方式在迁移之后又写过，删了会丢
            newer = [p.as_posix() for p in wd.outputs(old) if p.as_posix() not in stored
                     or (stored[p.as_posix()] != sha_of(old / p) and (old / p).stat().st_mtime > ck.at)]
            if newer:
                r.problems[unit] = f"原目录里有检查点里没有的产物（{'、'.join(newer[:5])}），不删：先跑 migrate workdirs 或 export"
                continue
        targets = [p for p in (old, net.store.unit_state_dir(kind, cid)) if p.exists()]
        if holder.dsh_session_id:
            d, index = dsh_session_files(net.legacy_dsh_home, old, holder.dsh_session_id)
            targets += [p for p in (d, index) if p is not None and p.exists()]
        r.removed += [str(p) for p in targets]
        if yes:
            for p in targets:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        r.done.append(unit)
    return r


def main(argv: list[str] | None = None) -> None:
    from . import compat
    from .engine import Network
    from .runtime import ScriptedRuntime
    p = argparse.ArgumentParser(prog="matrix migrate", description="云侧执行面的旧数据迁移与回退（服务停着时跑）")
    p.add_argument("action", choices=["workdirs", "export", "purge-legacy"])
    p.add_argument("--data", default=None, help="数据目录；默认 MATRIX_DATA，没有就用 .matrix")
    p.add_argument("--yes", action="store_true", help="purge-legacy：真的删（不带只列出会删什么）")
    args = p.parse_args(argv)
    data = Path(args.data or compat.data_dir()).resolve()
    from . import db as dbmod
    if not (data / "state.json").is_file() and not dbmod.resolve_url(compat.env("STORE"), data):
        raise SystemExit(f"{data} 里没有 state.json（状态在数据库里时设好 MATRIX_STORE）")
    net = Network(ScriptedRuntime(lambda call: ""), data, seed=False)   # 不跑任何一轮，只读写状态与检查点
    if args.action == "workdirs":
        workdirs(net).show("建检查点")
    elif args.action == "export":
        export(net).show("写回原目录")
    else:
        rep = purge(net, args.yes)
        rep.show("清理原目录" if args.yes else "会删这些（加 --yes 才删）")
        if not args.yes and rep.removed:
            print("执行过就只能从备份恢复。", file=sys.stderr)
