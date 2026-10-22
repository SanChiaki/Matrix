"""多用户：登录、归属与可见性、按用户隔离的私聊、事件过滤、工作区外溢审批、工作台账、私有目录合并、看板。"""

from __future__ import annotations

import re
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


def joint(net: Network, host: str, guest_email: str, guest: str, members: list[str] | None = None):
    """主持人建联合项目、邀请 guest，guest 接受。返回 (项目, 群聊)。"""
    g = net.create_project("J", "联合", members or [], actor=host, kind="joint")
    net.invite(g.id, guest_email, actor=host)
    net.respond_invitation(g.id, True, actor=guest)
    return g, net.project_chat(g.id, actor=host)


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
    raw = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "password-123" not in raw  # 只存 scrypt 哈希


def test_first_user_claims_existing_data(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path)  # 带种子数据
    first, second = users(net, "first@x.io", "second@x.io")
    assert all(a.owner_id == first for a in net.store.agents.values())
    assert all(g.owner_id == first for g in net.store.projects.values())
    assert all(c.owner_id == first for c in net.store.conversations.values())
    assert net.snapshot(second)["agents"] == [] and net.snapshot(second)["projects"] == []


def test_server_does_not_seed(tmp_path: Path):
    from agentnet.server import build_network
    net = build_network("scripted", str(tmp_path), 1)
    assert not net.store.agents and not net.store.projects


# ================= 信任域：Agent 只属于它的所有者 =================
def test_agents_belong_to_owner_only(tmp_path: Path):
    """不变量 16：Agent 只有所有者能用（私聊、拉进项目）、能改；别人看不到。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    assert [x["name"] for x in net.snapshot(alice)["agents"]] == ["A"]
    assert net.snapshot(bob)["agents"] == []
    with pytest.raises(NetworkError, match="unknown agent"):
        net.direct_chat(a.id, actor=bob)
    with pytest.raises(NetworkError, match="unknown agent"):
        net.update_agent(a.id, actor=bob, description="hack")
    with pytest.raises(NetworkError, match="unknown agent"):
        net.delete_agent(a.id, actor=bob)
    with pytest.raises(NetworkError, match="不能修改"):
        net.update_agent(a.id, actor=alice, owner_id=bob)
    gb = net.create_project("B", "", [], actor=bob)
    with pytest.raises(NetworkError, match="unknown agent"):
        net.add_member(gb.id, a.id, actor=bob)
    with pytest.raises(NetworkError, match="unknown agent"):
        net.create_project("B2", "", [a.id], actor=bob)
    c = client(net, "bob@x.io")
    assert c.post("/api/conversations", json={"agent_id": a.id}).status_code == 404
    assert c.post("/api/agents", json={"name": "Bx", "description": "bb", "visibility": "public"}).status_code == 200
    assert "visibility" not in net.store.agents[next(iter(x for x in net.store.agents if x != a.id))].to_dict()


def test_groups_are_private_to_owner(tmp_path: Path):
    """不变量 13。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    g = net.create_project("G", "", [a.id], actor=alice)
    assert g.kind == "internal" and net.snapshot(bob)["projects"] == []
    with pytest.raises(NetworkError, match="unknown project"):
        net.project_chat(g.id, actor=bob)
    chat = net.project_chat(g.id, actor=alice)
    with pytest.raises(NetworkError, match="unknown conversation"):
        net.send_message(chat.id, "@A hi", actor=bob)
    with pytest.raises(NetworkError, match="内部项目"):
        net.invite(g.id, "bob@x.io", actor=alice)


def test_legacy_shared_agents_migrate_to_owner_only(tmp_path: Path):
    """旧版 public / shared：别人的私聊归档为只读；内部项目里别人的 Agent 移出并记事件；委托记录保留。"""
    import json
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=bob)
    g = net.create_project("G", "", [b.id], actor=bob)
    dm = net.direct_chat(b.id, actor=bob)
    net.wait_quiet()
    raw = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    for x in raw["agents"]:
        x["visibility"], x["shared_with"] = "public", []
    raw["projects"][0]["member_ids"].append(a.id)        # 旧版：bob 的群里有 alice 的公开 Agent
    legacy = {**next(c for c in raw["conversations"] if c["id"] == dm.id), "id": "conv-legacy",
              "agent_id": a.id}                         # 旧版：bob 和 alice 的公开 Agent 私聊
    raw["conversations"].append(legacy)
    (tmp_path / "state.json").write_text(json.dumps(raw), encoding="utf-8")
    net2 = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    assert net2.store.conversations["conv-legacy"].archived
    assert not net2.store.conversations[dm.id].archived
    assert net2.store.projects[g.id].member_ids == [b.id]
    chat = net2._project_chat_of(g.id)
    assert any("因为不属于你而被移出" in m.text for m in chat.messages if m.role == "event")
    assert "visibility" not in json.dumps(net2.store.agents[a.id].to_dict())


