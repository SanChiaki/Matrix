"""数据库存储（M7，docs/reference/storage.md）：首次启动自动导入 state.json、增量保存、消息只追加、事件与轮次落库、
导出回 state.json 用于回退、对账与按内容比对。

默认用 SQLite；设了 MATRIX_TEST_PG（postgresql://…，要装 psycopg）时同一组测试另在 PostgreSQL 上跑一遍。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from matrix import db as dbmod
from matrix import storage_cli
from matrix.engine import Network
from matrix.models import Message, new_id
from matrix.runtime import ScriptedRuntime
from matrix.server import create_app
from matrix.store import DICT_KINDS, ENTITY_KINDS, DbBackend, Store

PG = os.environ.get("MATRIX_TEST_PG", "")
BACKENDS = ["sqlite"] + ([PG] if PG else [])


@pytest.fixture(params=BACKENDS, ids=lambda u: "pg" if u.startswith("postgres") else u)
def url(request: pytest.FixtureRequest) -> str:
    u = request.param
    if u.startswith("postgres"):  # 每个测试从空库开始
        d = dbmod.Database(u)
        d.execute("DROP TABLE IF EXISTS entity, message, event, run, meta")
        d.close()
    return u


def _net(path: Path, url: str = "json") -> Network:
    return Network(ScriptedRuntime(lambda call: f"收到：{call.prompt[-20:]}"), path, seed=False, store_url=url)


def _chat(net: Network, c: TestClient, text: str) -> str:
    me = c.get("/api/me").json()
    home = me["home_id"]
    r = c.post(f"/api/conversations/{home}/messages", json={"text": text})
    assert r.status_code == 200, r.text
    net.wait_idle(home, timeout=10)
    return home


def _populated(tmp_path: Path) -> tuple[Path, dict]:
    """用 state.json 存储跑出一份有用户、助理、私聊消息、登录的状态。"""
    data = tmp_path / "data"
    net = _net(data)
    c = TestClient(create_app(net))
    assert c.post("/api/auth/register", json={"email": "a@example.com", "password": "password-123"}).status_code == 200
    for i in range(3):
        _chat(net, c, f"第 {i} 条消息")
    net.store.close()
    return data, json.loads((data / "state.json").read_text(encoding="utf-8"))


def test_db_kinds_cover_snapshot(tmp_path: Path):
    """新增一类状态时 ENTITY_KINDS / DICT_KINDS 也要加，否则数据库存储会悄悄丢掉它。"""
    s = Store(tmp_path, seed=True, url="json")
    s.sandboxes.clear()
    snap = s.snapshot()
    known = {k for k, *_ in ENTITY_KINDS} | {k for k, _ in DICT_KINDS}
    assert set(snap) <= known, f"数据库存储不认识：{set(snap) - known}"
    attrs = {a for _, a, _ in ENTITY_KINDS} | {a for _, a in DICT_KINDS}
    assert all(isinstance(getattr(s, a), dict) for a in attrs)


def test_first_start_imports_state_json(tmp_path: Path, url: str):
    data, raw = _populated(tmp_path)
    net = _net(data, url)
    assert not (data / "state.json").exists() and (data / "state.json.imported").exists()
    assert (data / "state.json.bak.imported").exists(), ".bak 也改名，免得回到 json 时读到旧的"
    rep = net.store.backend.import_report
    assert rep and rep["ok"], rep
    msgs = sum(len(c["messages"]) for c in raw["conversations"])
    assert rep["messages"]["db"] == msgs > 0
    c = TestClient(create_app(net))
    assert c.post("/api/auth/login", json={"email": "a@example.com", "password": "password-123"}).status_code == 200
    home = c.get("/api/me").json()["home_id"]
    assert [m["text"] for m in c.get(f"/api/conversations/{home}").json()["messages"] if m["role"] == "user"] == \
        ["第 0 条消息", "第 1 条消息", "第 2 条消息"]
    net.store.close()
    again = _net(data, url)  # 第二次启动从库里读，不再导入
    assert again.store.backend.import_report is None and again.store.loaded_from.startswith(("sqlite", "postgresql"))
    assert sum(len(c.messages) for c in again.store.conversations.values()) == msgs
    again.store.close()


def test_logged_in_users_stay_logged_in_after_import(tmp_path: Path, url: str):
    data = tmp_path / "data"
    net = _net(data)
    c = TestClient(create_app(net))
    assert c.post("/api/auth/register", json={"email": "a@example.com", "password": "password-123"}).status_code == 200
    cookie = dict(c.cookies)
    net.store.close()
    after = _net(data, url)
    c2 = TestClient(create_app(after))
    c2.cookies.update(cookie)
    assert c2.get("/api/me").status_code == 200, "导入后没人被登出"
    after.store.close()


def test_incremental_save_writes_only_changes(tmp_path: Path, url: str):
    s = Store(tmp_path, seed=True, url=url)
    conv = next(iter(s.conversations.values()))
    for i in range(2000):
        conv.messages.append(Message(id=new_id("msg"), role="user", text=f"消息 {i}" + "x" * 300))
    s.save()
    assert s.backend.last_rows >= 2000
    t0 = time.perf_counter()
    s.save()
    assert s.backend.last_rows == 0, "什么都没变就什么都不写"
    conv.messages.append(Message(id=new_id("msg"), role="user", text="新的一条"))
    s.save()
    assert s.backend.last_rows == 1
    conv.messages[5].claimed_by = "agent-x"   # 改一条旧消息：只重写它
    s.save()
    assert s.backend.last_rows == 1
    assert time.perf_counter() - t0 < 2
    conv.messages, keep = [], conv.messages[:3]   # 清空对话：整段重写
    conv.messages.extend(keep)
    s.save()
    s.close()
    again = Store(tmp_path, seed=False, url=url)
    got = again.conversations[conv.id].messages
    assert [m.text for m in got] == [m.text for m in keep] and got[0].claimed_by is None
    assert len(again.agents) == len(s.agents)
    again.close()


def test_deleted_objects_leave_the_db(tmp_path: Path, url: str):
    s = Store(tmp_path, seed=True, url=url)
    aid = next(iter(s.agents))
    del s.agents[aid]
    cid = next(iter(s.conversations))
    del s.conversations[cid]
    s.save()
    s.close()
    again = Store(tmp_path, seed=False, url=url)
    assert aid not in again.agents
    again.close()


def test_db_with_data_and_state_json_refuses(tmp_path: Path, url: str):
    data, _ = _populated(tmp_path)
    _net(data, url).store.close()
    (data / "state.json.imported").rename(data / "state.json")   # 回退期间又有了一份 state.json
    with pytest.raises(RuntimeError, match="不知道该用哪份"):
        Store(data, seed=False, url=url)
    with pytest.raises(RuntimeError, match="已经导进了数据库"):
        (data / "state.json").rename(data / "state.json.imported")
        Store(data, seed=False, url="json")


def test_export_state_round_trip(tmp_path: Path, url: str, monkeypatch: pytest.MonkeyPatch):
    data, raw = _populated(tmp_path)
    _net(data, url).store.close()
    store_arg = ["--data", str(data), "--store", "sqlite" if url == "sqlite" else url]
    assert storage_cli.verify(store_arg) == 0, "库里的和原来的 state.json 按内容一致"
    assert storage_cli.export_state(store_arg) == 0
    back = Store(data, seed=False, url="json")   # 旧版本的读法：就是 state.json
    assert back.loaded_from == "state.json"
    assert sorted(u.email for u in back.users.values()) == ["a@example.com"]
    assert sum(len(c.messages) for c in back.conversations.values()) == sum(len(c["messages"]) for c in raw["conversations"])
    with pytest.raises(SystemExit):
        storage_cli.export_state(store_arg)   # 已经有了，不加 --force 不覆盖


def test_replace_reimports(tmp_path: Path, url: str):
    data, _ = _populated(tmp_path)
    _net(data, url).store.close()
    (data / "state.json.imported").rename(data / "state.json")
    store_arg = ["--data", str(data), "--store", "sqlite" if url == "sqlite" else url]
    assert storage_cli.migrate_state([*store_arg, "--replace"]) == 0
    assert (data / "state.json.imported").exists() and not (data / "state.json").exists()


def test_events_survive_restart(tmp_path: Path, url: str):
    net = _net(tmp_path, url)
    for i in range(5):
        net.bus.emit("test.event", n=i)
    last = net.bus.last_seq
    net.store.close()
    again = _net(tmp_path, url)
    assert again.bus.last_seq >= last, "序号接着往下数，网页的 Last-Event-ID 还有效"
    old = [e for e in again.bus.since(0) if e.type == "test.event"]
    assert [e.data["n"] for e in old] == [0, 1, 2, 3, 4]
    again.bus.emit("test.event", n=5)
    assert again.bus.since(last)[-1].data["n"] == 5
    again.store.close()


def test_runs_recorded_and_interrupted_on_restart(tmp_path: Path, url: str):
    s = Store(tmp_path, seed=False, url=url)
    done = s.run_started("seat", "c/a", "a")
    s.run_ended(done)
    s.run_started("task", "t1", "b")  # 进程在这一轮中间没了
    s.close()
    again = Store(tmp_path, seed=False, url=url)
    assert again.metrics()["interrupted_runs_at_start"] == 1
    states = dict(again.backend.db.query("SELECT unit, state FROM run"))
    assert states == {"c/a": "done", "t1": "interrupted"}
    again.close()


def test_rounds_write_runs(tmp_path: Path):
    data, _ = _populated(tmp_path)
    net = _net(data, "sqlite")
    c = TestClient(create_app(net))
    assert c.post("/api/auth/login", json={"email": "a@example.com", "password": "password-123"}).status_code == 200
    _chat(net, c, "再来一条")
    rows = net.store.backend.db.query("SELECT kind, state FROM run")
    assert ("seat", "done") in rows
    net.store.close()


_WRITER = """
import sys
from pathlib import Path
from matrix.models import Message, new_id
from matrix.store import Store
s = Store(Path(sys.argv[1]), seed=True, url="sqlite")
conv = next(iter(s.conversations.values()))
i = 0
while True:
    i += 1
    conv.messages.append(Message(id=new_id("msg"), role="user", text=str(i)))
    s.save()
    print(i, flush=True)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL 只在 POSIX 上有")
