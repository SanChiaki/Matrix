"""ScriptedRuntime 驱动的网络语义测试：群组内委托、权限、防环、知识隔离、持久化、DSH 覆盖层，
以及异步委托（收件箱 + 唤醒）、验收、续接、提议新建 Agent、私有目录、按 Agent 配模型。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agentnet.engine import Limits, Network, NetworkError
from agentnet.models import MCP_PREFIX, TaskStatus
from agentnet.runtime import (ScriptCall, ScriptedRuntime, ToolBridge, build_patch_yaml, classify_error,
                              permission_mode)
from agentnet.scripted import scenario_handler

GOAL = "我要研究一个新的 AI 产品机会。"


def ids(net: Network, *names: str) -> list[str]:
    return [net.store.agent_by_name(n).id for n in names]


@pytest.fixture
def scenario(tmp_path: Path):
    rt = ScriptedRuntime(scenario_handler)
    net = Network(rt, tmp_path)
    lead = net.store.agent_by_name("Product Strategy Agent")
    group = net.store.groups_of(lead.id)[0]
    conv = net.create_conversation(group.id)
    net.send_message(conv.id, f"@{lead.name} {GOAL}", background=False)
    return net, rt, conv


def talk(net: Network, agent, group, text: str, conv=None):
    """在群聊里 @ 一个成员（没有群组时开私聊）；返回对话。"""
    if conv is None:
        conv = net.create_conversation(group.id) if group is not None else net.create_conversation(agent_id=agent.id)
    net.send_message(conv.id, f"@{agent.name} {text}" if conv.kind == "group" else text, background=False)
    return conv


def seat(net: Network, conv, agent):
    return net.store.conversations[conv.id].seats[agent.id]


def calls_of(rt: ScriptedRuntime, conv, agent):
    return [c for c in rt.calls if c.context_id == f"{conv.id}/{agent.id}"]


def tasks(net: Network):
    return list(net.store.tasks.values())


def by_target(net: Network, name: str):
    a = net.store.agent_by_name(name)
    return [t for t in tasks(net) if t.delegatee_id == a.id]


def tid(text: str) -> str:
    return re.search(r"(task-[0-9a-f]{8})", text).group(1)


def is_task(call: ScriptCall) -> bool:
    return "你收到一份委托" in call.prompt or call.context_id.startswith("task-")


def pair(net: Network, a_auth=("delegate", "web.research"), b_auth=("web.research",)):
    a = net.create_agent("A", "A", ["a"], list(a_auth))
    b = net.create_agent("B", "B", ["b"], list(b_auth))
    g = net.create_group("G", "", [a.id, b.id])
    return a, b, g


# ================= 种子与基本委托 =================
def test_seed_network(tmp_path: Path):
    net = Network(ScriptedRuntime(scenario_handler), tmp_path)
    assert len(net.store.agents) == 5 and len(net.store.groups) == 1
    g = next(iter(net.store.groups.values()))
    legal = net.store.agent_by_name("Legal Compliance Agent")
    assert legal.id not in g.member_ids


def test_conversation_delegates_within_group(scenario):
    """异步：第一轮发出委托就结束；交付唤醒对话 Agent 验收；全部有结果后整合回复。"""
    net, rt, conv = scenario
    assert conv.status == "idle"
    talk_msgs = [m for m in conv.messages if m.role != "event"]
    first, last = talk_msgs[1], talk_msgs[-1]
    assert first.role == "agent" and "已委托 3 个成员" in first.text
    assert last.role == "agent" and "机会成立" in last.text
    req = next(iter(net.store.requests.values()))  # 被 @ 的事登记为用户任务，整合后交付
    assert req.status == "in_review" and req.requester_id is None
    assert sorted(req.task_ids) == sorted(t.id for t in tasks(net) if t.status != TaskStatus.REJECTED)  # 被拒的不算
    done = [t for t in tasks(net) if t.status == TaskStatus.COMPLETED]
    assert len(done) == 4  # 市场 ×2、技术、竞争：全部经过 accept_task
    assert not any(t.auto_accepted for t in done)
    assert {t for m in conv.messages for t in m.task_ids} == {t.id for t in tasks(net)}
    assert any(m.wake for m in conv.messages[2:])  # 后续回合由委托回报唤醒
    assert len(by_target(net, "Market Research Agent")) == 2  # 同一成员被复用，不创建新节点


def test_delegate_returns_immediately(tmp_path: Path):
    """delegate 不等待执行：返回时任务还在 running，委托方这一轮先结束。"""
    seen: dict[str, str] = {}

    def handler(call: ScriptCall) -> str:
        if is_task(call):
            seen["task_ran_after"] = seen.get("returned", "no")
            return "B done"
        if call.turn == 1:
            r = call.tool("delegate", agent="B", mission="m", expected_outcome="o")
            seen["returned"] = r
            seen["status_at_return"] = net.store.tasks[tid(r)].status.value
            return "委托了"
        return "收到"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a, b, g = pair(net)
    talk(net, a, g, "go")
    assert "已受理" in seen["returned"] and seen["status_at_return"] == "running"
    assert seen["task_ran_after"] != "no"


def test_outside_group_rejected_no_autocreate(scenario):
    """不变量 2。"""
    net, _, _ = scenario
    rejected = [t for t in tasks(net) if t.status == TaskStatus.REJECTED]
    assert len(rejected) == 1 and rejected[0].delegatee_id is None
    assert "不是群组" in rejected[0].error
    assert len(net.store.agents) == 5


def test_authority_is_agent_own_intersect_grant(scenario):
    """不变量 1。"""
    net, _, _ = scenario
    tech = by_target(net, "Technical Analysis Agent")[0]
    # 委托方请求 web.research + shell.exec；执行者自身权限是 web.research + file.write
    assert tech.granted_authority == ["web.research"]
    assert tech.denied_authority == ["shell.exec"]
    # 不指定 authority = 不额外收窄：取执行者自身权限 ∩ 委托方有效权限
    market = by_target(net, "Market Research Agent")[0]
    assert market.granted_authority == ["web.research"]


def test_grant_cannot_exceed_delegator(tmp_path: Path):
    """不变量 1。"""
    def handler(call: ScriptCall) -> str:
        if call.can_delegate and not is_task(call) and call.turn == 1:
            call.tool("delegate", agent="Worker", mission="m", expected_outcome="o",
                      authority=["web.research", "shell.exec"])
        return "done"

    net = Network(ScriptedRuntime(handler), tmp_path)
    lead = net.create_agent("Lead", "lead", ["plan"], ["delegate", "web.research"])
    worker = net.create_agent("Worker", "w", ["x"], ["web.research", "shell.exec"])
    g = net.create_group("G", "", [lead.id, worker.id])
    talk(net, lead, g, "hi")
    t = tasks(net)[0]
    assert t.granted_authority == ["web.research"]  # 委托方没有 shell.exec，给不出去
    assert t.denied_authority == ["shell.exec"]


def test_constraints_inherit_and_nested_no_cycle(tmp_path: Path):
    """不变量 3、4：嵌套委托：B 在等 C 时这一轮只算进展，C 交付唤醒 B 后 B 才交付给 A。约束沿链累加，成环被拒。"""
    seen: dict[str, str] = {}

    def handler(call: ScriptCall) -> str:
        name = "A" if "**A**" in call.prompt else "B" if "**B**" in call.prompt else seen.get(call.session_id, "C")
        seen.setdefault(call.session_id, name)
        name = seen[call.session_id]
        if not call.can_delegate:
            return f"{name} done"
        if name == "A":
            if call.turn == 1:
                call.tool("delegate", agent="B", mission="m1", expected_outcome="o", constraints="c1")
                return "等 B"
            return "A 整合完成"
        if name == "B":
            if call.turn == 1:
                seen["cycle"] = call.tool("delegate", agent="A", mission="back", expected_outcome="o")
                call.tool("delegate", agent="C", mission="m2", expected_outcome="o", constraints="c2")
                return "B 在等 C"
            return "B 交付（含 C 的结论）"
        return "C done"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a, b, c = (net.create_agent(n, n, [n.lower()], ["delegate", "web.research"]) for n in "ABC")
    g = net.create_group("G", "", [a.id, b.id, c.id])
    conv = talk(net, a, g, "go")
    t_c = next(t for t in tasks(net) if t.delegatee_id == c.id)
    t_b = next(t for t in tasks(net) if t.delegatee_id == b.id)
    assert t_c.depth == 2 and t_c.effective_constraints == ["c1", "c2"]
    assert t_c.parent_task_id == t_b.id
    assert "不能成环" in seen["cycle"]
    assert net._chain(t_c) == [a.id, b.id]
    assert t_b.notes == ["B 在等 C"] and t_b.result["report"] == "B 交付（含 C 的结论）"
    assert t_c.status == TaskStatus.COMPLETED and t_c.auto_accepted  # B 没显式验收：一轮结束视为接受
    assert conv.messages[-1].text == "A 整合完成"


def test_run_progress_is_live_and_kept_on_reply(tmp_path: Path):
    """轮次进行中：工具调用与中间文本实时推送并可查询；结束后作为过程留在回复上。委托任务同样可查。"""
    seen: dict[str, list] = {}

    def handler(call: ScriptCall) -> str:
        if is_task(call):
            call.say("正在检索")
            seen["task"] = net.task_detail(call.context_id)["live"]
            return "B done"
        if call.turn == 1:
            call.say("先看看群组成员")
            call.tool("list_members")
            seen["conv"] = net.conversation_detail(call.context_id.split("/")[0])["live"]
            call.tool("delegate", agent="B", mission="查资料", expected_outcome="摘要")
            return "## 委托了"
        return "## 结论\n完成"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a, b, g = pair(net)
    conv = talk(net, a, g, "go")

    assert [s["kind"] for s in seen["conv"][a.id]] == ["text", "tool"]  # 按席位给出实时过程
    assert seen["conv"][a.id][1]["tool"] == MCP_PREFIX + "list_members"
    assert [s["text"] for s in seen["task"]] == ["正在检索"]
    first = net.store.conversations[conv.id].messages[1]
    assert first.agent_id == a.id and [s["kind"] for s in first.steps] == ["text", "tool", "tool"]
    assert first.steps[2]["brief"] == "B"  # 委托只显示目标成员，不显示整段参数
    assert net.conversation_detail(conv.id)["live"] == {}
    progress = [e.data for e in net.bus.since(0) if e.type == "run.progress"]
    assert {p["conversation_id"] for p in progress} == {conv.id}
    assert [p["context"] for p in progress].count("task") == 1

    net2 = Network(ScriptedRuntime(handler), tmp_path)  # 过程随回复持久化
    assert len(net2.store.conversations[conv.id].messages[1].steps) == 3


def test_delegate_constraints_as_text(tmp_path: Path):
    """constraints 在工具 schema 里是字符串（每行一条），引擎拆成列表；旧的数组写法仍然兼容。"""
    def handler(call: ScriptCall) -> str:
        if not call.can_delegate or is_task(call) or call.turn > 1:
            return "done"
        call.tool("delegate", agent="B", mission="m", expected_outcome="o",
                  constraints="只用公开信息\n- 不编造数据；不评估技术实现\n\n")
        call.tool("delegate", agent="B", mission="m2", expected_outcome="o", constraints=["c1", " "])
        return "done"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a, b = (net.create_agent(n, n, [n.lower()], ["delegate", "web.research"]) for n in "AB")
    g = net.create_group("G", "", [a.id, b.id])
    talk(net, a, g, "go")
    t1, t2 = tasks(net)
    assert t1.effective_constraints == ["只用公开信息", "不编造数据", "不评估技术实现"]
    assert t2.effective_constraints == ["c1"]


def test_delegate_schema_keeps_free_text_in_strings():
    """自由文本只放 string 参数：数组参数的值要由模型直接写成 JSON，长中文容易被写成裸文本。"""
    from agentnet.mcp_server import TOOLS
    for tool in TOOLS:
        for key, spec in tool["inputSchema"]["properties"].items():
            if key in ("mission", "expected_outcome", "constraints", "feedback", "note", "reason", "description"):
                assert spec["type"] == "string", f"{tool['name']}.{key}"


def test_depth_limit_unmounts_tools(tmp_path: Path):
    """不变量 4。"""
    mounted: list[bool] = []

    def handler(call: ScriptCall) -> str:
        if is_task(call):
            mounted.append(any(t.endswith("__delegate") for t in call.tool_names))
            return "leaf"
        if call.turn == 1:
            call.tool("delegate", agent="B", mission="m", expected_outcome="o")
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path, Limits(max_depth=1))
    a = net.create_agent("A", "a", ["a"], ["delegate"])
    b = net.create_agent("B", "b", ["b"], ["delegate"])
    g = net.create_group("G", "", [a.id, b.id])
    talk(net, a, g, "go")
    assert mounted == [False]  # 协作工具卸下；群组档案工具仍在（不需要 delegate）


def test_delegation_limit_spans_rounds(tmp_path: Path):
    """不变量 4：委托次数上限按“一条用户消息引出的工作”累计，跨轮次有效；新的用户消息重新计数。"""
    results: list[str] = []

    def handler(call: ScriptCall) -> str:
        if is_task(call):
            return "ok"
        if "（@ 你）" in call.prompt or "交付了任务" in call.prompt:
            r = call.tool("delegate", agent="B", mission="m", expected_outcome="o")
            results.append("rejected" if "上限" in r else "ok")
        return "next"

    net = Network(ScriptedRuntime(handler), tmp_path, Limits(max_delegations=2))
    a, b, g = pair(net)
    conv = talk(net, a, g, "go")
    assert results[:3] == ["ok", "ok", "rejected"]
    talk(net, a, g, "再来", conv)  # 再次被 @：重新计数
    assert "ok" in results[3:]


def test_dm_has_no_network_tools(tmp_path: Path):
    """私聊：一对一，不用 @，不能委托。"""
    rt = ScriptedRuntime(scenario_handler)
    net = Network(rt, tmp_path)
    lead = net.store.agent_by_name("Product Strategy Agent")
    conv = talk(net, lead, None, GOAL)
    assert conv.kind == "dm" and not rt.calls[0].can_delegate
    assert sorted(t.removeprefix("mcp__agentnet__") for t in rt.calls[0].tool_names) == ["deliver_request", "open_request"]
    assert "不能委托" in conv.messages[-1].text and not tasks(net)
    assert conv.messages[0].mentions == [lead.id]


def test_conversation_rules(tmp_path: Path):
    net = Network(ScriptedRuntime(scenario_handler), tmp_path)
    legal = net.store.agent_by_name("Legal Compliance Agent")
    g = next(iter(net.store.groups.values()))
    conv = net.create_conversation(g.id)
    with pytest.raises(NetworkError, match="不是群组"):  # 只能 @ 群组成员
        net.send_message(conv.id, "hi", mentions=[legal.id])
    assert net.resolve_mentions(conv, f"@{legal.name} 你好") == []  # 文本里 @ 群组外的人：不唤醒
    net.update_agent(legal.id, chat_enabled=False)
    with pytest.raises(NetworkError, match="未开放"):
        net.create_conversation(agent_id=legal.id)
    with pytest.raises(NetworkError, match="未知权限"):
        net.create_agent("X Agent", "x", [], ["root"])
    with pytest.raises(NetworkError, match="群聊需要"):
        net.create_conversation()


def test_session_resumed_per_seat_isolated_per_task(scenario):
    """不变量 7。"""
    net, rt, conv = scenario
    lead = net.store.agent_by_name("Product Strategy Agent")
    lead_calls = calls_of(rt, conv, lead)
    assert not lead_calls[0].resumed and all(c.resumed for c in lead_calls[1:])
    sid = seat(net, conv, lead).dsh_session_id
    assert {c.session_id for c in lead_calls} == {sid}  # 唤醒回合续接同一会话
    talk(net, lead, None, "继续", conv)
    lead_calls = calls_of(rt, conv, lead)
    assert lead_calls[-1].resumed and lead_calls[-1].session_id == sid
    sessions = [t.dsh_session_id for t in tasks(net) if t.status != TaskStatus.REJECTED]
    assert all(sessions) and len(set(sessions)) == len(sessions)
    cwds = {str(c.cwd) for c in rt.calls if c.context_id.startswith("task-")}
    assert len(cwds) == len(sessions)


# ================= 群聊：被 @ 的成员接任务 =================
def test_mentioned_member_takes_task_without_lead(scenario):
    """没有主 Agent：@ 谁谁接。专项成员被 @ 时自己做，看得到群聊里之前的公开讨论。"""
    net, rt, conv = scenario
    market = net.store.agent_by_name("Market Research Agent")
    before = len(tasks(net))
    talk(net, market, None, "基于上面的结论补一句市场判断", conv)
    reply = [m for m in conv.messages if m.role != "event"][-1]
    assert reply.agent_id == market.id and "已参考前面的讨论" in reply.text
    assert len(tasks(net)) == before  # 自己做，没有转手
    prompt = calls_of(rt, conv, market)[0].prompt
    assert "**Product Strategy Agent**：机会成立" in prompt  # 公开消息可见
    assert "（@ 你）" in prompt and "交付了任务" not in prompt  # 委托回报只给委托方
    # 席位彼此隔离：各自的会话与工作目录
    lead = net.store.agent_by_name("Product Strategy Agent")
    assert seat(net, conv, market).dsh_session_id != seat(net, conv, lead).dsh_session_id
    assert seat(net, conv, market).workdir != seat(net, conv, lead).workdir


def test_message_without_mention_wakes_nobody(tmp_path: Path):
    """没 @ 任何人：消息留在群聊里，不唤醒任何人；之后被 @ 的成员能看到它。"""
    rt = ScriptedRuntime(lambda call: "ok")
    net = Network(rt, tmp_path)
    a, b, g = pair(net)
    conv = net.create_conversation(g.id)
    net.send_message(conv.id, "先说个背景：预算只有 10 万", background=False)
    assert rt.calls == [] and conv.messages[-1].mentions == []
    talk(net, b, None, "按预算给建议", conv)
    assert len(rt.calls) == 1 and "预算只有 10 万" in rt.calls[0].prompt


def test_mention_several_members_runs_in_parallel_seats(tmp_path: Path):
    """一条消息 @ 两个成员：各自一个席位并行处理，各自回复；第二次唤醒只送新消息。"""
    rt = ScriptedRuntime(lambda call: f"{'A' if '**A**' in call.prompt else 'B'} 回复 {call.turn}")
    net = Network(rt, tmp_path)
    a, b, g = pair(net)
    conv = net.create_conversation(g.id)
    net.send_message(conv.id, "@A @B 各说一句", background=False)
    assert set(conv.messages[0].mentions) == {a.id, b.id}
    assert {m.agent_id for m in conv.messages[1:]} == {a.id, b.id}
    talk(net, a, None, "再说一句", conv)
    second = calls_of(rt, conv, a)[-1].prompt
    assert "各说一句" not in second and "再说一句" in second and "**B**：B 回复 1" in second


def test_one_chat_per_group_and_per_agent(tmp_path: Path):
    """参照 Raft：群组就是群聊，建群组即有，不会重复创建；每个 Agent 一个私聊，第一次打开时创建。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path)
    seed = next(iter(net.store.groups.values()))
    assert net._group_chat_of(seed.id) is not None  # 种子群组自带群聊
    a, b, g = pair(net)
    chat = net._group_chat_of(g.id)
    assert chat is not None and chat.title == "G"
    assert net.create_conversation(g.id).id == net.group_chat(g.id).id == chat.id
    dm = net.direct_chat(a.id)
    assert dm.kind == "dm" and net.direct_chat(a.id).id == dm.id
    net.update_group(g.id, name="G2")
    assert chat.title == "G2"
    with pytest.raises(NetworkError, match="不能删除"):
        net.delete_conversation(chat.id)
    talk(net, a, None, "hi", chat)
    net.clear_conversation(chat.id)
    assert chat.messages == [] and chat.seats == {}


