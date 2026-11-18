"""外部应用接入（docs/open-api.md，不变量 30）：管理员登记应用；两种身份（代表个人 / 业务系统账号）；
查（公共、自己的、开放给它的 Agent）、组（建项目、成员、资料、技能、模板）、发（服务）、调（标准 HTTP 形状）；
令牌拿不到内容；每次写操作留下“经哪个应用”；撤销 / 停用立即生效；系统账号不能登录、负责人等同主持人。"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from matrix import auth, remote_agents as remote
from matrix.engine import NeedsConfirm, Network, NetworkError
from matrix.runtime import ScriptCall, ScriptedRuntime
from matrix.server import create_app
from tests.test_blueprints import DEV_TEAM, entries
from tests.test_users import client, users


def setup(tmp_path: Path, handler=None, plugins=None):
    """管理员 ops；maya 是「报销系统」的负责人；ann 有个人 Agent「合同审核」；pub 是 ops 的公共 Agent「财务助手」。"""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    net = Network(ScriptedRuntime(handler or (lambda call: "好的")), data, seed=False, plugins=plugins)
    ops, maya, ann, ben = users(net, "ops@x.io", "maya@x.io", "ann@x.io", "ben@x.io")
    for u, n in ((ops, "Ops 的助理"), (maya, "Maya 的助理"), (ann, "Ann 的助理"), (ben, "Ben 的助理")):
        net.create_agent(n, "助理", [], [], actor=u)
    pub = net.create_agent("财务助手", "查报销", [], ["web.research"], actor=ops)
    net.request_public(pub.id, "财务入口", [], actor=ops)
    net.review_public(pub.id, True, actor=ops)
    contract = net.create_agent("合同审核", "看合同", ["合同"], ["web.research"], actor=ann)
    app = net.create_app(ops, "报销系统", "公司报销", system=True, managers=["maya@x.io"])
    return net, (ops, maya, ann, ben), pub, contract, app


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ================= 第 1 期：应用、身份、查 =================
def test_admin_registers_apps_and_system_accounts_cannot_log_in(tmp_path: Path):
    """不变量 30：只有管理员登记应用、签系统账号的令牌；系统账号至少一名负责人，不能登录、不能重置密码、不能被邀请、不是管理员。"""
    net, (ops, maya, ann, ben), pub, contract, app = setup(tmp_path)
    with pytest.raises(NetworkError, match="unknown admin"):
        net.create_app(maya, "偷偷的应用")
    with pytest.raises(NetworkError, match="至少要有一名负责人"):
        net.create_app(ops, "无人管", system=True, managers=[])
    sysu = net.store.users[app.system_user_id]
    assert sysu.is_system and sysu.managers == [maya] and not net.is_admin(sysu.id)
    with pytest.raises(auth.AuthError):
        auth.login(net.store, sysu.email, "")
    with pytest.raises(auth.AuthError, match="不能登录"):
        auth.issue_reset(net.store, sysu.id, by=ops)
    g = net.create_project("Ann 的项目", "", [], actor=ann)
    with pytest.raises(NetworkError, match="没有用户"):
        net.invite(g.id, sysu.email, actor=ann)
    with pytest.raises(NetworkError, match="unknown admin"):
        net.issue_system_token(maya, app.id)
    grant, token = net.issue_system_token(ops, app.id, ["agents.read"])
    assert token.startswith("mat_") and token not in json.dumps(net.store.app_grants[grant.id].to_dict())
    assert net.store.app_grants[grant.id].token_sha != token
    _, token2 = net.issue_system_token(ops, app.id, ["agents.read"])  # 重签：旧的作废
    c = TestClient(create_app(net))
    assert c.get("/open/v1/agents", headers=bearer(token)).status_code == 401
    assert c.get("/open/v1/agents", headers=bearer(token2)).status_code == 200


def test_discovery_lists_public_own_and_opened_agents_only(tmp_path: Path):
    """不变量 30：查到的是主体本来就能用的（公共、自己的）加上所有者开放给这个应用的；别人的个人 Agent 看不到；只给名片。"""
    net, (ops, maya, ann, ben), pub, contract, app = setup(tmp_path)
    _, token = net.issue_system_token(ops, app.id, ["agents.read"])
    c = TestClient(create_app(net))
    names = lambda: {a["name"]: a["kind"] for a in c.get("/open/v1/agents", headers=bearer(token)).json()}  # noqa: E731
    assert names() == {"财务助手": "public"}
    with pytest.raises(NetworkError, match="unknown agent"):
        net.set_agent_open(contract.id, [app.id], actor=maya)          # 别人的 Agent：当它不存在
    net.set_agent_open(contract.id, [app.id], actor=ann)
    assert names() == {"财务助手": "public", "合同审核": "opened"}
    card = next(a for a in c.get("/open/v1/agents", headers=bearer(token)).json() if a["name"] == "合同审核")
    assert card["owner"] == "ann@x.io" and "model" not in card and "maintainers" not in card
    assert c.get("/open/v1/projects", headers=bearer(token)).status_code == 403   # 没有这个 scope
    assert c.get("/open/v1/agents").status_code == 401

    # 代表个人：ann 授权，查到的是她自己的与公共的；范围不能超过应用的上限
    net.update_app(ops, app.id, scopes=["agents.read", "projects.write", "services.call"])
    with pytest.raises(NetworkError, match="不能申请"):
        net.grant_app(ann, app.id, ["services.publish"])
    grant, mine = net.grant_app(ann, app.id, ["agents.read"])
    assert set(a["name"] for a in c.get("/open/v1/agents", headers=bearer(mine)).json()) == {"Ann 的助理", "合同审核", "财务助手"}
    net.revoke_grant(ann, grant.id)                                                # 撤销立即生效
    assert c.get("/open/v1/agents", headers=bearer(mine)).status_code == 401
    net.update_app(ops, app.id, status="disabled")                                # 停用：系统账号的令牌也立即失效
    assert c.get("/open/v1/agents", headers=bearer(token)).status_code == 401
    with pytest.raises(NetworkError, match="unknown grant"):
        net.revoke_grant(ben, grant.id)


# ================= 第 2 期：组与发 =================
def test_system_account_builds_a_project_and_publishes_a_service(tmp_path: Path):
    """不变量 30：系统账号建项目当主持人；负责人成为参与者、等同主持人；开放的个人 Agent 拉进来时所有者成为参与者；
    写操作经应用留痕；令牌只拿到配置，拿不到群聊；能发布服务、看用量。"""
    net, (ops, maya, ann, ben), pub, contract, app = setup(tmp_path)
    net.set_agent_open(contract.id, [app.id], actor=ann)
    _, token = net.issue_system_token(ops, app.id)
    c = TestClient(create_app(net))
    h = bearer(token)
    r = c.post("/open/v1/projects", headers=h, json={"name": "报销流程", "description": "线上报销",
                                                      "brief": "报销 5000 以下直接批", "members": [pub.id, contract.id]})
    assert r.status_code == 200, r.text
    p = r.json()
    g = net.store.projects[p["id"]]
    sysu = net.store.users[app.system_user_id]
    assert g.owner_id == sysu.id and g.via_app == app.id and set(g.member_ids) >= {pub.id, contract.id}
    assert maya in g.participants and ann in g.participants and ben not in g.people
    assert "messages" not in json.dumps(p) and p["brief"] == "报销 5000 以下直接批"
    chat = net.project_chat(g.id, actor=maya)
    assert any("（经「报销系统」）" in m.text for m in chat.messages)
    assert any("合同审核」开放给了「报销系统」" in m.text for m in chat.messages)
    log = [json.loads(x) for x in (net.store.project_dir(g.id).parent / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log and all(e.get("via_app") == app.id for e in log)
    net.set_brief(g.id, "负责人改的纲要", actor=maya)                                # 负责人在网页上等同主持人
    with pytest.raises(NetworkError, match="只有主持人"):
        net.set_brief(g.id, "参与者不能改", actor=ann)
    view = net.project_view(g, maya)
    assert view["host_email"].startswith("报销系统（负责人 maya@x.io）")

    assert c.put(f"/open/v1/projects/{g.id}/files/公开/手册.md", headers=h, content="手册".encode()).status_code == 200
    assert c.post(f"/open/v1/projects/{g.id}/skills", headers=h,
                  json={"name": "expense-rules", "description": "报销规则", "body": "先看金额"}).status_code == 200
    r = c.post(f"/open/v1/projects/{g.id}/services", headers=h, json={
        "name": "baoxiao", "title": "报销服务", "entry": pub.id, "scope": ["公开"], "public": True})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert c.get(f"/open/v1/projects/{g.id}/services/{sid}/usage", headers=h).json()["runs"] == 0
    assert c.get(f"/api/conversations/{chat.id}", headers=h).status_code == 401  # 令牌拿不到内容
    assert c.get(f"/open/v1/projects/{g.id}", headers=h).json()["services"][0]["title"] == "报销服务"

    # 负责人换人：新负责人进项目，卸任的退出
    net.update_app(ops, app.id, managers=["ben@x.io"])
    assert ben in g.participants and maya not in g.participants


def test_delegated_token_manages_only_its_own_projects(tmp_path: Path):
    """不变量 30：代表个人的令牌只管经这个应用建的、和授权人交给它管的项目；做的事不超过授权人本来能做的。"""
    net, (ops, maya, ann, ben), pub, contract, app = setup(tmp_path)
    other = net.create_project("Ann 的旧项目", "", [], actor=ann)
    _, token = net.grant_app(ann, app.id, ["projects.write"])
    c = TestClient(create_app(net))
    h = bearer(token)
    r = c.post("/open/v1/projects", headers=h, json={"name": "新项目", "members": [contract.id]})
    assert r.status_code == 200 and net.store.projects[r.json()["id"]].owner_id == ann
    assert c.get(f"/open/v1/projects/{other.id}", headers=h).status_code == 404
    assert [x["name"] for x in c.get("/open/v1/projects", headers=h).json()] == ["新项目"]
    benny = net.create_agent("Ben 的私人", "", [], [], actor=ben)
    assert c.put(f"/open/v1/projects/{r.json()['id']}/members/{benny.id}", headers=h).status_code == 404
    n = len(net.store.projects)
    assert c.post("/open/v1/projects", headers=h, json={"name": "拉不进", "members": [benny.id]}).status_code == 404
    assert len(net.store.projects) == n                                    # 拉不进来的不建项目
    _, token2 = net.grant_app(ann, app.id, ["projects.write"], projects=[other.id])  # 交给它管的已有项目
    assert c.get(f"/open/v1/projects/{other.id}", headers=bearer(token2)).status_code == 200
    with pytest.raises(NetworkError):
        net.grant_app(ann, app.id, ["projects.write"], projects=[net.create_project("Ben 的", "", [], actor=ben).id])


def test_blueprints_only_with_existing_agents(tmp_path: Path):
    """不变量 30（决策 5）：应用用模板时，模板里的每个角色都要指给项目里的成员，不新建 Agent；先确认再装。"""
    from matrix import plugins as pluginfs
    from tests.test_blueprints import CATALOG
    net, (ops, maya, ann, ben), pub, contract, app = setup(tmp_path, plugins=pluginfs.PluginCatalog(CATALOG))
    bp = net.admin_blueprint_upload(ops, entries(DEV_TEAM))
    net.set_agent_open(contract.id, [app.id], actor=ann)
    _, token = net.issue_system_token(ops, app.id)
    c = TestClient(create_app(net))
    h = bearer(token)
    assert [b["id"] for b in c.get("/open/v1/blueprints", headers=h).json()] == [bp["id"]]
    r = c.post("/open/v1/projects", headers=h, json={"name": "研发", "members": [pub.id], "blueprint": bp["id"],
                                                      "params": {"product": "白板"}})
    assert r.status_code == 409 and "不能新建 Agent" in r.json()["detail"]
    roles = {k: pub.id for k in ("feedback", "design", "coder", "reviewer", "deployer")}
    roles["reviewer"] = contract.id
    before = len(net.store.agents)
    body = {"name": "研发", "members": [pub.id, contract.id], "blueprint": bp["id"], "params": {"product": "白板"},
            "roles": roles}
    r = c.post("/open/v1/projects", headers=h, json=body)
    if r.status_code == 409 and "digest" in r.json():   # 先看确认卡，带 digest 再来
        assert not [g for g in net.store.projects.values() if g.name == "研发"]
        r = c.post("/open/v1/projects", headers=h, json={**body, "digest": r.json()["digest"]})
    assert r.status_code == 200, r.text
    assert len(net.store.agents) == before                     # 没新建 Agent
    g = net.store.projects[r.json()["id"]]
    assert g.roles["reviewer"]["members"] == [contract.id] and g.plugins


# ================= 第 3 期：调 =================
def sse(text: str) -> list[tuple[str, dict]]:
    out, event = [], "message"
    for block in text.split("\n\n"):
        ev, data = "message", None
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if data is not None:
            out.append((ev, data))
    return out


def test_system_account_calls_a_service_in_the_standard_shape(tmp_path: Path):
    """不变量 30（与 29）：系统账号凭令牌调用服务，调用方就是它自己；会话续接；中断；它看得到的只有自己的会话。"""
    def handler(call: ScriptCall) -> str:
        call.say("在查")
        return f"第 {call.turn} 轮：收到"

    net, (ops, maya, ann, ben), pub, contract, app = setup(tmp_path, handler)
    g = net.create_project("报销", "", [pub.id], actor=maya)
    sysu = net.store.users[app.system_user_id]
    x = net.create_service(g.id, "baoxiao", "报销服务", "报销咨询", actor=maya, entry=pub.id, readers=[sysu.email])
    _, token = net.issue_system_token(ops, app.id, ["services.call"])
    c = TestClient(create_app(net))
    h = bearer(token)
    assert c.get(f"/svc/agents/{x.id}", headers=h).json() == {"name": "报销服务", "description": "报销咨询"}
    r = c.post(f"/svc/agents/{x.id}/chat", headers=h, json={"message": "我的报销"})
    evs = sse(r.text)
    assert evs[0][0] == "session" and evs[-1] == ("done", {"text": "第 1 轮：收到"})
    assert ("delta", {"text": "在查"}) in evs
    sid = evs[0][1]["session_id"]
    conv = net.store.conversations[sid]
    assert conv.kind == "service" and conv.owner_id == sysu.id
    evs = sse(c.post(f"/svc/agents/{x.id}/chat", headers=h, json={"message": "再问", "session_id": sid}).text)
    assert evs[0][1]["session_id"] == sid and evs[-1] == ("done", {"text": "第 2 轮：收到"})  # 续接同一个会话
    assert c.post("/svc/sessions/{sid}/interrupt".format(sid=sid), headers=h).json() == {"ok": True}
    rows = [json.loads(r) for r in (net.store.root / "usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {r["user"] for r in rows if r.get("service") == x.id} == {sysu.id}       # 调用方是系统账号，记在项目下
    # 别人的会话、看不到的服务：不存在
    other = net.open_service_session(x.id, actor=maya)
    assert c.post(f"/svc/agents/{x.id}/chat", headers=h, json={"message": "x", "session_id": other.id}).status_code == 404
    net.update_service(g.id, x.id, actor=maya, readers=[])
    assert c.get(f"/svc/agents/{x.id}", headers=h).status_code == 404
    _, nocall = net.issue_system_token(ops, app.id, ["agents.read"])
    assert c.get(f"/svc/agents/{x.id}", headers=bearer(nocall)).status_code == 403


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_another_matrix_connects_with_the_standard_driver(tmp_path: Path):
    """第 3 期：/svc 和外部平台的标准 HTTP 形状一字不差，另一个 Matrix 用内置 standard 驱动、url 填 …/svc 就能接。"""
    import uvicorn
    net, (ops, maya, ann, ben), pub, contract, app = setup(tmp_path, lambda call: "服务的回复")
    g = net.create_project("报销", "", [pub.id], actor=maya)
    sysu = net.store.users[app.system_user_id]
    x = net.create_service(g.id, "baoxiao", "报销服务", "报销咨询", actor=maya, entry=pub.id, readers=[sysu.email])
    _, token = net.issue_system_token(ops, app.id, ["services.call"])
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(net), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        spec = remote.PlatformSpec(id="m2", name="另一个 Matrix", url=f"http://127.0.0.1:{port}/svc", timeout_s=30)
        drv = remote.StandardDriver()
        hdr = {"Authorization": f"Bearer {token}"}
        assert drv.describe(spec, x.id, hdr) == {"name": "报销服务", "description": "报销咨询"}
        evs = list(drv.chat(spec, x.id, "你好", None, hdr))
        assert evs[0]["type"] == "session" and evs[-1] == {"type": "done", "text": "服务的回复"}
        drv.interrupt(spec, evs[0]["id"], hdr)
    finally:
        server.should_exit = True
        t.join(timeout=5)
