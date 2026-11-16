"""云侧执行面的沙箱方式（docs/cloud-runtime.md 阶段 1a）：假后端（MemorySandboxes）+ ScriptedRuntime，确定性、不起进程。

覆盖：启动令牌只能用一次；一条沙箱连接只收得到自己执行单元的运行与材料；容量不够时任务进 WAITING；委托交付的产物
仍进委托方工作目录；产物检查点与人浏览；沙箱重建后产物与会话包恢复；对账的三种情况；旧数据第一次运行时导入；
客户端报来的内容不可信（路径、大小、sha）；默认关闭时什么都不变。私有目录的三方合并与来源标签两种通道都跑的测试在
test_flow.py、test_users.py（不变量 15）、test_network.py（不变量 8）。"""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from matrix.checkpoints import ContentStore, Uploads, pack_session, unpack_session
from matrix.connector.sandbox import SandboxConnector
from matrix.device import (FEATURE_MATERIALS_CACHE, HELLO_OK, MATERIAL_FETCH, PROTOCOL, DeviceRuntime, MemoryLink,
                           capabilities_from_hello)
from matrix.engine import Network
from matrix.models import TaskStatus
from matrix.runtime import RunRequest, ScriptCall, ScriptedRuntime, dsh_session_dirname, material_bytes
from matrix.sandboxes import MemorySandboxes, SandboxHub, local_argv, sandbox_env, SandboxHost
from matrix.server import create_app
from tests.test_users import users


def build(tmp_path: Path, handler, **limits):
    sb = MemorySandboxes()
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False, sandbox=sb, sandbox_limits=limits or None)
    alice, = users(net, "alice@x.io")
    return net, sb, alice


def unit_of(net: Network, conv_id: str, agent_id: str) -> str:
    return f"seat:{conv_id}/{agent_id}"


# ================= 默认关闭 =================
def test_default_is_off_and_nothing_changes(tmp_path: Path):
    """开关默认关：云侧 Agent 照旧是服务的子进程，工作目录在数据目录里，没有执行面目录树与台账。"""
    seen: list[Path] = []
    net = Network(ScriptedRuntime(lambda c: seen.append(c.cwd) or "ok"), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    net.send_message(net.direct_chat(a.id, actor=alice).id, "hi", actor=alice, background=False)
    assert net.sandbox is None and net.runtime_for(a) is net.runtime
    assert seen and tmp_path in seen[0].parents and "conversations" in seen[0].parts
    assert not (tmp_path / "exec").exists() and not net.store.sandboxes
    assert "sandboxes" not in json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))


# ================= 身份 =================
def test_boot_token_is_one_time_and_expires():
    hub = SandboxHub()
    tok = hub.issue("seat:c/a", 1)
    g = hub.redeem(tok)
    assert g is not None and g.unit == "seat:c/a" and g.credential
    assert hub.redeem(tok) is None                      # 用过即废
    assert hub.by_credential(g.credential).unit == "seat:c/a"
    hub.issue("seat:c/a", 2)                             # 重建：旧凭证随之作废
    assert hub.by_credential(g.credential) is None
    old = hub.issue("seat:c/b", 1)
    hub._boot = {k: (u, gen, 0.0) for k, (u, gen, _) in hub._boot.items()}  # 过期
    assert hub.redeem(old) is None


def test_sandbox_endpoint_takes_boot_token_once(tmp_path: Path):
    """沙箱端点不是设备：不认设备令牌；启动令牌换到凭证后再用一次就被拒；凭凭证可以重连。"""
    net, sb, alice = build(tmp_path, lambda c: "ok")
    tok = net.sandbox_hub.issue("seat:c1/a1", 1)
    client = TestClient(create_app(net))
    hello = {"type": HELLO_OK, "protocol": PROTOCOL, "runtime": "dsh", "capabilities": {"name": "dsh"}}
    with client.websocket_connect(f"/api/sandbox/connect?token={tok}") as ws:
        first = ws.receive_json()
        assert first["unit"] == "seat:c1/a1" and first["credential"]
        ws.send_json(hello)
        time.sleep(0.1)
        assert net.sandbox_hub.live("seat:c1/a1") is not None
        assert net.store.devices == {}   # 不出现在“设备”里
    with pytest.raises(Exception):
        with client.websocket_connect(f"/api/sandbox/connect?token={tok}") as ws:
            ws.receive_json()
    with client.websocket_connect(f"/api/sandbox/connect?credential={first['credential']}") as ws:
        assert ws.receive_json()["unit"] == "seat:c1/a1"


