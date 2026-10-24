"""信息只流向当时在场的人（不变量 22）：来源标签、挂载、open_note 两段式、会话收窄、拦截与确认。"""

from __future__ import annotations

from pathlib import Path

import pytest

from matrix import flow
from matrix.engine import Network, NetworkError
from matrix.models import TaskStatus
from matrix.runtime import ScriptCall, ScriptedRuntime
from tests.test_users import client, joint, users

HOME = "agent-home"


def setup(tmp_path: Path, handler, authority=("file.write", "web.research")):
    """alice 的 A（她的助理）在只有她的项目 Q 里写过一条笔记；A 也在 bob 主持的共享项目 J 里。"""
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], list(authority), actor=alice)
    q = net.create_project("Q", "", [a.id], actor=alice)
    net.send_message(net.project_chat(q.id, actor=alice).id, "@A 记下：报价底线 50 万", actor=alice, background=False)
    g, chat = joint(net, bob, "alice@x.io", alice)
    return net, alice, bob, a, q, g, chat


def writes_note(call: ScriptCall) -> str | None:
    if "（@ 你）：@A 记下：报价底线" in call.prompt and (call.cwd / HOME).exists():
        (call.cwd / HOME / "q.md").write_text("报价底线 50 万", encoding="utf-8")
        return "记下了"
    return None


# ================= 纯函数 =================
def test_flow_rules_are_set_comparisons():
    """不变量 22：给 audience 看，每个来源场合在场的人都要包含 audience，或者有授权；没有标签按 ME。"""
    present = {"me": {"u1"}, "P": {"u1", "u2"}, "Q": {"u1"}}.get
    assert flow.passes(["P"], {"u1", "u2"}, present) == []
    assert flow.passes(["P", "Q"], {"u1", "u2"}, present) == ["Q"]
    assert flow.passes(["Q"], {"u1", "u2"}, present, granted=["Q"]) == []
    assert flow.venues_of({}, "old.md") == ["me"]
    assert flow.mountable("skills/x/SKILL.md", {}, {"u1", "u2"}, present)
    assert not flow.mountable("old.md", {}, {"u1", "u2"}, present)
    labels = flow.relabel({}, ["old.md", "new.md"], [], "P", ["Q"], False, existed=["old.md"])
    assert labels == {"old.md": {"venues": ["me", "P", "Q"], "mixed": False},
                      "new.md": {"venues": ["P", "Q"], "mixed": False}}
    assert flow.relabel(labels, [], ["new.md"], "P", [], True) == {"old.md": labels["old.md"]}


# ================= 挂载与标签 =================
def test_notes_from_elsewhere_are_hidden_and_labeled(tmp_path: Path):
    """不变量 22：Q 里写的笔记带 Q 的标签；在有 bob 的 J 里不挂载、只列名字，并给 open_note。"""
    mounted: list[set[str]] = []

    def handler(call: ScriptCall) -> str:
        if (r := writes_note(call)) is not None:
            return r
        home = call.cwd / HOME
        mounted.append({p.name for p in home.rglob("*") if p.is_file()})
        if "写一条" in call.prompt:
            (home / "j.md").write_text("J 的进展", encoding="utf-8")
        return "ok"

    net, alice, bob, a, q, g, chat = setup(tmp_path, handler)
    labels = flow.load(net._flow_path(a.id))
    assert labels["q.md"]["venues"] == [q.id]
    net.send_message(chat.id, "@A 写一条进展", actor=bob, background=False)
    assert mounted[-1] == set()
    call = net.runtime.calls[-1]
    assert "`q.md`" in call.prompt and "mcp__matrix_net__open_note" in call.tool_names
    assert flow.load(net._flow_path(a.id))["j.md"] == {"venues": [g.id], "mixed": True}  # 有别人在场写的
    net.send_message(chat.id, "@A 再看看", actor=bob, background=False)
    assert mounted[-1] == {"j.md"}
    # 回到只有 alice 的 Q：两个都挂（alice 在 J 里在场）
    net.send_message(net.project_chat(q.id, actor=alice).id, "@A 看看", actor=alice, background=False)
    assert mounted[-1] == {"q.md", "j.md"}


def test_hidden_file_written_back_becomes_conflict_copy(tmp_path: Path):
    """不变量 22：没挂上的文件在这一轮里被同名写出，写回成冲突副本，不覆盖原来的笔记。"""
    def handler(call: ScriptCall) -> str:
        if (r := writes_note(call)) is not None:
            return r
        (call.cwd / HOME / "q.md").write_text("J 里写的", encoding="utf-8")
        return "ok"

    net, alice, bob, a, q, g, chat = setup(tmp_path, handler)
    net.send_message(chat.id, "@A 写", actor=bob, background=False)
    home = net.agent_home(a.id)
    assert (home / "q.md").read_text(encoding="utf-8") == "报价底线 50 万"
    assert any(p.name.startswith("q.md.conflict-") or ".conflict-" in p.name for p in home.iterdir())


