"""示例插件「队友协作（Raft 式）」（examples/plugins/teammates，不变量 24）：被动投递、任务板与认领锁、
Agent 只能交到待验收、人在群聊里说完成；被叫来看的成员只回“（略过）”时群聊里不出现；服务按网络给的调用者身份加锁。"""

from __future__ import annotations

import json
from pathlib import Path

from matrix import plugins as pluginfs
from matrix.engine import Network
from matrix.plugin_check import lint, load
from matrix.plugin_service import StdioClient, resolve_command
from matrix.runtime import ScriptCall, ScriptedRuntime
from matrix.scripted import scenario_handler
from tests.test_users import users

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "examples" / "plugins" / "teammates"
DEMO = ROOT / "examples" / "teammates" / "setup_demo.py"


def engine(tmp: Path) -> StdioClient:
    c = StdioClient(resolve_command("python", ("{plugin}/service/team_engine.py",), PKG, tmp), tmp)
    c.start()
    return c


AGENT = lambda name: {"matrix/caller": {"kind": "agent", "name": name}}  # noqa: E731
NET = {"matrix/caller": {"kind": "network"}}


def test_package_is_valid():
    files = load(PKG)
    m = pluginfs.check_package(files)
    assert dict(m.service.tools)["route_suggest"] == "hook" and m.routes[0].may == ("wake",)
    assert not [w for w in lint(files, m) if "service" not in w]


def test_claim_lock_and_transitions(tmp_path: Path):
    """认领先到先得；只有负责人能改；Agent 最多交到待验收，完成、关闭只能由人在群聊里说（经 route，作者由网络认定）。
    身份取 _meta 里网络给的调用者，参数里自报的名字不算。"""
    c = engine(tmp_path)
    try:
        assert "#1" in c.call("task_create", {"title": "登录页加验证码"}, meta=AGENT("前端"))[0]
        assert "认领成功" in c.call("task_claim", {"id": 1}, meta=AGENT("后端"))[0]
        text, err = c.call("task_claim", {"id": 1, "name": "前端"}, meta=AGENT("前端"))
        assert err and "已经归 @后端" in text
        text, err = c.call("task_update", {"id": 1, "status": "in_review"}, meta=AGENT("前端"))
        assert err and "只有负责人" in text
        text, err = c.call("task_update", {"id": 1, "status": "done"}, meta=AGENT("后端"))
        assert err and "要由人在群聊里说" in text
        assert not c.call("task_update", {"id": 1, "status": "in_review", "note": "好了"}, meta=AGENT("后端"))[1]
        text, err = c.call("route_suggest", {"message": {"text": "#1 完成"}}, meta=AGENT("后端"))
        assert err and "只给网络" in text  # Agent 调不到 route 工具（网络也不会开给它）
        # Agent 在群聊里说“#1 完成”不算：route 收到的作者是 Agent
        c.call("route_suggest", {"message": {"text": "#1 完成", "author_kind": "agent", "author": "后端"},
                                 "members": [], "relay": {"budget": 3}}, meta=NET)
        assert "[待验收]" in c.call("tasks", {}, meta=AGENT("测试"))[0]
        c.call("route_suggest", {"message": {"text": "#1 完成", "author_kind": "user", "author": "wang@x.io"},
                                 "members": [], "relay": {"budget": 3}}, meta=NET)
        assert "[已完成]" in c.call("tasks", {}, meta=AGENT("测试"))[0]
        text, err = c.call("task_claim", {"id": 1}, meta=AGENT("测试"))
        assert err and "已完成" in text
    finally:
        c.stop()


def test_route_passive_delivery(tmp_path: Path):
    """被动投递：人的发言叫所有频道成员（先叫被提到的任务负责人、名字或能力被提到的），不叫个人助理、作者、已经在这一串里的；
    最多叫 relay.budget 个。Agent 的发言只叫被点到的。只有指令的消息只叫相关任务的负责人。"""
    c = engine(tmp_path)
    members = [{"name": n, "capabilities": caps} for n, caps in (("前端", ["登录页"]), ("后端", ["接口"]), ("测试", ["用例"]))]
    members.append({"name": "王的助理", "capabilities": [], "assistant": True})
    route = lambda text, kind="user", author="wang@x.io", budget=3, involved=(): json.loads(c.call(  # noqa: E731
        "route_suggest", {"message": {"text": text, "author_kind": kind, "author": author}, "members": members,
                          "relay": {"budget": budget, "involved": list(involved)}}, meta=NET)[0])
    try:
        assert [w["agent"] for w in route("接口有点慢")["wake"]] == ["后端", "前端", "测试"]
        assert [w["agent"] for w in route("大家看一下", budget=2)["wake"]] == ["前端", "后端"]
        assert [w["agent"] for w in route("大家看一下", involved=["前端"])["wake"]] == ["后端", "测试"]
        assert route("我改好了，需要写用例", kind="agent", author="前端")["wake"] == [{"agent": "测试", "reason": "消息里提到「用例」"}]
        assert route("收到", kind="agent", author="前端")["wake"] == []
        r = route("建任务：登录页加验证码")
        assert r["done"] == ["创建 #1"]
        c.call("task_claim", {"id": 1}, meta=AGENT("后端"))
        assert [w["agent"] for w in route("#1 退回")["wake"]] == ["后端"]  # 只有指令：只叫负责人
    finally:
        c.stop()


