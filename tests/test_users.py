"""多用户：登录、归属与可见性、按用户隔离的私聊、事件过滤、工作区外溢审批、工作台账、私有目录合并、看板。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentnet import auth
from agentnet.engine import Limits, Network, NetworkError
from agentnet.models import TaskStatus
from agentnet.runtime import ApprovalBridge, ScriptCall, ScriptedRuntime, build_patch_yaml
from agentnet.server import create_app


def users(net: Network, *emails: str) -> list[str]:
    return [auth.register(net.store, e, "password-123")[0].id for e in emails]


def client(net: Network, email: str) -> TestClient:
    c = TestClient(create_app(net))
    r = c.post("/api/auth/login", json={"email": email, "password": "password-123"})
    assert r.status_code == 200, r.text
    return c


# ================= 登录 =================
def test_register_login_logout(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    c = TestClient(create_app(net))
    assert c.get("/api/me").status_code == 401
    assert c.post("/api/auth/register", json={"email": "bad", "password": "password-123"}).status_code == 400
    assert c.post("/api/auth/register", json={"email": "a@x.io", "password": "short"}).status_code == 400
    r = c.post("/api/auth/register", json={"email": " A@X.io ", "password": "password-123"})
    assert r.status_code == 200 and r.json()["email"] == "a@x.io"
    assert c.get("/api/me").json()["agent_count"] == 0  # 引导去创建首个 Agent
    assert c.post("/api/auth/register", json={"email": "a@x.io", "password": "password-123"}).status_code == 400
    c.post("/api/auth/logout")
    assert c.get("/api/me").status_code == 401
    assert c.post("/api/auth/login", json={"email": "a@x.io", "password": "wrong-pass"}).status_code == 400
    assert c.post("/api/auth/login", json={"email": "a@x.io", "password": "password-123"}).status_code == 200
    assert c.get("/api/me").status_code == 200
    raw = (tmp_path / "state.json").read_text()
    assert "password-123" not in raw  # 只存 scrypt 哈希


def test_first_user_claims_existing_data(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path)  # 带种子数据
    first, second = users(net, "first@x.io", "second@x.io")
    assert all(a.owner_id == first for a in net.store.agents.values())
    assert all(g.owner_id == first for g in net.store.groups.values())
    assert all(c.owner_id == first for c in net.store.conversations.values())
    assert net.snapshot(second)["agents"] == [] and net.snapshot(second)["groups"] == []


def test_server_does_not_seed(tmp_path: Path):
    from agentnet.server import build_network
    net = build_network("scripted", str(tmp_path), 1)
    assert not net.store.agents and not net.store.groups


# ================= 可见性 =================
def test_agent_visibility_private_public_shared(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob, carol = users(net, "alice@x.io", "bob@x.io", "carol@x.io")
    priv = net.create_agent("Priv", "p", [], [], actor=alice)
    pub = net.create_agent("Pub", "p", [], [], actor=alice, visibility="public")
    sh = net.create_agent("Sh", "s", [], [], actor=alice, visibility="shared", shared_with=[bob, alice])
    assert sh.shared_with == [bob]  # 所有者自己不在名单里
    names = lambda u: {a["name"] for a in net.snapshot(u)["agents"]}  # noqa: E731
    assert names(alice) == {"Priv", "Pub", "Sh"}
    assert names(bob) == {"Pub", "Sh"}
    assert names(carol) == {"Pub"}
    with pytest.raises(NetworkError, match="unknown agent"):
        net.direct_chat(priv.id, actor=bob)
    with pytest.raises(NetworkError, match="所有者"):
        net.update_agent(pub.id, actor=bob, description="hack")
    with pytest.raises(NetworkError, match="所有者"):
        net.delete_agent(sh.id, actor=bob)
    bob_view = next(a for a in net.snapshot(bob)["agents"] if a["name"] == "Sh")
    assert bob_view["mine"] is False and "shared_with" not in bob_view and bob_view["owner_email"] == "alice@x.io"
    with pytest.raises(NetworkError, match="未知用户"):
        net.update_agent(sh.id, actor=alice, shared_with=["user-nope"])


def test_groups_are_private_to_owner(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    pub = net.create_agent("Pub", "p", [], [], actor=alice, visibility="public")
    priv = net.create_agent("Priv", "p", [], [], actor=alice)
    g = net.create_group("G", "", [pub.id, priv.id], actor=alice)
    assert net.snapshot(bob)["groups"] == []
    with pytest.raises(NetworkError, match="unknown group"):
        net.group_chat(g.id, actor=bob)
    chat = net.group_chat(g.id, actor=alice)
    with pytest.raises(NetworkError, match="unknown conversation"):
        net.send_message(chat.id, "@Pub hi", actor=bob)
    # bob 建自己的群：能加公开的 Agent，不能加别人的私有 Agent
    gb = net.create_group("B", "", [pub.id], actor=bob)
    with pytest.raises(NetworkError, match="unknown agent"):
        net.add_member(gb.id, priv.id, actor=bob)


def test_dm_is_per_user_and_runs_in_parallel(tmp_path: Path):
    """同一个公开 Agent，两个用户各有自己的私聊（独立会话），可以并行运行。"""
    gate = threading.Barrier(2, timeout=5)

    def handler(call: ScriptCall) -> str:
        gate.wait()  # 两个私聊必须同时在跑才能都通过
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    pub = net.create_agent("Pub", "p", [], [], actor=alice, visibility="public")
    da, db = net.direct_chat(pub.id, actor=alice), net.direct_chat(pub.id, actor=bob)
    assert da.id != db.id and net.direct_chat(pub.id, actor=bob).id == db.id
    net.send_message(da.id, "我是 alice", actor=alice)
    net.send_message(db.id, "我是 bob", actor=bob)
    net.wait_quiet()
    assert [m.role for m in da.messages] == ["user", "agent"] and [m.role for m in db.messages] == ["user", "agent"]
    assert da.seats[pub.id].dsh_session_id != db.seats[pub.id].dsh_session_id
    assert [c["id"] for c in net.snapshot(bob)["conversations"]] == [db.id]


def test_unsharing_closes_dm(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    sh = net.create_agent("Sh", "s", [], [], actor=alice, visibility="shared", shared_with=[bob])
    dm = net.direct_chat(sh.id, actor=bob)
    net.update_agent(sh.id, actor=alice, visibility="private")
    with pytest.raises(NetworkError, match="不再对你开放"):
        net.send_message(dm.id, "还在吗", actor=bob)


def test_event_stream_filtered_per_user(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "secret", background=False, actor=alice)
    evs = net.bus.since(0)
    seen_by = lambda u: [e for e in evs if net.event_visible(e.type, e.data, u)]  # noqa: E731
    assert any(e.type == "message.added" for e in seen_by(alice))
    leaked = [e for e in seen_by(bob) if e.type in ("message.added", "run.progress", "runtime.invoke",
                                                     "conversation.updated", "agent.updated")]
    assert leaked == []


# ================= 工作区外溢审批 =================
def _escalating(outcome: dict[str, str]):
    def handler(call: ScriptCall) -> str:
        outcome["result"] = call.escalate("写 ~/outside/x.txt：用户要求放到工作目录外")
        return f"结果：{outcome['result']}"
    return handler


def _decide_when_pending(net: Network, allow: bool, actor: str | None) -> threading.Thread:
    def run() -> None:
        for _ in range(200):
            pend = net.pending_approvals(actor)
            if pend:
                net.decide_approval(pend[0].id, allow, actor=actor)
                return
            time.sleep(0.02)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


@pytest.mark.parametrize("allow,expected", [(True, "allowed-once"), (False, "rejected")])
def test_outside_workspace_needs_owner_approval(tmp_path: Path, allow: bool, expected: str):
    """Agent 被别人使用时申请在工作目录外操作：申请发给 Agent 的所有者，不是发起对话的人。"""
    out: dict[str, str] = {}
    net = Network(ScriptedRuntime(_escalating(out)), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    pub = net.create_agent("Pub", "p", [], ["file.write"], actor=alice, visibility="public")
    dm = net.direct_chat(pub.id, actor=bob)
    th = _decide_when_pending(net, allow, alice)
    net.send_message(dm.id, "写到外面", background=False, actor=bob)
    th.join(5)
    assert out["result"] == expected
    ap = next(iter(net.store.approvals.values()))
    assert ap.owner_id == alice and ap.requested_by == bob and "工作目录外" in ap.reason
    assert ap.status == ("allowed" if allow else "rejected")
    assert any("申请在工作目录以外操作" in m.text for m in dm.messages if m.role == "system")
    # bob 不是所有者：看不到也批不了
    assert net.pending_approvals(bob) == []
    assert not net.event_visible("approval.updated", {"approval": ap.to_dict()}, bob)


def test_approval_rules(tmp_path: Path):
    out: dict[str, str] = {}
    net = Network(ScriptedRuntime(_escalating(out)), tmp_path, Limits(approval_timeout_s=0.3), seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "go", background=False, actor=alice)  # 没人审批：超时作废
    assert out["result"] == "cancelled"
    ap = next(iter(net.store.approvals.values()))
    assert ap.status == "cancelled" and "超时" in ap.note
    with pytest.raises(NetworkError, match="已经作废"):
        net.decide_approval(ap.id, True, actor=alice)
    with pytest.raises(NetworkError, match="unknown approval"):
        net.decide_approval(ap.id, True, actor=bob)
    assert net.request_approval("bad-token", "bash", "x") == "unavailable"


def test_patch_mounts_approval_plugin():
    from agentnet.runtime import APPROVAL_PLUGIN
    y = build_patch_yaml(["file.write"], approvals=ApprovalBridge(url="http://127.0.0.1:1/internal/approval",
                                                                   token="t", handler=lambda t, r: "rejected"))
    assert "id: agentnet-approval" in y and str(APPROVAL_PLUGIN) in y and '"t"' in y
    assert APPROVAL_PLUGIN.exists() and "approval/request" in APPROVAL_PLUGIN.read_text()


def test_approval_endpoint_and_api(tmp_path: Path):
    out: dict[str, str] = {}
    net = Network(ScriptedRuntime(_escalating(out)), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice, visibility="public")
    ca, cb = client(net, "alice@x.io"), client(net, "bob@x.io")
    dm = cb.post("/api/conversations", json={"agent_id": a.id}).json()
    cb.post(f"/api/conversations/{dm['id']}/messages", json={"text": "go"})
    for _ in range(100):
        board = ca.get("/api/board").json()
        if board["approvals"]:
            break
        time.sleep(0.02)
    ap = board["approvals"][0]
    assert cb.get("/api/board").json()["approvals"] == []
    assert cb.post(f"/api/approvals/{ap['id']}", json={"allow": True}).status_code == 404
    assert ca.post(f"/api/approvals/{ap['id']}", json={"allow": True}).json()["status"] == "allowed"
    net.wait_quiet()
    assert out["result"] == "allowed-once"


# ================= 工作台账 =================
def test_ledger_tells_agent_about_parallel_work(tmp_path: Path):
    started, release = threading.Event(), threading.Event()
    prompts: dict[str, str] = {}

    def handler(call: ScriptCall) -> str:
        prompts[call.prompt.split("\n")[-1]] = call.prompt
        if "第一件" in call.prompt:
            started.set()
            release.wait(5)
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice, visibility="public")
    net.send_message(net.direct_chat(a.id, actor=alice).id, "第一件", actor=alice)
    started.wait(5)
    led = net.ledger(a.id)
    assert len(led) == 1 and led[0]["user"] == "alice@x.io" and led[0]["what"] == "私聊"
    assert net.board(bob)["ledger"] == {a.id: led}
    net.send_message(net.direct_chat(a.id, actor=bob).id, "第二件", actor=bob)
    time.sleep(0.2)
    release.set()
    net.wait_quiet()
    second = next(p for p in prompts.values() if "第二件" in p)
    first = next(p for p in prompts.values() if "第一件" in p)
    assert "你同时还在处理" in second and "为 alice@x.io" in second
    assert "你同时还在处理" not in first
    assert net.ledger(a.id) == []


def test_home_merge_keeps_both_on_conflict(tmp_path: Path):
    """两次并行运行改同一个私有文件：后写回的那份另存为冲突副本，不覆盖先写回的。"""
    gate = threading.Barrier(2, timeout=5)

    def handler(call: ScriptCall) -> str:
        who = "alice" if "我是 alice" in call.prompt else "bob"
        gate.wait()  # 两次都挂载完 home 之后再写
        (call.cwd / "agent-home" / "notes.md").write_text(f"{who} 的笔记", encoding="utf-8")
        if who == "bob":
            time.sleep(0.1)  # 让 alice 先写回
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], ["file.write"], actor=alice, visibility="public")
    net.send_message(net.direct_chat(a.id, actor=alice).id, "我是 alice", actor=alice)
    net.send_message(net.direct_chat(a.id, actor=bob).id, "我是 bob", actor=bob)
    net.wait_quiet()
    home = net.agent_home(a.id)
    files = sorted(p.name for p in home.iterdir())
    assert (home / "notes.md").read_text(encoding="utf-8") == "alice 的笔记"
    conflict = [f for f in files if f.startswith("notes.conflict-")]
    assert len(conflict) == 1 and (home / conflict[0]).read_text(encoding="utf-8") == "bob 的笔记"
    assert any(e.type == "home.conflict" for e in net.bus.since(0))


# ================= 看板 =================
def test_board_shows_my_tasks_only(tmp_path: Path):
    def handler(call: ScriptCall) -> str:
        if call.context_id.startswith("task-"):
            return "B 的结论"
        if call.turn == 1:
            call.tool("delegate", agent="B", mission="查资料", expected_outcome="摘要")
            return "等"
        return "完成"

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], ["delegate"], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=alice)
    g = net.create_group("G", "", [a.id, b.id], actor=alice)
    chat = net.group_chat(g.id, actor=alice)
    net.send_message(chat.id, "@A 开始", background=False, actor=alice)
    board = net.board(alice)
    assert len(board["tasks"]) == 1
    card = board["tasks"][0]
    assert card["status"] == TaskStatus.COMPLETED.value and card["delegator_name"] == "A"
    assert card["delegatee_name"] == "B" and card["conversation_title"] == "G"
    assert net.board(bob)["tasks"] == []
    with pytest.raises(NetworkError, match="unknown task"):
        net.task_detail(card["id"], actor=bob)
    c = client(net, "alice@x.io")
    assert len(c.get("/api/board").json()["tasks"]) == 1
    assert client(net, "bob@x.io").get(f"/api/tasks/{card['id']}").status_code == 404