def test_connection_only_serves_its_own_unit(tmp_path: Path):
    """不变量 10：一条沙箱连接只接受派给它那个执行单元的运行，只能取它这一轮清单里的材料。"""
    info = capabilities_from_hello({"type": HELLO_OK, "protocol": PROTOCOL, "runtime": "dsh", "features": [FEATURE_MATERIALS_CACHE],
                                    "capabilities": {"name": "dsh"}})
    sent: list[dict] = []
    rt = DeviceRuntime(MemoryLink(serve=lambda m: sent.append(m)), info, unit="seat:c/a")
    res = rt.run(RunRequest(agent_id="b", context_id="c/b", prompt="p", cwd=Path("."), authority=[], unit="seat:c/b",
                            timeout_s=1))
    assert not res.ok and "seat:c/a" in res.error and not [m for m in sent if m["type"] == "run.request"]

    # 两个执行单元各一个沙箱：一个单元的材料，另一个单元的连接取不到
    other = material_bytes("project/secret.md", b"secret")

    def handler(call: ScriptCall) -> str:
        return "ok"
    net, sb, alice = build(tmp_path, handler)
    a = net.create_agent("A", "a", [], [], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=alice)
    for x in (a, b):
        net.send_message(net.direct_chat(x.id, actor=alice).id, "hi", actor=alice, background=False)
    assert len(sb.started) == 2 and len(set(sb.started)) == 2
    rt_a = net.sandbox_hub.live(sb.started[0])
    rt_a._blobs["run-x"] = {other.sha: other}  # 别的轮次（假设）的材料
    reply = rt_a.on_message({"type": MATERIAL_FETCH, "run_id": "run-y", "call_id": "c1", "sha": other.sha, "offset": 0})
    assert "error" in reply
    reply = rt_a.on_message({"type": "tool.call", "run_id": "run-x", "call_id": "c2", "name": "list_members", "args": {}})
    assert reply["is_error"]  # 不认识的轮次：没有执行上下文


# ================= 运行、材料、检查点 =================
def test_round_runs_in_sandbox_with_materials_and_checkpoint(tmp_path: Path):
    """工作目录在执行面目录树里（不在数据目录的 conversations/ 里）；项目文件、技能经材料清单下发；产物进检查点，
    人浏览与下载读检查点；dsh 的事件存在控制面这边。"""
    seen: dict = {}

    def handler(call: ScriptCall) -> str:
        seen["cwd"] = call.cwd
        seen["project"] = (call.cwd / "project" / "brief.md").read_text(encoding="utf-8")
        (call.cwd / "report.md").write_text(f"第 {call.turn} 版", encoding="utf-8")
        (call.cwd / "node_modules").mkdir(exist_ok=True)
        (call.cwd / "node_modules" / "x.js").write_text("x", encoding="utf-8")
        return "ok"
    net, sb, alice = build(tmp_path, handler)
    a = net.create_agent("A", "a", [], ["file.write"], actor=alice)
    g = net.create_project("G", "", [a.id], actor=alice)
    (net.store.project_dir(g.id) / "brief.md").write_text("目标", encoding="utf-8")
    chat = net.project_chat(g.id, actor=alice)
    net.send_message(chat.id, "@A 写报告", actor=alice, background=False)
    net.send_message(chat.id, "@A 再改", actor=alice, background=False)
    assert seen["project"] == "目标"
    assert (tmp_path / "exec") in seen["cwd"].parents
    assert not (tmp_path / chat.seats[a.id].workdir / "report.md").exists()
    listing = net.workdir_files("seat", f"{chat.id}/{a.id}", actor=alice)
    assert [f["path"] for f in listing["files"]] == ["report.md"]   # 依赖目录不算产物
    path, name = net.workdir_download("seat", f"{chat.id}/{a.id}", "report.md", actor=alice)
    assert name == "report.md" and path.read_text(encoding="utf-8") == "第 2 版"
    unit = unit_of(net, chat.id, a.id)
    log = tmp_path / "run-logs" / "seats" / chat.id / a.id / "dsh-events.jsonl"
    assert log.is_file() and any(json.loads(x)["type"] == "final" for x in log.read_text(encoding="utf-8").splitlines())
    assert net.store.sandboxes[unit].desired == "running"
    # 台账写进 state.json，重新加载读得回来（旧 state.json 没有这一项也能读，见 test_default_is_off_and_nothing_changes）
    again = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    assert again.store.sandboxes[unit].workdir == net.store.sandboxes[unit].workdir


