"""联合项目（不变量 13、16）与项目档案（不变量 17）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from matrix import auth
from matrix.engine import Network, NetworkError
from matrix.models import TaskStatus
from matrix.runtime import ScriptCall, ScriptedRuntime
from tests.test_users import client, joint, users


def is_task(call: ScriptCall) -> bool:
    return call.context_id.startswith("task-")


# ================= 联合项目：参与者 =================
def test_invite_accept_leave(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob, carol = users(net, "alice@x.io", "bob@x.io", "carol@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=bob)
    g = net.create_project("J", "跨团队", [a.id], actor=alice, kind="joint")
    with pytest.raises(NetworkError, match="未知项目类型"):
        net.create_project("X", "", [], actor=alice, kind="public")
    net.invite(g.id, "bob@x.io", actor=alice)
    # 接受之前：bob 只看得到邀请（名称、主持人），看不到项目与群聊
    assert net.invitations(bob) == [{"project_id": g.id, "name": "J", "description": "跨团队", "host": "alice@x.io"}]
    assert net.snapshot(bob)["projects"] == []
    with pytest.raises(NetworkError, match="unknown project"):
        net.project_chat(g.id, actor=bob)
    with pytest.raises(NetworkError, match="unknown invitation"):
        net.respond_invitation(g.id, True, actor=carol)
    with pytest.raises(NetworkError, match="unknown project"):
        net.invite(g.id, "carol@x.io", actor=bob)  # 还没接受：项目对他不存在
    net.respond_invitation(g.id, True, actor=bob)
    with pytest.raises(NetworkError, match="主持人"):
        net.invite(g.id, "carol@x.io", actor=bob)
    assert [x["id"] for x in net.snapshot(bob)["projects"]] == [g.id]
    assert "invited" not in net.snapshot(bob)["projects"][0]  # 邀请名单只给主持人
    assert b.id in g.member_ids  # 第一个 Agent 是助理：人进项目时自动加入
    # bob 能看到 alice 的 Agent 的名片，但不能私聊、不能改、不能拉进自己的群
    seen = {x["name"]: x for x in net.snapshot(bob)["agents"]}
    assert seen["A"]["mine"] is False and seen["A"]["owner_email"] == "alice@x.io" and "model" not in seen["A"]
    with pytest.raises(NetworkError, match="unknown agent"):
        net.direct_chat(a.id, actor=bob)
    with pytest.raises(NetworkError, match="unknown agent"):
        net.create_project("Mine", "", [a.id], actor=bob)
    # 参与者只能移出自己的 Agent；不能改名、删群
    a2 = net.create_agent("A2", "a2", [], [], actor=alice)
    net.add_member(g.id, a2.id, actor=alice)
    with pytest.raises(NetworkError, match="不是你的 Agent"):
        net.remove_member(g.id, a2.id, actor=bob)
    with pytest.raises(NetworkError, match="助理"):  # 人在项目里时，其助理谁都不能移出
        net.remove_member(g.id, a.id, actor=bob)
    net.remove_member(g.id, a2.id, actor=alice)
    with pytest.raises(NetworkError, match="主持人"):
        net.update_project(g.id, name="改名", actor=bob)
    with pytest.raises(NetworkError, match="主持人"):
        net.delete_project(g.id, actor=bob)
    with pytest.raises(NetworkError, match="主持人不能退出"):
        net.leave_project(g.id, actor=alice)
    # 退出：bob 的 Agent 一并移出，之后看不到项目与群聊
    chat = net.project_chat(g.id, actor=alice)
    net.leave_project(g.id, actor=bob)
    assert net.store.projects[g.id].member_ids == [a.id]
    assert net.snapshot(bob)["projects"] == [] and all(c["id"] != chat.id for c in net.snapshot(bob)["conversations"])
    with pytest.raises(NetworkError, match="unknown conversation"):
        net.conversation_detail(chat.id, actor=bob)
    events = [m.text for m in chat.messages if m.role == "event"]
    assert "bob@x.io 加入了联合项目" in events and "B 离开了项目（所有者已退出）" in events
    assert "bob@x.io 退出了联合项目" in events


def test_host_removes_participant_and_any_agent(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    b1, b2 = (net.create_agent(n, n, [], [], actor=bob) for n in ("B1", "B2"))
    g, _ = joint(net, alice, "bob@x.io", bob)
    assert b1.id in g.member_ids  # 第一个 Agent 是助理：人进项目时自动加入
    net.add_member(g.id, b2.id, actor=bob)
    net.remove_member(g.id, b2.id, actor=alice)  # 主持人能移出任何 Agent（助理除外）
    with pytest.raises(NetworkError, match="助理"):
        net.remove_member(g.id, b1.id, actor=alice)
    assert net.store.projects[g.id].member_ids == [b1.id]
    net.remove_participant(g.id, bob, actor=alice)
    assert net.store.projects[g.id].member_ids == [] and net.store.projects[g.id].participants == []
    assert net.snapshot(bob)["projects"] == []


# ================= 联合项目：可见性 =================
def _cross_handler(call: ScriptCall) -> str:
    """A（alice 的）被 @ 后委托 B（bob 的）；B 交付；A 验收后回复。"""
    if is_task(call):
        call.use("web_search", {"query": "B 的检索"})
        return "B 的结论"
    if call.can_delegate and call.turn == 1:
        call.say("A 的思考过程")
        call.tool("delegate", agent="B", mission="查竞品", expected_outcome="摘要")
        return "已委托 B"
    return "A 汇总完成"


def test_cross_owner_delegation_visible_to_both_owners_only(tmp_path: Path):
    """不变量 1、2、13。"""
    net = Network(ScriptedRuntime(_cross_handler), tmp_path, seed=False)
    alice, bob, carol = users(net, "alice@x.io", "bob@x.io", "carol@x.io")
    a = net.create_agent("A", "a", [], ["delegate", "web.research"], actor=alice)
    b = net.create_agent("B", "b", [], ["web.research"], actor=bob)
    c = net.create_agent("C", "c", [], [], actor=carol)
    g, chat = joint(net, alice, "bob@x.io", bob, [a.id])
    net.invite(g.id, "carol@x.io", actor=alice)
    net.respond_invitation(g.id, True, actor=carol)
    assert b.id in g.member_ids  # 第一个 Agent 是助理：人进项目时自动加入
    assert c.id in g.member_ids
    net.send_message(chat.id, "@A 研究一下", background=False, actor=carol)  # 任何参与者能 @ 任何成员
    t = next(iter(net.store.tasks.values()))
    assert t.status == TaskStatus.COMPLETED and sorted(t.owners) == sorted([alice, bob])
    assert t.granted_authority == ["web.research"]  # 跨所有者的委托：权限照样只收窄
    # 委托只给两端的所有者
    assert net.task_detail(t.id, alice)["id"] == t.id and net.task_detail(t.id, bob)["id"] == t.id
    with pytest.raises(NetworkError, match="unknown task"):
        net.task_detail(t.id, carol)

    # 群聊：所有人都看到 A 的最终回复；过程（steps）与委托只给 A 的所有者
    view = lambda u: net.conversation_detail(chat.id, u)  # noqa: E731
    reply = lambda u: next(m for m in view(u)["messages"] if m["role"] == "agent" and m["text"] == "已委托 B")  # noqa: E731
    assert reply(alice)["steps"] and reply(alice)["task_ids"] == [t.id]
    assert reply(carol)["steps"] == [] and reply(carol)["task_ids"] == []
    assert reply(bob)["task_ids"] == [t.id] and reply(bob)["steps"] == []
    assert [x["id"] for x in view(carol)["tasks"]] == [] and [x["id"] for x in view(bob)["tasks"]] == [t.id]
    assert "audience" not in reply(alice) and "private_to" not in reply(alice)
    # 事件流同样过滤
    evs = net.bus.since(0)
    seen = lambda u, typ: [e for e in evs if e.type == typ and net.event_visible(e.type, e.data, u)]  # noqa: E731
    assert seen(alice, "task.created") and seen(bob, "task.created") and not seen(carol, "task.created")
    progress_of = lambda u: {e.data["context_id"] for e in seen(u, "run.progress")}  # noqa: E731
    assert progress_of(alice) == {f"{chat.id}/{a.id}"}           # 只看自己 Agent 的运行过程
    assert progress_of(bob) == {t.id}
    assert progress_of(carol) == set()
    msg_events = [net.event_view(e.type, e.data, carol) for e in evs if e.type == "message.added"]
    assert all(m is None or m["message"]["steps"] == [] for m in msg_events)


def test_seat_failure_only_shown_to_agent_owner(tmp_path: Path):
    def handler(call: ScriptCall) -> str:
        call.fail("RATE_LIMIT: slow down")
        return ""

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    g, chat = joint(net, bob, "alice@x.io", alice)
    assert a.id in g.member_ids  # 第一个 Agent 是助理：人进项目时自动加入
    net.send_message(chat.id, "@A 做事", background=False, actor=bob)
    roles = lambda u: [m["role"] for m in net.conversation_detail(chat.id, u)["messages"]]  # noqa: E731
    assert "system" in roles(alice) and "system" not in roles(bob)
    assert net.conversation_detail(chat.id, bob)["blocked"] is False
    assert net.conversation_detail(chat.id, alice)["blocked"] is True
    summary = lambda u: next(c for c in net.snapshot(u)["conversations"] if c["id"] == chat.id)  # noqa: E731
    assert summary(bob)["seats"] == {} and "RATE_LIMIT" not in summary(bob)["last_message"]
    assert summary(alice)["seats"][a.id]["blocked"]
    upd = [net.event_view(e.type, e.data, bob) for e in net.bus.since(0) if e.type == "conversation.updated"]
    assert all(not u["conversation"]["blocked"] and u["conversation"]["seats"] == {} for u in upd if u)
    with pytest.raises(NetworkError, match="不需要继续"):
        net.resume_conversation(chat.id, actor=bob)  # bob 不能替 alice 的 Agent 续接
    assert net.resume_conversation(chat.id, actor=alice) == [a.id]


def test_proposal_reviewed_by_proposer_owner(tmp_path: Path):
    def handler(call: ScriptCall) -> str:
        if call.can_delegate and call.turn == 1:
            call.tool("propose_agent", name="NewOne", description="新成员", reason="缺人")
            return "提议了"
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], ["delegate"], actor=alice)
    g, chat = joint(net, bob, "alice@x.io", alice)  # bob 主持
    assert a.id in g.member_ids  # 第一个 Agent 是助理：人进项目时自动加入
    net.send_message(chat.id, "@A 开始", background=False, actor=bob)
    p = next(iter(net.store.proposals.values()))
    assert p.owner_id == alice
    assert net.board(bob)["proposals"] == [] and len(net.board(alice)["proposals"]) == 1
    with pytest.raises(NetworkError, match="unknown proposal"):
        net.approve_proposal(p.id, actor=bob)
    new = net.approve_proposal(p.id, actor=alice)
    assert new.owner_id == alice and new.id in net.store.projects[g.id].member_ids


def test_retry_task_only_by_delegator_owner(tmp_path: Path):
    def handler(call: ScriptCall) -> str:
        if is_task(call):
            call.fail("RATE_LIMIT: no")
        if call.can_delegate and call.turn == 1:
            call.tool("delegate", agent="B", mission="m", expected_outcome="o")
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], ["delegate"], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=bob)
    g, chat = joint(net, alice, "bob@x.io", bob, [a.id])
    assert b.id in g.member_ids  # 第一个 Agent 是助理：人进项目时自动加入
    net.send_message(chat.id, "@A go", background=False, actor=alice)
    t = next(iter(net.store.tasks.values()))
    assert t.status == TaskStatus.FAILED
    with pytest.raises(NetworkError, match="委托方 Agent 的所有者"):
        net.retry_task(t.id, actor=bob)
    net.retry_task(t.id, actor=alice)


# ================= 私有目录：联合项目默认不挂载 =================
def test_home_not_mounted_in_joint_group_by_default(tmp_path: Path):
    seen: list[bool] = []

    def handler(call: ScriptCall) -> str:
        seen.append((call.cwd / "agent-home").exists())
        if (call.cwd / "agent-home").exists():
            (call.cwd / "agent-home" / "from-joint.md").write_text("x", encoding="utf-8")
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], ["file.write"], actor=alice)
    (net.agent_home(a.id) / "secret.md").write_text("alice 的内部笔记", encoding="utf-8")
    g, chat = joint(net, bob, "alice@x.io", alice)
    assert a.id in g.member_ids  # 第一个 Agent 是助理：人进项目时自动加入
    net.send_message(chat.id, "@A 1", background=False, actor=bob)
    assert seen == [False]
    assert "没有挂载" in net.runtime.calls[-1].prompt
    with pytest.raises(NetworkError, match="unknown agent"):
        net.set_home_mount(g.id, a.id, True, actor=bob)  # 只有所有者能打开
    net.set_home_mount(g.id, a.id, True, actor=alice)
    net.send_message(chat.id, "@A 2", background=False, actor=bob)
    assert seen == [False, True]
    assert (net.agent_home(a.id) / "from-joint.md").exists()
    # 内部项目、私聊总是挂载
    inner = net.create_project("I", "", [a.id], actor=alice)
    net.send_message(net.project_chat(inner.id, actor=alice).id, "@A 3", background=False, actor=alice)
    assert seen[-1] is True


# ================= 项目档案 =================
def test_archive_brief_decisions_refs_pushed_to_seats_and_tasks(tmp_path: Path):
    prompts: dict[str, list[str]] = {}

    def handler(call: ScriptCall) -> str:
        prompts.setdefault(call.context_id, []).append(call.prompt)
        files = sorted(p.name for p in (call.cwd / "project").iterdir()) if (call.cwd / "project").exists() else []
        prompts[call.context_id].append("FILES:" + ",".join(files))
        if is_task(call):
            return "B 结论"
        if call.can_delegate and call.turn == 1:
            call.tool("record_decision", text="只做日本市场", reason="资源有限")
            call.tool("delegate", agent="B", mission="查日本", expected_outcome="摘要")
            return "委托了"
        return "完成"

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], ["delegate"], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=alice)
    g = net.create_project("G", "", [a.id, b.id], actor=alice)
    net.set_brief(g.id, "目标：评估出海机会。范围：亚洲。", actor=alice)
    net.add_ref(g.id, "前端仓库", "~/code/web", "只读参考", actor=alice)
    net.publish_file(g.id, "背景.md", "背景资料".encode(), actor=alice)
    chat = net.project_chat(g.id, actor=alice)
    net.send_message(chat.id, "@A 开始", background=False, actor=alice)
    seat = prompts[f"{chat.id}/{a.id}"]
    assert "目标：评估出海机会" in seat[0] and "前端仓库：~/code/web" in seat[0] and "背景.md" in seat[0]
    assert seat[1] == "FILES:背景.md"
    task = next(v for k, v in prompts.items() if k.startswith("task-"))
    assert "目标：评估出海机会" in task[0] and "只做日本市场" in task[0]  # 委托任务也拿到档案（含刚记录的决定）
    # 档案在这一轮里变了（A 记录了决定）：下一轮重新推送；没变的轮次不推送
    assert "只做日本市场" in seat[2]
    net.send_message(chat.id, "@A 没变化", background=False, actor=alice)
    assert "项目档案" not in seat[-2]
    d = net.store.projects[g.id].decisions[0]
    assert d.by_agent == a.id and d.status == "active"
    assert any("A 记录了决定" in m.text for m in chat.messages if m.role == "event")
    net.record_decision(g.id, "改做韩国市场", supersedes=d.id, actor=alice)
    assert d.status == "superseded"
    net.send_message(chat.id, "@A 继续", background=False, actor=alice)
    latest = seat[-2]
    assert "改做韩国市场" in latest and "只做日本市场" not in latest
    # 档案文件只读：运行里改了也不会保存
    log = (tmp_path / "projects" / g.id / "log.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["action"] for x in log][:3] == ["brief.set", "ref.add", "file.publish"]


def test_archive_writes_only_through_network(tmp_path: Path):
    """不变量 17。"""
    def handler(call: ScriptCall) -> str:
        (call.cwd / "project" / "背景.md").write_text("被 Agent 改了", encoding="utf-8")
        (call.cwd / "report.md").write_text("A 的报告", encoding="utf-8")
        (call.cwd / "agent-home" / "private.md").write_text("私有", encoding="utf-8")
        out = [call.tool("publish_file", path="report.md"),
               call.tool("publish_file", path="../../state.json"),
               # 带根不带盘符：Windows 上不算 is_absolute()，拼上去却指向本盘的 state.json
               call.tool("publish_file", path="/" + (tmp_path / "state.json").relative_to(tmp_path.anchor).as_posix()),
               call.tool("publish_file", path="agent-home/private.md"),
               call.tool("publish_file", path="project/背景.md")]
        return "\n".join(out)

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], ["file.write"], actor=alice)  # 没有 delegate 也有档案工具
    g = net.create_project("G", "", [a.id], actor=alice)
    net.publish_file(g.id, "背景.md", "原文".encode(), actor=alice)
    chat = net.project_chat(g.id, actor=alice)
    net.send_message(chat.id, "@A 写", background=False, actor=alice)
    files = net.store.project_dir(g.id)
    assert (files / "背景.md").read_text(encoding="utf-8") == "原文"
    assert (files / "report.md").read_text(encoding="utf-8") == "A 的报告"
    reply = chat.messages[-1].text.splitlines()
    assert reply[0].startswith("已发布") and all(x.startswith("找不到") for x in reply[1:])
    assert not (tmp_path / "conversations" / chat.id / a.id / "project").exists()  # 只读副本运行后移除
    assert net.store.tasks == {} and not net.runtime.calls[0].can_delegate
    with pytest.raises(NetworkError, match="文件名无效"):
        net.publish_file(g.id, "../..", b"x", actor=alice)
    assert net.publish_file(g.id, "../../evil.md", b"x", actor=alice) == "evil.md"
    assert (files / "evil.md").exists() and not (tmp_path / "evil.md").exists()


def test_brief_host_only_suggestions(tmp_path: Path):
    def handler(call: ScriptCall) -> str:
        return call.tool("propose_brief", text="新的目标：只做 B2B", reason="用户反馈")

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    b = net.create_agent("B", "b", [], [], actor=bob)
    g, chat = joint(net, alice, "bob@x.io", bob)
    assert b.id in g.member_ids  # 第一个 Agent 是助理：人进项目时自动加入
    net.set_brief(g.id, "原 brief", actor=alice)
    with pytest.raises(NetworkError, match="主持人"):
        net.set_brief(g.id, "bob 想改", actor=bob)
    with pytest.raises(NetworkError, match="最多"):
        net.set_brief(g.id, "x" * 4001, actor=alice)
    net.suggest_brief(g.id, "bob 的版本", "更准确", actor=bob)
    net.send_message(chat.id, "@B 改 brief", background=False, actor=bob)
    gg = net.store.projects[g.id]
    assert gg.brief == "原 brief" and [s.by_agent for s in gg.suggestions] == [None, b.id]
    assert len(net.board(alice)["suggestions"]) == 2 and net.board(bob)["suggestions"] == []
    with pytest.raises(NetworkError, match="主持人"):
        net.decide_suggestion(g.id, gg.suggestions[1].id, True, actor=bob)
    net.decide_suggestion(g.id, gg.suggestions[1].id, True, actor=alice)
    net.decide_suggestion(g.id, gg.suggestions[0].id, False, actor=alice)
    assert gg.brief == "新的目标：只做 B2B" and [s.status for s in gg.suggestions] == ["rejected", "accepted"]
    # 参与者能记录、撤销决定（人可以撤销 Agent 记录的）；资源引用只有主持人
    d = net.record_decision(g.id, "周五前交付", actor=bob)
    net.revoke_decision(g.id, d.id, actor=alice)
    assert d.status == "revoked"
    with pytest.raises(NetworkError, match="主持人"):
        net.add_ref(g.id, "P", "~/p", actor=bob)


def test_archive_api(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    auth.register(net.store, "eve@x.io", "password-123")
    ca, cb, ce = client(net, "alice@x.io"), client(net, "bob@x.io"), client(net, "eve@x.io")
    g = ca.post("/api/projects", json={"name": "J", "kind": "joint"}).json()
    assert g["kind"] == "joint"
    assert ca.post(f"/api/projects/{g['id']}/invitations", json={"email": "bob@x.io"}).status_code == 200
    assert cb.get(f"/api/projects/{g['id']}").status_code == 404
    assert cb.get("/api/network").json()["invitations"][0]["host"] == "alice@x.io"
    assert cb.post(f"/api/projects/{g['id']}/invitation", json={"accept": True}).status_code == 200
    assert ca.put(f"/api/projects/{g['id']}/brief", json={"text": "目标"}).json()["brief"] == "目标"
    assert cb.put(f"/api/projects/{g['id']}/brief", json={"text": "x"}).status_code == 409
    ca.post(f"/api/projects/{g['id']}/files", json={"name": "a.md", "content": "内容"})
    r = cb.get(f"/api/projects/{g['id']}/files/a.md")
    assert r.status_code == 200 and r.text == "内容" and "attachment" in r.headers["content-disposition"]
    assert ce.get(f"/api/projects/{g['id']}/files/a.md").status_code == 404
    d = cb.post(f"/api/projects/{g['id']}/decisions", json={"text": "定了"}).json()["decisions"][0]
    assert d["by_user"] == bob
    assert ce.post(f"/api/projects/{g['id']}/decisions", json={"text": "x"}).status_code == 404
    assert cb.post(f"/api/projects/{g['id']}/leave").status_code == 200
    assert cb.get(f"/api/projects/{g['id']}").status_code == 404
    assert cb.get(f"/api/projects/{g['id']}/files/a.md").status_code == 404


def test_project_files_report_last_updater(tmp_path: Path):
    """项目页的资产列表：每个文件带最近一次发布它的人 / Agent；删除后再发布以新的为准。"""
    net = Network(ScriptedRuntime(lambda call: "完成"), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    g = net.create_project("G", "", [a.id], actor=alice)
    net.publish_file(g.id, "a.md", b"1", actor=alice)
    net.publish_file(g.id, "b.md", b"2", actor=alice, agent_id=a.id)
    net.publish_file(g.id, "a.md", b"3", actor=alice, agent_id=a.id)  # 覆盖：更新人换成 Agent
    files = {f["name"]: f for f in net.project_detail(g.id, actor=alice)["files"]}
    assert files["a.md"]["by_agent"] == a.id and files["b.md"]["by_agent"] == a.id
    net.delete_file(g.id, "b.md", actor=alice)
    net.publish_file(g.id, "b.md", b"4", actor=alice)
    files = {f["name"]: f for f in net.project_detail(g.id, actor=alice)["files"]}
    assert files["b.md"]["by_agent"] is None and files["b.md"]["by_user"] == alice
