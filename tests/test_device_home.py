"""端侧私有目录（docs/device-runtimes.md 第 4 节）：留在用户电脑上，由连接器挂载与写回，服务器不碰。"""
import base64
import threading
from pathlib import Path

import pytest

from matrix.connector.client import HOMES, Connector, _Run
from matrix.connector.drivers.base import RunOutcome, RunSpec
from matrix.device import FILE_FETCH, RUN_REQUEST, RUN_RESULT, DeviceRuntime, MemoryLink
from matrix.engine import Network
from matrix.runtime import ADVISORY, ENFORCED, ScriptedRuntime
from tests.test_device import connector, hello
from tests.test_users import joint, users


# ================= 服务器：只告诉连接器怎么挂载，自己不挂载 =================
def device_net(tmp_path: Path, file_write: str = ENFORCED):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], ["web.research", "file.write"], actor=alice)
    (net.agent_home(a.id) / "server-copy.md").write_text("不该出现在端侧", encoding="utf-8")
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None),
                       hello("dev", enforcement={"web.research": ENFORCED, "file.write": file_write}))
    seen: list[dict] = []

    def script(msg, deliver):
        conv_id, _, agent_id = msg["context_id"].partition("/")
        seat = net.store.conversations[conv_id].seats[agent_id]
        wd = net.store.root / seat.workdir
        seen.append({"home": msg["home"], "server_home": (wd / "agent-home").exists(),
                     "server_group": (wd / "project").exists(), "materials": [m["path"] for m in msg["materials"]]})
        deliver({"type": RUN_RESULT, "run_id": msg["run_id"], "session_id": "s", "final_text": "ok"})

    connector(rt, script=script)
    a.device_runtime = "dev"
    net.register_runtime("dev", rt)
    return net, alice, bob, a, seen


def test_device_home_is_mounted_by_the_connector_not_the_server(tmp_path):
    """端侧 Agent：服务器不把 agents/<id>/ 拷进服务器侧工作目录，只在 run.request 里说明挂载方式。"""
    net, alice, _, a, seen = device_net(tmp_path)
    net.send_message(net.direct_chat(a.id, actor=alice).id, "记一下", actor=alice, background=False)
    g = net.create_project("G", "", [a.id], actor=alice)
    net.publish_file(g.id, "brief.md", "档案".encode(), agent_id=None)
    net.send_message(net.project_chat(g.id, actor=alice).id, "@A 看看档案", actor=alice, background=False)
    assert [s["home"] for s in seen] == ["rw", "rw"]
    assert not any(s["server_home"] or s["server_group"] for s in seen)
    assert seen[1]["materials"] == ["project/brief.md"]  # 项目文件照常随 run.request 下发


def test_device_home_is_read_only_without_enforced_file_write(tmp_path):
    """端侧不能强制 file.write 时它不进有效权限（不变量 1），私有目录只读挂载。"""
    net, alice, _, a, seen = device_net(tmp_path, file_write=ADVISORY)
    net.send_message(net.direct_chat(a.id, actor=alice).id, "记一下", actor=alice, background=False)
    assert seen[-1]["home"] == "ro"


def test_device_home_not_mounted_in_joint_group_by_default(tmp_path):
    """不变量 16：联合项目里端侧私有目录同样默认不挂载，所有者打开后才挂载。"""
    net, alice, bob, a, seen = device_net(tmp_path)
    g, chat = joint(net, bob, "alice@x.io", alice)
    assert a.id in g.member_ids  # 第一个 Agent 是助理：人进项目时自动加入
    net.send_message(chat.id, "@A 1", actor=bob, background=False)
    net.set_home_mount(g.id, a.id, True, actor=alice)
    net.send_message(chat.id, "@A 2", actor=bob, background=False)
    assert [s["home"] for s in seen] == [None, "rw"]


# ================= 连接器：本机挂载与写回 =================
class Driver:
    """假 harness：按 body 操作这一轮的工作目录。"""
    name = "fake"

    def __init__(self, body):
        self.body = body

    def run(self, spec, ctx, cancel):
        self.body(spec)
        return RunOutcome(session_id="s", final_text="ok")


def run_once(c: Connector, agent: str, context: str, home: str | None) -> dict:
    if not hasattr(c, "sent"):  # 并行运行共用一个连接器：结果按 run_id 取
        c.sent = []
        c.send = c.sent.append
    run = _Run(c, f"run-{context}")
    run.home = home
    spec = RunSpec(run_id=run.run_id, agent_id=agent, context_id=context, prompt="p",
                   workdir=c.workdir_of(f"{agent}/{context}"), authority=[], tool_names=[])
    run.workdir = spec.workdir
    c._execute(run, spec)
    assert not (spec.workdir / "agent-home").exists()  # 运行后不留副本
    return next(m for m in c.sent if m["type"] == RUN_RESULT and m["run_id"] == run.run_id)


def test_connector_home_persists_across_contexts(tmp_path):
    """rw：一次运行写进 agent-home/ 的东西，换一个对话还在；它在 <root>/.homes/ 下，不在网络上。"""
    def body(spec):
        home = spec.workdir / "agent-home"
        if spec.context_id == "c1":
            (home / "notes.md").write_text("第一次的笔记", encoding="utf-8")
        else:
            spec.workdir.joinpath("seen.txt").write_text((home / "notes.md").read_text(encoding="utf-8"), encoding="utf-8")

    c = Connector(Driver(body), tmp_path)
    assert run_once(c, "a", "c1", "rw")["ok"]
    run_once(c, "a", "c2", "rw")
    assert (tmp_path / HOMES / "a" / "notes.md").read_text(encoding="utf-8") == "第一次的笔记"
    assert (c.workdir_of("a/c2") / "seen.txt").read_text(encoding="utf-8") == "第一次的笔记"
    c.close()