def test_delegation_delivers_checkpoint_files_into_delegator_workdir(tmp_path: Path):
    """委托交付在沙箱方式下不退化：执行者的产物（检查点）作为材料放进委托方工作目录的 deliveries/<任务>/，
    孙任务的产物不越级，委托方能读到、能集成。"""
    got: dict = {}

    def handler(call: ScriptCall) -> str:
        cwd = Path(call.cwd)
        if call.agent_id == lead.id:
            if call.turn == 1:
                call.tool("delegate", agent="Eng", mission="实现 pkg", expected_outcome="代码")
                return "已委托"
            d = cwd / "deliveries"
            got["lead"] = sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()) if d.is_dir() else []
            return "收到"
        if call.agent_id == eng.id:
            if call.turn == 1:
                (cwd / "pkg").mkdir(exist_ok=True)
                (cwd / "pkg" / "a.py").write_text("A = 1", encoding="utf-8")
                call.tool("delegate", agent="Helper", mission="写文档", expected_outcome="doc")
                return "等 Helper"
            sub = next((cwd / "deliveries").iterdir())
            (cwd / "docs.md").write_text((sub / "doc.md").read_text(encoding="utf-8"), encoding="utf-8")
            got["delegation"] = json.loads((cwd / "DELEGATION.json").read_text(encoding="utf-8"))["mission"]
            return "实现完成"
        if call.agent_id == helper.id:
            (cwd / "doc.md").write_text("# doc", encoding="utf-8")
            return "文档完成"
        return "ok"
    net, sb, alice = build(tmp_path, handler)
    lead = net.create_agent("Lead", "lead", [], ["delegate", "file.write"], actor=alice)
    eng = net.create_agent("Eng", "eng", [], ["file.write", "delegate"], actor=alice)
    helper = net.create_agent("Helper", "helper", [], ["file.write"], actor=alice)
    g = net.create_project("开发组", "", [lead.id, eng.id, helper.id], actor=alice)
    chat = net.project_chat(g.id, actor=alice)
    net.send_message(chat.id, "@Lead 实现 pkg", actor=alice, background=False)
    net.wait_quiet()
    t_eng = next(t for t in net.store.tasks.values() if t.delegatee_id == eng.id)
    t_help = next(t for t in net.store.tasks.values() if t.delegatee_id == helper.id)
    assert got["delegation"] == "实现 pkg"
    assert t_eng.result["files"] == ["docs.md", "pkg/a.py"]
    assert got["lead"] == [f"{t_eng.id}/docs.md", f"{t_eng.id}/pkg/a.py"]   # 孙任务的不在
    assert not (tmp_path / "tasks" / t_eng.id).exists()                    # 数据目录里没有任务的工作目录
    assert t_help.result["files"] == ["doc.md"]


