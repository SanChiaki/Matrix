"""ScriptedRuntime 驱动的网络语义测试：群组内委托、权限、防环、知识隔离、持久化、DSH 覆盖层。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentnet.engine import Limits, Network, NetworkError
from agentnet.models import MCP_PREFIX, TaskStatus
from agentnet.runtime import (ScriptCall, ScriptedRuntime, ToolBridge, build_patch_yaml, permission_mode)
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
    conv = net.create_conversation(lead.id, group.id)
    net.send_message(conv.id, GOAL, background=False)
    return net, rt, conv


def tasks(net: Network):
    return list(net.store.tasks.values())


def by_target(net: Network, name: str):
    a = net.store.agent_by_name(name)
    return [t for t in tasks(net) if t.delegatee_id == a.id]


def test_seed_network(tmp_path: Path):
    net = Network(ScriptedRuntime(scenario_handler), tmp_path)
    assert len(net.store.agents) == 5 and len(net.store.groups) == 1
    g = next(iter(net.store.groups.values()))
    legal = net.store.agent_by_name("Legal Compliance Agent")
    assert legal.id not in g.member_ids


def test_conversation_delegates_within_group(scenario):
    net, rt, conv = scenario
    assert conv.status == "idle"
    reply = conv.messages[-1]
    assert reply.role == "agent" and "机会成立" in reply.text
    done = [t for t in tasks(net) if t.status == TaskStatus.COMPLETED]
    assert len(done) == 4  # 市场 ×2、技术、竞争
    assert set(reply.task_ids) == {t.id for t in tasks(net)}
    assert len(by_target(net, "Market Research Agent")) == 2  # 同一成员被复用，不创建新节点


def test_outside_group_rejected_no_autocreate(scenario):
    net, _, _ = scenario
    rejected = [t for t in tasks(net) if t.status == TaskStatus.REJECTED]
    assert len(rejected) == 1 and rejected[0].delegatee_id is None
    assert "不是群组" in rejected[0].error
    assert len(net.store.agents) == 5


def test_authority_is_agent_own_intersect_grant(scenario):
    net, _, _ = scenario
    tech = by_target(net, "Technical Analysis Agent")[0]
    # 委托方请求 web.research + shell.exec；执行者自身权限是 web.research + file.write
    assert tech.granted_authority == ["web.research"]
    assert tech.denied_authority == ["shell.exec"]
    # 不指定 authority = 不额外收窄：取执行者自身权限 ∩ 委托方有效权限
    market = by_target(net, "Market Research Agent")[0]
    assert market.granted_authority == ["web.research"]


def test_grant_cannot_exceed_delegator(tmp_path: Path):
    def handler(call: ScriptCall) -> str:
        if call.tool_names and "你收到一份委托" not in call.prompt:
            call.tool("delegate", agent="Worker", mission="m", expected_outcome="o",
                      authority=["web.research", "shell.exec"])
        return "done"

    net = Network(ScriptedRuntime(handler), tmp_path)
    lead = net.create_agent("Lead", "lead", ["plan"], ["delegate", "web.research"])
    worker = net.create_agent("Worker", "w", ["x"], ["web.research", "shell.exec"])
    g = net.create_group("G", "", [lead.id, worker.id])
    net.send_message(net.create_conversation(lead.id, g.id).id, "hi", background=False)
    t = tasks(net)[0]
    assert t.granted_authority == ["web.research"]  # 委托方没有 shell.exec，给不出去
    assert t.denied_authority == ["shell.exec"]


def test_constraints_inherit_and_nested_no_cycle(tmp_path: Path):
    seen: dict[str, str] = {}

    def handler(call: ScriptCall) -> str:
        name = "A" if "**A**" in call.prompt else "B" if "**B**" in call.prompt else "C"
        if not call.tool_names:
            return f"{name} done"
        if name == "A":
            return call.tool("delegate", agent="B", mission="m1", expected_outcome="o", constraints=["c1"])
        if name == "B":
            seen["cycle"] = call.tool("delegate", agent="A", mission="back", expected_outcome="o")
            return call.tool("delegate", agent="C", mission="m2", expected_outcome="o", constraints=["c2"])
        return "C done"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a, b, c = (net.create_agent(n, n, [n.lower()], ["delegate", "web.research"]) for n in "ABC")
    g = net.create_group("G", "", [a.id, b.id, c.id])
    net.send_message(net.create_conversation(a.id, g.id).id, "go", background=False)
    t_c = next(t for t in tasks(net) if t.delegatee_id == c.id)
    assert t_c.depth == 2 and t_c.effective_constraints == ["c1", "c2"]
    assert t_c.parent_task_id is not None
    assert "不能成环" in seen["cycle"]
    assert net._chain(t_c) == [a.id, b.id]


def test_run_progress_is_live_and_kept_on_reply(tmp_path: Path):
    """轮次进行中：工具调用与中间文本实时推送并可查询；结束后作为过程留在回复上。委托任务同样可查。"""
    seen: dict[str, list] = {}

    def handler(call: ScriptCall) -> str:
        if call.tool_names:  # A：对话 Agent
            call.say("先看看群组成员")
            call.tool("list_members")
            seen["conv"] = net.conversation_detail(conv.id)["live"]
            call.tool("delegate", agent="B", mission="查资料", expected_outcome="摘要")
            return "## 结论\n完成"
        call.say("正在检索")
        seen["task"] = net.task_detail(call.context_id)["live"]
        return "B done"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a = net.create_agent("A", "A", ["a"], ["delegate", "web.research"])
    b = net.create_agent("B", "B", ["b"], ["web.research"])
    g = net.create_group("G", "", [a.id, b.id])
    conv = net.create_conversation(a.id, g.id)
    net.send_message(conv.id, "go", background=False)

    assert [s["kind"] for s in seen["conv"]] == ["text", "tool"]
    assert seen["conv"][1]["tool"] == MCP_PREFIX + "list_members"
    assert [s["text"] for s in seen["task"]] == ["正在检索"]
    reply = net.store.conversations[conv.id].messages[-1]
    assert [s["kind"] for s in reply.steps] == ["text", "tool", "tool"]
    assert reply.steps[2]["brief"] == "B"  # 委托只显示目标成员，不显示整段参数
    assert net.conversation_detail(conv.id)["live"] == []
    progress = [e.data for e in net.bus.since(0) if e.type == "run.progress"]
    assert {p["conversation_id"] for p in progress} == {conv.id}
    assert [p["context"] for p in progress].count("task") == 1

    net2 = Network(ScriptedRuntime(handler), tmp_path)  # 过程随回复持久化
    assert len(net2.store.conversations[conv.id].messages[-1].steps) == 3


def test_delegate_constraints_as_text(tmp_path: Path):
    """constraints 在工具 schema 里是字符串（每行一条），引擎拆成列表；旧的数组写法仍然兼容。"""
    def handler(call: ScriptCall) -> str:
        if not call.tool_names:
            return "done"
        if "**A**" in call.prompt:
            call.tool("delegate", agent="B", mission="m", expected_outcome="o",
                      constraints="只用公开信息\n- 不编造数据；不评估技术实现\n\n")
            return call.tool("delegate", agent="B", mission="m2", expected_outcome="o", constraints=["c1", " "])
        return "done"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a, b = (net.create_agent(n, n, [n.lower()], ["delegate", "web.research"]) for n in "AB")
    g = net.create_group("G", "", [a.id, b.id])
    net.send_message(net.create_conversation(a.id, g.id).id, "go", background=False)
    t1, t2 = tasks(net)
    assert t1.effective_constraints == ["只用公开信息", "不编造数据", "不评估技术实现"]
    assert t2.effective_constraints == ["c1"]


def test_delegate_schema_keeps_free_text_in_strings():
    """自由文本只放 string 参数：数组参数的值要由模型直接写成 JSON，长中文容易被写成裸文本。"""
    from agentnet.mcp_server import TOOLS
    props = next(t for t in TOOLS if t["name"] == "delegate")["inputSchema"]["properties"]
    for key in ("mission", "expected_outcome", "constraints"):
        assert props[key]["type"] == "string", key


def test_depth_limit_unmounts_tools(tmp_path: Path):
    mounted: list[bool] = []

    def handler(call: ScriptCall) -> str:
        if "你收到一份委托" in call.prompt:
            mounted.append(bool(call.tool_names))
            return "leaf"
        return call.tool("delegate", agent="B", mission="m", expected_outcome="o")

    net = Network(ScriptedRuntime(handler), tmp_path, Limits(max_depth=1))
    a = net.create_agent("A", "a", ["a"], ["delegate"])
    b = net.create_agent("B", "b", ["b"], ["delegate"])
    g = net.create_group("G", "", [a.id, b.id])
    net.send_message(net.create_conversation(a.id, g.id).id, "go", background=False)
    assert mounted == [False]


def test_no_group_no_network_tools(tmp_path: Path):
    rt = ScriptedRuntime(scenario_handler)
    net = Network(rt, tmp_path)
    lead = net.store.agent_by_name("Product Strategy Agent")
    conv = net.create_conversation(lead.id)  # 不在群组中
    net.send_message(conv.id, GOAL, background=False)
    assert rt.calls[0].tool_names == []
    assert "无法委托" in conv.messages[-1].text and not tasks(net)


def test_conversation_rules(tmp_path: Path):
    net = Network(ScriptedRuntime(scenario_handler), tmp_path)
    legal = net.store.agent_by_name("Legal Compliance Agent")
    g = next(iter(net.store.groups.values()))
    with pytest.raises(NetworkError, match="不是群组"):
        net.create_conversation(legal.id, g.id)
    net.update_agent(legal.id, chat_enabled=False)
    with pytest.raises(NetworkError, match="未开放"):
        net.create_conversation(legal.id)
    with pytest.raises(NetworkError, match="未知权限"):
        net.create_agent("X Agent", "x", [], ["root"])


def test_session_resumed_per_conversation_isolated_per_task(scenario):
    net, rt, conv = scenario
    lead_calls = [c for c in rt.calls if c.context_id == conv.id]
    assert len(lead_calls) == 1 and not lead_calls[0].resumed
    net.send_message(conv.id, "继续", background=False)
    lead_calls = [c for c in rt.calls if c.context_id == conv.id]
    assert lead_calls[1].resumed and lead_calls[1].session_id == conv.dsh_session_id
    sessions = [t.dsh_session_id for t in tasks(net) if t.status != TaskStatus.REJECTED]
    assert all(sessions) and len(set(sessions)) == len(sessions)
    cwds = {str(c.cwd) for c in rt.calls if c.context_id != conv.id}
    assert len(cwds) == len(sessions)


def test_knowledge_shared_only_explicitly(scenario):
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
    def handler(call: ScriptCall) -> str:
        if "你收到一份委托" in call.prompt:
            call.use("bash", {"command": "ls"})
            call.use("subagent", {})
            return "x"
        call.tool("delegate", agent="W", mission="m", expected_outcome="o")
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path)
    a = net.create_agent("L", "l", ["l"], ["delegate", "shell.exec"])
    w = net.create_agent("W", "w", ["w"], ["web.research"])
    g = net.create_group("G", "", [a.id, w.id])
    net.send_message(net.create_conversation(a.id, g.id).id, "go", background=False)
    t = tasks(net)[0]
    assert any("bash" in v for v in t.violations) and any("subagent" in v for v in t.violations)
    assert "authority.violation" in [e.type for e in net.bus.since(0)]


def test_persistence_roundtrip(scenario, tmp_path: Path):
    net, _, conv = scenario
    net2 = Network(ScriptedRuntime(scenario_handler), tmp_path)
    c2 = net2.store.conversations[conv.id]
    assert c2.dsh_session_id == conv.dsh_session_id and len(c2.messages) == 2
    assert len(net2.store.tasks) == len(net.store.tasks)
    rel = net2.relations()
    assert rel[0]["count"] == 2 and rel[0]["to"] == ids(net2, "Market Research Agent")[0]


def test_load_ignores_legacy_agent_memory(scenario, tmp_path: Path):
    """旧版 state.json 里每个 Agent 带 memory 字段；现在不再管理，读取时必须照常加载。"""
    net, _, _ = scenario
    path = tmp_path / "state.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["agents"], "种子数据应已写入"
    for a in raw["agents"]:
        a["memory"] = ["与用户对话：旧版本写的经验"]
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    net2 = Network(ScriptedRuntime(scenario_handler), tmp_path)
    assert len(net2.store.agents) == len(net.store.agents)
    assert not hasattr(net2.store.agent_by_name("Market Research Agent"), "memory")


def test_dsh_patch_reflects_authority_and_mounts_mcp():
    y = build_patch_yaml(["web.research"])
    assert "id: tool-bash" in y and "id: tool-subagent" in y and "id: tool-web" not in y
    assert "insert" not in y
    y2 = build_patch_yaml(["web.research", "shell.exec", "delegate"],
                          ToolBridge(url="http://127.0.0.1:1/internal/tools", token="t", handler=lambda n, a: ("", False)))
    assert "id: tool-bash" not in y2
    assert y2.startswith("#") and "- insert:" in y2 and "dsh-mcp-client" in y2 and "AGENTNET_TOKEN" in y2
    assert permission_mode(["web.research"]) == "read-only"
    assert permission_mode(["file.write"]) == "workspace-write"


def test_mcp_server_stdio(tmp_path: Path):
    """MCP 服务握手 + tools/list；tools/call 在网络不可达时返回 isError。"""
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
    assert {t["name"] for t in res[1]["result"]["tools"]} == {"list_members", "delegate", "share_knowledge"}
    assert res[2]["result"]["isError"] is True
    assert MCP_PREFIX == "mcp__agentnet__"
