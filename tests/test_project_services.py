"""项目服务（docs/project-services.md，不变量 29）：主持人发布即生效；调用方只和入口对话，入口只委托服务的岗位；
服务调用沿途都在服务面：为调用方做、业务授权只用调用方自己的；档案只读圈定范围、只写收件目录；用量记在项目下，
主持人只看用量，服务会话只属于调用方。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from matrix.connector_catalog import ConnectorCatalog
from matrix.engine import Network, NetworkError
from matrix.models import PROJECT_FILES
from matrix.runtime import ScriptCall, ScriptedRuntime
from matrix.store import Store
from tests.test_connections import SECRET, FakeMCP, catalog_file
from tests.test_users import client, users


@pytest.fixture
def fake():
    f = FakeMCP()
    yield f
    f.close()


def setup(tmp_path: Path, handler, connectors: Path | None = None,
          entry_auth=("delegate", "web.research", "file.write")):
    """主持人 maya 的项目「报销」：入口「前台」（maya 的）、岗位「财务」（ben 的，ben 是参与者）、「法务」（maya 的）。
    档案里有公开资料和内部资料。ann 是项目外的调用方。"""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    net = Network(ScriptedRuntime(handler), data, seed=False,
                  connectors=ConnectorCatalog(connectors) if connectors else None)
    maya, ben, ann, carl = users(net, "maya@x.io", "ben@x.io", "ann@x.io", "carl@x.io")
    for u, name in ((maya, "Maya 的助理"), (ben, "Ben 的助理"), (ann, "Ann 的助理"), (carl, "Carl 的助理")):
        net.create_agent(name, "助理", [], [], actor=u)
    desk = net.create_agent("前台", "接待", [], list(entry_auth), actor=maya)
    legal = net.create_agent("法务", "看合同", [], ["web.research"], actor=maya)
    g = net.create_project("报销", "报销流程", [desk.id, legal.id], actor=maya)
    net.invite(g.id, "ben@x.io", actor=maya)
    net.respond_invitation(g.id, True, actor=ben)
    fin = net.create_agent("财务", "算账", [], ["web.research"], actor=ben)
    net.add_member(g.id, fin.id, actor=ben)
    net.publish_file(g.id, "公开/手册.md", "报销 5000 以下直接批".encode(), actor=maya)
    net.publish_file(g.id, "内部/薪资.md", "老板的工资".encode(), actor=maya)
    return net, (maya, ben, ann, carl), g, desk, fin, legal


def publish(net: Network, g, maya: str, **kw):
    args = dict(entry="前台", staff=["财务"], scope=["公开"], inbox="收件", authority=["delegate", "web.research"],
                readers=["ann@x.io"])
    args.update(kw)
    return net.create_service(g.id, "baoxiao", "报销服务", "报销咨询与提交", actor=maya, **args)


def by_agent(net: Network, handlers: dict[str, object]):
    """按 Agent 名字分派的 handler；没有对应的回“ok”。"""
    def handler(call: ScriptCall) -> str:
        name = net.store.agents[call.agent_id].name
        h = handlers.get(name)
        return h(call) if callable(h) else "ok"
    return handler


# ================= 发布：主持人发布即生效；谁能用 =================
def test_only_the_host_publishes_and_readers_can_use(tmp_path: Path):
    """不变量 29：只有主持人发布，发布即生效；点名的调用方与项目参与者能用，别人当它不存在；改配置切新版本。"""
    net, (maya, ben, ann, carl), g, desk, fin, legal = setup(tmp_path, lambda call: "ok")
    with pytest.raises(NetworkError, match="只有主持人"):
        publish(net, g, ben)
    with pytest.raises(NetworkError, match="不是项目里的 Agent"):
        publish(net, g, maya, entry="Ann 的助理")
    with pytest.raises(NetworkError, match="档案里没有"):
        publish(net, g, maya, scope=["不存在"])
    with pytest.raises(NetworkError, match="没有这些权限"):
        publish(net, g, maya, authority=["root"])
    x = publish(net, g, maya)
    assert x.version == 1 and x.entry == desk.id and x.staff == [fin.id]
    assert (net.store.project_dir(g.id) / "收件").is_dir()
    assert any("发布了服务「报销服务」v1" in m.text for m in net.project_chat(g.id, actor=maya).messages)

    assert [s["id"] for s in net.services_for(ann)] == [x.id]
    card = net.services_for(ann)[0]
    assert "scope" not in card and "authority" not in card            # 调用方只看名片
    assert {p["agent"] for p in card["passes"]} == {"前台", "财务"}    # 名片写明请求会经过谁的 Agent
    assert net.services_for(ben)[0]["scope"] == ["公开"]               # 参与者看配置
    assert net.services_for(carl) == []
    with pytest.raises(NetworkError, match="unknown service"):
        net.open_service_session(x.id, actor=carl)

    net.update_service(g.id, x.id, actor=maya, public=True)
    assert x.version == 2 and [s["id"] for s in net.services_for(carl)] == [x.id]
    mine = net.open_service_session(x.id, actor=ann)
    net.update_service(g.id, x.id, actor=maya, public=False, readers=[])
    assert x.version == 3
    with pytest.raises(NetworkError, match="不再对你开放"):
        net.send_message(mine.id, "还能用吗", actor=ann, background=False)
    own = net.open_service_session(x.id, actor=maya)                  # 项目参与者照样能用
    net.send_message(own.id, "自己用", actor=maya, background=False)
    assert own.messages[-1].role == "agent"


def test_withdraw_pause_and_switch_entry(tmp_path: Path):
    """不变量 29：撤回后会话只读；入口不在项目里时服务暂停；换了入口，旧会话接着用新入口；删项目时服务会话一起删。"""
    net, (maya, ben, ann, carl), g, desk, fin, legal = setup(tmp_path, lambda call: net.store.agents[call.agent_id].name)
    x = publish(net, g, maya)
    conv = net.open_service_session(x.id, actor=ann)
    net.send_message(conv.id, "你好", actor=ann, background=False)
    assert conv.messages[-1].text == "前台"
    net.update_service(g.id, x.id, actor=maya, entry="法务")
    net.send_message(conv.id, "换人了吗", actor=ann, background=False)
    assert conv.agent_id == legal.id and conv.messages[-1].text == "法务"
    net.remove_member(g.id, legal.id, actor=maya)
    with pytest.raises(NetworkError, match="暂停"):
        net.send_message(conv.id, "在吗", actor=ann, background=False)
    net.update_service(g.id, x.id, actor=maya, entry="前台")
    net.withdraw_service(g.id, x.id, actor=maya)
    assert conv.archived and net.services_for(ann) == []
    with pytest.raises(NetworkError, match="只读"):
        net.send_message(conv.id, "还在吗", actor=ann, background=False)
    assert net.conversation_detail(conv.id, ann)["messages"]        # 调用方还看得到记录

    y = net.create_service(g.id, "again", "再来", actor=maya, entry="前台", readers=["ann@x.io"])
    other = net.open_service_session(y.id, actor=ann)
    net.delete_conversation(other.id, actor=ann)                     # 调用方自己随时能删
    keep = net.open_service_session(y.id, actor=ann)
    net.delete_project(g.id, actor=maya)
    assert keep.id not in net.store.conversations and y.status == "withdrawn"


# ================= 服务会话：只属于调用方；档案只读圈定范围、只写收件目录 =================
def test_session_belongs_to_the_caller_and_archive_is_scoped(tmp_path: Path):
    """不变量 29：服务会话只有调用方看得到（主持人也看不到、群聊里没有）；入口只读圈定范围里的实时资料，
    改档案的工具不给，只能写收件目录（不通知）。"""
    seen: dict[str, object] = {}

    def desk_run(call: ScriptCall) -> str:
        root = call.cwd / PROJECT_FILES
        seen["files"] = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
        seen["tools"] = list(call.tool_names)
        seen["prompt"] = call.prompt
        (call.cwd / "申请.md").write_text("ann 的报销申请", encoding="utf-8")
        return call.tool("publish_file", path="申请.md")

    net, (maya, ben, ann, carl), g, desk, fin, legal = setup(tmp_path, None)
    net.runtime.handler = by_agent(net, {"前台": desk_run})
    x = publish(net, g, maya)
    conv = net.open_service_session(x.id, actor=ann)
    before = len(net.project_chat(g.id, actor=maya).messages)
    net.send_message(conv.id, "我要报销", actor=ann, background=False)

    assert seen["files"] == ["公开/手册.md"]                          # 只读圈定的范围
    names = {t.removeprefix("mcp__matrix_net__") for t in seen["tools"]}
    assert {"publish_file", "delegate", "list_members"} <= names
    assert not names & {"record_decision", "list_decisions", "add_link", "propose_brief", "propose_agent"}
    assert "服务「报销服务」" in seen["prompt"] and "老板的工资" not in seen["prompt"] and "内部/薪资.md" not in seen["prompt"]
    assert conv.messages[-1].text.startswith("已放进收件目录：收件/申请.md")
    assert (net.store.project_dir(g.id) / "收件" / "申请.md").read_text(encoding="utf-8") == "ann 的报销申请"
    assert len(net.project_chat(g.id, actor=maya).messages) == before  # 收件不通知，群聊里也没有这段对话
    log = (net.store.project_dir(g.id).parent / "log.jsonl").read_text(encoding="utf-8")
    assert '"收件/申请.md"' in log                                    # 档案日志照记（不变量 17）

    assert conv.messages[-1].private_to == [ann]
    for who in (maya, ben):                                          # 主持人、参与者都看不到服务会话
        with pytest.raises(NetworkError, match="unknown conversation"):
            net.conversation_detail(conv.id, who)
        assert conv.id not in {c["id"] for c in net.snapshot(who)["conversations"]}
    assert conv.id in {c["id"] for c in net.snapshot(ann)["conversations"]}

    # 内容是实时的：档案改了，下一轮就用新的
    net.publish_file(g.id, "公开/新规.md", "新规".encode(), actor=maya)
    net.send_message(conv.id, "有新规吗", actor=ann, background=False)
    assert seen["files"] == ["公开/手册.md", "公开/新规.md"]
    # 没有收件目录就不能写
    net.update_service(g.id, x.id, actor=maya, inbox="")
    net.send_message(conv.id, "再交一份", actor=ann, background=False)
    assert "没有收件目录" in conv.messages[-1].text


def test_entry_delegates_only_to_staff_for_the_caller(tmp_path: Path):
    """不变量 29：入口只委托服务的岗位；委托也在服务面，为调用方做、只读圈定范围；
    委托给两端看得到（调用方与岗位的所有者），主持人看不到；权限交服务授予的。"""
    seen: dict[str, object] = {}

    def desk_run(call: ScriptCall) -> str:
        if call.turn > 1:
            return "财务算好了"
        seen["members"] = call.tool("list_members")
        seen["legal"] = call.tool("delegate", agent="法务", mission="看合同", expected_outcome="意见")
        return call.tool("delegate", agent="财务", mission="算一下报销额", expected_outcome="金额")

    def fin_run(call: ScriptCall) -> str:
        root = call.cwd / PROJECT_FILES
        seen["fin_files"] = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
        seen["fin_auth"] = list(call.authority)
        seen["fin_prompt"] = call.prompt
        return "3000 元"

    net, (maya, ben, ann, carl), g, desk, fin, legal = setup(tmp_path, None)
    net.runtime.handler = by_agent(net, {"前台": desk_run, "财务": fin_run})
    x = publish(net, g, maya, authority=["delegate"])
    conv = net.open_service_session(x.id, actor=ann)
    net.send_message(conv.id, "帮我算报销", actor=ann, background=False)
    net.wait_quiet()

    assert "财务" in seen["members"] and "法务" not in seen["members"]
    assert "不是服务「报销服务」的岗位" in seen["legal"]
    task = next(t for t in net.store.tasks.values() if t.delegatee_id == fin.id)
    assert task.caller_id == ann and set(task.owners) == {ann, ben}
    assert seen["fin_files"] == ["公开/手册.md"] and "服务「报销服务」" in seen["fin_prompt"]
    assert seen["fin_auth"] == []                                    # web.research 不在服务授予的权限里
    assert net.can_see_task(task, ann) and net.can_see_task(task, ben) and not net.can_see_task(task, maya)
    assert conv.messages[-1].text == "财务算好了"


def test_business_auth_is_the_callers_own(tmp_path: Path, fake: FakeMCP):
    """不变量 29（与不变量 19）：要用户鉴权的系统只用调用方自己的授权，不用入口所有者的；调用方没授权就没有连接器。"""
    def desk_run(call: ScriptCall) -> str:
        if "docs__search_docs" in call.connector_tools():
            return call.connector("docs__search_docs", q="报销进度")
        return "没有连接器"

    net, (maya, ben, ann, carl), g, desk, fin, legal = setup(tmp_path, None, catalog_file(tmp_path, fake.url),
                                                             entry_auth=("connector.use", "delegate"))
    net.runtime.handler = by_agent(net, {"前台": desk_run})
    owners = net.authorize_connector("docs", {"token": SECRET}, maya)
    net.update_agent(desk.id, actor=maya, connections=[owners["id"]])
    x = publish(net, g, maya, authority=["connector.use"])
    conv = net.open_service_session(x.id, actor=ann)
    net.send_message(conv.id, "我的报销到哪了", actor=ann, background=False)
    assert conv.messages[-1].text == "没有连接器" and fake.calls == []   # 所有者的授权不借给调用方
    net.authorize_connector("docs", {"token": SECRET}, ann)
    net.send_message(conv.id, "再查一次", actor=ann, background=False)
    assert len(fake.calls) == 1 and conv.messages[-1].text != "没有连接器"


# ================= 用量：记在项目下，主持人只看用量 =================
def test_usage_is_billed_to_the_project_and_host_sees_only_usage(tmp_path: Path):
    """不变量 29：服务的每一轮记在项目下（payer=project），主持人看得到多少轮、谁用了，看不到内容；别人看不到用量。"""
    net, (maya, ben, ann, carl), g, desk, fin, legal = setup(tmp_path, lambda call: "好的")
    x = publish(net, g, maya, public=True)
    for who, text in ((ann, "我的报销单号 123"), (carl, "Carl 的问题")):
        conv = net.open_service_session(x.id, actor=who)
        net.send_message(conv.id, text, actor=who, background=False)
    rows = [json.loads(r) for r in (net.store.root / "usage.jsonl").read_text(encoding="utf-8").splitlines()]
    svc_rows = [r for r in rows if r.get("service") == x.id]
    assert [(r["user"], r["payer"], r["project"]) for r in svc_rows] == [(ann, "project", g.id), (carl, "project", g.id)]
    usage = net.service_usage(g.id, x.id, actor=maya)
    assert usage["runs"] == 2 and usage["sessions"] == 2
    assert {u["user"] for u in usage["users"]} == {"ann@x.io", "carl@x.io"}
    assert "报销单号" not in json.dumps(usage, ensure_ascii=False)
    for who in (ben, ann):
        with pytest.raises(NetworkError):
            net.service_usage(g.id, x.id, actor=who)


def test_services_survive_reload_and_api(tmp_path: Path):
    """服务与服务会话写进 state.json，重启后照旧；接口：发布、名片、开会话、发消息、用量。"""
    net, (maya, ben, ann, carl), g, desk, fin, legal = setup(tmp_path, lambda call: "收到")
    host, caller = client(net, "maya@x.io"), client(net, "ann@x.io")
    r = host.post(f"/api/projects/{g.id}/services", json={
        "name": "baoxiao", "title": "报销服务", "entry": "前台", "scope": ["公开"], "readers": ["ann@x.io"]})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert caller.post(f"/api/projects/{g.id}/services", json={"name": "x", "title": "x", "entry": "前台"}).status_code == 404
    assert [s["id"] for s in caller.get("/api/services").json()] == [sid]
    conv = caller.post(f"/api/services/{sid}/sessions", json={}).json()
    assert conv["kind"] == "service" and conv["service_id"] == sid
    assert caller.post(f"/api/conversations/{conv['id']}/messages", json={"text": "你好"}).status_code == 200
    net.wait_quiet()
    assert host.get(f"/api/conversations/{conv['id']}").status_code == 404
    assert host.get(f"/api/projects/{g.id}/services/{sid}/usage").json()["runs"] == 1
    assert caller.get(f"/api/projects/{g.id}/services/{sid}/usage").status_code == 404
    assert host.patch(f"/api/projects/{g.id}/services/{sid}", json={"description": "新说明"}).json()["version"] == 2
    net.store.save()
    again = Store(net.store.root)
    assert again.project_services[sid].version == 2 and again.conversations[conv["id"]].service_id == sid
    assert host.delete(f"/api/projects/{g.id}/services/{sid}").json()["status"] == "withdrawn"