def test_event_stream_filtered_per_user(tmp_path: Path):
    """不变量 13。"""
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
    """不变量 14：联合项目里 bob @ alice 的 Agent，它申请在工作目录外操作：申请发给 Agent 的所有者，不是 @ 它的人；
    申请理由只给所有者看。"""
    out: dict[str, str] = {}
    net = Network(ScriptedRuntime(_escalating(out)), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    pub = net.create_agent("Pub", "p", [], ["file.write"], actor=alice)
    g, chat = joint(net, bob, "alice@x.io", alice)
    assert pub.id in g.member_ids  # 第一个 Agent 是助理：人进项目时自动加入
    th = _decide_when_pending(net, allow, alice)
    net.send_message(chat.id, "@Pub 写到外面", background=False, actor=bob)
    th.join(5)
    assert out["result"] == expected
    ap = next(iter(net.store.approvals.values()))
    assert ap.owner_id == alice and ap.requested_by == bob and "工作目录外" in ap.reason
    assert ap.status == ("allowed" if allow else "rejected")
    texts = lambda u: [m["text"] for m in net.conversation_detail(chat.id, u)["messages"]]  # noqa: E731
    assert any("申请在工作目录以外操作" in t for t in texts(alice))
    assert not any("申请在工作目录以外操作" in t for t in texts(bob))
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
    import json
    assert "id: agentnet-approval" in y and json.dumps(str(APPROVAL_PLUGIN)) in y and '"t"' in y
    assert APPROVAL_PLUGIN.exists() and "approval/request" in APPROVAL_PLUGIN.read_text(encoding="utf-8")


def test_approval_endpoint_and_api(tmp_path: Path):
    out: dict[str, str] = {}
    net = Network(ScriptedRuntime(_escalating(out)), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    g, chat = joint(net, alice, "bob@x.io", bob, [a.id])
    ca, cb = client(net, "alice@x.io"), client(net, "bob@x.io")
    cb.post(f"/api/conversations/{chat.id}/messages", json={"text": "@A go"})
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
    """内部：台账列出所有者自己的其他工作。"""
    started, release = threading.Event(), threading.Event()
    prompts: dict[str, str] = {}

    def handler(call: ScriptCall) -> str:
        prompts[call.context_id] = call.prompt
        if "第一件" in call.prompt:
            started.set()
            release.wait(5)
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    g = net.create_project("G", "", [a.id], actor=alice)
    net.send_message(net.direct_chat(a.id, actor=alice).id, "第一件", actor=alice)
    started.wait(5)
    led = net.ledger(a.id)
    assert len(led) == 1 and led[0]["user"] == "alice@x.io" and led[0]["what"] == "私聊"
    assert net.board(alice)["ledger"] == {a.id: led}
    chat = net.project_chat(g.id, actor=alice)
    net.send_message(chat.id, "@A 第二件", actor=alice)
    time.sleep(0.2)
    release.set()
    net.wait_quiet()
    second = next(p for p in prompts.values() if "第二件" in p)
    first = next(p for p in prompts.values() if "第一件" in p)
    assert "你同时还在处理" in second and "私聊（为 alice@x.io）" in second
    assert "你同时还在处理" not in first
    assert net.ledger(a.id) == []


def test_ledger_in_joint_group_hides_other_work(tmp_path: Path):
    """不变量 16：联合项目里的运行，台账只列同一项目的工作，其余只给数量——bob @ 它问不出 alice 在别处做什么。"""
    started, release = threading.Event(), threading.Event()
    prompts: dict[str, str] = {}

    def handler(call: ScriptCall) -> str:
        prompts[call.context_id] = call.prompt
        if "机密收购案" in call.prompt:
            started.set()
            release.wait(5)
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    inner = net.create_project("内部收购组", "", [a.id], actor=alice)
    g, chat = joint(net, alice, "bob@x.io", bob, [a.id])
    net.send_message(net.project_chat(inner.id, actor=alice).id, "@A 机密收购案", actor=alice)
    started.wait(5)
    net.send_message(chat.id, "@A 你在忙什么", actor=bob)
    time.sleep(0.2)
    release.set()
    net.wait_quiet()
    p = next(v for v in prompts.values() if "你在忙什么" in v)
    assert "另有 1 项不在这个项目里的工作" in p
    assert "内部收购组" not in p and "alice@x.io）" not in p.split("你同时还在处理")[1]


def test_home_merge_keeps_both_on_conflict(tmp_path: Path):
    """不变量 15：两次并行运行改同一个私有文件：后写回的那份另存为冲突副本，不覆盖先写回的。"""
    gate = threading.Barrier(2, timeout=5)

    def handler(call: ScriptCall) -> str:
        who = "dm" if "私聊里" in call.prompt else "group"
        gate.wait()  # 两次都挂载完 home 之后再写
        (call.cwd / "agent-home" / "notes.md").write_text(f"{who} 的笔记", encoding="utf-8")
        if who == "group":
            time.sleep(0.1)  # 让私聊那次先写回
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], ["file.write"], actor=alice)
    g = net.create_project("G", "", [a.id], actor=alice)
    net.send_message(net.direct_chat(a.id, actor=alice).id, "私聊里", actor=alice)
    net.send_message(net.project_chat(g.id, actor=alice).id, "@A 群聊里", actor=alice)
    net.wait_quiet()
    home = net.agent_home(a.id)
    files = sorted(p.name for p in home.iterdir())
    assert (home / "notes.md").read_text(encoding="utf-8") == "dm 的笔记"
    conflict = [f for f in files if f.startswith("notes.conflict-")]
    assert len(conflict) == 1 and (home / conflict[0]).read_text(encoding="utf-8") == "group 的笔记"
    assert any(e.type == "home.conflict" for e in net.bus.since(0))