def test_connector_read_only_home_discards_changes(tmp_path):
    """ro：能读到，改动不写回；None：不挂载，并清掉上次残留的副本。"""
    seen = []

    def body(spec):
        home = spec.workdir / "agent-home"
        seen.append(home.exists() and (home / "keep.md").read_text(encoding="utf-8"))
        if home.exists():
            (home / "keep.md").write_text("被改了", encoding="utf-8")

    c = Connector(Driver(body), tmp_path)
    (tmp_path / HOMES / "a").mkdir(parents=True)
    (tmp_path / HOMES / "a" / "keep.md").write_text("原样", encoding="utf-8")
    run_once(c, "a", "c1", "ro")
    stale = c.workdir_of("a/c2") / "agent-home"
    stale.mkdir(parents=True)
    (stale / "keep.md").write_text("残留", encoding="utf-8")
    run_once(c, "a", "c2", None)
    assert seen == ["原样", False]
    assert (tmp_path / HOMES / "a" / "keep.md").read_text(encoding="utf-8") == "原样"
    c.close()


def test_connector_home_merge_keeps_both_on_conflict(tmp_path):
    """不变量 15（端侧）：同一 Agent 两次并行运行改同一个文件，后写回的另存为冲突副本。"""
    gate = threading.Barrier(2, timeout=5)

    def body(spec):
        gate.wait()
        (spec.workdir / "agent-home" / "notes.md").write_text(f"{spec.context_id} 的笔记", encoding="utf-8")
        if spec.context_id == "c2":
            import time
            time.sleep(0.1)  # 让 c1 先写回

    c = Connector(Driver(body), tmp_path)
    c.sent = []
    c.send = c.sent.append  # 两个线程开始前就装好，避免各自装一份
    threads = [threading.Thread(target=run_once, args=(c, "a", ctx, "rw")) for ctx in ("c1", "c2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    home = tmp_path / HOMES / "a"
    assert (home / "notes.md").read_text(encoding="utf-8") == "c1 的笔记"
    conflicts = [p for p in home.iterdir() if p.name.startswith("notes.conflict-")]
    assert len(conflicts) == 1 and conflicts[0].read_text(encoding="utf-8") == "c2 的笔记"
    c.close()


def test_connector_refuses_to_expose_homes(tmp_path):
    """私有目录不经过网络：工作目录引用碰不到 .homes/，file.fetch 不回传 agent-home/ 与 group/，材料不能写进 agent-home/。"""
    c = Connector(Driver(lambda spec: None), tmp_path)
    for bad in (".homes/a", "a/.homes", ".x"):
        with pytest.raises(Exception):
            c.workdir_of(bad)
    for bad in ("", "../x", "a/b", ".homes", "C:", "C:x"):
        with pytest.raises(Exception):
            c.home_of(bad)
    with pytest.raises(Exception):
        c._write_materials(c.workdir_of("a/c"), [{"path": "agent-home/x.md", "data": ""}])

    wd = c.workdir_of("a/c")
    (wd / "agent-home").mkdir(parents=True)
    (wd / "agent-home" / "secret.md").write_text("私密", encoding="utf-8")
    (wd / "project").mkdir()
    (wd / "project" / "brief.md").write_text("档案", encoding="utf-8")
    (wd / "out.md").write_text("产物", encoding="utf-8")
    try:
        (wd / "link.md").symlink_to(wd / "agent-home" / "secret.md")
    except OSError:  # Windows 没开开发者模式时不能建符号链接：其余检查照做
        pass
    run = _Run(c, "run-f")
    run.workdir = wd
    c._runs["run-f"] = run
    replies: list[dict] = []
    c.send = replies.append
    for path in ("agent-home/secret.md", "project/brief.md", "link.md", "out.md"):
        c._fetch({"type": FILE_FETCH, "run_id": "run-f", "call_id": path, "path": path})
    got = {r["reply_to"]: r for r in replies}
    assert all("error" in got[p] for p in ("agent-home/secret.md", "project/brief.md", "link.md"))
    assert base64.b64decode(got["out.md"]["data"]) == "产物".encode()
    c.close()


def test_run_request_home_reaches_the_driver(tmp_path):
    """run.request 的 home 字段经 _start 生效；未知取值按不挂载处理。"""
    seen, done = [], threading.Event()

    def body(spec):
        seen.append((spec.workdir / "agent-home").exists())
        if len(seen) == 2:
            done.set()

    c = Connector(Driver(body), tmp_path)
    c.send = lambda payload: None
    for i, home in enumerate(("rw", "everything")):
        c._start({"type": RUN_REQUEST, "run_id": f"r{i}", "agent_id": "a", "context_id": f"c{i}", "prompt": "p",
                  "workdir_ref": f"a/c{i}", "authority": [], "tool_names": [], "home": home})
    assert done.wait(5)
    assert sorted(seen) == [False, True]
    c.close()