def test_done_hands_off_to_the_next_stage(tmp_path: Path):
    """人说“#N 完成”：叫引用了 #N 的后续任务的负责人；没人认领的，按标题前缀（“方案：…”）叫对应岗位的成员。
    这样上一棒由人确认、下一棒自动接上（真实模型实测里方案没被叫醒，交棒卡住）。"""
    c = engine(tmp_path)
    members = [{"name": "小方", "roles": ["方案"]}, {"name": "小编", "roles": ["编码"]}, {"name": "小反", "roles": ["反馈"]}]
    route = lambda text: json.loads(c.call("route_suggest", {"message": {"text": text, "author_kind": "user", "author": "wang@x.io"},  # noqa: E731
                                                             "members": members, "relay": {"budget": 3}}, meta=NET)[0])
    try:
        c.call("task_create", {"title": "反馈：中文变方框", "claim": True}, meta=AGENT("小反"))
        c.call("task_update", {"id": 1, "status": "in_review"}, meta=AGENT("小反"))
        c.call("task_create", {"title": "方案：中文变方框（反馈 #1）"}, meta=AGENT("小反"))
        c.call("task_create", {"title": "编码：别的事"}, meta=AGENT("小反"))
        assert route("#1 完成")["wake"] == [{"agent": "小反", "reason": "#1 的负责人"},
                                           {"agent": "小方", "reason": "#1 完成了，#2（方案）还没人认领"}]
        c.call("task_claim", {"id": 2}, meta=AGENT("小方"))
        c.call("task_create", {"title": "编码：按方案改（方案 #2）"}, meta=AGENT("小方"))
        c.call("task_update", {"id": 2, "status": "in_review"}, meta=AGENT("小方"))
        assert [w["agent"] for w in route("#2 完成")["wake"]] == ["小方", "小编"]
    finally:
        c.stop()


def test_pass_is_not_posted_only_for_route_wakes(tmp_path: Path):
    """被 route 叫来看的那一轮只回“（略过）”，网络不发进群聊；被人 @ 的那一轮回同样的话照常发出。"""
    net = Network(ScriptedRuntime(lambda call: "（略过）"), tmp_path / "data", seed=False,
                  plugins=pluginfs.PluginCatalog(ROOT / "examples" / "plugins.json"))
    host, = users(net, "wang@x.io")
    net.ensure_assistant(host)
    a = net.create_agent("前端", "前端", ["登录页"], ["file.write"], actor=host)
    g = net.create_project("官网", "", [a.id], actor=host)
    net.install_plugin(g.id, "teammates", actor=host)
    chat = net.project_chat(g.id, actor=host)
    net.send_message(chat.id, "登录页有点慢", actor=host, background=False)
    assert len(net.runtime.calls) == 1 and not [m for m in chat.messages if m.role == "agent"]
    net.send_message(chat.id, "@前端 看一下", actor=host, background=False)
    assert [m.text for m in chat.messages if m.role == "agent"] == ["（略过）"]


def test_teammates_demo_end_to_end(tmp_path: Path):
    """不变量 24：演示走一遍。“建任务”建出 #1 并送给三个队友：后端认领 #1、前端开 #2 认领、测试略过；
    “下午评审”三人都略过、群聊里没有回复；人点名问进度后两人交到待验收；人说“#1 完成 #2 完成”后两个任务完成。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("team_demo", DEMO)
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    seen: list[str] = []

    def spy(call: ScriptCall) -> str:
        seen.append(call.agent_id)
        return scenario_handler(call)
    net = Network(ScriptedRuntime(spy), tmp_path / "data", seed=False,
                  plugins=pluginfs.PluginCatalog(ROOT / "examples" / "plugins.json"))
    host, = users(net, "wang@x.io")
    net.ensure_assistant(host)
    ids = {n: net.create_agent(n, d, caps, auth, actor=host).id for n, d, caps, auth in demo.TEAM}
    g = net.create_project("官网", "", list(ids.values()), actor=host)
    net.install_plugin(g.id, "teammates", actor=host)
    chat = net.project_chat(g.id, actor=host)
    board = lambda: json.loads((net.store.plugin_data_dir(g.id, "teammates") / "service" / "tasks.json")  # noqa: E731
                               .read_text(encoding="utf-8"))["tasks"]
    replies = lambda: [m for m in chat.messages if m.role == "agent"]  # noqa: E731

    net.send_message(chat.id, demo.SCRIPT[0], actor=host, background=False)
    assert sorted(seen) == sorted(ids.values())  # 没 @ 人，三个队友都收到了
    assert sorted(net._name(m.agent_id) for m in replies()) == ["前端", "后端"]  # 测试略过，群里看不到
    assert [(t["id"], t["assignee"], t["status"]) for t in board()] == [(1, "后端", "in_progress"), (2, "前端", "in_progress")]

    seen.clear()
    n = len(replies())
    net.send_message(chat.id, demo.SCRIPT[1], actor=host, background=False)
    assert len(seen) == 3 and len(replies()) == n  # 三人都收到、都略过

    net.send_message(chat.id, demo.SCRIPT[2], actor=host, background=False)
    assert {t["status"] for t in board()} == {"in_review"}
    net.send_message(chat.id, demo.SCRIPT[3], actor=host, background=False)
    assert {t["status"] for t in board()} == {"done"}
    assert not any("用完了" in m.text for m in chat.messages)  # 插件按剩余预算建议，不会被网络拒