# ================= 看板：人交代的任务 =================
def _worker(call: ScriptCall) -> str:
    """A：闲聊直接回复；有交付目标的登记为任务，委托 B，B 交付后整合并交付任务。B：被委托时交付。"""
    if call.context_id.startswith("task-"):
        return "B 的结论"
    if "你好" in call.prompt and call.turn == 1:
        return "你好！"
    if "要求修改" in call.prompt:
        rid = re.search(r"(req-[0-9a-f]{8})", call.prompt).group(1)
        call.tool("deliver_request", request_id=rid, summary="改好了：报告 v2")
        return "已按意见修改"
    if "（@ 你）" in call.prompt and "写一份" in call.prompt:
        out = call.tool("open_request", title="竞品报告", expected_outcome="一份 markdown 报告")
        if call.can_delegate:
            call.tool("delegate", agent="B", mission="查竞品", expected_outcome="摘要")
            return "已接下，委托 B 查资料"
        rid = re.search(r"(req-[0-9a-f]{8})", out).group(1)
        call.tool("deliver_request", request_id=rid, summary="报告在 report.md")
        return "报告写好了"
    if "交付了任务" in call.prompt:
        tid = re.search(r"交付了任务 (task-[0-9a-f]{8})", call.prompt).group(1)
        call.tool("accept_task", task_id=tid)
        rid = re.search(r"(req-[0-9a-f]{8})", call.prompt).group(1)
        return call.tool("deliver_request", request_id=rid, summary="竞品报告：整合了 B 的结论")
    return "ok"


def test_board_shows_user_tasks_not_delegations(tmp_path: Path):
    """看板只有人交代的、有交付目标的事；闲聊不算；Agent 之间的委托只在任务详情里。"""
    net = Network(ScriptedRuntime(_worker), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], ["delegate"], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=alice)
    g = net.create_project("G", "", [a.id, b.id], actor=alice)
    chat = net.project_chat(g.id, actor=alice)
    net.send_message(chat.id, "@A 你好", background=False, actor=alice)
    assert net.board(alice)["requests"] == []           # 闲聊不是任务
    net.send_message(chat.id, "@A 写一份竞品报告", background=False, actor=alice)
    board = net.board(alice)
    assert len(board["requests"]) == 1 and "tasks" not in board
    card = board["requests"][0]
    assert card["title"] == "竞品报告" and card["agent_name"] == "A" and card["requester"] == "alice@x.io"
    assert card["status"] == "in_review" and card["delegations"] == 1 and card["conversation_title"] == "G"
    assert len(net.store.tasks) == 1                     # 委托照常存在，只是不上看板
    detail = net.request_detail(card["id"], alice)
    assert detail["message"] == "@A 写一份竞品报告" and [t["delegatee_id"] for t in detail["tasks"]] == [b.id]
    assert detail["summary"] == "竞品报告：整合了 B 的结论"
    assert net.board(bob)["requests"] == []
    with pytest.raises(NetworkError, match="unknown request"):
        net.request_detail(card["id"], actor=bob)
    events = [m.text for m in chat.messages if m.role == "event"]
    assert "A 接下了「竞品报告」" in events and "A 完成了「竞品报告」，等 alice@x.io 验收" in events
    c = client(net, "alice@x.io")
    assert [r["id"] for r in c.get("/api/board").json()["requests"]] == [card["id"]]
    assert client(net, "bob@x.io").get(f"/api/requests/{card['id']}").status_code == 404
    # 验收：接受
    assert c.post(f"/api/requests/{card['id']}/review", json={"accept": True}).json()["status"] == "completed"