def test_kill_during_db_save_keeps_confirmed_writes(tmp_path: Path):
    """确认过的写入不丢：save() 返回之后被 kill -9，重新打开那条消息还在。"""
    script = tmp_path / "writer.py"
    script.write_text(_WRITER, encoding="utf-8")
    data = tmp_path / "data"
    for _ in range(3):
        proc = subprocess.Popen([sys.executable, str(script), str(data)], stdout=subprocess.PIPE, text=True,
                                encoding="utf-8", cwd=Path(__file__).parents[1])
        assert proc.stdout is not None
        confirmed = 0
        for line in proc.stdout:
            confirmed = int(line)
            if confirmed >= 30:
                break
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        proc.stdout.close()
        s = Store(data, seed=False, url="sqlite")
        texts = [m.text for c in s.conversations.values() for m in c.messages]
        assert str(confirmed) in texts
        s.close()


def test_backend_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert dbmod.resolve_url(None, tmp_path) is None and dbmod.resolve_url("json", tmp_path) is None
    assert dbmod.resolve_url("sqlite", tmp_path) == f"sqlite:{tmp_path / 'matrix.db'}"
    assert dbmod.resolve_url("sqlite:x/y.db", tmp_path) == f"sqlite:{tmp_path / 'x' / 'y.db'}"
    assert dbmod.describe("postgresql://u:secret@db:5432/matrix") == "postgresql://u@db:5432/matrix"
    with pytest.raises(dbmod.DatabaseError):
        dbmod.resolve_url("mysql://x", tmp_path)
    monkeypatch.setenv("MATRIX_STORE", "sqlite")
    s = Store(tmp_path, seed=False)
    assert isinstance(s.backend, DbBackend) and (tmp_path / "matrix.db").exists()
    assert s.metrics()["backend"] == "sqlite"
    s.close()


