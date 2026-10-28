"""发布与订阅（docs/project-publishing.md，不变量 23）：只有主持人发布、按版本固定；方法可以公开，事实只给读者；
订阅只在订阅者自己的 Agent 上、能到达的人都看得到时生效；跟随档案；撤回；发布物里的插件视图只读快照；反馈回流。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from matrix.engine import Network, NetworkError
from matrix.models import RELEASES, SKILLS
from matrix.runtime import ScriptCall, ScriptedRuntime
from matrix.store import Store
from tests.test_plugins import manifest, package
from tests.test_users import client, joint, users


def setup(tmp_path: Path, handler=None):
    """host 主持交付项目 P（成员是她的 Dash），档案里有一份看板文件和一个项目技能；reader 是区域负责人。"""
    rt = ScriptedRuntime(handler or (lambda call: "ok"))
    net = Network(rt, tmp_path, seed=False)
    host, reader, other = users(net, "host@x.io", "reader@x.io", "other@x.io")
    for u in (reader, other):  # 注册即有助理
        net.ensure_assistant(u)
    dash = net.create_agent("Dash", "做看板", [], ["file.write"], actor=host)
    g = net.create_project("交付", "华东交付", [dash.id], actor=host)
    net.publish_file(g.id, "weekly.json", b'{"on_time": 0.82}', actor=host)
    net.save_project_skill(g.id, "delivery-query", "怎么查交付数据", "先查项目清单，再按项目取指标。", actor=host)
    return net, rt, host, reader, other, dash, g


def assistant(net: Network, user: str):
    return net.store.agents[net.store.users[user].assistant_id]


# ================= 发布：只经主持人 =================
def test_only_host_publishes_and_contents_are_checked(tmp_path: Path):
    """不变量 23：只有主持人能发布；方法只带技能、事实只带档案文件；名字、读者都要对得上。"""
    net, _, host, reader, other, _, g = setup(tmp_path)
    net.invite(g.id, "other@x.io", actor=host)
    net.respond_invitation(g.id, True, actor=other)
    with pytest.raises(NetworkError, match="只有主持人"):
        net.create_release(g.id, "weekly", "fact", "周报", files=["weekly.json"], actor=other)
    with pytest.raises(NetworkError, match="只带技能"):
        net.create_release(g.id, "x", "method", "x", skills=["delivery-query"], files=["weekly.json"], actor=host)
    with pytest.raises(NetworkError, match="没有这些文件"):
        net.create_release(g.id, "x", "fact", "x", files=["nope.json"], actor=host)
    with pytest.raises(NetworkError, match="没有这些技能"):
        net.create_release(g.id, "x", "method", "x", skills=["nope"], actor=host)
    with pytest.raises(NetworkError, match="没有用户"):
        net.create_release(g.id, "x", "fact", "x", files=["weekly.json"], readers=["ghost@x.io"], actor=host)
    with pytest.raises(NetworkError, match="名字"):
        net.create_release(g.id, "Bad Name", "fact", "x", files=["weekly.json"], actor=host)
    r = net.create_release(g.id, "weekly", "fact", "交付周报", files=["weekly.json"], readers=["reader@x.io"],
                           public=True, actor=host)
    assert (r.version, r.public, r.readers) == (1, False, [reader])  # 事实不能公开
    chat = net.project_chat(g.id, actor=host)
    assert "主持人发布了事实「交付周报」v1（读者：reader@x.io）" in chat.messages[-1].text
    with pytest.raises(NetworkError, match="已经有"):
        net.create_release(g.id, "weekly", "fact", "again", files=["weekly.json"], actor=host)


def test_fact_only_for_readers_method_can_be_public(tmp_path: Path):
    """不变量 23：事实只给点名的读者（加项目参与者），看不到的一律 404；公开的方法所有登录用户都看得到。"""
    net, _, host, reader, other, _, g = setup(tmp_path)
    fact = net.create_release(g.id, "weekly", "fact", "周报", files=["weekly.json"], readers=["reader@x.io"], actor=host)
    method = net.create_release(g.id, "query", "method", "查数口径", skills=["delivery-query"], public=True, actor=host)
    assert {x["id"] for x in net.releases_for(reader)} == {fact.id, method.id}
    assert {x["id"] for x in net.releases_for(other)} == {method.id}
    with pytest.raises(NetworkError, match="unknown release"):
        net.release_detail(fact.id, actor=other)
    with pytest.raises(NetworkError, match="unknown"):
        net.release_file(fact.id, "weekly.json", actor=other)
    assert net.release_file(fact.id, "weekly.json", actor=reader).read_text(encoding="utf-8") == '{"on_time": 0.82}'
    d = net.release_detail(fact.id, actor=reader)
    assert "readers" not in d and "subscribers" not in d  # 名单与订阅数只给项目参与者
    assert net.release_detail(fact.id, actor=host)["readers"] == [{"id": reader, "email": "reader@x.io"}]
    with pytest.raises(NetworkError, match="unknown project"):  # 读者看不到项目本身
        net.project_detail(g.id, actor=reader)
    assert net.release_detail(method.id, actor=other)["skills"][0]["name"] == "delivery-query"


def test_snapshot_is_fixed_until_next_version_or_follow(tmp_path: Path):
    """不变量 23：发布物按版本固定；没开跟随的，档案再改也不变，主持人发布新版本才变；开了跟随的自动切版本。"""
    net, _, host, reader, _, _, g = setup(tmp_path)
    fixed = net.create_release(g.id, "fixed", "fact", "固定", files=["weekly.json"], readers=["reader@x.io"], actor=host)
    live = net.create_release(g.id, "live", "fact", "跟随", files=["weekly.json"], readers=["reader@x.io"],
                              follow=True, actor=host)
    net.publish_file(g.id, "weekly.json", b'{"on_time": 0.9}', agent_id=g.member_ids[0])
    assert net.release_file(fixed.id, "weekly.json", actor=reader).read_text(encoding="utf-8") == '{"on_time": 0.82}'
    assert net.release_file(live.id, "weekly.json", actor=reader).read_text(encoding="utf-8") == '{"on_time": 0.9}'
    assert (fixed.version, live.version) == (1, 2)
    assert any("「跟随」跟随档案更新到 v2（weekly.json）" in m.text for m in net.project_chat(g.id, actor=host).messages)
    net.publish_release_version(g.id, fixed.id, actor=host)
    assert fixed.version == 2
    assert net.release_file(fixed.id, "weekly.json", actor=reader).read_text(encoding="utf-8") == '{"on_time": 0.9}'
    for _ in range(4):  # 快照只保留最近 3 个版本
        net.publish_release_version(g.id, fixed.id, actor=host)
    assert sorted(p.name for p in net.store.release_dir(fixed.id).iterdir()) == ["v4", "v5", "v6"]


def test_withdraw_hides_from_readers_and_subscribers(tmp_path: Path):
    """不变量 23：撤回后读者看不到、订阅不再生效，项目参与者还看得到记录。"""
    seen: list[list[str]] = []

    def handler(call: ScriptCall) -> str:
        d = call.cwd / RELEASES
        seen.append(sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()) if d.is_dir() else [])
        return "ok"

    net, _, host, reader, _, _, g = setup(tmp_path, handler)
    r = net.create_release(g.id, "weekly", "fact", "周报", files=["weekly.json"], readers=["reader@x.io"], actor=host)
    me = assistant(net, reader)
    net.set_subscription(r.id, me.id, True, actor=reader)
    dm = net.direct_chat(me.id, actor=reader)
    net.send_message(dm.id, "这周准时率怎么样？", actor=reader, background=False)
    assert seen[-1] == ["weekly/weekly.json"]
    net.withdraw_release(g.id, r.id, actor=host)
    with pytest.raises(NetworkError, match="unknown"):
        net.release_detail(r.id, actor=reader)
    assert net.release_detail(r.id, actor=host)["status"] == "withdrawn"
    net.send_message(dm.id, "再看一下", actor=reader, background=False)
    assert seen[-1] == []
    with pytest.raises(NetworkError, match="unknown release"):  # 撤回的不能再订阅
        net.set_subscription(r.id, me.id, True, actor=reader)


# ================= 订阅：在订阅者自己的信任域里 =================
def test_subscriptions_follow_reach(tmp_path: Path):
    """不变量 23：订阅的事实只在这一轮能到达的人都是读者时挂上：私聊里生效；在有不是读者的人的共享项目里不挂，
    提示词只说明没挂上；订阅的方法进技能层。"""
    rounds: list[dict] = []

    def handler(call: ScriptCall) -> str:
        d = call.cwd / RELEASES
        rounds.append({"files": sorted(p.name for p in d.rglob("*") if p.is_file()) if d.is_dir() else [],
                       "skills": call.skills, "prompt": call.prompt})
        return "ok"

    net, _, host, reader, other, _, g = setup(tmp_path, handler)
    fact = net.create_release(g.id, "weekly", "fact", "周报", files=["weekly.json"], readers=["reader@x.io"], actor=host)
    method = net.create_release(g.id, "query", "method", "查数口径", skills=["delivery-query"], public=True, actor=host)
    me = assistant(net, reader)
    net.update_agent(me.id, actor=reader, subscriptions=[fact.id, method.id])
    with pytest.raises(NetworkError, match="unknown release"):  # 只能订阅自己看得到的
        net.update_agent(assistant(net, other).id, actor=other, subscriptions=[fact.id])
    with pytest.raises(NetworkError, match="unknown agent"):  # 只能给自己的 Agent 订阅
        net.set_subscription(method.id, me.id, True, actor=other)

    net.send_message(net.direct_chat(me.id, actor=reader).id, "看看", actor=reader, background=False)
    assert rounds[-1]["files"] == ["weekly.json"] and "delivery-query" in rounds[-1]["skills"]
    assert "事实「周报」v1，快照于" in rounds[-1]["prompt"] and f"./{RELEASES}/weekly/" in rounds[-1]["prompt"]

    j, chat = joint(net, other, "reader@x.io", reader, members=[])
    net.send_message(chat.id, f"@{me.name} 说说周报", actor=other, background=False)
    assert rounds[-1]["files"] == []  # other 不是读者：事实不挂
    assert "delivery-query" in rounds[-1]["skills"]  # 公开的方法照常
    assert "事实「周报」v1：这一轮没有挂上（这一轮能看到你输出的人里，有不在它读者名单上的）" in rounds[-1]["prompt"]


def test_device_materials_include_reachable_facts(tmp_path: Path):
    """不变量 23：端侧随材料下发的订阅事实同样按能到达的人判断。"""
    net, _, host, reader, _, _, g = setup(tmp_path)
    r = net.create_release(g.id, "weekly", "fact", "周报", files=["weekly.json"], readers=["reader@x.io"], actor=host)
    me = assistant(net, reader)
    net.set_subscription(r.id, me.id, True, actor=reader)
    dm = net.direct_chat(me.id, actor=reader)
    from matrix.engine import ExecContext
    ctx = ExecContext(kind="seat", id=f"{dm.id}/{me.id}", agent_id=me.id, conversation_id=dm.id, project_id=None,
                      authority=[], constraints=[], depth=0, chain=[me.id])
    with net._lock:
        paths = [m["path"] for m in net._materials(ctx)]
    assert f"{RELEASES}/weekly/weekly.json" in paths


# ================= 插件视图：沙箱 + 只读快照 =================
def test_release_view_reads_only_the_snapshot(tmp_path: Path):
    """不变量 23：发布物里的插件页面用发布时快照的页面代码，令牌绑定读者 + 发布物 + 版本；桥只能读快照里的文件。"""
    net, _, host, reader, other, _, g = setup(tmp_path)
    m = manifest(release_views=["board"])
    net.upload_plugin(g.id, package(m), actor=host)
    with pytest.raises(NetworkError, match="release_views"):
        net.create_release(g.id, "x", "fact", "x", files=["weekly.json"], view={"plugin": "deals", "tab": "nope"},
                           actor=host)
    r = net.create_release(g.id, "board", "fact", "看板", files=["weekly.json"], view={"plugin": "deals", "tab": "board"},
                           readers=["reader@x.io"], actor=host)
    with pytest.raises(NetworkError, match="unknown release"):
        net.release_frame(r.id, actor=other)
    src = net.release_frame(r.id, actor=reader)
    token, _, entry = src.partition("/")
    assert entry == "page/index.html"
    assert net.release_frame_file(token, entry).read_text(encoding="utf-8") == "<p>hi</p>"
    for bad in ("plugin.json", "files/weekly.json", "page/../plugin.json"):
        with pytest.raises(NetworkError):
            net.release_frame_file(token, bad)
    with pytest.raises(NetworkError):  # 插件页面的令牌不能拿来取发布物，反之亦然
        net.plugin_frame_check(token)
    ctx = net.release_bridge(r.id, "context", {}, actor=reader)
    assert ctx["mode"] == "release" and ctx["release"]["version"] == 1
    assert [f["name"] for f in net.release_bridge(r.id, "archive.read", {}, actor=reader)["files"]] == ["weekly.json"]
    assert net.release_bridge(r.id, "archive.file", {"name": "weekly.json"}, actor=reader)["text"] == '{"on_time": 0.82}'
    for op in ("records.list", "chat.post", "members.read"):
        with pytest.raises(NetworkError, match="只能读快照"):
            net.release_bridge(r.id, op, {}, actor=reader)
    net.publish_release_version(g.id, r.id, actor=host)
    with pytest.raises(NetworkError):  # 换了版本，旧令牌作废
        net.release_frame_file(token, entry)


# ================= 回流 =================
def test_feedback_comes_back_as_a_chat_event(tmp_path: Path):
    """不变量 23：读者自己写的反馈进项目群聊（事件，不唤醒任何成员）；读者只看得到自己的反馈。"""
    net, rt, host, reader, other, _, g = setup(tmp_path)
    r = net.create_release(g.id, "query", "method", "查数口径", skills=["delivery-query"], public=True, actor=host)
    calls = len(rt.calls)
    net.release_feedback(r.id, "能不能加上延期原因？", actor=reader)
    net.release_feedback(r.id, "口径里少了回款", actor=other)
    msgs = net.project_chat(g.id, actor=host).messages
    assert msgs[-1].role == "event" and "读者 other@x.io 对发布物「查数口径」v1 的反馈：口径里少了回款" in msgs[-1].text
    assert len(rt.calls) == calls
    assert [x["text"] for x in net.release_detail(r.id, actor=reader)["feedback"]] == ["能不能加上延期原因？"]
    assert len(net.release_detail(r.id, actor=host)["feedback"]) == 2


# ================= 持久化与 HTTP =================
def test_releases_survive_restart_and_old_state_loads(tmp_path: Path):
    net, _, host, reader, _, _, g = setup(tmp_path)
    r = net.create_release(g.id, "weekly", "fact", "周报", files=["weekly.json"], readers=["reader@x.io"], actor=host)
    net.set_subscription(r.id, assistant(net, reader).id, True, actor=reader)
    store = Store(tmp_path)
    assert store.releases[r.id].readers == [reader]
    assert store.agents[store.users[reader].assistant_id].subscriptions == [r.id]
    raw = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    raw.pop("releases")
    for a in raw["agents"]:
        a.pop("subscriptions")
    (tmp_path / "state.json").write_text(json.dumps(raw), encoding="utf-8")
    assert Store(tmp_path).releases == {}


def test_release_api(tmp_path: Path):
    """不变量 23：HTTP 上同样：非主持人发布 409，看不到的 404，读者能读、订阅、写反馈。"""
    net, _, host, reader, other, _, g = setup(tmp_path)
    h, rd, ot = client(net, "host@x.io"), client(net, "reader@x.io"), client(net, "other@x.io")
    r = h.post(f"/api/projects/{g.id}/releases", json={"name": "weekly", "kind": "fact", "title": "周报",
                                                         "files": ["weekly.json"], "readers": ["reader@x.io"]})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    assert ot.post(f"/api/projects/{g.id}/releases", json={"name": "x", "kind": "fact", "title": "x",
                                                          "files": ["weekly.json"]}).status_code == 404
    assert ot.get(f"/api/releases/{rid}").status_code == 404
    assert ot.get(f"/api/releases/{rid}/files/weekly.json").status_code == 404
    got = rd.get(f"/api/releases/{rid}/files/weekly.json")
    assert got.status_code == 200 and got.headers["content-disposition"].startswith("attachment")
    me = net.store.users[reader].assistant_id
    assert rd.post(f"/api/releases/{rid}/subscribe", json={"agent_id": me}).json()["mine"] == [me]
    assert rd.patch(f"/api/agents/{me}", json={"subscriptions": []}).json()["subscriptions"] == []
    assert rd.post(f"/api/releases/{rid}/feedback", json={"text": "好用"}).status_code == 200
    assert [x["id"] for x in rd.get("/api/releases").json()] == [rid]
    assert h.get(f"/api/projects/{g.id}").json()["release_list"][0]["subscribers"] == 0
    assert h.patch(f"/api/projects/{g.id}/releases/{rid}", json={"readers": []}).json()["readers"] == []
    assert rd.get(f"/api/releases/{rid}").status_code == 404
    assert h.delete(f"/api/projects/{g.id}/releases/{rid}").json()["status"] == "withdrawn"


def test_method_snapshot_reaches_skill_layer(tmp_path: Path):
    """订阅的方法排在项目技能、插件打法之后：快照里的技能正文是发布时那一版。"""
    got: list[str] = []

    def handler(call: ScriptCall) -> str:
        f = call.cwd / SKILLS / "delivery-query" / "SKILL.md"
        got.append(f.read_text(encoding="utf-8") if f.is_file() else "")
        return "ok"

    net, _, host, reader, _, _, g = setup(tmp_path, handler)
    r = net.create_release(g.id, "query", "method", "查数口径", skills=["delivery-query"], public=True, actor=host)
    net.save_project_skill(g.id, "delivery-query", "怎么查交付数据", "第二版做法", actor=host)
    me = assistant(net, reader)
    net.set_subscription(r.id, me.id, True, actor=reader)
    net.send_message(net.direct_chat(me.id, actor=reader).id, "查一下", actor=reader, background=False)
    assert "先查项目清单" in got[-1] and "第二版" not in got[-1]
