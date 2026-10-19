"""项目：目标与汇总层（docs/projects.md）。归属由网络认定、只有所有者看得到、不流动上下文。"""
import json
import re
from pathlib import Path

import pytest

from agentnet.engine import Network, NetworkError
from agentnet.runtime import ScriptCall, ScriptedRuntime
from agentnet.store import Store
from tests.test_users import client, joint, users


def deliver_handler(prompts: list[str] | None = None):
    """每轮：登记任务并交付。prompts 收集席位看到的提示词。"""
    def handler(call: ScriptCall) -> str:
        if prompts is not None:
            prompts.append(call.prompt)
        text = call.tool("open_request", title="报告", expected_outcome="报告文件")
        rid = re.search(r"req-[0-9a-f]{8}", text)
        if rid:
            call.tool("deliver_request", request_id=rid.group(), summary="完成")
        return "ok"
    return handler


def setup(tmp_path: Path, prompts: list[str] | None = None):
    net = Network(ScriptedRuntime(deliver_handler(prompts)), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    g = net.create_group("调研组", "", [a.id], actor=alice)
    p = net.create_project("新产品上线", brief="目标：Q4 上线", actor=alice)
    research = net.add_workstream(p.id, "市场调研", actor=alice)
    proto = net.add_workstream(p.id, "原型开发", actor=alice)
    return net, alice, bob, a, g, p, research, proto


def test_requests_take_project_from_group(tmp_path: Path):
    """任务的项目归属由网络按群组认定；群组换方面时未结束的任务跟着走，已结束的保留当时的归属。"""
    net, alice, bob, a, g, p, research, proto = setup(tmp_path)
    net.assign_group(g.id, p.id, research.id, actor=alice)
    chat = net.group_chat(g.id, actor=alice)
    net.send_message(chat.id, "@A 写一份报告", actor=alice, background=False)
    r1 = next(iter(net.store.requests.values()))
    assert (r1.project_id, r1.workstream_id) == (p.id, research.id)
    card = net.board(alice)["requests"][0]
    assert card["project_name"] == "新产品上线" and card["workstream_name"] == "市场调研"
    assert [x["id"] for x in net.board(alice)["projects"]] == [p.id]

    net.review_request(r1.id, True, actor=alice)  # 已结束
    net.send_message(chat.id, "@A 再写一份", actor=alice, background=False)
    r2 = next(r for r in net.store.requests.values() if r.id != r1.id)
    assert r2.status == "in_review"
    net.assign_group(g.id, p.id, proto.id, actor=alice)
    assert r2.workstream_id == proto.id and r1.workstream_id == research.id
    detail = net.project_detail(p.id, actor=alice)
    assert {x["id"] for x in detail["requests"]} == {r1.id, r2.id}
    assert detail["groups"] == [{"id": g.id, "name": "调研组", "kind": "internal", "workstream_id": proto.id,
                                 "member_count": 1}]
    assert detail["request_counts"]["completed"] == 1 and detail["request_counts"]["in_review"] == 1

    net.assign_group(g.id, None, actor=alice)
    assert r2.project_id is None and r1.project_id == p.id
    assert net.store.groups[g.id].project_id is None


def test_project_is_owner_only(tmp_path: Path):
    """不变量 13、16：项目只有所有者看得到；群组与项目必须同一个所有者；联合群组暂不能挂项目。"""
    net, alice, bob, a, g, p, research, proto = setup(tmp_path)
    with pytest.raises(NetworkError, match="unknown project"):
        net.project_detail(p.id, actor=bob)
    with pytest.raises(NetworkError, match="unknown project"):
        net.add_workstream(p.id, "偷偷加", actor=bob)
    assert net.projects(bob) == [] and net.board(bob)["projects"] == []
    # 别人的群组不能挂进我的项目，我的群组也不能挂进别人的项目
    b = net.create_agent("B", "b", [], [], actor=bob)
    bg = net.create_group("Bob 组", "", [b.id], actor=bob)
    with pytest.raises(NetworkError, match="unknown project"):
        net.assign_group(bg.id, p.id, actor=bob)
    with pytest.raises(NetworkError, match="unknown group"):
        net.assign_group(g.id, p.id, actor=bob)
    jg, jchat = joint(net, alice, "bob@x.io", bob, [a.id])
    with pytest.raises(NetworkError, match="联合群组"):
        net.assign_group(jg.id, p.id, actor=alice)
    # 联合群组里的任务卡别的参与方也看得到：不能归到项目
    net.send_message(jchat.id, "@A 帮我写", actor=bob, background=False)
    jr = next(iter(net.store.requests.values()))
    with pytest.raises(NetworkError, match="联合群组"):
        net.assign_request(jr.id, p.id, actor=alice)
    # 项目事件只推给所有者
    net.update_project(p.id, brief="目标：Q1 上线", actor=alice)
    ev = next(e for e in reversed(net.bus.since(0)) if e.type == "project.updated")
    assert net.event_view(ev.type, ev.data, bob) is None
    assert net.event_view(ev.type, ev.data, alice)["project"]["brief"] == "目标：Q1 上线"


def test_dm_request_assigned_by_hand(tmp_path: Path):
    net, alice, bob, a, g, p, research, proto = setup(tmp_path)
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "写个报告", actor=alice, background=False)
    r = next(iter(net.store.requests.values()))
    assert r.project_id is None  # 私聊默认不属于项目
    net.assign_request(r.id, p.id, research.id, actor=alice)
    assert (r.project_id, r.workstream_id) == (p.id, research.id)
    with pytest.raises(NetworkError, match="方面"):
        net.assign_request(r.id, None, research.id, actor=alice)
    other = net.create_project("别的项目", actor=alice)
    with pytest.raises(NetworkError, match="unknown workstream"):
        net.assign_request(r.id, other.id, research.id, actor=alice)