def test_send_and_publish_read_through_the_client(tmp_path: Path):
    """send_file / publish_file 在沙箱方式下经客户端回读（目录打成 zip），不读执行面的磁盘。"""
    def handler(call: ScriptCall) -> str:
        (call.cwd / "site").mkdir(exist_ok=True)
        (call.cwd / "site" / "index.html").write_text("<p>hi</p>", encoding="utf-8")
        (call.cwd / "r.md").write_text("报告", encoding="utf-8")
        call.tool("send_file", path="r.md")
        call.tool("publish_file", path="site")
        return "发了"
    net, sb, alice = build(tmp_path, handler)
    a = net.create_agent("A", "a", [], ["file.write"], actor=alice)
    g = net.create_project("G", "", [a.id], actor=alice)
    chat = net.project_chat(g.id, actor=alice)
    net.send_message(chat.id, "@A 发", actor=alice, background=False)
    reply = [m for m in chat.messages if m.role == "agent"][-1]
    assert [x["name"] for x in reply.attachments] == ["r.md"]
    z = zipfile.ZipFile(io.BytesIO((net.store.project_dir(g.id) / "site.zip").read_bytes()))
    assert "site/index.html" in z.namelist()


# ================= 容量 =================
def test_full_capacity_means_waiting_not_failure(tmp_path: Path):
    """容量不够时是等待：委托进 WAITING、不判失败；位置腾出来后（委托方这一轮结束、它的空闲沙箱被停掉）接着跑。"""
    states: list = []

    def handler(call: ScriptCall) -> str:
        if call.agent_id == lead.id:
            if call.turn == 1:
                call.tool("delegate", agent="B", mission="做", expected_outcome="结果")
                t = next(iter(net.store.tasks.values()))
                states.append(t.status)
                return "已委托"
            return "收到"
        return "B 的结果"
    net, sb, alice = build(tmp_path, handler, per_user=1)
    lead = net.create_agent("Lead", "lead", [], ["delegate"], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=alice)
    g = net.create_project("G", "", [lead.id, b.id], actor=alice)
    chat = net.project_chat(g.id, actor=alice)
    net.send_message(chat.id, "@Lead 委托", actor=alice, background=False)
    net.wait_quiet()
    t = next(iter(net.store.tasks.values()))
    assert states == [TaskStatus.WAITING]
    assert t.status in (TaskStatus.IN_REVIEW, TaskStatus.COMPLETED) and t.error is None
    assert t.result["report"] == "B 的结果"
    live = [u for u, r in net.store.sandboxes.items() if r.desired == "running"]
    assert len(live) <= 1                                       # 每用户上限 1：同一时刻只有一个沙箱


# ================= 沙箱重建 =================
def test_rebuilt_sandbox_restores_products_and_session(tmp_path: Path):
    """沙箱（连目录）没了：下一轮从检查点恢复产物与 dsh 会话包，续得上。会话包是不透明的块，只存最新两份。"""
    seen: list[dict] = []

    def handler(call: ScriptCall) -> str:
        state = call.cwd.parent / "state"   # 内存后端的布局：单元目录下的 work/ 与 state/
        sess = state / ".dsh" / "sessions" / dsh_session_dirname(call.cwd) / call.session_id
        seen.append({"note": (call.cwd / "note.txt").read_text(encoding="utf-8") if (call.cwd / "note.txt").exists() else None,
                     "memory": (sess / "log.jsonl").read_text(encoding="utf-8") if (sess / "log.jsonl").exists() else None,
                     "resumed": call.resumed})
        sess.mkdir(parents=True, exist_ok=True)
        (sess / "log.jsonl").write_text(f"记住编号 K-1（第 {call.turn} 轮）", encoding="utf-8")
        (sess / "session.lock").write_text("lock", encoding="utf-8")
        (call.cwd / "note.txt").write_text(f"第 {call.turn} 轮", encoding="utf-8")
        return "ok"
    net, sb, alice = build(tmp_path, handler)
    a = net.create_agent("A", "a", [], ["file.write"], actor=alice)
    conv = net.direct_chat(a.id, actor=alice)
    net.send_message(conv.id, "一", actor=alice, background=False)
    unit = next(iter(net.store.sandboxes))
    ck = net.checkpoints.load(unit)
    assert ck.sessions and [f["path"] for f in ck.files] == ["note.txt"]
    sb.destroy(unit)                                         # 沙箱与它的目录都没了
    assert not (tmp_path / "exec" / "units" / "seats").joinpath(*unit.split(":")[1].split("/")).exists()
    net.send_message(conv.id, "二", actor=alice, background=False)
    assert seen[1] == {"note": "第 1 轮", "memory": "记住编号 K-1（第 1 轮）", "resumed": True}
    assert net.store.sandboxes[unit].generation == 2
    net.send_message(conv.id, "三", actor=alice, background=False)
    assert len(net.checkpoints.load(unit).sessions) == 2     # 只存最新两份


