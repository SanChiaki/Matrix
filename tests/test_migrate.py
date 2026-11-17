"""旧数据迁移与回退（docs/cloud-runtime.md 第 10 节）：matrix migrate workdirs / export / purge-legacy。"""

from __future__ import annotations

from pathlib import Path

from matrix import migrate
from matrix.engine import Network
from matrix.runtime import ScriptCall, ScriptedRuntime, dsh_session_dirname
from matrix.sandboxes import MemorySandboxes
from tests.test_users import users


def classic(tmp_path: Path, handler):
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], ["file.write"], actor=alice)
    conv = net.direct_chat(a.id, actor=alice)
    return net, alice, a, conv


def writes(name: str, text: str):
    def handler(call: ScriptCall) -> str:
        (call.cwd / name).write_text(text, encoding="utf-8")
        seen = sorted(p.name for p in call.cwd.iterdir() if p.is_file() and p.suffix == ".txt")
        return "看到 " + " ".join(seen)
    return handler


def test_workdirs_is_idempotent_and_keeps_the_original(tmp_path: Path):
    """建检查点：重复执行只处理还没导入的；原目录不动；已结束的委托也覆盖到。"""
    net, alice, a, conv = classic(tmp_path, writes("a.txt", "旧"))
    net.send_message(conv.id, "一", actor=alice, background=False)
    seat = conv.seats[a.id]
    r1 = migrate.workdirs(Network(ScriptedRuntime(lambda c: ""), tmp_path, seed=False))
    unit = f"seat:{conv.id}/{a.id}"
    assert r1.done == [unit]
    again = Network(ScriptedRuntime(lambda c: ""), tmp_path, seed=False)
    assert migrate.workdirs(again).done == [] and unit in migrate.workdirs(again).skipped
    ck = again.checkpoints.load(unit)
    assert [f["path"] for f in ck.files] == ["a.txt"] and all(f["stored"] for f in ck.files)
    assert (tmp_path / seat.workdir / "a.txt").read_text(encoding="utf-8") == "旧"


def test_over_limit_is_listed_not_truncated(tmp_path: Path):
    net, alice, a, conv = classic(tmp_path, writes("big.txt", "x" * 100))
    net.send_message(conv.id, "一", actor=alice, background=False)
    m = Network(ScriptedRuntime(lambda c: ""), tmp_path, seed=False)
    m.checkpoints.limit = 10
    r = migrate.workdirs(m)
    unit = f"seat:{conv.id}/{a.id}"
    assert r.over_limit == {unit: ["big.txt"]}
    assert (tmp_path / conv.seats[a.id].workdir / "big.txt").stat().st_size == 100   # 原处整份还在
    assert not m.checkpoints.load(unit).files[0]["stored"]


def test_export_brings_sandbox_products_back(tmp_path: Path):
    """旧方式写 a.txt → 迁移 → 沙箱方式写 b.txt → export → 关掉开关，旧方式看得到两批产物，会话放回状态目录续得上。"""
    net, alice, a, conv = classic(tmp_path, writes("a.txt", "旧"))
    net.send_message(conv.id, "一", actor=alice, background=False)
    migrate.workdirs(Network(ScriptedRuntime(lambda c: ""), tmp_path, seed=False))

    def sandboxed(call: ScriptCall) -> str:
        (call.cwd / "b.txt").write_text("沙箱里写的", encoding="utf-8")
        sess = call.cwd.parent / "state" / ".dsh" / "sessions" / dsh_session_dirname(
            Path(box.store.sandboxes[f"seat:{conv.id}/{a.id}"].workdir)) / call.session_id
        sess.mkdir(parents=True, exist_ok=True)
        (sess / "log.jsonl").write_text("沙箱里的会话", encoding="utf-8")
        return "ok"
    box = Network(ScriptedRuntime(sandboxed), tmp_path, seed=False, sandbox=MemorySandboxes())
    box.send_message(conv.id, "二", actor=alice, background=False)
    sid = box.store.conversations[conv.id].seats[a.id].dsh_session_id

    r = migrate.export(Network(ScriptedRuntime(lambda c: ""), tmp_path, seed=False))
    assert r.done == [f"seat:{conv.id}/{a.id}"]
    back = Network(ScriptedRuntime(writes("c.txt", "又回旧方式")), tmp_path, seed=False)
    back.send_message(conv.id, "三", actor=alice, background=False)
    reply = [m for m in back.store.conversations[conv.id].messages if m.role == "agent"][-1].text
    assert reply == "看到 a.txt b.txt c.txt"
    home = back.store.unit_state_dir("seat", f"{conv.id}/{a.id}")
    wdir = (tmp_path / conv.seats[a.id].workdir).resolve()
    assert (home / ".dsh" / "sessions" / dsh_session_dirname(wdir) / sid / "log.jsonl").read_text(encoding="utf-8") == "沙箱里的会话"
    rec = back.store.sandboxes[f"seat:{conv.id}/{a.id}"]
    assert rec.exported and not rec.imported    # 再打开沙箱方式时从原目录重新导入（含 c.txt）
    box2 = Network(ScriptedRuntime(writes("d.txt", "再进沙箱")), tmp_path, seed=False, sandbox=MemorySandboxes())
    box2.send_message(conv.id, "四", actor=alice, background=False)
    assert [m for m in box2.store.conversations[conv.id].messages if m.role == "agent"][-1].text == \
        "看到 a.txt b.txt c.txt d.txt"


def test_purge_needs_confirmation_and_complete_checkpoints(tmp_path: Path):
    net, alice, a, conv = classic(tmp_path, writes("a.txt", "旧"))
    net.send_message(conv.id, "一", actor=alice, background=False)
    m = Network(ScriptedRuntime(lambda c: ""), tmp_path, seed=False)
    migrate.workdirs(m)
    wdir = tmp_path / conv.seats[a.id].workdir
    plan = migrate.purge(m, yes=False)
    assert str(wdir) in plan.removed and (wdir / "a.txt").exists()     # 不带确认不删
    (wdir / "later.txt").write_text("迁移之后旧方式又写的", encoding="utf-8")
    blocked = migrate.purge(m, yes=True)
    assert blocked.done == [] and "later.txt" in next(iter(blocked.problems.values())) and wdir.exists()
    (wdir / "later.txt").unlink()
    done = migrate.purge(m, yes=True)
    assert done.done == [f"seat:{conv.id}/{a.id}"] and not wdir.exists()


def test_purge_skips_incomplete_checkpoints(tmp_path: Path):
    net, alice, a, conv = classic(tmp_path, writes("big.txt", "x" * 100))
    net.send_message(conv.id, "一", actor=alice, background=False)
    m = Network(ScriptedRuntime(lambda c: ""), tmp_path, seed=False)
    m.checkpoints.limit = 10
    migrate.workdirs(m)
    r = migrate.purge(m, yes=True)
    assert r.done == [] and r.problems and (tmp_path / conv.seats[a.id].workdir / "big.txt").exists()


def test_cli(tmp_path: Path, capsys):
    net, alice, a, conv = classic(tmp_path, writes("a.txt", "旧"))
    net.send_message(conv.id, "一", actor=alice, background=False)
    migrate.main(["workdirs", "--data", str(tmp_path)])
    assert "处理 1 个执行单元" in capsys.readouterr().out
    migrate.main(["purge-legacy", "--data", str(tmp_path)])
    out = capsys.readouterr()
    assert "加 --yes 才删" in out.out and (tmp_path / conv.seats[a.id].workdir / "a.txt").exists()