def test_members_can_change_any_time(tmp_path: Path):
    """成员随时加减：变动写进群聊；新成员马上可以被 @ 和被委托；移出的成员不能再被 @。"""
    prompts: list[str] = []

    def handler(call: ScriptCall) -> str:
        prompts.append(call.prompt)
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a, b, g = pair(net)
    c = net.create_agent("C", "C", ["c"], [])
    chat = net.group_chat(g.id)
    net.add_member(g.id, c.id)
    assert chat.messages[-1].role == "event" and "C 加入了群组" in chat.messages[-1].text
    talk(net, c, None, "欢迎", chat)
    assert "（C 加入了群组）" in prompts[-1] and chat.messages[-1].agent_id == c.id
    net.remove_member(g.id, c.id)
    assert "C 离开了群组" in chat.messages[-1].text
    assert net.resolve_mentions(chat, "@C 还在吗") == []
    with pytest.raises(NetworkError, match="不是群组"):
        net.send_message(chat.id, "x", mentions=[c.id])
    with pytest.raises(NetworkError, match="不在群组里"):
        net.remove_member(g.id, c.id)
    with pytest.raises(NetworkError, match="已经在群组里"):
        net.add_member(g.id, a.id)


def test_deleted_group_or_agent_archives_chat(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path)
    a, b, g = pair(net)
    chat = net.group_chat(g.id)
    dm = net.direct_chat(b.id)
    net.delete_group(g.id)
    net.delete_agent(b.id)
    assert chat.archived and dm.archived
    with pytest.raises(NetworkError, match="已归档"):
        net.send_message(chat.id, "hi")
    net.delete_conversation(chat.id)
    assert chat.id not in net.store.conversations


