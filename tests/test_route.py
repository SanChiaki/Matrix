"""route 挂点（docs/plugin-hooks.md 第 8 节，不变量 24）：插件只建议唤醒或认领，网络过闸门后执行。四条规则：
1. 一条人发的消息开一串接力，自动唤醒有预算；2. 这一串里出现过的成员不再被 route 唤醒；3. 只看项目群聊；
4. 认领只针对人发的、没 @ 人的消息，每条一次，认领那一轮下发人是原作者，叫来看的那一轮不能登记任务。
另有示例插件「接力分派」走通一遍。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from matrix import plugins as pluginfs
from matrix.engine import Network
from matrix.models import MCP_PREFIX, ROUTE_BUDGET
from matrix.runtime import ScriptCall, ScriptedRuntime
from matrix.scripted import scenario_handler
from tests.test_users import users

ROOT = Path(__file__).resolve().parents[1]

FAKE = '''import json, os, sys
for line in sys.stdin:
    m = json.loads(line)
    if m.get("id") is None:
        continue
    method, p = m["method"], m.get("params") or {}
    r = {}
    if method == "initialize":
        r = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
    elif method == "tools/list":
        r = {"tools": [{"name": "suggest", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        a = p.get("arguments") or {}
        with open("calls.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(a, ensure_ascii=False) + "\\n")
        rules = json.load(open("suggest.json", encoding="utf-8")) if os.path.exists("suggest.json") else {}
        text = (a.get("message") or {}).get("text", "")
        out = next((v for k, v in rules.items() if k in text), {})
        r = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}, ensure_ascii=False), flush=True)
'''


def fake_plugin(tmp: Path, may: list[str]) -> Path:
    pkg = tmp / "catalog" / "fake-route"
    (pkg / "service").mkdir(parents=True)
    (pkg / "service" / "fake.py").write_text(FAKE, encoding="utf-8")
    (pkg / "plugin.json").write_text(json.dumps({
        "name": "fake-route", "version": "1.0.0", "title": "分派", "description": "测试用的 route",
        "service": {"command": "python", "args": ["{plugin}/service/fake.py"], "tools": {"suggest": "hook"}},
        "hooks": {"route": [{"on": "message.posted", "tool": "suggest", "may": may}]}}), encoding="utf-8")
    (tmp / "catalog" / "plugins.json").write_text('{"plugins": ["fake-route"]}', encoding="utf-8")
    return tmp / "catalog" / "plugins.json"


def setup(tmp: Path, may=("wake", "claim"), replies: dict[str, str] | None = None):
    log: list[dict] = []
    names: dict[str, str] = {}

    def handler(call: ScriptCall) -> str:
        name = names.get(call.agent_id, "")
        entry = {"agent": call.agent_id, "prompt": call.prompt}
        if MCP_PREFIX + "open_request" in call.tool_names:
            entry["open"] = call.tool("open_request", title="接手的事", expected_outcome="结果")
        log.append(entry)
        return (replies or {}).get(name, "收到")
    net = Network(ScriptedRuntime(handler), tmp / "data", seed=False,
                  plugins=pluginfs.PluginCatalog(fake_plugin(tmp, list(may))))
    host, = users(net, "host@x.io")
    net.ensure_assistant(host)
    a, b, c = (net.create_agent(n, n, [], ["file.write"], actor=host) for n in ("甲", "乙", "丙"))
    names.update({x.id: x.name for x in (a, b, c)})
    g = net.create_project("研发", "", [a.id, b.id, c.id], actor=host)
    net.install_plugin(g.id, "fake-route", actor=host)
    data = net.store.plugin_data_dir(g.id, "fake-route") / "service"
    data.mkdir(parents=True, exist_ok=True)
    return net, host, (a, b, c), g, data, log


def rules(data: Path, r: dict) -> None:
    (data / "suggest.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")


def route_log(net: Network, g) -> list[dict]:
    lines = (net.store.project_dir(g.id).parent / "log.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e.get("action") == "plugin.route"]


# ================= 清单 =================
@pytest.mark.parametrize("hook, why", [
    ({"on": "message.posted", "tool": "write", "may": ["wake"]}, "read 或 hook"),
    ({"on": "message.posted", "tool": "index", "may": ["wake", "assign"]}, "wake/claim"),
    ({"on": "task.created", "tool": "index", "may": ["wake"]}, "message.posted"),
    ({"on": "message.posted", "tool": "index", "may": []}, "wake/claim"),
])
def test_route_manifest(hook: dict, why: str):
    m = json.loads((ROOT / "examples" / "plugins" / "llm-wiki" / "plugin.json").read_text(encoding="utf-8"))
    m["hooks"] = {"route": [hook]}
    with pytest.raises(pluginfs.PluginError, match=why):
        pluginfs.parse_manifest(m)


def test_hook_tools_are_not_given_to_agents(tmp_path: Path):
    """不变量 24：只给挂点用的工具（hook）不出现在 Agent 的工具里，Agent 也调不到。"""
    net, host, (a, _, _), g, data, log = setup(tmp_path)
    chat = net.project_chat(g.id, actor=host)
    net.send_message(chat.id, "@甲 在吗", actor=host, background=False)
    assert MCP_PREFIX + "fake-route__suggest" not in net.runtime.calls[-1].tool_names


# ================= 四条规则 =================
def test_rule1_budget_per_human_message(tmp_path: Path):
    """不变量 24 规则 1：一条人发的消息开一串接力，route 引出的唤醒（认领、叫来看）共用 ROUTE_BUDGET 次预算；
    下一条人发的消息重新开一串，预算满额。"""
    net, host, (a, b, c), g, data, log = setup(tmp_path, replies={"甲": "交给乙", "乙": "交给丙", "丙": "收尾"})
    rules(data, {"白屏": {"claim": {"agent": "甲", "reason": "能力匹配"}}, "交给乙": {"wake": [{"agent": "乙"}]},
                 "交给丙": {"wake": [{"agent": "丙"}]}})
    assert ROUTE_BUDGET == 3
    chat = net.project_chat(g.id, actor=host)
    net.send_message(chat.id, "登录页白屏", actor=host, background=False)
    assert [e["agent"] for e in log] == [a.id, b.id, c.id]  # 认领 + 两次叫来看 = 3 次，用满
    events = [m.text for m in chat.messages if m.role == "event" and m.text.startswith("插件「分派」")]
    assert events == ["插件「分派」把这条消息分派给 甲 认领：能力匹配", "插件「分派」请 乙 看一下", "插件「分派」请 丙 看一下"]

    rules(data, {"再来": {"wake": [{"agent": "甲"}, {"agent": "乙"}, {"agent": "丙"}]}})
    log.clear()
    net.send_message(chat.id, "再来一次", actor=host, background=False)
    assert sorted(e["agent"] for e in log) == sorted([a.id, b.id, c.id])  # 新的一串：预算满额


def test_rule1_budget_exhausted_note(tmp_path: Path):
    """不变量 24 规则 1：预算用完的那串接力，被拒的唤醒写进日志，群聊里说一次“用完了”。"""
    net, host, agents, g, data, log = setup(tmp_path, replies={"甲": "交给乙", "乙": "交给丙", "丙": "收尾"})
    net.create_agent("丁", "丁", [], [], actor=host)
    d = next(x for x in net.store.agents.values() if x.name == "丁")
    g.member_ids.append(d.id)
    rules(data, {"开始": {"claim": {"agent": "甲"}}, "交给乙": {"wake": [{"agent": "乙"}]},
                 "交给丙": {"wake": [{"agent": "丙"}]}, "收尾": {"wake": [{"agent": "丁"}]}})
    chat = net.project_chat(g.id, actor=host)
    net.send_message(chat.id, "开始吧", actor=host, background=False)
    assert len(log) == 3
    assert [m.text for m in chat.messages if "用完了" in m.text] == ["插件「分派」这一串接力的自动唤醒（3 次）用完了：要继续请 @ 成员"]
    assert any("预算" in r["why"] and r["agent"] == "丁" for e in route_log(net, g) for r in e["refused"])


def test_rule2_chain(tmp_path: Path):
    """不变量 24 规则 2：这一串里出现过的成员（人 @ 的、route 叫过的、正在回复的）不能再被 route 唤醒。"""
    net, host, (a, b, c), g, data, log = setup(tmp_path, replies={"甲": "交给乙", "乙": "交给甲"})
    rules(data, {"@甲": {"wake": [{"agent": "甲"}]}, "交给乙": {"wake": [{"agent": "乙"}, {"agent": "甲"}]},
                 "交给甲": {"wake": [{"agent": "甲"}]}})
    chat = net.project_chat(g.id, actor=host)
    net.send_message(chat.id, "@甲 看看", actor=host, background=False)
    assert [e["agent"] for e in log] == [a.id, b.id]  # 甲被 @；甲说交给乙 → 乙；乙说交给甲 → 拒
    refused = [(r["agent"], r["why"]) for e in route_log(net, g) for r in e["refused"]]
    assert ("甲", "这一串里已经出现过（规则 2）") in refused
    assert refused.count(("甲", "这一串里已经出现过（规则 2）")) == 3  # 人的消息、甲自己的回复、乙的回复里各一次


def test_rule3_only_project_chat(tmp_path: Path):
    """不变量 24 规则 3：项目会话、私聊里的消息从不送给 route。"""
    net, host, (a, b, c), g, data, log = setup(tmp_path)
    rules(data, {"": {"wake": [{"agent": "乙"}]}})
    session = net.create_session(g.id, "私下", actor=host)
    net.send_message(session.id, "私下说一句", actor=host, background=False)
    net.send_message(net.direct_chat(a.id, actor=host).id, "私聊一句", actor=host, background=False)
    calls = (data / "calls.jsonl").read_text(encoding="utf-8") if (data / "calls.jsonl").exists() else ""
    assert "私下说一句" not in calls and "私聊一句" not in calls
    assert all(e["agent"] != b.id for e in log)
    net.send_message(net.project_chat(g.id, actor=host).id, "群里一句", actor=host, background=False)
    assert "群里一句" in (data / "calls.jsonl").read_text(encoding="utf-8")
    assert any(e["agent"] == b.id for e in log)


def test_rule4_claim(tmp_path: Path):
    """不变量 24 规则 4：认领只针对人发的、没 @ 人的消息，每条只能认领一次；认领那一轮可以登记任务、下发人是原作者；
    叫来看的那一轮不能登记任务；Agent 的回复不能被认领。没申请的建议种类一律丢弃。"""
    net, host, (a, b, c), g, data, log = setup(tmp_path, replies={"甲": "做完了，请丙认领"})
    rules(data, {"修一下": {"claim": {"agent": "甲", "reason": "能力"}, "wake": [{"agent": "乙"}]},
                 "请丙认领": {"claim": {"agent": "丙"}},
                 "@乙": {"claim": {"agent": "丙"}}, "外人": {"wake": [{"agent": "不存在"}]}})
    chat = net.project_chat(g.id, actor=host)
    msg = net.send_message(chat.id, "登录页修一下", actor=host, background=False)
    by = {e["agent"]: e for e in log}
    assert msg.claimed_by == a.id
    assert "已登记任务" in by[a.id]["open"]  # 认领：可以登记
    req = next(r for r in net.store.requests.values() if r.agent_id == a.id)
    assert req.requester_id == host and req.message_id == msg.id  # 下发人是原作者
    assert "没有人交代" in by[b.id]["open"]  # 叫来看：不能登记
    assert c.id not in by  # 甲的回复不能被认领
    refused = [(r["agent"], r["why"]) for e in route_log(net, g) for r in e["refused"]]
    assert ("丙", "认领只针对人发的消息（规则 4）") in refused

    net.send_message(chat.id, "@乙 你来", actor=host, background=False)
    assert ("丙", "消息已经 @ 了成员，由他们处理") in [(r["agent"], r["why"]) for e in route_log(net, g) for r in e["refused"]]
    net.send_message(chat.id, "外人来", actor=host, background=False)
    assert ("不存在", "不是项目成员") in [(r["agent"], r["why"]) for e in route_log(net, g) for r in e["refused"]]


def test_may_limits_kinds(tmp_path: Path):
    """不变量 24：建议了插件没申请的种类，一律丢弃并留痕。"""
    net, host, (a, b, c), g, data, log = setup(tmp_path, may=("wake",))
    rules(data, {"修": {"claim": {"agent": "甲"}}})
    net.send_message(net.project_chat(g.id, actor=host).id, "修一下", actor=host, background=False)
    assert not log
    assert ("甲", "没申请 claim") in [(r["agent"], r["why"]) for e in route_log(net, g) for r in e["refused"]]


# ================= 示例：接力分派 =================
def test_relay_example_end_to_end(tmp_path: Path):
    """不变量 24：示例插件「接力分派」。没 @ 人的问题按能力分派给反馈认领（任务的下发人是发问题的人），
    反馈 → 方案 → 编码自动接力，3 次用完后请人接手；人 @ 审查后审查叫部署，部署等人确认。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("relay_demo", ROOT / "examples" / "relay" / "setup_demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    net = Network(ScriptedRuntime(scenario_handler), tmp_path / "data", seed=False,
                  plugins=pluginfs.PluginCatalog(ROOT / "examples" / "plugins.json"))
    host, = users(net, "wang@x.io")
    net.ensure_assistant(host)
    ids = {n: net.create_agent(n, d, caps, auth, actor=host).id for n, d, caps, auth in demo.ROLES}
    g = net.create_project("登录页", "", list(ids.values()), actor=host)
    net.install_plugin(g.id, "relay", actor=host)
    chat = net.project_chat(g.id, actor=host)

    first = net.send_message(chat.id, "登录页在 Safari 上白屏，用户进不去，帮忙看看", actor=host, background=False)
    replies = [m for m in chat.messages if m.role == "agent"]
    assert [net._name(m.agent_id) for m in replies] == ["反馈", "方案", "编码"]
    assert first.claimed_by == ids["反馈"]
    req = next(r for r in net.store.requests.values() if r.agent_id == ids["反馈"])
    assert req.requester_id == host
    assert any("用完了" in m.text for m in chat.messages if m.role == "event")

    net.send_message(chat.id, "@审查 帮忙审查一下编码的改动", actor=host, background=False)
    assert [net._name(m.agent_id) for m in chat.messages if m.role == "agent"][-2:] == ["审查", "部署"]
    assert "等人在群聊里 @ 我确认" in chat.messages[-1].text
    net.send_message(chat.id, "@部署 确认部署", actor=host, background=False)
    assert chat.messages[-1].text == "已部署登录页的兼容修复（演示）。"
