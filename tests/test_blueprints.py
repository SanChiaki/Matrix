"""蓝图（docs/blueprints.md）：项目岗位与插件设置、蓝图格式、从项目发布、用蓝图建项目。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from matrix import blueprints as bps, plugins as pluginfs
from matrix.engine import NeedsConfirm, Network, NetworkError
from matrix.runtime import ScriptedRuntime
from tests.test_users import users

ROOT = Path(__file__).resolve().parents[1]
DEV_TEAM = ROOT / "examples" / "blueprints" / "dev-team"
CATALOG = ROOT / "examples" / "plugins.json"


def make_net(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda c: "好的"), tmp_path / "data", seed=False, plugins=pluginfs.PluginCatalog(CATALOG))
    ops, wang, li = users(net, "ops@x.io", "wang@x.io", "li@x.io")
    for u in (ops, wang, li):
        net.ensure_assistant(u)
    return net, ops, wang, li


def entries(src: Path, prefix: str = "bp") -> list[tuple[str, bytes]]:
    return [(f"{prefix}/{p.relative_to(src).as_posix()}", p.read_bytes()) for p in sorted(src.rglob("*")) if p.is_file()]


def use(net: Network, bid: str, actor: str, name: str = "白板研发", params: dict | None = None, roles: dict | None = None):
    params = {"product": "协同白板", "repo": "acme/wb"} if params is None else params
    with pytest.raises(NeedsConfirm) as e:
        net.create_project_from_blueprint(bid, name, params, roles, actor=actor)
    out = net.create_project_from_blueprint(bid, name, params, roles, confirm=True, digest=e.value.digest, actor=actor)
    return net.store.projects[out["project"]["id"]], e.value.items, out


# ================= 格式 =================
def test_example_blueprint_is_valid():
    bp = bps.check_package(bps.dir_files(DEV_TEAM))
    assert [r.key for r in bp.roles] == ["feedback", "design", "coder", "reviewer", "deployer"]
    assert bp.plugins[0].name == "teammates" and dict(bp.plugins[0].settings) == {"intake": "{role:feedback}"}
    assert {c.kind for c in bp.checkpoints} == {"accept", "approve", "human_step", "notify"}


@pytest.mark.parametrize("change, why", [
    (lambda m, f: f.__setitem__(Path("brief.md"), b"{param:nope}"), "param:nope"),
    (lambda m, f: m["connectors"][0].__setitem__("roles", ["ghost"]), "没有的岗位"),
    (lambda m, f: m["roles"][0].__setitem__("authority", ["root"]), "不认识"),
    (lambda m, f: m["checkpoints"][0].__setitem__("kind", "vote"), "kind"),
    (lambda m, f: m.__setitem__("files", ["files/没有.md"]), "不在包里"),
    (lambda m, f: m["plugins"].append({"name": "x", "source": "bundled"}), "打包的插件 x"),
    (lambda m, f: f.__setitem__(Path("secrets.env"), b"K=V"), "不认识的文件"),
])
def test_bad_blueprints_rejected(change, why):
    files = bps.dir_files(DEV_TEAM)
    m = json.loads(files[Path(bps.MANIFEST)])
    change(m, files)
    files[Path(bps.MANIFEST)] = json.dumps(m, ensure_ascii=False).encode()
    with pytest.raises(bps.BlueprintError, match=why):
        bps.check_package(files)


# ================= 岗位与插件设置 =================
def test_plugin_settings_route_triggers_by_role(tmp_path: Path):
    """插件触发写 @{setting:x}，设置选的是岗位：发消息时找这个岗位当时的成员；换人不用改插件。插件设置还在用的岗位不能删。"""
    net, _, wang, _ = make_net(tmp_path)
    a = net.create_agent("小甲", "分析需求", [], [], actor=wang)
    b = net.create_agent("小乙", "分析需求", [], [], actor=wang)
    g = net.create_project("P", "", [a.id, b.id], actor=wang)
    net.install_plugin(g.id, "teammates", actor=wang)
    with pytest.raises(NetworkError, match="没有岗位"):
        net.set_plugin_settings(g.id, "teammates", {"intake": "role:triage"}, actor=wang)
    net.set_project_roles(g.id, {"triage": {"name": "需求分析", "members": ["小甲"]}}, actor=wang)
    out = net.set_plugin_settings(g.id, "teammates", {"intake": "role:triage"}, actor=wang)
    assert out["setting_values"] == {"intake": "role:triage"}
    net.set_plugin_trigger(g.id, "teammates", "intake", True, actor=wang)
    token = net.reset_plugin_hook(g.id, "teammates", "intake", actor=wang)
    chat = net.project_chat(g.id)
    r = net.plugin_webhook(token, {"title": "导出 PDF", "url": "https://x/1"})
    msg = next(m for m in chat.messages if m.id == r["message"])
    assert msg.text.startswith("@小甲 外部来了一个新需求：导出 PDF") and msg.mentions == [a.id]
    net.set_project_roles(g.id, {"triage": {"name": "需求分析", "members": [b.id]}}, actor=wang)
    r = net.plugin_webhook(token, {"title": "二", "url": ""})
    assert next(m for m in chat.messages if m.id == r["message"]).mentions == [b.id]
    with pytest.raises(NetworkError, match="插件设置还在用岗位"):
        net.set_project_roles(g.id, {}, actor=wang)
    with pytest.raises(pluginfs.PluginError, match="settings 里 role 或 member"):
        pluginfs.parse_manifest({"name": "x", "version": "1.0.0", "description": "x", "tables": {"t": {"fields": [{"key": "a"}]}},
                                 "triggers": [{"key": "k", "type": "webhook", "notify": {"to": "{setting:who}", "text": "t"}}]})


# ================= 用蓝图建项目 =================
def test_create_project_from_blueprint(tmp_path: Path):
    """用蓝图建项目：先出总确认卡（岗位与权限、插件、连接器、外部回调、人在哪里介入），确认后一次建好：
    岗位各新建一个归建项目的人的 Agent，纲要、模板、技能里的占位符换成填的值与岗位上的人，插件带设置，
    连接器槽位进连接器面板，剩下的手工步骤列在项目上。再用一次时 Agent 名字不冲突。"""
    net, ops, wang, li = make_net(tmp_path)
    bp = net.admin_blueprint_upload(ops, entries(DEV_TEAM))
    with pytest.raises(NetworkError):
        net.admin_blueprint_upload(wang, entries(DEV_TEAM))  # 只有管理员能放进目录
    assert [b["title"] for b in net.blueprint_list(li)] == ["研发小队"]  # 目录里的所有人都看得到
    with pytest.raises(NetworkError, match="产品名"):
        net.create_project_from_blueprint(bp["id"], "x", {}, actor=wang)
    g, items, _ = use(net, bp["id"], wang, params={"product": "协同白板"})
    told = "\n".join(items)
    assert "新建 Agent「反馈」" in told and "file.write、shell.exec、connector.use" in told
    assert "装插件「队友协作（Raft 式）」" in told and "外部回调" in told and "人在这里介入 · 合并" in told
    agents = {k: net.store.agents[r["members"][0]] for k, r in g.roles.items()}
    assert {a.owner_id for a in agents.values()} == {wang} and agents["coder"].authority == ["file.write", "shell.exec", "connector.use"]
    assert "# 协同白板 研发" in g.brief and "@反馈" in g.brief and "〔代码仓库〕" in g.brief  # 没填的可选参数留标记
    assert "{role:" not in (net.store.project_skills_dir(g.id) / "dev-conventions" / "SKILL.md").read_text(encoding="utf-8")
    assert "协同白板 研发流程" in (net.store.project_dir(g.id) / "研发流程.md").read_text(encoding="utf-8")
    assert g.plugins["teammates"]["settings"] == {"intake": "role:feedback"}
    assert [n.connector_id for n in g.connector_needs] == ["github"]
    assert any("回调地址" in s["text"] for s in g.setup_steps) and g.blueprint["id"] == bp["id"]
    assert "由蓝图「研发小队」" in "\n".join(m.text for m in net.project_chat(g.id).messages)
    g2, _, _ = use(net, bp["id"], wang, "第二个")
    assert net.store.agents[g2.roles["coder"]["members"][0]].name == "编码·第二个"
    assert net.blueprints.get(bp["id"])["uses"] == 2


def test_confirm_warns_roles_without_write(tmp_path: Path):
    """插件的写类工具要 file.write：蓝图里没给的岗位用不了建任务、认领、交棒，确认卡上先说清楚（实测里踩过）。"""
    import shutil
    net, ops, wang, _ = make_net(tmp_path)
    src = tmp_path / "bp"
    shutil.copytree(DEV_TEAM, src)
    m = json.loads((src / "blueprint.json").read_text(encoding="utf-8"))
    m["roles"][0]["authority"] = ["web.research"]
    (src / "blueprint.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    bp = net.admin_blueprint_upload(ops, entries(src))
    with pytest.raises(NeedsConfirm) as e:
        net.create_project_from_blueprint(bp["id"], "x", {"product": "p"}, actor=wang)
    assert any("岗位「反馈」没有 file.write" in x for x in e.value.items)
    assert not any("岗位「编码」没有 file.write" in x for x in e.value.items)


def test_blueprint_roles_use_own_agents_only_and_roll_back(tmp_path: Path, monkeypatch):
    """岗位可以由自己已有的 Agent 担任，别人的不行；建到一半失败整体撤回：不留项目，也不留新建的 Agent。"""
    net, ops, wang, li = make_net(tmp_path)
    bp = net.admin_blueprint_upload(ops, entries(DEV_TEAM))
    mine = net.create_agent("老开发", "写代码", [], ["file.write"], actor=wang)
    theirs = net.create_agent("别人的", "x", [], [], actor=li)
    with pytest.raises(NetworkError, match="只能选你自己的 Agent 或项目里的成员"):
        net.create_project_from_blueprint(bp["id"], "x", {"product": "p"}, {"coder": {"agent": theirs.id}}, actor=wang)
    g, items, _ = use(net, bp["id"], wang, roles={"coder": {"agent": mine.id}})
    assert "岗位「编码」由「老开发」担任" in items and g.roles["coder"]["members"] == [mine.id]
    before_projects, before_agents = set(net.store.projects), set(net.store.agents)

    def boom(*a, **k):
        raise NetworkError("磁盘满了")
    monkeypatch.setattr(net, "publish_file", boom)
    with pytest.raises(NetworkError, match="已撤回：磁盘满了"):
        use(net, bp["id"], wang, "坏的")
    assert set(net.store.projects) == before_projects and set(net.store.agents) == before_agents


def test_pick_parts_into_existing_project(tmp_path: Path):
    """用蓝图时勾选要的部件（默认全选）；也可以装进自己主持的已有项目——只勾技能，就是以前的“订阅技能包”：
    技能复制进项目，占位符里没选的岗位换成岗位名；已有的同名岗位沿用，不新建 Agent。"""
    net, ops, wang, li = make_net(tmp_path)
    bp = net.admin_blueprint_upload(ops, entries(DEV_TEAM))
    a = net.create_agent("老测试", "测试", [], [], actor=wang)
    g = net.create_project("老项目", "", [a.id], actor=wang)
    net.set_brief(g.id, "原来的纲要", actor=wang)
    agents_before = set(net.store.agents)
    with pytest.raises(NetworkError, match="unknown project|只有主持人"):
        net.create_project_from_blueprint(bp["id"], "", {"product": "x"}, parts={"skills": ["dev-conventions"]},
                                          project_id=g.id, actor=li)
    with pytest.raises(NeedsConfirm) as e:
        net.create_project_from_blueprint(bp["id"], "", {"product": "白板"}, parts={"brief": False, "roles": [], "plugins": [],
                                          "files": [], "connectors": [], "skills": ["dev-conventions"]}, project_id=g.id, actor=wang)
    assert e.value.items[0] == "装进项目「老项目」：技能 dev-conventions"
    out = net.create_project_from_blueprint(bp["id"], "", {"product": "白板"}, confirm=True, actor=wang, project_id=g.id,
                                            parts={"brief": False, "roles": [], "plugins": [], "files": [], "connectors": [],
                                                   "skills": ["dev-conventions"]})
    assert out["project"]["id"] == g.id and set(net.store.agents) == agents_before and g.brief == "原来的纲要"
    skill = (net.store.project_skills_dir(g.id) / "dev-conventions" / "SKILL.md").read_text(encoding="utf-8")
    assert "问 @反馈" in skill and g.blueprint is None and not g.plugins
    net.set_project_roles(g.id, {"reviewer": {"name": "审查", "members": [a.id]}}, actor=wang)
    with pytest.raises(NeedsConfirm) as e:
        net.create_project_from_blueprint(bp["id"], "", {"product": "白板"}, parts={"roles": ["reviewer"], "brief": True},
                                          project_id=g.id, actor=wang)
    told = "\n".join(e.value.items)
    assert "岗位「审查」沿用项目里已有的（老测试）" in told and "纲要：会换成蓝图里的纲要" in told and "覆盖项目里的同名技能" in told


# ================= 从项目发布 =================
def test_publish_from_project_and_reuse(tmp_path: Path):
    """主持人把跑通的项目发布成蓝图：@成员 换成岗位占位，参数的值换成参数占位，插件设置里的岗位照带；
    像是写死了人、带了项目事实的地方先要确认。读者之外的人看不到；用的人建出来的项目里换成了他的值。"""
    net, ops, wang, li = make_net(tmp_path)
    a = net.create_agent("前端小王", "页面", ["前端"], ["file.write"], actor=wang)
    g = net.create_project("官网", "官网迭代", [a.id], actor=wang)
    net.set_brief(g.id, "官网迭代：@前端小王 负责页面，2026-11-01 上线。", actor=wang)
    net.set_project_roles(g.id, {"fe": {"name": "前端", "members": [a.id]}}, actor=wang)
    net.install_plugin(g.id, "teammates", actor=wang)
    net.set_plugin_settings(g.id, "teammates", {"intake": "role:fe"}, actor=wang)
    draft = net.blueprint_draft(g.id, actor=wang)
    assert draft["roles"] == [{"key": "fe", "name": "前端", "description": "", "member": a.id}]
    spec = {**draft, "name": "site", "plugins": ["teammates"], "params": [{"key": "site", "label": "站点", "value": "官网"}],
            "readers": ["li@x.io"]}
    with pytest.raises(NetworkError):
        net.publish_blueprint(g.id, spec, actor=li)  # 不是主持人
    with pytest.raises(NeedsConfirm) as e:
        net.publish_blueprint(g.id, spec, actor=wang)
    assert any("日期「2026-11-01」" in x for x in e.value.items)
    b = net.publish_blueprint(g.id, spec, confirm=True, digest=e.value.digest, actor=wang)
    assert "{param:site}迭代：@{role:fe} 负责页面" in b["brief"]
    assert b["manifest"]["plugins"] == [{"name": "teammates", "version": "0.2.0", "source": "catalog", "settings": {"intake": "{role:fe}"}}]
    outsider, = users(net, "zhao@x.io")
    assert [x["id"] for x in net.blueprint_list(li)] == [b["id"]] and net.blueprint_list(outsider) == []
    g2, _, _ = use(net, b["id"], li, "商城", params={"site": "商城"})
    assert g2.brief.startswith("商城迭代：@前端 负责页面") and g2.plugins["teammates"]["settings"] == {"intake": "role:fe"}
    again = net.publish_blueprint(g.id, {**spec, "blueprint_id": b["id"]}, confirm=True, actor=wang)
    assert again["rev"] == 2 and again["id"] == b["id"]
    net.withdraw_blueprint(b["id"], actor=wang)
    assert net.blueprint_list(li) == []
    with pytest.raises(NetworkError):
        net.create_project_from_blueprint(b["id"], "x", {"site": "x"}, actor=li)


def test_uploaded_plugins_are_bundled(tmp_path: Path):
    """项目里上传的插件不在部署方目录：发布时打包进蓝图，并提示；用蓝图建项目时按“主持人自己装”装上。"""
    net, _, wang, li = make_net(tmp_path)
    g = net.create_project("P", "", [], actor=wang)
    manifest = {"name": "notes", "version": "1.0.0", "title": "便签", "description": "记一点东西",
                "tables": {"notes": {"title": "便签", "fields": [{"key": "text", "label": "内容"}]}}}
    net.upload_plugin(g.id, [("notes/plugin.json", json.dumps(manifest).encode())], actor=wang)
    spec = {**net.blueprint_draft(g.id, actor=wang), "name": "notes-kit", "plugins": ["notes"], "public": True}
    with pytest.raises(NeedsConfirm) as e:
        net.publish_blueprint(g.id, spec, actor=wang)
    assert any("会打包进蓝图" in x for x in e.value.items)
    b = net.publish_blueprint(g.id, spec, confirm=True, actor=wang)
    assert b["manifest"]["plugins"][0]["source"] == "bundled"
    g2, _, _ = use(net, b["id"], li, "我的", params={})
    assert g2.plugins["notes"]["source"] == "upload"


def test_blueprint_http_flow(tmp_path: Path):
    """接口：建项目先 409 带确认条目与指纹，带回指纹才建。"""
    from fastapi.testclient import TestClient
    from matrix.server import create_app
    net, ops, wang, _ = make_net(tmp_path)
    bp = net.admin_blueprint_upload(ops, entries(DEV_TEAM))
    c = TestClient(create_app(net))
    assert c.post("/api/auth/login", json={"email": "wang@x.io", "password": "password-123"}).status_code == 200
    assert [x["id"] for x in c.get("/api/blueprints").json()] == [bp["id"]]
    assert c.get(f"/api/blueprints/{bp['id']}").json()["manifest"]["name"] == "dev-team"
    body = {"name": "白板", "params": {"product": "白板"}}
    r = c.post(f"/api/blueprints/{bp['id']}/projects", json=body)
    assert r.status_code == 409 and r.json()["confirm"]
    r = c.post(f"/api/blueprints/{bp['id']}/projects", json={**body, "confirm": True, "digest": r.json()["digest"]})
    assert r.status_code == 200 and r.json()["project"]["name"] == "白板"
    pid = r.json()["project"]["id"]
    step = net.store.projects[pid].setup_steps[0]["id"]
    assert c.put(f"/api/projects/{pid}/setup-steps/{step}", json={"done": True}).status_code == 200
    assert net.store.projects[pid].setup_steps[0]["done"]