def test_delete_workstream_and_project_clear_links(tmp_path: Path):
    net, alice, bob, a, g, p, research, proto = setup(tmp_path)
    net.assign_group(g.id, p.id, research.id, actor=alice)
    chat = net.group_chat(g.id, actor=alice)
    net.send_message(chat.id, "@A 写", actor=alice, background=False)
    r = next(iter(net.store.requests.values()))
    net.remove_workstream(p.id, research.id, actor=alice)
    assert net.store.groups[g.id].workstream_id is None and r.workstream_id is None
    assert net.store.groups[g.id].project_id == p.id and r.project_id == p.id  # 仍在项目里，只是未分方面
    net.delete_project(p.id, actor=alice)
    assert net.store.groups[g.id].project_id is None and r.project_id is None
    assert g.id in net.store.groups and r.id in net.store.requests  # 群组与任务本身不动


def test_agents_do_not_see_projects(tmp_path: Path):
    """第一阶段 Agent 不知道项目存在：席位的提示词里没有项目名称、brief、方面，群聊里也不写事件。"""
    prompts: list[str] = []
    net, alice, bob, a, g, p, research, proto = setup(tmp_path, prompts)
    net.assign_group(g.id, p.id, research.id, actor=alice)
    chat = net.group_chat(g.id, actor=alice)
    net.send_message(chat.id, "@A 写", actor=alice, background=False)
    assert prompts and not any(x in prompts[0] for x in ("新产品上线", "Q4 上线", "市场调研"))
    assert not any("新产品上线" in m.text for m in chat.messages)


def test_projects_persist_and_old_project_refs_migrate(tmp_path: Path):
    net, alice, bob, a, g, p, research, proto = setup(tmp_path)
    net.assign_group(g.id, p.id, proto.id, actor=alice)
    # 旧版 state.json：群组的“项目引用”在 projects 字段里
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    raw = next(x for x in state["groups"] if x["id"] == g.id)
    raw["projects"] = [{"id": "proj-1", "name": "前端仓库", "location": "~/code/web", "note": ""}]
    raw.pop("refs")
    (tmp_path / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    store = Store(tmp_path, seed=False)
    assert [r.name for r in store.groups[g.id].refs] == ["前端仓库"]
    assert store.groups[g.id].workstream_id == proto.id
    assert [w.name for w in store.projects[p.id].workstreams] == ["市场调研", "原型开发"]


def test_projects_api(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    g = net.create_group("G", "", [a.id], actor=alice)
    ca, cb = client(net, "alice@x.io"), client(net, "bob@x.io")
    d = ca.post("/api/projects", json={"name": "上线", "description": "d"}).json()
    d = ca.post(f"/api/projects/{d['id']}/workstreams", json={"name": "调研"}).json()
    pid, ws = d["id"], d["workstreams"][0]["id"]
    assert ca.put(f"/api/groups/{g.id}/project", json={"project_id": pid, "workstream_id": ws}).status_code == 200
    d = ca.get(f"/api/projects/{pid}").json()
    assert d["groups"][0]["workstream_id"] == ws and d["group_count"] == 1
    assert ca.patch(f"/api/projects/{pid}", json={"brief": "目标"}).json()["brief"] == "目标"
    assert [x["id"] for x in ca.get("/api/projects").json()] == [pid]
    assert [x["id"] for x in ca.get("/api/network").json()["projects"]] == [pid]
    # 别人：按“不存在”处理
    assert cb.get(f"/api/projects/{pid}").status_code == 404
    assert cb.get("/api/projects").json() == []
    assert cb.delete(f"/api/projects/{pid}").status_code == 404
    # 资源引用改名后的接口
    assert ca.post(f"/api/groups/{g.id}/refs", json={"name": "仓库", "location": "~/r"}).json()["refs"][0]["name"] == "仓库"
    assert ca.delete(f"/api/projects/{pid}/workstreams/{ws}").json()["workstreams"] == []
    assert ca.delete(f"/api/projects/{pid}").json() == {"ok": True}
    assert ca.get(f"/api/groups/{g.id}").json()["project_id"] is None