# ================= 对账 =================
def test_reconcile_three_cases(tmp_path: Path):
    """对账：没有记录的沙箱结束掉（刚起的有宽限期）；代数过期的结束掉；记录是运行中而沙箱不在的，
    在途的那一轮按传输错误结束（云侧委托判失败、可续接，不变量 12），记录改成已停。"""
    gate, started = threading.Event(), threading.Event()

    def handler(call: ScriptCall) -> str:
        if call.agent_id == b.id:
            started.set()
            gate.wait(5)
            return "B"
        if call.turn == 1:
            call.tool("delegate", agent="B", mission="做", expected_outcome="结果")
        return "ok"
    net, sb, alice = build(tmp_path, handler)
    lead = net.create_agent("Lead", "lead", [], ["delegate"], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=alice)
    g = net.create_project("G", "", [lead.id, b.id], actor=alice)
    chat = net.project_chat(g.id, actor=alice)
    sb.adopt("seat:orphan/x", generation=1, started_at=time.time())
    acts = net.reconcile_sandboxes()
    assert acts["stopped"] == []                              # 刚起的：宽限期内不动
    acts = net.reconcile_sandboxes(now=time.time() + 120)
    assert "seat:orphan/x" in acts["stopped"]                 # 1. 没有记录

    net.send_message(chat.id, "@Lead 委托", actor=alice)
    assert started.wait(5)
    t = next(iter(net.store.tasks.values()))
    unit = f"task:{t.id}"
    sb.adopt(unit, generation=99, started_at=0)              # 2. 代数过期（同一单元冒出来一个别的代数）
    acts = net.reconcile_sandboxes(now=time.time() + 120)
    assert unit in acts["stopped"] and unit in acts["lost"]   # 3. 记录是运行中而（这一代的）沙箱不在
    gate.set()
    net.wait_quiet()
    assert t.status == TaskStatus.FAILED and "TRANSPORT" in (t.error or "")
    assert net.store.sandboxes[unit].desired == "stopped"


def test_idle_stop_and_retention_destroy(tmp_path: Path):
    """空闲超时停沙箱（目录保留）；执行单元结束（Agent 删除）后过保留期销毁。"""
    net, sb, alice = build(tmp_path, lambda c: "ok", idle_s=10, retain_s=100)
    a = net.create_agent("A", "a", [], [], actor=alice)
    conv = net.direct_chat(a.id, actor=alice)
    net.send_message(conv.id, "hi", actor=alice, background=False)
    unit = next(iter(net.store.sandboxes))
    now = time.time()
    assert net.reconcile_sandboxes(now=now + 20)["idle"] == [unit]
    assert sb.status(unit).state == "stopped" and net.store.sandboxes[unit].desired == "stopped"
    net.delete_agent(a.id, actor=alice)
    net.reconcile_sandboxes(now=now + 30)
    assert net.store.sandboxes[unit].ended_at is not None
    assert net.reconcile_sandboxes(now=now + 200)["destroyed"] == [unit]
    assert sb.status(unit).state == "absent" and unit not in net.store.sandboxes