def test_skills_are_read_only_where_others_are_present(tmp_path: Path):
    """不变量 22：有别人的场合里技能照常挂载但改动不写回，免得别人往技能里投毒。"""
    def handler(call: ScriptCall) -> str:
        if (r := writes_note(call)) is not None:
            return r
        s = call.cwd / HOME / "skills" / "quote"
        s.mkdir(parents=True, exist_ok=True)
        (s / "SKILL.md").write_text("改过", encoding="utf-8")
        return "ok"

    net, alice, bob, a, q, g, chat = setup(tmp_path, handler)
    skill = net.agent_home(a.id) / "skills" / "quote"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("原样", encoding="utf-8")
    net.send_message(chat.id, "@A 改技能", actor=bob, background=False)
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "原样"
    assert "技能这一轮只读" in net.runtime.calls[-1].prompt
    net.send_message(net.direct_chat(a.id, actor=alice).id, "改技能", actor=alice, background=False)
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "改过"


# ================= open_note 两段式与拦截 =================
def leaky(call: ScriptCall) -> str:
    """bob 问报价：第一轮要笔记，拿到后照实回答。"""
    if (r := writes_note(call)) is not None:
        return r
    if "## 笔记 `q.md`" in call.prompt:
        return "底线是 50 万"
    if "不用那些笔记" in call.prompt:
        return "这个我得问问 alice"
    if "能便宜吗" in call.prompt:
        call.tool("open_note", path="q.md")  # 还能联网：只排到下一轮
        return "我查一下资料"
    return "ok"


def test_open_note_is_deferred_and_reply_is_held(tmp_path: Path):
    """不变量 22：还能联网的一轮里 open_note 只排到下一轮；下一轮没有联网能力，回复拦下给所有者确认。"""
    net, alice, bob, a, q, g, chat = setup(tmp_path, leaky)
    net.send_message(chat.id, "@A 能便宜吗", actor=bob, background=False)
    first, second = net.runtime.calls[-2:]
    assert "web.research" in first.authority and "web.research" not in second.authority
    assert "## 笔记 `q.md`" in second.prompt
    seat = chat.seats[a.id]
    assert seat.session_sources == [q.id]
    bob_view = net.conversation_detail(chat.id, actor=bob)
    texts = [m["text"] for m in bob_view["messages"]]
    assert "我查一下资料" in texts and not any("50 万" in t for t in texts)
    assert any("要先给它的所有者确认" in t for t in texts)
    assert bob_view["held"] == {}
    held = net.board(alice)["held"]
    assert len(held) == 1 and held[0]["text"] == "底线是 50 万" and held[0]["source_names"] == ["「Q」"]
    assert net.board(bob)["held"] == []
    with pytest.raises(NetworkError, match="unknown held"):
        net.decide_held(held[0]["id"], "send", actor=bob)
    net.decide_held(held[0]["id"], "send", actor=alice)
    assert "底线是 50 万" in [m["text"] for m in net.conversation_detail(chat.id, actor=bob)["messages"]]
    # 只放行这一次：会话仍带着 Q，下一条回复还要问
    net.send_message(chat.id, "@A 还有吗", actor=bob, background=False)
    assert len(net.board(alice)["held"]) == 1


def test_grant_lets_later_replies_through_and_lifts_narrowing(tmp_path: Path):
    """不变量 22：授权（Q → J）写进项目，之后自动放行，会话也不再收窄；授权只给 Agent 的所有者看。"""
    net, alice, bob, a, q, g, chat = setup(tmp_path, leaky)
    net.send_message(chat.id, "@A 能便宜吗", actor=bob, background=False)
    h = net.board(alice)["held"][0]
    net.decide_held(h["id"], "grant", actor=alice)
    assert net.store.projects[g.id].flow_grants == {a.id: [q.id]}
    assert net.project_detail(g.id, actor=bob)["flow_grants"] == {}
    net.send_message(chat.id, "@A 还有吗", actor=bob, background=False)
    assert net.board(alice)["held"] == [] and "web.research" in net.runtime.calls[-1].authority
    assert "q.md" in {p.name for p in (net.agent_home(a.id)).iterdir()}


def test_rewrite_starts_a_new_session_without_the_notes(tmp_path: Path):
    """不变量 22：重写 = 换新会话、清掉来源，原来的问题重新送达；新回复不再拦。"""
    net, alice, bob, a, q, g, chat = setup(tmp_path, leaky)
    net.send_message(chat.id, "@A 能便宜吗", actor=bob, background=False)
    h = net.board(alice)["held"][0]
    net.decide_held(h["id"], "rewrite", actor=alice)
    net.wait_quiet()
    last = net.runtime.calls[-1]
    assert not last.resumed and "能便宜吗" in last.prompt and "不用那些笔记" in last.prompt
    assert chat.seats[a.id].session_sources == []
    texts = [m["text"] for m in net.conversation_detail(chat.id, actor=bob)["messages"]]
    assert "这个我得问问 alice" in texts and not any("50 万" in t for t in texts)
    assert net.store.held[h["id"]].status == "rewritten"