def test_mention_resolution_longest_name_first(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path)
    m = net.create_agent("Market", "m", ["m"], [])
    mr = net.create_agent("Market Research", "mr", ["mr"], [])
    g = net.create_group("G", "", [m.id, mr.id])
    conv = net.create_conversation(g.id)
    assert net.resolve_mentions(conv, "@Market Research 看看") == [mr.id]
    assert set(net.resolve_mentions(conv, "@market 和 @Market Research")) == {m.id, mr.id}
    assert net.resolve_mentions(conv, "邮件 a@Marketing.com") == []


def test_user_message_queued_while_running(tmp_path: Path):
    """Agent 运行时用户再发消息：进收件箱排队，这一轮结束后在同一会话送达，不报错也不并发。"""
    import threading
    gate, started = threading.Event(), threading.Event()
    prompts: list[str] = []

    def handler(call: ScriptCall) -> str:
        prompts.append(call.prompt)
        if call.turn == 1:
            started.set()
            gate.wait(5)
        return f"reply {call.turn}"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a = net.create_agent("A", "A", ["a"], [])
    conv = net.create_conversation(agent_id=a.id)
    net.send_message(conv.id, "第一条")
    started.wait(5)
    assert conv.status == "thinking"
    net.send_message(conv.id, "第二条")  # 运行中：排队
    gate.set()
    net.wait_quiet()
    assert [m.text for m in conv.messages] == ["第一条", "第二条", "reply 1", "reply 2"]
    assert "第二条" in prompts[1] and "第一条" not in prompts[1]