# ================= 旧数据 =================
def test_first_sandbox_run_imports_old_workdir_and_session(tmp_path: Path):
    """打开开关后第一次在沙箱方式下运行：旧工作目录的产物导入检查点、恢复进沙箱；阶段 0 状态目录里的 dsh 会话打成包搬过来。
    原目录不删。"""
    def old(call: ScriptCall) -> str:
        (call.cwd / "old.md").write_text("旧产物", encoding="utf-8")
        return "ok"
    net = Network(ScriptedRuntime(old), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], ["file.write"], actor=alice)
    conv = net.direct_chat(a.id, actor=alice)
    net.send_message(conv.id, "一", actor=alice, background=False)
    seat = conv.seats[a.id]
    oldwd = (tmp_path / seat.workdir).resolve()
    sid = seat.dsh_session_id
    state = net.store.unit_state_dir("seat", f"{conv.id}/{a.id}")   # 阶段 0 的状态目录里的会话
    d = state / ".dsh" / "sessions" / dsh_session_dirname(oldwd) / sid
    d.mkdir(parents=True)
    (d / "log.jsonl").write_text("旧会话", encoding="utf-8")

    seen: dict = {}

    def new(call: ScriptCall) -> str:
        sess = call.cwd.parent / "state" / ".dsh" / "sessions" / dsh_session_dirname(Path(net2.store.sandboxes[
            f"seat:{conv.id}/{a.id}"].workdir)) / call.session_id / "log.jsonl"
        seen.update(old=(call.cwd / "old.md").read_text(encoding="utf-8"), session=call.session_id,
                    memory=sess.read_text(encoding="utf-8") if sess.exists() else None)
        return "ok"
    net2 = Network(ScriptedRuntime(new), tmp_path, seed=False, sandbox=MemorySandboxes())
    net2.send_message(conv.id, "二", actor=alice, background=False)
    assert seen == {"old": "旧产物", "session": sid, "memory": "旧会话"}
    assert (oldwd / "old.md").is_file()                                      # 原目录不删
    rec = net2.store.sandboxes[f"seat:{conv.id}/{a.id}"]
    assert rec.imported and rec.workdir == str(oldwd)                        # 逻辑路径沿用旧的绝对路径


def test_missing_old_session_starts_new(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda c: "ok"), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    conv = net.direct_chat(a.id, actor=alice)
    net.send_message(conv.id, "一", actor=alice, background=False)
    old_sid = conv.seats[a.id].dsh_session_id
    resumed: list[bool] = []
    net2 = Network(ScriptedRuntime(lambda c: resumed.append(c.resumed) or "ok"), tmp_path, seed=False,
                   sandbox=MemorySandboxes())
    net2.legacy_dsh_home = tmp_path / "no-home"
    net2.send_message(conv.id, "二", actor=alice, background=False)
    assert resumed == [False] and net2.store.conversations[conv.id].seats[a.id].dsh_session_id != old_sid


# ================= 不可信的输入 =================
def test_home_changes_from_client_are_validated(tmp_path: Path):
    """客户端报的私有目录改动：越界路径、符号链接、没交上来的内容、超过上限的一律不收。"""
    net, sb, alice = build(tmp_path, lambda c: "ok")
    stage = tmp_path / "stage" / "agent-home"
    stage.mkdir(parents=True)
    (stage / "keep.md").write_text("keep", encoding="utf-8")
    good = net.content.put_bytes(b"good")
    net._apply_home_changes(stage, {"written": [
        {"path": "../escape.md", "sha": good, "size": 4},
        {"path": "/etc/x", "sha": good, "size": 4},
        {"path": "missing.md", "sha": "0" * 64, "size": 4},
        {"path": "big.md", "sha": good, "size": 10**12},
        {"path": "ok/new.md", "sha": good, "size": 4}], "deleted": ["../../state.json", "keep.md"]})
    assert (stage / "ok" / "new.md").read_bytes() == b"good"
    assert not (tmp_path / "stage" / "escape.md").exists() and not (stage / "keep.md").exists()
    assert (tmp_path / "state.json").exists()


def test_uploads_verify_sha_and_limits(tmp_path: Path):
    up = Uploads(ContentStore(tmp_path / "c"))
    data = b"x" * 10
    sha = hashlib.sha256(data).hexdigest()
    assert up.put("r", sha, 0, data[:4], 10) is None and up.put("r", sha, 0, data[4:], 10) is not None  # 偏移不对
    assert up.put("r", sha, 4, data[4:], 10) is None and up.store.has(sha, 10)
    bad = hashlib.sha256(b"y" * 3).hexdigest()
    assert up.put("r", bad, 0, b"zzz", 3) == "内容和 sha256 对不上" and not up.store.has(bad)
    assert up.put("r", "nope", 0, b"", 0) is not None