def test_round_survives_run_ledger_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """轮次台账只是记录：它写不了（库一时不通）时这一轮照跑、照出队，席位不会一直算“在跑”。"""
    data, _ = _populated(tmp_path)
    net = _net(data, "sqlite")
    c = TestClient(create_app(net))
    assert c.post("/api/auth/login", json={"email": "a@example.com", "password": "password-123"}).status_code == 200

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("database is unreachable")
    monkeypatch.setattr(net.store.backend, "run_started", boom)
    home = _chat(net, c, "台账坏了也要回")
    assert net.store.conversations[home].messages[-1].role == "agent"
    assert not net._running, "出队照常"
    monkeypatch.undo()
    monkeypatch.setattr(net.store.backend, "run_ended", boom)
    home = _chat(net, c, "结束时写不了也一样")
    assert net.store.conversations[home].messages[-1].role == "agent" and not net._running
    net.store.close()


@pytest.mark.skipif(not PG, reason="要一个真的 PostgreSQL（MATRIX_TEST_PG）")
def test_pg_reconnects_after_connection_loss(tmp_path: Path):
    """库重启、切换、网络抖动之后连接没了：下一次保存重连并把那个事务整个重做，不用重启服务。"""
    d = dbmod.Database(PG)
    d.execute("DROP TABLE IF EXISTS entity, message, event, run, meta")
    s = Store(tmp_path, seed=False, url=PG)
    s.sandbox_orphans["before"] = 1.0
    s.save()
    d.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
              "WHERE datname = current_database() AND pid <> pg_backend_pid()")
    s.sandbox_orphans["after"] = 2.0
    s.save()   # 第一次写会撞上断掉的连接：重连、重做
    rid = s.run_started("seat", "c/a", "a")
    s.run_ended(rid)
    s.close()
    d.close()
    again = Store(tmp_path, seed=False, url=PG)
    assert again.sandbox_orphans == {"before": 1.0, "after": 2.0}
    again.close()