def test_knowledge_shared_only_explicitly(scenario):
    """不变量 9。"""
    net, rt, _ = scenario
    markets = sorted(by_target(net, "Market Research Agent"), key=lambda t: t.created_at)
    first, second = markets
    assert first.delegation.share == [] and "共享知识" not in first.prompt
    assert "Competitive Intelligence Agent：" in second.prompt
    assert len(second.delegation.share) == 1 and "已参考共享的竞争格局" in second.result["report"]
    tech = by_target(net, "Technical Analysis Agent")[0]
    ci_key = second.delegation.share[0]
    assert tech.id not in net.store.knowledge[ci_key].visible_to
    assert (net.store.task_dir(second.id) / "shared").exists()
    assert not (net.store.task_dir(first.id) / "shared").exists()


def test_share_requires_visibility(scenario):
    """不变量 9。"""
    net, _, conv = scenario
    from agentnet.engine import ExecContext
    tech = net.store.agent_by_name("Technical Analysis Agent")
    tech_task = by_target(net, "Technical Analysis Agent")[0]
    ci_key = next(k for k in net.store.knowledge if "Competitive" in net.store.knowledge[k].title)
    ctx = ExecContext(kind="task", id=tech_task.id, agent_id=tech.id, conversation_id=conv.id,
                      group_id=conv.group_id, authority=["delegate"], constraints=[], depth=1, chain=[tech.id])
    net._contexts["tok"] = ctx
    text, err = net.handle_tool("tok", "share_knowledge", {"key": ci_key, "agent": "Market Research Agent"})
    assert err and "不可见" in text
    assert net.handle_tool("bad", "list_members", {})[1]