def test_session_pack_roundtrip_ignores_foreign_paths(tmp_path: Path):
    home, cwd = tmp_path / "h", Path("/data/conversations/c/a")
    d = home / ".dsh" / "sessions" / dsh_session_dirname(cwd) / "s-1"
    d.mkdir(parents=True)
    (d / "log.jsonl").write_text("x", encoding="utf-8")
    (d / "session.lock").write_text("l", encoding="utf-8")
    data = pack_session(home, cwd, "s-1")
    out = tmp_path / "o"
    assert unpack_session(data, out)
    assert (out / ".dsh" / "sessions" / dsh_session_dirname(cwd) / "s-1" / "log.jsonl").is_file()
    assert not (out / ".dsh" / "sessions" / dsh_session_dirname(cwd) / "s-1" / "session.lock").exists()
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        info = tarfile.TarInfo("../evil")
        info.size = 1
        t.addfile(info, io.BytesIO(b"e"))
    assert not unpack_session(buf.getvalue(), out) and not (tmp_path / "evil").exists()


# ================= local 后端 =================
def test_local_backend_command_binds_original_path(tmp_path: Path):
    """local 后端：工作目录 bind 到逻辑路径（原来的绝对路径），数据目录不挂进去；环境里没有部署方变量。"""
    from matrix import sandbox as bw
    if bw._bwrap() is None:
        pytest.skip("没有 bwrap")
    data = tmp_path / "data"
    logical = data / "conversations" / "c" / "a"
    host = SandboxHost(hub=SandboxHub(), connect_url="ws://127.0.0.1:1/api/sandbox/connect",
                       workdir_of=lambda u: logical, root=tmp_path / "exec", forbidden=(data, tmp_path / "exec"))
    cmd, env, got = local_argv(host, "seat:c/a")
    i = cmd.index(str(logical))
    assert cmd[i - 2] == "--bind" and cmd[i - 1].endswith("units/seats/c/a/work") and got == logical
    assert str(data) not in [cmd[j + 1] for j, x in enumerate(cmd) if x == "--ro-bind"]
    env = sandbox_env(tmp_path / "s", {"PATH": "/usr/bin", "MATRIX_VAULT_KEY": "k", "DEEPSEEK_API_KEY": "d",
                                       "MATRIX_X_API_KEY": "no", "SECRET": "s"})
    assert env["DEEPSEEK_API_KEY"] == "d" and "MATRIX_VAULT_KEY" not in env and "MATRIX_X_API_KEY" not in env
    assert "SECRET" not in env and env["HOME"] == str(tmp_path / "s")


def test_sandbox_connector_rejects_bad_material_paths(tmp_path: Path):
    from matrix.device import DeviceError
    from matrix.sandboxes import RuntimeDriver
    c = SandboxConnector([RuntimeDriver(ScriptedRuntime(lambda x: "ok"))], tmp_path / "w", tmp_path / "s")
    for bad in ("../x", "/etc/passwd", "a/../../x", ""):
        with pytest.raises(DeviceError):
            c._material_path(tmp_path / "w", bad)
    assert c._material_path(tmp_path / "w", "agent-home/.n.md") == tmp_path / "w" / "agent-home" / ".n.md"
    c.bridge.close()


def test_capabilities_report_the_sandbox_backend(tmp_path: Path):
    """沙箱方式下能力报告与管理后台显示后端的隔离等级；云侧不审批（不变量 14）照旧。"""
    net, sb, alice = build(tmp_path, lambda c: "ok")
    a = net.create_agent("A", "a", [], [], actor=alice)
    caps = net.capabilities(a)
    assert caps.approval == "sandbox-only" and caps.placement == "hosted" and caps.isolation == "none"
    iso = net.cloud_isolation()
    assert iso["mode"] == "sandbox:memory" and not iso["active"]
    assert net.sandbox_report()["backend"] == "memory"
