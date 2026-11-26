"""项目：成员集合、委托边界与项目档案（docs/projects.md）。用户任务的项目归属由网络认定。"""
import re
from pathlib import Path

import pytest

from matrix.engine import ExecContext, NeedsConfirm, Network, NetworkError
from matrix.models import Message, seat_id
from matrix.runtime import ScriptCall, ScriptedRuntime
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
def test_assistant_joins_only_when_asked(tmp_path: Path):
    """不变量 18：第一个 Agent 默认是助理；建项目、接受邀请都不会把助理带进来，助理和别的成员一样能加能移出；
    可以换助理；删掉助理改用最早的另一个。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=alice)
    net.create_agent("Bob 的助理", "", [], [], actor=bob)
    assert net.store.users[alice].assistant_id == a.id
    p = net.create_project("P", "", [b.id], actor=alice)
    assert p.member_ids == [b.id]
    net.invite(p.id, "bob@x.io", actor=alice)
    net.respond_invitation(p.id, True, actor=bob)
    assert p.member_ids == [b.id]  # 人进来了，助理没有跟着
    net.add_member(p.id, a.id, actor=alice)
    net.remove_member(p.id, a.id, actor=alice)  # 助理也能移出
    net.set_assistant(b.id, actor=alice)
    net.delete_agent(b.id, actor=alice)
    assert net.store.users[alice].assistant_id == a.id


def test_session_needs_no_assistant(tmp_path: Path):
    """不变量 18：项目会话不需要助理。开会话不动项目成员；默认成员是已经在项目里的助理，或开会话时选的成员，
    都没有时 @ 谁谁接、没 @ 的消息只留在会话里；明确带上助理（join）才把它加进项目并在群聊里记事件。"""
    runs: list[str] = []

    def handler(call: ScriptCall) -> str:
        runs.append(call.agent_id)
        return "收到"
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    empty = net.create_session(net.create_project("空", "", [], actor=bob).id, actor=bob)  # 一个 Agent 都没有也能开
    assert empty.agent_id is None
    a = net.create_agent("A", "a", [], [], actor=alice)                 # alice 的助理，不在项目里
    w = net.create_agent("Writer", "w", [], [], actor=alice)
    p = net.create_project("P", "", [w.id], actor=alice)
    s = net.create_session(p.id, "了解进度", actor=alice)
    assert (s.kind, s.project_id, s.agent_id, s.owner_id, s.title) == ("session", p.id, None, alice, "了解进度")
    assert p.member_ids == [w.id]                                         # 助理没有被带进来
    net.send_message(s.id, "大家好", actor=alice, background=False)       # 没 @、没有默认成员：只留在会话里
    assert runs == [] and s.messages[-1].text == "大家好"
    net.send_message(s.id, "@Writer 写个提纲", actor=alice, background=False)
    assert runs == [w.id]
    # 开会话时选默认成员
    s2 = net.create_session(p.id, actor=alice, agent="Writer")
    assert s2.agent_id == w.id and s2.id != s.id
    assert net.create_session(p.id, actor=alice, agent=w.id).id == s2.id  # 同一个默认成员的空会话：沿用
    with pytest.raises(NetworkError, match="不是项目"):
        net.create_session(p.id, actor=alice, agent="A")
    # 明确带上助理
    with pytest.raises(NetworkError, match="助理"):
        net.create_session(p.id, actor=alice, join=w.id)
    s3 = net.create_session(p.id, actor=alice, join=a.id)
    assert s3.agent_id == a.id and a.id in p.member_ids
    chat = net.project_chat(p.id, actor=alice)
    assert any(m.role == "event" and m.text == "A 加入了项目（alice@x.io 的助理）" for m in chat.messages)
    assert net.create_session(p.id, actor=alice).agent_id == a.id       # 助理已经在项目里：默认就是它
    net.send_message(s3.id, "在吗", actor=alice, background=False)
    assert runs[-1] == a.id
    # 归档与恢复
    net.update_session(s3.id, title="细节", archived=True, actor=alice)
    with pytest.raises(NetworkError, match="已归档"):
        net.send_message(s3.id, "还在吗", actor=alice)
    net.update_session(s3.id, archived=False, actor=alice)
    assert net.store.conversations[s3.id].title == "细节"
    # 默认成员移出项目：会话照样能用，没 @ 的只留在会话里
    net.remove_member(p.id, a.id, actor=alice)
    n = len(runs)
    net.send_message(s3.id, "还在吗", actor=alice, background=False)
    assert len(runs) == n
    net.send_message(s3.id, "@Writer 你在吗", actor=alice, background=False)
    assert runs[-1] == w.id


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
    s = net.create_session(p.id, actor=alice, join=a.id)
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


def test_session_mentions_any_member_as_a_guest(tmp_path: Path):
    """不变量 18：项目会话里能 @ 项目里的任何 Agent。别人的 Agent 被 @ 进来时是客人：没有读项目现状的工具与摘要，
    提示词写明会话属于谁；它的所有者看不到会话，只在 guest_rounds 里看到经过它的那几轮；会话主人自己的 Agent 不算。"""
    runs: list[tuple[str, str, list[str]]] = []

    def handler(call: ScriptCall) -> str:
        runs.append((call.agent_id, call.prompt, list(call.tool_names)))
        return "收到"
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)            # alice 的助理
    w = net.create_agent("Writer", "w", [], [], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=bob)             # bob 的助理，接受邀请时进项目
    g, _ = joint(net, alice, "bob@x.io", bob, members=[w.id, a.id])
    s = net.create_session(g.id, actor=alice)
    assert s.agent_id == a.id
    members = net.conversation_detail(s.id, actor=alice)["members"]
    assert members[0] == a.id and set(members) == {a.id, w.id, b.id}
    net.send_message(s.id, "@Writer 写个提纲", actor=alice, background=False)
    assert runs[-1][0] == w.id and "（@ 你）" in runs[-1][1] and "所有者 @ 你时由你接手" in runs[-1][1]
    assert any(t.endswith("read_chat") for t in runs[-1][2])        # 会话主人自己的 Agent：有读项目现状的工具
    net.send_message(s.id, "提纲怎么样", actor=alice, background=False)
    assert runs[-1][0] == a.id and "没 @ 任何人的消息由你回答" in runs[-1][1]
    net.send_message(s.id, "@B 你看看报销规则", actor=alice, background=False)
    who, prompt, tools = runs[-1]
    assert who == b.id and "这个会话属于 alice@x.io" in prompt and "项目现状" not in prompt
    assert "写个提纲" not in prompt and "提纲怎么样" not in prompt   # 会话里没 @ 它的部分不给它
    assert not any(t.endswith("read_chat") or t.endswith("read_request") for t in tools)
    assert s.messages[-1].agent_id == b.id and s.messages[-1].text == "收到"
    # 会话仍只属于 alice；bob 只看得到经过 B 的那几轮
    with pytest.raises(NetworkError, match="unknown conversation"):
        net.conversation_detail(s.id, actor=bob)
    rounds = net.guest_rounds(b.id, actor=bob)
    assert len(rounds) == 1 and "title" not in rounds[0] and rounds[0]["caller"] == "alice@x.io"
    assert [m["text"] for m in rounds[0]["messages"]] == ["@B 你看看报销规则", "收到"]
    assert net.guest_rounds(w.id, actor=alice) == []                 # 自己的会话里自己的 Agent：不算客人
    # 附件也一样：没 @ 它的消息上的不给它，@ 它的那条上的给
    private = net.upload_attachment(s.id, "私.txt", b"private", "text/plain", actor=alice)
    net.send_message(s.id, "@Writer 看这份", actor=alice, attachments=[private], background=False)
    shared = net.upload_attachment(s.id, "公.txt", b"shared", "text/plain", actor=alice)
    net.send_message(s.id, "@B 看这份", actor=alice, attachments=[shared], background=False)
    seat = ExecContext(kind="seat", id=seat_id(s.id, b.id), agent_id=b.id, conversation_id=s.id, project_id=g.id,
                       authority=[], constraints=[], depth=0, chain=[b.id], guest=True)
    with net._lock:
        assert [x["name"] for x in net._attachments_for(seat)] == ["公.txt"]
    with pytest.raises(NetworkError):
        net.guest_rounds(b.id, actor=alice)

def test_new_session_reuses_the_empty_one(tmp_path: Path):
    """没发过消息的会话不进侧栏：再点“新会话”沿用它，不攒空会话；发过消息、归档了的才另开。"""
    net, alice, bob, a, p = setup(tmp_path)
    s1 = net.create_session(p.id, actor=alice)
    assert net.create_session(p.id, actor=alice).id == s1.id
    assert net.create_session(p.id, "细节", actor=alice).id == s1.id and s1.title == "细节"
    net.send_message(s1.id, "进展如何", actor=alice, background=False)
    s2 = net.create_session(p.id, actor=alice)
    assert s2.id != s1.id and s2.title == "新会话"
    net.update_session(s2.id, archived=True, actor=alice)
    assert net.create_session(p.id, actor=alice).id not in (s1.id, s2.id)


def test_session_title_follows_the_first_message(tmp_path: Path):
    """新会话发第一条消息时按内容起标题；人自己起过名的、之后的消息都不改。"""
    net, alice, bob, a, p = setup(tmp_path)
    s1 = net.create_session(p.id, actor=alice)
    seq = net.bus.last_seq
    net.send_message(s1.id, "## 竞品定价\n**白板**定价策略怎么定？顺便看看 https://e.com", actor=alice, background=False)
    assert s1.title == "竞品定价"
    assert any(e.type == "conversation.updated" and e.data["conversation"]["title"] == "竞品定价" for e in net.bus.since(seq))
    net.send_message(s1.id, "再补充一点别的", actor=alice, background=False)
    assert s1.title == "竞品定价"  # 只看第一条
    s2 = net.create_session(p.id, "我起的名", actor=alice)
    net.send_message(s2.id, "进展如何", actor=alice, background=False)
    assert s2.title == "我起的名"
    s3 = net.create_session(p.id, actor=alice)
    net.send_message(s3.id, "把报告里的结论整理成一页给老板看的材料，下周开会要用，格式简单点", actor=alice, background=False)
    assert s3.title == "把报告里的结论整理成一页给老板看的材料，下周开会…"


def test_auto_title():
    from matrix.engine import auto_title
    assert auto_title("@知识管家 帮我对比两家的定价：\n- 甲") == "帮我对比两家的定价"
    assert auto_title("<b>hi</b> there. second") == "hi there"
    assert auto_title("看 [报告](wiki/r.md) 的结论") == "看 报告 的结论"
    assert auto_title("```\ncode\n```") == "新会话"
    assert auto_title("", [{"name": "需求.docx"}]) == "需求.docx"


def test_sessions_api(tmp_path: Path):
    """不变量 18：会话接口只对它的人开放；换助理只能换成自己的 Agent。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    a2 = net.create_agent("A2", "a2", [], [], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=bob)             # bob 的助理，接受邀请时进项目
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