def test_note_returned_at_once_without_exfil_abilities(tmp_path: Path):
    """不变量 22：本来就不能联网、执行命令、用连接器的运行，open_note 当场返回。"""
    def handler(call: ScriptCall) -> str:
        if (r := writes_note(call)) is not None:
            return r
        if "能便宜吗" in call.prompt:
            return "底线 " + ("50 万" if "50 万" in call.tool("open_note", path="q.md") else "?")
        return "ok"

    net, alice, bob, a, q, g, chat = setup(tmp_path, handler, authority=("file.write",))
    net.send_message(chat.id, "@A 能便宜吗", actor=bob, background=False)
    assert [h["text"] for h in net.board(alice)["held"]] == ["底线 50 万"]


def test_outbound_tools_refused_without_violation(tmp_path: Path):
    """不变量 22：会话读过越界来源时，写进项目的工具不放行；这是收窄，不记违规。"""
    out: list[str] = []

    def handler(call: ScriptCall) -> str:
        if (r := writes_note(call)) is not None:
            return r
        if "能便宜吗" in call.prompt:
            call.tool("open_note", path="q.md")
            out.append(call.tool("record_decision", content="底线 50 万", reason="笔记"))
            return "ok"
        return "ok"

    net, alice, bob, a, q, g, chat = setup(tmp_path, handler, authority=("file.write",))
    net.send_message(chat.id, "@A 能便宜吗", actor=bob, background=False)
    assert "网络不放行" in out[0] and net.store.projects[g.id].decisions == []
    assert not any(m.role == "system" and "违规" in m.text for m in chat.messages)


def test_owner_alone_is_never_held(tmp_path: Path):
    """不变量 22：私聊里只有所有者，读哪里的笔记都直接回。"""
    def handler(call: ScriptCall) -> str:
        if (r := writes_note(call)) is not None:
            return r
        return "J 的事" if "J" in call.prompt else "ok"

    net, alice, bob, a, q, g, chat = setup(tmp_path, handler)
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "J 怎么样", actor=alice, background=False)
    assert dm.messages[-1].text == "J 的事" and net.board(alice)["held"] == []


# ================= 委托交付 =================
def test_delivery_is_held_and_workdir_hidden(tmp_path: Path):
    """不变量 22：受托方用到别处的资料，交付先给它的所有者确认；确认前委托方的所有者看不到它的工作目录。"""
    def handler(call: ScriptCall) -> str:
        if (r := writes_note(call)) is not None:
            return r
        if "委托 A" in call.prompt and call.can_delegate:
            call.tool("delegate", agent="A", mission="给报价建议", expected_outcome="一句话")
            return "已委托"
        if "给报价建议" in call.prompt:
            return "建议 " + ("50 万" if "50 万" in call.tool("open_note", path="q.md") else "?")
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], ["file.write"], actor=alice)
    q = net.create_project("Q", "", [a.id], actor=alice)
    net.send_message(net.project_chat(q.id, actor=alice).id, "@A 记下：报价底线 50 万", actor=alice, background=False)
    b = net.create_agent("B", "b", [], ["delegate"], actor=bob)
    g, chat = joint(net, bob, "alice@x.io", alice, [b.id])
    net.send_message(chat.id, "@B 委托 A", actor=bob, background=False)
    net.wait_quiet()
    task = next(t for t in net.store.tasks.values() if t.delegatee_id == a.id)
    assert task.status == TaskStatus.RUNNING and task.result is None
    held = net.board(alice)["held"]
    assert [h["kind"] for h in held] == ["delivery"]
    with pytest.raises(NetworkError):
        net.workdir_files("task", task.id, actor=bob)
    net.workdir_files("task", task.id, actor=alice)
    net.decide_held(held[0]["id"], "send", actor=alice)
    net.wait_quiet()
    assert task.result and "50 万" in task.result["report"]


def test_held_survives_restart(tmp_path: Path):
    """不变量 22：等确认的输出持久化；重启后还在，等交付的任务不判失败。"""
    net, alice, bob, a, q, g, chat = setup(tmp_path, leaky)
    net.send_message(chat.id, "@A 能便宜吗", actor=bob, background=False)
    again = Network(ScriptedRuntime(leaky), tmp_path, seed=False)
    assert [h["text"] for h in again.board(alice)["held"]] == ["底线是 50 万"]


# ================= 项目共享 =================
def test_invite_from_any_project_starts_sharing(tmp_path: Path):
    """不变量 16：任何项目都能邀请；第一次邀请时群聊席位换新会话，项目会话不动。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    g = net.create_project("P", "", [a.id], actor=alice)
    chat = net.project_chat(g.id, actor=alice)
    net.send_message(chat.id, "@A hi", actor=alice, background=False)
    assert chat.seats[a.id].dsh_session_id
    net.invite(g.id, "bob@x.io", actor=alice)
    assert g.shared and chat.seats[a.id].dsh_session_id is None
    assert any(m.text.startswith("项目开始与他人共享") for m in chat.messages)
    net.respond_invitation(g.id, False, actor=bob)
    assert not g.shared
    ca = client(net, "alice@x.io")
    assert ca.get(f"/api/projects/{g.id}").json()["kind"] == "internal"
    assert ca.post("/api/held/held-nope", json={"action": "send"}).status_code == 404
