"""公共 Agent（docs/public-agents.md，不变量 27）：维护组申请、管理员批准；所有用户都能私聊、拉进项目；
服务面只为调用方做事：私有目录只读不写回、台账不列别处、不申请外溢、用调用方自己的授权、过程只给调用方、维护人只看用量。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from matrix.connector_catalog import ConnectorCatalog
from matrix.engine import Network, NetworkError
from matrix.models import AGENT_HOME, TaskStatus
from matrix.runtime import ScriptCall, ScriptedRuntime
from matrix.store import Store
from tests.test_connections import SECRET, FakeMCP, catalog_file
from tests.test_questions import answer_when_pending
from tests.test_users import client, joint, users


@pytest.fixture
def fake():
    f = FakeMCP()
    yield f
    f.close()


def setup(tmp_path: Path, handler, connectors: Path | None = None, authority=("web.research", "file.write")):
    """管理员（最早注册的人）、维护人 maya（先有自己的助理）、两个普通用户；maya 的「报销助手」申请并批准成公共。"""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    net = Network(ScriptedRuntime(handler), data, seed=False,
                  connectors=ConnectorCatalog(connectors) if connectors else None)
    admin, maya, ann, ben = users(net, "admin@x.io", "maya@x.io", "ann@x.io", "ben@x.io")
    for u, name in ((ann, "Ann 的助理"), (ben, "Ben 的助理"), (maya, "Maya 的助理")):
        net.create_agent(name, "助理", [], [], actor=u)
    pub = net.create_agent("报销助手", "处理报销", ["报销"], list(authority), actor=maya)
    net.request_public(pub.id, "财务系统的服务入口", [], actor=maya)
    net.review_public(pub.id, True, actor=admin)
    return net, admin, maya, ann, ben, pub


# ================= 归属：申请、批准、撤回 =================
def test_public_needs_admin_approval_and_freezes_authority(tmp_path: Path):
    """不变量 27：设成公共由管理员批准，批准时冻结权限；之前别人当它不存在，之后谁都能私聊、拉进项目。"""
    net = Network(ScriptedRuntime(lambda call: call.authority and ",".join(sorted(call.authority)) or "无"),
                  tmp_path, seed=False)
    admin, maya, ann = users(net, "admin@x.io", "maya@x.io", "ann@x.io")
    net.create_agent("Maya 的助理", "助理", [], [], actor=maya)
    pub = net.create_agent("报销助手", "处理报销", [], ["web.research"], actor=maya)
    with pytest.raises(NetworkError, match="unknown agent"):
        net.request_public(pub.id, actor=ann)  # 不是维护人
    net.request_public(pub.id, "财务入口", actor=maya)
    assert pub.public_status == "pending" and not pub.is_public
    with pytest.raises(NetworkError, match="unknown agent"):
        net.direct_chat(pub.id, actor=ann)  # 还没批准：对别人不存在
    assert net.public_agents(ann) == []
    with pytest.raises(NetworkError, match="unknown admin"):
        net.review_public(pub.id, True, actor=maya)  # 维护人不能自己批准
    assert [x["status"] for x in net.public_requests(admin)] == ["pending"]
    net.review_public(pub.id, True, actor=admin)
    assert pub.is_public and pub.public_authority == ["web.research"]
    assert [x["name"] for x in net.public_agents(ann)] == ["报销助手"]
    card = next(x for x in net.snapshot(ann)["agents"] if x["id"] == pub.id)
    assert card["public"] and not card["mine"] and "model" not in card  # 别人只看到名片
    with pytest.raises(NetworkError, match="unknown agent"):
        net.update_agent(pub.id, actor=ann, description="改它")  # 能用不等于能改

    dm = net.direct_chat(pub.id, actor=ann)
    net.send_message(dm.id, "你好", actor=ann, background=False)
    assert dm.messages[-1].text == "web.research"
    # 维护人放宽权限：服务面不跟着放宽，要管理员再批准
    net.update_agent(pub.id, actor=maya, authority=["web.research", "shell.exec"])
    assert net.public_requests(admin)[0]["wider"] == ["shell.exec"]
    net.send_message(dm.id, "再来", actor=ann, background=False)
    assert dm.messages[-1].text == "web.research"
    net.request_public(pub.id, actor=maya)
    net.review_public(pub.id, True, actor=admin)
    net.send_message(dm.id, "又来", actor=ann, background=False)
    assert dm.messages[-1].text == "shell.exec,web.research"


def test_withdrawing_public_closes_it_to_others(tmp_path: Path):
    """不变量 27：不再公共——别人和它的私聊归档为只读，没有维护人在的项目里移出；维护人自己的不动。"""
    net, admin, maya, ann, ben, pub = setup(tmp_path, lambda call: "ok")
    dm = net.direct_chat(pub.id, actor=ann)
    g = net.create_project("Ann 的项目", "", [pub.id], actor=ann)  # 任何人都能拉进自己的项目
    mine = net.create_project("Maya 的项目", "", [pub.id], actor=maya)
    own_dm = net.direct_chat(pub.id, actor=maya)
    net.withdraw_public(pub.id, actor=maya)
    assert dm.archived and not own_dm.archived
    assert pub.id not in g.member_ids and pub.id in mine.member_ids
    assert any("不再是公共 Agent" in m.text for m in net.project_chat(g.id, actor=ann).messages)
    with pytest.raises(NetworkError, match="unknown agent"):
        net.direct_chat(pub.id, actor=ann)


def test_public_guards(tmp_path: Path):
    """不变量 27：公共 Agent 只在平台云侧、不当助理；维护人能维护不能删除；迁移不把它当作跨信任域。"""
    net, admin, maya, ann, ben, pub = setup(tmp_path, lambda call: "ok")
    with pytest.raises(NetworkError, match="不能当助理"):
        net.set_assistant(pub.id, actor=maya)
    helper = net.store.users[maya].assistant_id
    with pytest.raises(NetworkError, match="助理"):
        net.request_public(helper, actor=maya)
    net.set_maintainers(pub.id, ["ben@x.io"], actor=maya)
    with pytest.raises(NetworkError, match="只有所有者"):
        net.set_maintainers(pub.id, [], actor=ben)
    net.update_agent(pub.id, actor=ben, description="Ben 也能维护")
    with pytest.raises(NetworkError, match="只有所有者能删除"):
        net.delete_agent(pub.id, actor=ben)
    dm = net.direct_chat(pub.id, actor=ann)
    g = net.create_project("Ann 的项目", "", [pub.id], actor=ann)
    net.store.save()
    again = Store(net.store.root)  # 重新加载：旧版的跨信任域清理不碰公共 Agent
    assert not again.conversations[dm.id].archived and pub.id in again.projects[g.id].member_ids
    assert again.agents[pub.id].is_public and again.agents[pub.id].public_authority == pub.public_authority


# ================= 服务面 =================
def test_service_face_keeps_nothing_and_shows_process_only_to_the_caller(tmp_path: Path):
    """不变量 27：维护面写下的是公共资料，服务面只读、写了也不写回；台账不列别处；不申请外溢；
    过程只给调用方，维护人只看用量。"""
    seen: dict[str, list] = {"home": [], "prompt": []}

    def handler(call: ScriptCall) -> str:
        home = call.cwd / AGENT_HOME
        seen["home"].append(sorted(p.name for p in home.iterdir()) if home.is_dir() else None)
        seen["prompt"].append(call.prompt)
        if "【维护】" in call.prompt:
            (home / "规则.md").write_text("报销 5000 以下直接批", encoding="utf-8")
            return "记下了"
        call.say("查了一下规则")
        (home / f"{call.context_id.replace('/', '_')}.md").write_text("某人的报销单号", encoding="utf-8")
        out = call.escalate("想改系统配置")
        return f"按规则处理；外溢：{out}"

    net, admin, maya, ann, ben, pub = setup(tmp_path, handler)
    upkeep = net.direct_chat(pub.id, actor=maya)
    net.send_message(upkeep.id, "【维护】整理一下报销规则", actor=maya, background=False)  # 维护面：写回
    assert (net.agent_home(pub.id) / "规则.md").is_file()

    dm = net.direct_chat(pub.id, actor=ann)
    net.send_message(dm.id, "我的报销单", actor=ann, background=False)
    assert "规则.md" in seen["home"][-1]                     # 公共资料挂进服务面
    assert sorted(p.name for p in net.agent_home(pub.id).iterdir()) == ["规则.md"]  # 调用方的东西不写回
    reply = dm.messages[-1]
    # 不申请外溢、不打扰维护人：云侧本来就不审批外溢（不变量 14，docs/cloud-runtime.md 7.1），服务面同样拿到 unavailable
    assert reply.text == "按规则处理；外溢：unavailable" and not net.store.approvals
    assert reply.private_to == [ann]
    view = net.conversation_detail(dm.id, ann)
    assert next(m for m in view["messages"] if m["role"] == "agent")["steps"]
    with pytest.raises(NetworkError, match="unknown conversation"):
        net.conversation_detail(dm.id, maya)                 # 维护人看不到别人和它的私聊

    # 群聊里：@ 它的人看过程，维护人哪怕在项目里也只看到回复
    g, chat = joint(net, ben, "maya@x.io", maya, [pub.id])
    net.send_message(chat.id, "@报销助手 帮我看看", actor=ben, background=False)
    msg = next(m for m in reversed(chat.messages) if m.role == "agent" and m.agent_id == pub.id)
    assert msg.private_to == [ben]
    as_maya = next(m for m in net.conversation_detail(chat.id, maya)["messages"] if m["id"] == msg.id)
    assert as_maya["steps"] == []
    with pytest.raises(NetworkError, match="unknown seat"):
        net.workdir_files("seat", f"{chat.id}/{pub.id}", actor=maya)
    assert net.workdir_files("seat", f"{chat.id}/{pub.id}", actor=ben)["files"] is not None
    progress = [e for e in net.bus.since(0) if e.type == "run.progress" and e.data.get("context_id") == f"{chat.id}/{pub.id}"]
    assert progress and not any(net.event_visible(e.type, e.data, maya) for e in progress)
    assert all(net.event_visible(e.type, e.data, ben) for e in progress)
    assert not any("你同时还在处理（工作台账）" in p for p in seen["prompt"][1:])

    # 维护人只看用量：谁、什么场合、几次，不给内容
    usage = net.public_usage(pub.id, actor=maya)
    assert usage["runs"] == 2 and {u["user"] for u in usage["users"]} == {"ann@x.io", "ben@x.io"}
    assert "报销单" not in json.dumps(usage, ensure_ascii=False)
    with pytest.raises(NetworkError, match="unknown agent"):
        net.public_usage(pub.id, actor=ann)
    rows = [json.loads(x) for x in (net.store.root / "usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["user"], r.get("public", False)) for r in rows] == [(maya, False), (ann, True), (ben, True)]


def test_service_face_uses_only_the_callers_own_connection(tmp_path: Path, fake: FakeMCP):
    """不变量 27（与不变量 19）：要用户鉴权的系统用调用方自己的授权，从不用维护人的；调用方没授权就没有连接器。"""
    def handler(call: ScriptCall) -> str:
        if "docs__search_docs" in call.connector_tools():
            return call.connector("docs__search_docs", q="报销进度")
        return "没有连接器"

    net, admin, maya, ann, ben, pub = setup(tmp_path, handler, catalog_file(tmp_path, fake.url),
                                            authority=("connector.use",))
    with pytest.raises(NetworkError, match="没有这些连接器"):
        net.request_public(pub.id, connectors=["nope"], actor=maya)
    net.request_public(pub.id, connectors=["docs"], actor=maya)
    theirs = net.authorize_connector("docs", {"token": SECRET}, maya)
    net.update_agent(pub.id, actor=maya, connections=[theirs["id"]])
    dm = net.direct_chat(pub.id, actor=ann)
    net.send_message(dm.id, "我的报销到哪了", actor=ann, background=False)
    assert dm.messages[-1].text == "没有连接器" and fake.calls == []  # 维护人的授权不借给调用方
    assert next(x for x in net.public_agents(ann) if x["id"] == pub.id)["connectors"] == \
        [{"id": "docs", "name": "文档库", "ready": False}]
    mine = net.authorize_connector("docs", {"token": SECRET}, ann)
    net.send_message(dm.id, "再查一次", actor=ann, background=False)
    assert dm.messages[-1].text == "找到：报销进度"
    assert len(net.connection_calls(mine["id"], ann)) == 1 and net.connection_calls(theirs["id"], maya) == []


def test_delegation_to_public_agent_is_for_the_caller(tmp_path: Path):
    """不变量 27（与不变量 1、13）：委托给公共 Agent 只在项目里；为委托为之而做的人，权限在冻结的之内，
    委托只给这个人看，问题也只问他，不给维护人。"""
    def handler(call: ScriptCall) -> str:
        if call.context_id.startswith("task-"):
            out = call.ask({"id": "who", "question": "报销给谁？", "options": [{"label": "我"}, {"label": "部门"}]})
            return f"办好了：{out.get('answers')}"
        if call.turn == 1 and call.can_delegate:
            call.tool("delegate", agent="报销助手", mission="提交报销", expected_outcome="单号",
                      authority=["web.research", "shell.exec"])
            return "已交给报销助手"
        return "收到"

    net, admin, maya, ann, ben, pub = setup(tmp_path, handler)
    helper = net.create_agent("Ann 的秘书", "秘书", [], ["delegate", "web.research", "shell.exec"], actor=ann)
    net.update_agent(pub.id, actor=maya, authority=["web.research", "file.write", "shell.exec"])  # 放宽了，没再批准
    g = net.create_project("报销", "", [helper.id, pub.id], actor=ann)
    chat = net.project_chat(g.id, actor=ann)
    th = answer_when_pending(net, ann, answers=[{"id": "who", "selected": ["我"]}])
    net.send_message(chat.id, "@Ann 的秘书 帮我报销", actor=ann, background=False)
    th.join(5)
    net.wait_idle(chat.id)
    t = next(iter(net.store.tasks.values()))
    assert t.caller_id == ann and t.owners == [ann]
    assert t.granted_authority == ["web.research"]  # shell.exec 放宽了还没批准，不给
    q = next(iter(net.store.questions.values()))
    assert q.target_user_id == ann and q.owner_id == ann
    assert t.status in (TaskStatus.IN_REVIEW, TaskStatus.COMPLETED)
    with pytest.raises(NetworkError, match="unknown task"):
        net.task_detail(t.id, maya)


def test_public_agent_api(tmp_path: Path):
    """不变量 27：HTTP 上的目录、申请、管理员批准、维护人设维护人与看用量。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    admin, maya, ann = users(net, "admin@x.io", "maya@x.io", "ann@x.io")
    net.create_agent("Maya 的助理", "助理", [], [], actor=maya)
    pub = net.create_agent("报销助手", "处理报销", [], [], actor=maya)
    cm, ca, cadmin = client(net, "maya@x.io"), client(net, "ann@x.io"), client(net, "admin@x.io")
    assert cm.post(f"/api/agents/{pub.id}/public", json={"note": "财务入口"}).json()["public"] is False
    assert ca.post(f"/api/admin/public-agents/{pub.id}/approve").status_code == 404
    assert ca.get("/api/admin/public-agents").status_code == 404
    listed = cadmin.post(f"/api/admin/public-agents/{pub.id}/approve").json()
    assert listed[0]["status"] == "approved" and listed[0]["note"] == "财务入口"
    assert [x["name"] for x in ca.get("/api/public-agents").json()] == ["报销助手"]
    r = cm.put(f"/api/agents/{pub.id}/maintainers", json={"emails": ["ann@x.io"]})
    assert r.status_code == 200 and r.json()["maintainer_emails"] == ["ann@x.io"]
    assert cm.get(f"/api/agents/{pub.id}/public/usage").json()["runs"] == 0
    assert cm.delete(f"/api/agents/{pub.id}/public").json()["public"] is False
    assert ca.get("/api/public-agents").json() == []


