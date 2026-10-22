"""项目：成员集合、委托边界与项目档案（docs/projects.md）。用户任务的项目归属由网络认定。"""
import re
from pathlib import Path

import pytest

from agentnet.engine import Network, NetworkError
from agentnet.models import Message
from agentnet.runtime import ScriptCall, ScriptedRuntime
from tests.test_users import client, joint, users


def deliver_handler(prompts: list[str] | None = None):
    """每轮：登记任务并交付。prompts 收集席位看到的提示词。"""
    def handler(call: ScriptCall) -> str:
        if prompts is not None:
            prompts.append(call.prompt)
        text = call.tool("open_request", title="报告", expected_outcome="报告文件")
        rid = re.search(r"req-[0-9a-f]{8}", text)
        if rid:
            call.tool("deliver_request", request_id=rid.group(), summary="完成")
        return "ok"
    return handler


def setup(tmp_path: Path, prompts: list[str] | None = None):
    net = Network(ScriptedRuntime(deliver_handler(prompts)), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    p = net.create_project("新产品上线", "", [a.id], actor=alice)
    return net, alice, bob, a, p


def test_requests_take_project_from_chat(tmp_path: Path):
    """项目群聊里登记的任务属于这个项目，看板卡片带上项目名；不能再手动改归属。"""
    net, alice, bob, a, p = setup(tmp_path)
    chat = net.project_chat(p.id, actor=alice)
    net.send_message(chat.id, "@A 写一份报告", actor=alice, background=False)
    r = next(iter(net.store.requests.values()))
    assert r.project_id == p.id
    assert net.board(alice)["requests"][0]["project_name"] == "新产品上线"
    other = net.create_project("别的", "", [], actor=alice)
    with pytest.raises(NetworkError, match="已经属于这个项目"):
        net.assign_request(r.id, other.id, actor=alice)


def test_dm_request_assigned_by_hand(tmp_path: Path):
    """私聊里的任务默认不属于项目，可以手动归到自己主持的项目；别人的项目按不存在处理（不变量 13）。"""
    net, alice, bob, a, p = setup(tmp_path)
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "写个报告", actor=alice, background=False)
    r = next(iter(net.store.requests.values()))
    assert r.project_id is None
    net.assign_request(r.id, p.id, actor=alice)
    assert r.project_id == p.id
    net.assign_request(r.id, None, actor=alice)
    assert r.project_id is None
    pb = net.create_project("bob 的", "", [], actor=bob)
    with pytest.raises(NetworkError, match="unknown project"):
        net.assign_request(r.id, pb.id, actor=alice)
    jp, _ = joint(net, bob, "alice@x.io", alice, [])
    with pytest.raises(NetworkError, match="只有主持人"):
        net.assign_request(r.id, jp.id, actor=alice)


# ================= 助理与项目会话 =================
def test_assistant_joins_projects_and_stays(tmp_path: Path):
    """第一个 Agent 默认是助理；人进项目时助理自动加入并记事件；人在项目里时助理不能移出；可以换助理；删掉助理改用最早的另一个。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=alice)
    assert net.store.users[alice].assistant_id == a.id
    p = net.create_project("P", "", [b.id], actor=alice)
    assert p.member_ids == [b.id, a.id]
    chat = net.project_chat(p.id, actor=alice)
    assert any(m.role == "event" and m.text == "A 加入了项目（alice@x.io 的助理）" for m in chat.messages)
    with pytest.raises(NetworkError, match="助理"):
        net.remove_member(p.id, a.id, actor=alice)
    net.set_assistant(b.id, actor=alice)
    net.remove_member(p.id, a.id, actor=alice)  # 换了助理：旧助理去留由人决定
    with pytest.raises(NetworkError, match="助理"):
        net.remove_member(p.id, b.id, actor=alice)
    net.delete_agent(b.id, actor=alice)
    assert net.store.users[alice].assistant_id == a.id


def test_session_brings_assistant_into_existing_project(tmp_path: Path):
    """已有项目里没有助理：第一次开项目会话时加入，并在群聊里记事件；没有助理的人开不了会话。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    with pytest.raises(NetworkError, match="还没有助理"):
        net.create_session(net.create_project("空", "", [], actor=bob).id, actor=bob)
    a = net.create_agent("A", "a", [], [], actor=alice)
    p = net.create_project("P", "", [], actor=alice)
    p.member_ids.remove(a.id)  # 模拟迁移来的旧项目
    s = net.create_session(p.id, "了解进度", actor=alice)
    assert (s.kind, s.project_id, s.agent_id, s.owner_id, s.title) == ("session", p.id, a.id, alice, "了解进度")
    assert a.id in p.member_ids
    assert [x["id"] for x in net.sessions(p.id, actor=alice)] == [s.id]
    net.update_session(s.id, title="细节", archived=True, actor=alice)
    with pytest.raises(NetworkError, match="已归档"):
        net.send_message(s.id, "还在吗", actor=alice)
    net.update_session(s.id, archived=False, actor=alice)
    assert net.store.conversations[s.id].title == "细节"


def _session_handler(out: dict):
    def handler(call: ScriptCall) -> str:
        if call.context_id.startswith("task-"):
            return "B 的结论"
        out.setdefault("prompts", []).append(call.prompt)
        out["tools"] = call.tool_names
        p = call.prompt
        if "记一个决定" in p:
            call.tool("record_decision", text="只做日本市场")
        if "委托 B" in p:
            call.tool("delegate", agent="B", mission="查竞品", expected_outcome="摘要")
        if "写一份内部报告" in p:
            call.tool("open_request", title="内部报告", expected_outcome="报告")
        if "读群聊" in p:
            out["chat"] = call.tool("read_chat")
        if m := re.search(r"读任务 (req-[0-9a-f]{8})", p):
            out["request"] = call.tool("read_request", request_id=m.group(1))
        return "好的"
    return handler