def test_project_default_agent(tmp_path: Path):
    """项目的默认回答成员：建项目时定、主持人能换；开会话没指定就用它（排在已在项目里的助理前面）；
    已开的会话各记各的、能单独换；它离开项目就清空。"""
    runs: list[str] = []

    def handler(call: ScriptCall) -> str:
        runs.append(call.agent_id)
        return "收到"
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)                 # alice 的助理
    w = net.create_agent("Writer", "w", [], [], actor=alice)
    r = net.create_agent("Reviewer", "r", [], [], actor=alice)
    with pytest.raises(NetworkError, match="不是项目"):
        net.create_project("P", "", [w.id], actor=alice, default_agent=r.id)
    ca, cb = client(net, "alice@x.io"), client(net, "bob@x.io")
    p = ca.post("/api/projects", json={"name": "P", "member_ids": [a.id, w.id, r.id], "default_agent": "Writer"}).json()
    assert p["default_agent_id"] == w.id
    s = ca.post(f"/api/projects/{p['id']}/sessions", json={}).json()
    assert s["agent_id"] == w.id                                          # 不是助理 A
    assert ca.post(f"/api/conversations/{s['id']}/messages", json={"text": "写个提纲"}).status_code == 200
    net.wait_quiet()
    assert runs == [w.id]
    # 换项目的默认：只影响新开的会话
    assert ca.patch(f"/api/projects/{p['id']}", json={"default_agent": r.id}).json()["default_agent_id"] == r.id
    assert net.store.conversations[s["id"]].agent_id == w.id
    assert ca.post(f"/api/projects/{p['id']}/sessions", json={}).json()["agent_id"] == r.id
    assert ca.patch(f"/api/projects/{p['id']}", json={"default_agent": "nobody"}).status_code == 409
    # 换已开的会话的默认成员；null = 不要默认成员；只改标题不动它
    assert ca.patch(f"/api/sessions/{s['id']}", json={"agent": "Reviewer"}).json()["agent_id"] == r.id
    ca.post(f"/api/conversations/{s['id']}/messages", json={"text": "看一下"})
    net.wait_quiet()
    assert runs[-1] == r.id
    assert ca.patch(f"/api/sessions/{s['id']}", json={"title": "改名"}).json()["agent_id"] == r.id
    assert ca.patch(f"/api/sessions/{s['id']}", json={"agent": None}).json()["agent_id"] is None
    assert ca.patch(f"/api/sessions/{s['id']}", json={"agent": "nobody"}).status_code == 409
    # 只有主持人能换项目的默认
    net.invite(p["id"], "bob@x.io", actor=alice)
    net.respond_invitation(p["id"], True, actor=bob)
    assert cb.patch(f"/api/projects/{p['id']}", json={"default_agent": w.id}).status_code in (404, 409)
    assert cb.post(f"/api/projects/{p['id']}/sessions", json={}).json()["agent_id"] == r.id   # 参与者开会话也用它
    # 默认成员离开项目：清空；null 也能清
    assert ca.delete(f"/api/projects/{p['id']}/members/{r.id}").json()["default_agent_id"] is None
    assert ca.post(f"/api/projects/{p['id']}/sessions", json={"title": "新"}).json()["agent_id"] == a.id  # 退回已在项目里的助理
    ca.patch(f"/api/projects/{p['id']}", json={"default_agent": w.id})
    assert ca.patch(f"/api/projects/{p['id']}", json={"default_agent": None}).json()["default_agent_id"] is None