def test_violation_recorded(tmp_path: Path):
    """不变量 6、11。"""
    def handler(call: ScriptCall) -> str:
        if is_task(call):
            call.use("bash", {"command": "ls"})
            call.use("subagent", {})  # Agent 自己的作用域，不算违规
            return "x"
        if call.turn == 1:
            call.tool("delegate", agent="W", mission="m", expected_outcome="o")
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a = net.create_agent("L", "l", ["l"], ["delegate", "shell.exec"])
    w = net.create_agent("W", "w", ["w"], ["web.research"])
    g = net.create_group("G", "", [a.id, w.id])
    talk(net, a, g, "go")
    t = tasks(net)[0]
    assert any("bash" in v for v in t.violations) and not any("subagent" in v for v in t.violations)
    assert "authority.violation" in [e.type for e in net.bus.since(0)]


# ================= 验收与续接 =================
def test_request_changes_reworks_in_same_session(tmp_path: Path):
    """委托方退回：执行者在原会话里看到意见并重新交付；第二次交付后接受。"""
    def handler(call: ScriptCall) -> str:
        if is_task(call):
            return "v2 修改版" if "委托方要求修改" in call.prompt else "v1 初稿"
        if call.turn == 1:
            call.tool("delegate", agent="B", mission="写摘要", expected_outcome="摘要")
            return "等"
        t = tid(call.prompt)
        if "v1 初稿" in call.prompt:
            call.tool("request_changes", task_id=t, feedback="补上数据来源")
            return "退回了"
        call.tool("accept_task", task_id=t)
        return "收下了"

    rt = ScriptedRuntime(handler)
    net = Network(rt, tmp_path)
    a, b, g = pair(net)
    conv = talk(net, a, g, "go")
    t = tasks(net)[0]
    assert t.status == TaskStatus.COMPLETED and t.revisions == 1 and t.result["report"] == "v2 修改版"
    b_calls = [c for c in rt.calls if c.context_id == t.id]
    assert len(b_calls) == 2 and b_calls[1].resumed and b_calls[1].session_id == b_calls[0].session_id
    assert "补上数据来源" in b_calls[1].prompt and "你收到一份委托" not in b_calls[1].prompt
    assert [m.text for m in conv.messages if m.role == "agent"] == ["等", "退回了", "收下了"]


def test_only_direct_delegator_can_review(tmp_path: Path):
    """不变量 5。"""
    out: dict[str, str] = {}

    def handler(call: ScriptCall) -> str:
        if is_task(call):
            return "done"
        if call.turn == 1:
            call.tool("delegate", agent="B", mission="m", expected_outcome="o")
            return "等"
        out["accept_bogus"] = call.tool("accept_task", task_id="task-00000000")
        out["changes_empty"] = call.tool("request_changes", task_id=tid(call.prompt), feedback="")
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a, b, g = pair(net)
    talk(net, a, g, "go")
    assert "不是你在当前上下文中发起的委托" in out["accept_bogus"]
    assert "不能为空" in out["changes_empty"]


def test_failed_task_retry_resumes_session(tmp_path: Path):
    """执行者失败：委托方收到失败通知，retry_task 续接原会话，已有工作不丢。"""
    def handler(call: ScriptCall) -> str:
        if is_task(call):
            if call.turn == 1:
                call.use("write", {"file_path": "draft.md"})
                call.fail("dsh exited 1: dsh: RATE_LIMIT: too many requests")
            return "续接后完成"
        if call.turn == 1:
            call.tool("delegate", agent="B", mission="m", expected_outcome="o")
            return "等"
        if "失败" in call.prompt and "retry_task" in call.prompt:
            call.tool("retry_task", task_id=tid(call.prompt), note="限流已恢复")
            return "已续接"
        call.tool("accept_task", task_id=tid(call.prompt))
        return "完成"

    rt = ScriptedRuntime(handler)
    net = Network(rt, tmp_path)
    a, b, g = pair(net, b_auth=("web.research", "file.write"))
    conv = talk(net, a, g, "go")
    t = tasks(net)[0]
    assert t.status == TaskStatus.COMPLETED and t.retries == 1 and t.result["report"] == "续接后完成"
    b_calls = [c for c in rt.calls if c.context_id == t.id]
    assert len(b_calls) == 2 and b_calls[1].session_id == b_calls[0].session_id
    assert "限流已恢复" in b_calls[1].prompt and "RATE_LIMIT" in b_calls[1].prompt
    assert [e["tool"] for e in t.evidence][:1] == ["write"]  # 失败那一轮的证据保留
    assert [m.text for m in conv.messages if m.role == "agent"] == ["等", "已续接", "完成"]


def test_malformed_response_auto_resumes_once(tmp_path: Path):
    """MALFORMED_RESPONSE（工具参数 JSON 损坏）可恢复：网络用同一会话自动续接一次，用户看不到中断。"""
    def handler(call: ScriptCall) -> str:
        if call.turn == 1:
            call.say("写到一半")
            call.fail("dsh exited 1: dsh: MALFORMED_RESPONSE: DeepSeek Messages stream: tool input is invalid JSON")
        assert "上一轮运行中断" in call.prompt and "合法 JSON" in call.prompt
        return "续上了"

    rt = ScriptedRuntime(handler)
    net = Network(rt, tmp_path)
    a = net.create_agent("A", "A", ["a"], [])
    conv = talk(net, a, None, "go")
    assert [m.role for m in conv.messages] == ["user", "agent"] and conv.messages[-1].text == "续上了"
    assert rt.calls[1].session_id == rt.calls[0].session_id and rt.calls[1].resumed
    assert [e.data["error_class"] for e in net.bus.since(0) if e.type == "run.retry"] == ["malformed_tool_call"]
    assert classify_error("x TIMEOUT y") == ("timeout", False)