def test_session_seat_works_like_chat_seat(tmp_path: Path):
    """项目会话里的助理：带项目档案与项目现状，能用档案工具、委托项目成员、读项目现状；
    记录决定在群聊里留事件，会话里的对话不进群聊。"""
    out: dict = {}
    net = Network(ScriptedRuntime(_session_handler(out)), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], ["delegate"], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=alice)
    p = net.create_project("出海", "", [b.id], actor=alice)
    net.set_brief(p.id, "目标：评估出海机会", actor=alice)
    chat = net.project_chat(p.id, actor=alice)
    net.send_message(chat.id, "大家好，本周定方向", actor=alice)
    s = net.create_session(p.id, actor=alice)
    net.send_message(s.id, "记一个决定，再委托 B，然后读群聊", actor=alice, background=False)
    prompt = out["prompts"][0]
    assert "项目会话" in prompt and "目标：评估出海机会" in prompt and "项目现状" in prompt
    assert "大家好，本周定方向" in prompt
    assert any(t.endswith("read_chat") for t in out["tools"]) and any(t.endswith("delegate") for t in out["tools"])
    assert "大家好，本周定方向" in out["chat"]
    t = next(iter(net.store.tasks.values()))
    assert (t.delegatee_id, t.conversation_id, t.project_id) == (b.id, s.id, p.id)
    assert any(m.role == "event" and "只做日本市场" in m.text for m in chat.messages)
    assert not any("记一个决定" in m.text for m in chat.messages)
    # 群聊席位没有读项目现状的工具
    net.send_message(chat.id, "@A 你好", actor=alice, background=False)
    assert not any(t.endswith("read_chat") for t in out["tools"])
    # 项目现状没变时不再重复推送
    net.send_message(s.id, "谢谢", actor=alice, background=False)
    net.send_message(s.id, "再见", actor=alice, background=False)
    assert "项目现状" not in out["prompts"][-1]


def test_session_private_and_reads_by_owner_visibility(tmp_path: Path):
    """不变量 18：项目会话只属于它的人（联合项目里别的参与方看不到、收不到事件）；
    助理读到的项目现状按会话主人的可见性过滤：只给别人看的消息、看不到的用户任务读不到。"""
    out: dict = {}
    net = Network(ScriptedRuntime(_session_handler(out)), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], ["delegate"], actor=alice)
    b = net.create_agent("B", "b", [], ["delegate"], actor=bob)
    g, chat = joint(net, alice, "bob@x.io", bob)
    net.send_message(chat.id, "公开的安排", actor=alice)
    net._append(chat, Message(id="msg-private", role="agent", agent_id=a.id, text="只给 alice 的内容", audience=[alice]))
    net.send_message(chat.id, "@A 写一份内部报告", actor=alice, background=False)
    r = next(iter(net.store.requests.values()))
    assert not net.can_see_request(r, bob)
    s = net.create_session(g.id, actor=bob)
    assert s.agent_id == b.id
    net.send_message(s.id, f"读群聊，再读任务 {r.id}", actor=bob, background=False)
    assert "公开的安排" in out["chat"] and "只给 alice 的内容" not in out["chat"]
    assert "没有你的所有者看得到的任务" in out["request"]
    assert "公开的安排" in out["prompts"][-1]  # 项目现状里有公开的群聊消息
    assert "只给 alice 的内容" not in out["prompts"][-1] and f"- {r.id}（" not in out["prompts"][-1]
    # 会话只给 bob：alice 看不到、收不到事件
    with pytest.raises(NetworkError, match="unknown conversation"):
        net.conversation_detail(s.id, actor=alice)
    assert s.id not in {c["id"] for c in net.snapshot(alice)["conversations"]}
    assert net.sessions(g.id, actor=alice) == []
    evs = [e for e in net.bus.since(0) if e.data.get("conversation_id") == s.id
           or (e.data.get("conversation") or {}).get("id") == s.id]
    assert evs and all(net.event_view(e.type, e.data, alice) is None for e in evs)
    assert any(net.event_view(e.type, e.data, bob) is not None for e in evs)
    # bob 退出后会话归档，只读
    net.leave_project(g.id, actor=bob)
    assert net.store.conversations[s.id].archived


def test_sessions_api(tmp_path: Path):
    """不变量 18：会话接口只对它的人开放；换助理只能换成自己的 Agent。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    a2 = net.create_agent("A2", "a2", [], [], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=bob)
    g, _ = joint(net, alice, "bob@x.io", bob)
    ca, cb = client(net, "alice@x.io"), client(net, "bob@x.io")
    s = ca.post(f"/api/projects/{g.id}/sessions", json={"title": "细节"}).json()
    assert (s["kind"], s["agent_id"], s["title"]) == ("session", a.id, "细节")
    assert [x["id"] for x in ca.get(f"/api/projects/{g.id}/sessions").json()] == [s["id"]]
    assert cb.get(f"/api/projects/{g.id}/sessions").json() == []
    assert cb.get(f"/api/conversations/{s['id']}").status_code == 404
    assert cb.patch(f"/api/sessions/{s['id']}", json={"title": "x"}).status_code == 404
    assert cb.post(f"/api/conversations/{s['id']}/messages", json={"text": "hi"}).status_code == 404
    assert ca.patch(f"/api/sessions/{s['id']}", json={"title": "改名"}).json()["title"] == "改名"
    assert ca.put("/api/me/assistant", json={"agent_id": b.id}).status_code == 404
    assert ca.put("/api/me/assistant", json={"agent_id": a2.id}).json()["assistant_id"] == a2.id
    assert ca.get("/api/me").json()["assistant_id"] == a2.id