def test_ledger_lists_nothing_elsewhere_and_maintainers_see_only_usage(tmp_path: Path):
    """不变量 27：公共 Agent 的提示词里不列别处的工作（连数量都不给）；维护人看台账只看到谁、什么场合、从什么时候。"""
    from matrix.engine import ExecContext
    net, admin, maya, ann, ben, pub = setup(tmp_path, lambda call: "ok")
    dm = net.direct_chat(pub.id, actor=ann)
    other = ExecContext(kind="seat", id=f"{dm.id}/{pub.id}", agent_id=pub.id, conversation_id=dm.id, project_id=None,
                        authority=[], constraints=[], depth=0, chain=[pub.id], service=True, principal=ann,
                        callers=[ann])
    here = ExecContext(kind="seat", id="conv-x/" + pub.id, agent_id=pub.id, conversation_id="conv-x", project_id=None,
                       authority=[], constraints=[], depth=0, chain=[pub.id], service=True, principal=ben,
                       callers=[ben])
    net._contexts["tok-1"], net._contexts["tok-2"] = other, here
    assert net._ledger_for(here) == ([], 0)
    seen = net.ledger(pub.id)
    assert {x["user"] for x in seen} == {"ann@x.io", "ben@x.io"}
    assert all(x["conversation_id"] is None and x["context_id"] == "" and x["what"] == "服务" for x in seen)


def test_several_callers_in_one_round_get_no_connectors(tmp_path: Path, fake: FakeMCP):
    """不变量 27：一轮里有几个人 @ 它时说不清用谁的授权，一个连接器都不给；只有一个人时只给他自己的。"""
    from matrix.engine import ExecContext
    net, admin, maya, ann, ben, pub = setup(tmp_path, lambda call: "ok", catalog_file(tmp_path, fake.url),
                                            authority=("connector.use",))
    net.request_public(pub.id, connectors=["docs"], actor=maya)
    mine = net.authorize_connector("docs", {"token": SECRET}, ann)
    net.authorize_connector("docs", {"token": SECRET}, ben)
    ctx = lambda callers: ExecContext(kind="seat", id="c/" + pub.id, agent_id=pub.id, conversation_id="c",  # noqa: E731
                                      project_id=None, authority=["connector.use"], constraints=[], depth=0,
                                      chain=[pub.id], service=True, principal=callers[-1], callers=callers)
    with net._lock:
        assert net._connections_for(ctx([ann, ben]), pub) == []
        assert [c.id for c in net._connections_for(ctx([ann]), pub)] == [mine["id"]]