def test_failed_round_keeps_progress_and_resumes(tmp_path: Path):
    """一轮最终失败：已完成的委托不丢，失败说明里列出来；“继续”在同一会话接着做，不必重发原需求。"""
    def handler(call: ScriptCall) -> str:
        if is_task(call):
            return "B 的结论"
        if call.turn == 1:
            call.tool("delegate", agent="B", mission="m", expected_outcome="o")
            return "等"
        if call.turn == 2:  # 交付送达后写长报告时坏掉（两次：自动续接也失败）
            call.tool("accept_task", task_id=tid(call.prompt))
            call.fail("dsh exited 1: dsh: MALFORMED_RESPONSE: tool input is invalid JSON")
        if call.turn == 3:
            call.fail("dsh exited 1: dsh: MALFORMED_RESPONSE: tool input is invalid JSON")
        assert "上一轮运行中断" in call.prompt and "研究并写报告" not in call.prompt
        return "报告写完了"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a, b, g = pair(net)
    conv = talk(net, a, g, "研究并写报告")
    fail = conv.messages[-1]
    assert fail.role == "system" and fail.agent_id == a.id and seat(net, conv, a).blocked and conv.status == "idle"
    assert "已完成的委托：B" in fail.text and "已入库的知识 1 条" in fail.text and "继续" in fail.text
    assert tasks(net)[0].status == TaskStatus.COMPLETED  # 成果在失败前已验收
    assert net.resume_conversation(conv.id) == [a.id]
    net.wait_quiet()
    assert conv.messages[-1].text == "报告写完了" and seat(net, conv, a).blocked is None
    with pytest.raises(NetworkError, match="没有中断"):
        net.resume_conversation(conv.id)


def test_new_message_after_failure_continues_session(tmp_path: Path):
    prompts: list[str] = []

    def handler(call: ScriptCall) -> str:
        prompts.append(call.prompt)
        if call.turn == 1:
            call.fail("dsh exited 1: dsh: RATE_LIMIT: slow down")
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a = net.create_agent("A", "A", ["a"], [])
    conv = talk(net, a, None, "第一条")
    assert seat(net, conv, a).blocked and "限流" in conv.messages[-1].text
    net.send_message(conv.id, "好了吗", background=False)
    assert "上一轮运行中断" in prompts[-1] and "好了吗" in prompts[-1] and seat(net, conv, a).blocked is None


# ================= 提议新建 Agent =================
def _proposer(out: dict[str, str]):
    def handler(call: ScriptCall) -> str:
        if is_task(call):
            return "新成员的结论"
        if "（@ 你）" in call.prompt:
            out["over"] = call.tool("propose_agent", name="Legal Agent", description="合规评估", reason="r",
                                    authority=["shell.exec"])
            out["ok"] = call.tool("propose_agent", name="Legal Agent", description="合规评估", reason="群组里没人懂合规",
                                  capabilities=["legal"], authority=["web.research"])
            return "提议了，等人审核"
        if "已批准" in call.prompt:
            call.tool("delegate", agent="Legal Agent", mission="评估合规", expected_outcome="风险清单")
            return "委托给新成员"
        if "被拒绝" in call.prompt:
            return "那我自己来"
        return "收到"
    return handler


def test_propose_agent_approved_joins_group(tmp_path: Path):
    """不变量 2：Agent 只能提议；人批准（可修改，但权限不超出提议者）后以人的身份创建、加入群组，并唤醒提议者。"""
    out: dict[str, str] = {}
    net = Network(ScriptedRuntime(_proposer(out)), tmp_path)
    a, b, g = pair(net)
    conv = talk(net, a, g, "需要合规意见")
    assert "超出你当前的有效权限" in out["over"] and "等人审核" in out["ok"]
    assert net.store.agent_by_name("Legal Agent") is None  # 网络不会自己创建
    p = next(iter(net.store.proposals.values()))
    assert p.status == "pending" and p.max_authority == ["delegate", "web.research"]
    with pytest.raises(NetworkError, match="超出提议者"):
        net.approve_proposal(p.id, authority=["shell.exec"])
    new = net.approve_proposal(p.id, description="合规与隐私评估", note="先只给检索权限")
    net.wait_quiet()
    assert new.id in g.member_ids and new.created_by == f"proposal:{p.id}" and new.description == "合规与隐私评估"
    assert p.status == "approved" and p.agent_id == new.id
    assert conv.messages[-2].text == "委托给新成员"
    assert any(t.delegatee_id == new.id for t in tasks(net))
    with pytest.raises(NetworkError, match="已经批准"):
        net.approve_proposal(p.id)


def test_propose_agent_rejected_notifies(tmp_path: Path):
    out: dict[str, str] = {}
    net = Network(ScriptedRuntime(_proposer(out)), tmp_path)
    a, b, g = pair(net)
    conv = talk(net, a, g, "需要合规意见")
    p = next(iter(net.store.proposals.values()))
    net.reject_proposal(p.id, note="用现有成员")
    net.wait_quiet()
    assert p.status == "rejected" and conv.messages[-1].text == "那我自己来"
    assert net.store.agent_by_name("Legal Agent") is None


def test_task_waits_for_pending_proposal(tmp_path: Path):
    """任务里提议的 Agent 还没审核时，执行者这一轮只算进展，不会提前交付。"""
    def handler(call: ScriptCall) -> str:
        if not is_task(call):
            if call.turn == 1:
                call.tool("delegate", agent="B", mission="m", expected_outcome="o")
            return "ok"
        if call.turn == 1:
            call.tool("propose_agent", name="Helper Agent", description="帮手", reason="缺人")
            return "等审核"
        return "B 最终交付"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a, b, g = pair(net, b_auth=("delegate", "web.research"))
    talk(net, a, g, "go")
    t = tasks(net)[0]
    assert t.status == TaskStatus.RUNNING and t.notes == ["等审核"]
    net.reject_proposal(next(iter(net.store.proposals)))
    net.wait_quiet()
    assert t.status == TaskStatus.COMPLETED and t.result["report"] == "B 最终交付"


# ================= 私有目录与模型 =================
def test_agent_home_persists_without_injection(tmp_path: Path):
    """不变量 8：每个 Agent 有跨对话、跨任务持久的私有目录；网络只挂载，不读取也不注入提示词。"""
    seen: list[str] = []

    def handler(call: ScriptCall) -> str:
        home = call.cwd / "agent-home"
        seen.append((home / "notes.md").read_text() if (home / "notes.md").exists() else "")
        (home / "notes.md").write_text(f"第 {len(seen)} 次运行的笔记")
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a = net.create_agent("A", "A", ["a"], ["file.write"])
    g = net.create_group("G", "", [a.id])
    c1 = talk(net, a, None, "一")
    c2 = talk(net, a, g, "二")  # 另一个对话（群聊），同一个 Agent
    assert seen == ["", "第 1 次运行的笔记"]
    assert (net.store.agent_dir(a.id) / "notes.md").read_text() == "第 2 次运行的笔记"
    assert not (net.store.root / seat(net, c1, a).workdir / "agent-home").exists()  # 运行结束后卸载
    first_prompt = net.runtime.calls[1].prompt
    assert "第 1 次运行的笔记" not in first_prompt and "agent-home" in first_prompt