def test_dm_task_review_and_rework(tmp_path: Path):
    """私聊里也能交代任务；下发人退回 → Agent 在原会话返工后重新交付。"""
    net = Network(ScriptedRuntime(_worker), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "写一份竞品报告", background=False, actor=alice)
    r = net.board(alice)["requests"][0]
    assert r["status"] == "in_review" and r["conversation_kind"] == "dm" and r["delegations"] == 0
    with pytest.raises(NetworkError, match="写明要改什么"):
        net.review_request(r["id"], False, "", actor=alice)
    sessions = {call.session_id for call in net.runtime.calls}
    net.review_request(r["id"], False, "加上价格对比", actor=alice)
    net.wait_quiet()
    rr = net.store.requests[r["id"]]
    assert rr.status == "in_review" and rr.revisions == 1 and rr.summary == "改好了：报告 v2"
    assert {call.session_id for call in net.runtime.calls} == sessions  # 原会话返工
    assert "加上价格对比" in net.runtime.calls[-1].prompt
    net.cancel_request(r["id"], actor=alice)
    assert net.store.requests[r["id"]].status == "cancelled"


def test_request_rules(tmp_path: Path):
    """来源由网络认定：没有交代给它的新消息不能登记；一条消息只登记一次；委托没回报不能交付；
    委托任务里没有这两个工具；联合项目里由下发人验收，其他参与者看不到别人的任务。"""
    out: dict[str, str] = {}

    def handler(call: ScriptCall) -> str:
        if call.context_id.startswith("task-"):
            out["task_tools"] = ",".join(call.tool_names)
            return "x"
        if "空轮" in call.prompt:
            return "ok"
        if "（@ 你）" not in call.prompt:
            out["no_source"] = call.tool("open_request", title="t", expected_outcome="o")
            return "ok"
        out["empty"] = call.tool("open_request", title="t", expected_outcome="")
        first = call.tool("open_request", title="t", expected_outcome="o")
        out["dup"] = call.tool("open_request", title="t2", expected_outcome="o")
        rid = re.search(r"(req-[0-9a-f]{8})", first).group(1)
        if call.can_delegate:
            call.tool("delegate", agent="B", mission="m", expected_outcome="o")
            out["early"] = call.tool("deliver_request", request_id=rid, summary="s")
        return "ok"

    gate = threading.Event()

    def slow(call: ScriptCall) -> str:
        if call.context_id.startswith("task-"):
            gate.wait(5)
        return handler(call)

    net = Network(ScriptedRuntime(slow), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], ["delegate"], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=bob)
    g, chat = joint(net, alice, "bob@x.io", bob, [a.id])
    assert b.id in g.member_ids  # 第一个 Agent 是助理：人进项目时自动加入
    net.send_message(chat.id, "@A 做一件事", actor=bob)  # bob 交代给 alice 的 Agent
    time.sleep(0.3)
    assert "不能为空" in out["empty"] and "已经登记" in out["dup"] and "还有委托没有回报" in out["early"]
    gate.set()
    net.wait_quiet()
    assert "open_request" not in out["task_tools"]
    assert "没有人交代给你的新消息" in out["no_source"]  # 委托回报唤醒的那一轮：没有来源，不能登记
    r = next(iter(net.store.requests.values()))
    assert r.requester_id == bob and r.agent_id == a.id
    assert [x["id"] for x in net.board(bob)["requests"]] == [r.id]      # 下发的人
    assert [x["id"] for x in net.board(alice)["requests"]] == [r.id]    # 接任务的 Agent 的所有者
    net.wait_quiet()
    with pytest.raises(NetworkError, match="只有下发"):
        net.cancel_request(r.id, actor=alice)