def test_agent_model_reaches_runtime(tmp_path: Path):
    models: list = []

    def handler(call: ScriptCall) -> str:
        models.append(call._req.model)
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a = net.create_agent("A", "A", ["a"], [], model={"provider": "deepseek-official", "model": "deepseek-flash",
                                                     "reasoning_effort": " "})
    conv = talk(net, a, None, "x")
    net.update_agent(a.id, model=None)
    net.send_message(conv.id, "y", background=False)
    assert models == [{"provider": "deepseek-official", "model": "deepseek-flash"}, None]
    y = build_patch_yaml([], model={"provider": "hi-code-claude", "model": "claude-opus-5-5",
                                    "reasoning_effort": "high"})
    assert '- id: agent-default-model' in y and '"claude-opus-5-5"' in y and 'reasoningEffort: "high"' in y
    assert "agent-default-model" not in build_patch_yaml([])


# ================= 持久化 =================
def test_persistence_roundtrip(scenario, tmp_path: Path):
    net, _, conv = scenario
    net2 = Network(ScriptedRuntime(scenario_handler), tmp_path)
    c2 = net2.store.conversations[conv.id]
    lead = net.store.agent_by_name("Product Strategy Agent")
    assert c2.kind == "group" and c2.seats[lead.id].dsh_session_id == conv.seats[lead.id].dsh_session_id
    assert c2.seats[lead.id].seen == conv.seats[lead.id].seen and len(c2.messages) == len(conv.messages)
    assert len(net2.store.tasks) == len(net.store.tasks)
    rel = net2.relations()
    assert rel[0]["count"] == 2 and rel[0]["to"] == ids(net2, "Market Research Agent")[0]


def test_restart_fails_running_tasks_and_notifies(tmp_path: Path):
    """不变量 12：重启：运行中的任务标记为失败，失败通知进委托方收件箱，下一次运行时送达，可以续接。"""
    net = Network(ScriptedRuntime(lambda c: "x"), tmp_path)
    a, b, g = pair(net)
    conv = talk(net, a, g, "go")
    from agentnet.models import Delegation, Task
    t = Task(id="task-0000beef", conversation_id=conv.id, group_id=g.id, delegator_id=a.id, delegatee_id=b.id,
             parent_task_id=None, depth=1, delegation=Delegation("m", [], [], "o"), dsh_session_id="s1")
    net.store.tasks[t.id] = t
    conv.status = "waiting"
    net.store.save()
    net2 = Network(ScriptedRuntime(lambda c: "x"), tmp_path)
    t2, c2 = net2.store.tasks[t.id], net2.store.conversations[conv.id]
    assert t2.status == TaskStatus.FAILED and "重启" in t2.error and c2.status == "idle"
    assert c2.seats[a.id].inbox == [{"kind": "failed", "task_id": t.id}]  # 进委托方席位的收件箱


def test_load_legacy_state(scenario, tmp_path: Path):
    """旧版 state.json：Agent 带 memory 字段；对话是“主 Agent”模型（agent_id + 单个会话）；任务缺少新字段。
    对话迁移成只有这一个席位的群聊（在群组里）或私聊，沿用原会话与目录，还能续接；委托结果的可见性迁到席位。"""
    net, _, conv = scenario
    lead = net.store.agent_by_name("Product Strategy Agent")
    old_seat = conv.seats[lead.id]
    path = tmp_path / "state.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    for a in raw["agents"]:
        a["memory"] = ["与用户对话：旧版本写的经验"]
        a.pop("model", None)
    for c in raw["conversations"]:
        c.update(agent_id=lead.id, dsh_session_id=old_seat.dsh_session_id, inbox=[])
        for k in ("kind", "seats"):
            c.pop(k)
        c["messages"] = [m for m in c["messages"] if m["role"] != "event"]  # 旧版没有事件消息
        for m in c["messages"]:
            for k in ("wake", "agent_id", "mentions"):
                m.pop(k, None)
    dm = {"id": "conv-0000dead", "agent_id": lead.id, "group_id": None, "title": "旧私聊", "messages": [],
          "dsh_session_id": None}
    raw["conversations"].append(dm)
    for t in raw["tasks"]:
        for k in ("inbox", "notes", "revisions", "retries", "delegations", "delivered_at", "auto_accepted"):
            t.pop(k, None)
    for k in raw["knowledge"]:  # 旧版：委托方的执行上下文就是对话本身
        k["visible_to"] = [conv.id if v == f"{conv.id}/{lead.id}" else v for v in k["visible_to"]]
    raw.pop("proposals", None)
    raw.pop("requests", None)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    net2 = Network(ScriptedRuntime(scenario_handler), tmp_path)
    assert len(net2.store.agents) == len(net.store.agents)
    assert not hasattr(net2.store.agent_by_name("Market Research Agent"), "memory")
    c2 = net2.store.conversations[conv.id]
    assert c2.kind == "group" and list(c2.seats) == [lead.id]
    s2 = c2.seats[lead.id]
    assert s2.dsh_session_id == old_seat.dsh_session_id and s2.workdir == f"conversations/{conv.id}"
    assert s2.seen == len(c2.messages)  # 旧消息已在会话里，不再重复送
    assert c2.messages[0].mentions == [lead.id] and c2.messages[1].agent_id == lead.id
    assert net2.store.conversations["conv-0000dead"].kind == "dm"
    assert not c2.archived and net2._group_chat_of(c2.group_id).id == c2.id
    first_ci = by_target(net2, "Competitive Intelligence Agent")[0]
    assert f"{conv.id}/{lead.id}" in net2.store.knowledge[f"{first_ci.id}:result"].visible_to


def test_legacy_multiple_chats_collapse_to_one(tmp_path: Path):
    """旧版每个群组可以开多个对话：加载后最近活跃的那个是群聊，其余归档只读，记录不丢。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path)
    lead = net.store.agent_by_name("Product Strategy Agent")
    g = net.store.groups_of(lead.id)[0]
    path = tmp_path / "state.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["conversations"] = [
        {"id": f"conv-old{i}", "agent_id": lead.id, "group_id": g.id, "title": f"与 {lead.name} 的对话",
         "messages": [{"id": f"m{i}", "role": "user", "text": f"第 {i} 个"}], "updated_at": 100 + i}
        for i in range(3)]
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    net2 = Network(ScriptedRuntime(lambda call: "ok"), tmp_path)
    convs = net2.store.conversations
    assert [convs[f"conv-old{i}"].archived for i in range(3)] == [True, True, False]
    assert net2.group_chat(g.id).id == "conv-old2" and convs["conv-old2"].title == g.name
    assert convs["conv-old0"].messages[0].text == "第 0 个"


# ================= 结构性约束：一处定义，处处一致 =================
def test_network_tools_defined_consistently():
    """新增网络工具要同步 schema、实现、提示词三处：由这条测试保证，而不是靠记住。"""
    from agentnet.engine import TOOL_HANDLERS
    from agentnet.mcp_server import TOOLS
    from agentnet.protocol import TOOL_GUIDE
    from agentnet.models import ARCHIVE_TOOLS
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names))
    assert set(names) == set(TOOL_HANDLERS) == set(TOOL_GUIDE)
    from agentnet.models import REQUEST_TOOLS
    assert set(ARCHIVE_TOOLS + REQUEST_TOOLS) <= set(names)  # 档案、用户任务工具（不需要 delegate）也必须是已定义的网络工具


def test_archive_tools_mounted_without_delegate_and_checked_by_network():
    """没有 delegate 时 MCP 只列档案工具，网络端也拒绝协作工具（不只靠 MCP 过滤）。"""
    import os
    import subprocess
    import sys
    from agentnet.engine import network_tools_for
    from agentnet.models import ARCHIVE_TOOLS
    from agentnet.runtime import MCP_SERVER_SCRIPT
    from agentnet.models import REQUEST_TOOLS
    assert network_tools_for("task", True, False) == list(ARCHIVE_TOOLS)          # 委托任务：没有用户任务工具
    assert network_tools_for("seat", False, False) == list(REQUEST_TOOLS)          # 私聊
    assert set(network_tools_for("seat", True, False)) == set(ARCHIVE_TOOLS + REQUEST_TOOLS)
    env = {**os.environ, "AGENTNET_TOOLS": ",".join(network_tools_for("task", True, False))}
    out = subprocess.run([sys.executable, str(MCP_SERVER_SCRIPT)], input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
                         capture_output=True, text=True, env=env, timeout=10).stdout
    import json
    assert [t["name"] for t in json.loads(out)["result"]["tools"]] == list(ARCHIVE_TOOLS)


def test_invariants_have_tests():
    """AGENTS.md 里的每条网络不变量都要有测试：测试 docstring 写“不变量 N”。新增不变量却没写测试时变红。"""
    import re
    root = Path(__file__).parent.parent
    doc = (root / "AGENTS.md").read_text(encoding="utf-8")
    section = doc.split("### 一、网络不变量", 1)[1].split("### 二、", 1)[0]
    declared = {int(n) for n in re.findall(r"^(\d+)\. \*\*", section, re.M)}
    tests = "".join(p.read_text(encoding="utf-8") for p in (root / "tests").glob("test_*.py"))
    covered = {int(n) for tag in re.findall(r"不变量 ([\d、]+)", tests) for n in tag.split("、") if n}
    assert declared and declared <= covered, f"没有测试标注的不变量：{sorted(declared - covered)}"


def test_authorities_registered_everywhere():
    """新增权限要同步词表、工具映射、运行时插件：每个权限都必须能落到某种强制手段上。"""
    from agentnet.models import AUTHORITIES, TOOL_AUTHORITY
    from agentnet.runtime import AUTHORITY_PLUGINS
    assert set(TOOL_AUTHORITY.values()) <= set(AUTHORITIES)
    assert set(AUTHORITY_PLUGINS) <= set(AUTHORITIES)
    enforced = set(AUTHORITY_PLUGINS) | {"file.write", "delegate"}  # file.write → 沙箱模式；delegate → 是否挂载网络工具
    assert set(AUTHORITIES) <= enforced


def test_dsh_patch_reflects_authority_and_mounts_mcp():
    y = build_patch_yaml(["web.research"])
    assert "id: tool-bash" in y and "id: tool-web" not in y
    assert "id: tool-plugin-manager" in y and "tool-subagent" not in y and "tool-workflow" not in y
    assert "insert" not in y
    y2 = build_patch_yaml(["web.research", "shell.exec", "delegate"],
                          ToolBridge(url="http://127.0.0.1:1/internal/tools", token="t", handler=lambda n, a: ("", False)))
    assert "id: tool-bash" not in y2
    assert y2.startswith("#") and "- insert:" in y2 and "dsh-mcp-client" in y2 and "AGENTNET_TOKEN" in y2
    assert permission_mode(["web.research"]) == "read-only"
    assert permission_mode(["file.write"]) == "workspace-write"


def test_mcp_server_stdio(tmp_path: Path):
    """MCP 服务握手 + tools/list；tools/call 在网络不可达时返回 isError。"""
    from agentnet.mcp_server import TOOLS
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2026-07-28"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_members", "arguments": {}}},
    ]
    from agentnet.runtime import MCP_SERVER_SCRIPT
    out = subprocess.run([sys.executable, str(MCP_SERVER_SCRIPT)], input="\n".join(json.dumps(m) for m in msgs),
                         capture_output=True, text=True, timeout=20,
                         env={"AGENTNET_TOOL_URL": "http://127.0.0.1:9/x", "AGENTNET_TOKEN": "t"}).stdout
    res = [json.loads(line) for line in out.splitlines()]
    assert [r["id"] for r in res] == [1, 2, 3]
    assert res[0]["result"]["protocolVersion"] == "2025-06-18"
    assert {t["name"] for t in res[1]["result"]["tools"]} == {t["name"] for t in TOOLS}
    assert res[2]["result"]["isError"] is True
    assert MCP_PREFIX == "mcp__agentnet__"
