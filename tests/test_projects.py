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


def test_project_context_only_to_attached_groups(tmp_path: Path):
    """不变量 18：项目背景与项目文件只推送 / 挂载给挂载群组；私聊、未挂载的群组拿不到；只在版本变化时重新推送。"""
    seen: list[tuple[str, str, bool]] = []  # (上下文, 提示词, 工作目录里有没有 project/spec.md)

    def handler(call: ScriptCall) -> str:
        seen.append((call.context_id, call.prompt, (Path(call.cwd) / "project" / "spec.md").is_file()))
        return "ok"
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=alice)
    g = net.create_group("调研组", "", [a.id], actor=alice)
    g2 = net.create_group("闲聊组", "", [b.id], actor=alice)
    p = net.create_project("新产品上线", brief="目标：Q4 上线", actor=alice)
    research = net.add_workstream(p.id, "市场调研", actor=alice)
    net.add_workstream(p.id, "原型开发", actor=alice)
    net.assign_group(g.id, p.id, research.id, actor=alice)
    net.publish_project_file(p.id, "spec.md", b"# spec", actor=alice)

    chat = net.group_chat(g.id, actor=alice)
    net.send_message(chat.id, "@A 写", actor=alice, background=False)
    _, prompt, mounted = seen[-1]
    assert "Q4 上线" in prompt and "负责「市场调研」" in prompt and "spec.md" in prompt and mounted
    assert not (tmp_path / chat.seats[a.id].workdir / "project").exists()  # 只读副本运行后丢弃

    for conv, who in ((net.group_chat(g2.id, actor=alice), "@B 写"), (net.direct_chat(a.id, actor=alice), "写")):
        net.send_message(conv.id, who, actor=alice, background=False)
        _, prompt, mounted = seen[-1]
        assert "Q4 上线" not in prompt and "新产品上线" not in prompt and not mounted

    net.send_message(chat.id, "@A 再写", actor=alice, background=False)  # 没有变化：不重复推送
    assert "Q4 上线" not in seen[-1][1] and seen[-1][2]
    net.update_project(p.id, brief="目标：Q1 上线", actor=alice)
    net.send_message(chat.id, "@A 又写", actor=alice, background=False)
    assert "Q1 上线" in seen[-1][1]
    assert not any("新产品上线" in m.text for m in chat.messages)  # 人的项目操作不写进群聊

    net.assign_group(g.id, None, actor=alice)  # 移出项目：之后不再挂载
    net.send_message(chat.id, "@A 最后一次", actor=alice, background=False)
    assert not seen[-1][2]


def test_agent_publishes_to_project(tmp_path: Path):
    """不变量 18：publish_file target=project 只能发到自己群组所属的项目；别的挂载群组的委托任务读得到。"""
    def handler(call: ScriptCall) -> str:
        if call.context_id.startswith("task-"):
            assert (Path(call.cwd) / "project" / "prd.md").read_text() == "# PRD"
            assert "负责「工程」" in call.prompt
            return "done"
        if call.agent_id == pm.id:
            Path(call.cwd, "prd.md").write_text("# PRD")
            out = call.tool("publish_file", path="prd.md", target="project")
            assert "已发布到项目" in out, out
            assert "不能" not in call.tool("publish_file", path="prd.md")  # 默认仍是群组档案
            return "published"
        if call.agent_id == lead.id:
            call.tool("delegate", agent="Eng", mission="按 project/prd.md 实现", expected_outcome="代码")
            return "delegated"
        return "ok"
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    pm = net.create_agent("PM", "pm", [], ["file.write"], actor=alice)
    lead = net.create_agent("Lead", "lead", [], ["delegate"], actor=alice)
    eng = net.create_agent("Eng", "eng", [], [], actor=alice)
    loner = net.create_agent("Loner", "x", [], ["file.write"], actor=alice)
    pg = net.create_group("产品组", "", [pm.id], actor=alice)
    dg = net.create_group("开发组", "", [lead.id, eng.id], actor=alice)
    lg = net.create_group("别的组", "", [loner.id], actor=alice)
    p = net.create_project("tasky", actor=alice)
    need = net.add_workstream(p.id, "需求", actor=alice)
    eng_ws = net.add_workstream(p.id, "工程", actor=alice)
    net.assign_group(pg.id, p.id, need.id, actor=alice)
    net.assign_group(dg.id, p.id, eng_ws.id, actor=alice)

    pchat = net.group_chat(pg.id, actor=alice)
    net.send_message(pchat.id, "@PM 写 PRD 发到项目", actor=alice, background=False)
    assert [f["name"] for f in net.project_detail(p.id, actor=alice)["files"]] == ["prd.md"]
    assert any("发布到了项目「tasky」" in m.text for m in pchat.messages)
    assert (tmp_path / "projects" / p.id / "log.jsonl").read_text().count("file.publish") == 1

    net.send_message(net.group_chat(dg.id, actor=alice).id, "@Lead 实现", actor=alice, background=False)
    net.wait_quiet()
    t = next(t for t in net.store.tasks.values() if t.delegatee_id == eng.id)
    assert t.status.value == "completed" and t.result["report"] == "done", (t.status, t.error)

    # 不在项目里的群组：target=project 被拒
    holder = []
    def lone(call: ScriptCall) -> str:
        Path(call.cwd, "x.md").write_text("x")
        holder.append(call.tool("publish_file", path="x.md", target="project"))
        holder.append(call.tool("publish_file", path="project/x.md"))
        return "ok"
    net.runtime.handler = lone
    net.send_message(net.group_chat(lg.id, actor=alice).id, "@Loner 发", actor=alice, background=False)
    assert "不属于任何项目" in holder[0] and "找不到" in holder[1]


def test_project_files_api(tmp_path: Path):
    """项目文件：所有者上传、从挂载群组提升、下载（附件）、删除；别人 404。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    g = net.create_group("G", "", [a.id], actor=alice)
    g2 = net.create_group("G2", "", [a.id], actor=alice)
    ca, cb = client(net, "alice@x.io"), client(net, "bob@x.io")
    pid = ca.post("/api/projects", json={"name": "上线"}).json()["id"]
    ca.put(f"/api/groups/{g.id}/project", json={"project_id": pid})
    ca.post(f"/api/groups/{g.id}/files", json={"name": "prd.md", "content": "# PRD"})
    ca.post(f"/api/groups/{g2.id}/files", json={"name": "other.md", "content": "x"})
    d = ca.post(f"/api/projects/{pid}/files/promote", json={"group_id": g.id, "name": "prd.md"}).json()
    assert [f["name"] for f in d["files"]] == ["prd.md"]
    r = ca.post(f"/api/projects/{pid}/files/promote", json={"group_id": g2.id, "name": "other.md"})
    assert r.status_code == 409 and "不在项目" in r.json()["detail"]
    d = ca.post(f"/api/projects/{pid}/files", json={"name": "../plan.md", "content": "plan"}).json()
    assert sorted(f["name"] for f in d["files"]) == ["plan.md", "prd.md"]
    r = ca.get(f"/api/projects/{pid}/files/prd.md")
    assert r.text == "# PRD" and "attachment" in r.headers["content-disposition"]
    assert cb.get(f"/api/projects/{pid}/files/prd.md").status_code == 404
    assert cb.post(f"/api/projects/{pid}/files", json={"name": "x", "content": "x"}).status_code == 404
    assert [f["name"] for f in ca.delete(f"/api/projects/{pid}/files/plan.md").json()["files"]] == ["prd.md"]


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


def test_handoff_goes_through_the_owner(tmp_path: Path):
    """不变量 19：Agent 不能直接跨群组委托或发言，只能向同一项目里的其他群组提交交接；项目所有者确认（可修改）后
    以人的名义发进目标群聊并 @ 目标成员。没挂项目的群组没有这个工具。"""
    runs: list[tuple[str, str, list[str]]] = []
    plan: dict[str, list] = {}

    def handler(call: ScriptCall) -> str:
        runs.append((call.agent_id, call.prompt, call.tool_names))
        for kw in plan.pop(call.agent_id, []):
            plan.setdefault("out", []).append(call.tool("handoff", **kw))
        if call.agent_id == eng.id and "先做 add/list" in call.prompt:
            rid = re.search(r"req-[0-9a-f]{8}", call.tool("open_request", title="实现", expected_outcome="代码")).group()
            call.tool("deliver_request", request_id=rid, summary="add/list 已实现")
        return "ok"
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    pm = net.create_agent("PM", "pm", [], [], actor=alice)
    eng = net.create_agent("Eng", "eng", [], [], actor=alice)
    loner = net.create_agent("Loner", "x", [], [], actor=alice)
    pg = net.create_group("产品组", "", [pm.id], actor=alice)
    dg = net.create_group("开发组", "", [eng.id], actor=alice)
    og = net.create_group("别的组", "", [loner.id], actor=alice)
    p = net.create_project("tasky", actor=alice)
    net.assign_group(pg.id, p.id, actor=alice)
    net.assign_group(dg.id, p.id, actor=alice)
    pchat, dchat = net.group_chat(pg.id, actor=alice), net.group_chat(dg.id, actor=alice)

    plan[pm.id] = [dict(group="别的组", agent="Loner", message="x"), dict(group="开发组", agent="PM", message="x"),
                   dict(group="产品组", agent="PM", message="x"),
                   dict(group="开发组", agent="Eng", message="按 project/prd.md 实现")]
    net.send_message(pchat.id, "@PM 定稿后交给开发组", actor=alice, background=False)
    out = plan.pop("out")
    assert "不是项目「tasky」里的其他群组" in out[0] and "不是「开发组」的成员" in out[1]
    assert "不是项目「tasky」里的其他群组" in out[2] and "已提交交接" in out[3]
    h = next(iter(net.store.handoffs.values()))
    assert h.status == "pending" and not dchat.messages and all(a != eng.id for a, *_ in runs)  # 人确认之前什么都不发生

    with pytest.raises(NetworkError, match="unknown handoff"):
        net.approve_handoff(h.id, actor=bob)
    pm_runs = sum(1 for a, *_ in runs if a == pm.id)
    net.approve_handoff(h.id, text="按 project/prd.md 实现 v1，先做 add/list", actor=alice)
    net.wait_quiet()
    m = next(m for m in dchat.messages if m.role == "user")
    assert m.user_id == alice and m.mentions == [eng.id] and "先做 add/list" in m.text and "来自「产品组」的 PM" in m.text
    assert any(a == eng.id and "先做 add/list" in pr for a, pr, _ in runs)
    assert sum(1 for a, *_ in runs if a == pm.id) == pm_runs  # 发出不唤醒提交者
    assert pchat.seats[pm.id].inbox and pchat.seats[pm.id].inbox[0]["kind"] == "handoff"
    with pytest.raises(NetworkError, match="已经处理过"):
        net.reject_handoff(h.id, actor=alice)
    # 回路：交接引出的任务验收通过后，唤醒提交者并带上交付说明
    r = next(r for r in net.store.requests.values() if r.agent_id == eng.id)
    net.review_request(r.id, True, actor=alice)
    net.wait_quiet()
    assert runs[-1][0] == pm.id and "已完成，人已验收" in runs[-1][1] and "add/list 已实现" in runs[-1][1]
    assert h.returned_at is not None

    # 拒绝：唤醒提交者，带上意见
    plan[pm.id] = [dict(group="开发组", agent="Eng", message="再做一个")]
    net.send_message(pchat.id, "@PM 再交一个", actor=alice, background=False)
    h2 = next(x for x in net.store.handoffs.values() if x.id != h.id)
    net.reject_handoff(h2.id, note="先别做", actor=alice)
    net.wait_quiet()
    assert "没有被采纳" in runs[-1][1] and "先别做" in runs[-1][1] and runs[-1][0] == pm.id
    assert net.board(alice)["handoffs"] == [] and net.board(bob)["handoffs"] == []

    # 没挂项目的群组：没有 handoff 工具
    net.send_message(net.group_chat(og.id, actor=alice).id, "@Loner 在吗", actor=alice, background=False)
    assert runs[-1][0] == loner.id and not any(t.endswith("handoff") for t in runs[-1][2])
    assert any(t.endswith("handoff") for a, _, tools in runs if a == pm.id for t in tools)


def test_handoff_waits_for_dependencies(tmp_path: Path):
    """不变量 19：有依赖的交接确认后先排队（scheduled），依赖的交接完成（人验收）后才由网络以确认人的名义发出；依赖不能成环。"""
    out: list[str] = []
    plan: dict[str, list] = {}

    def handler(call: ScriptCall) -> str:
        for kw in plan.pop(call.agent_id, []):
            out.append(call.tool("handoff", **kw))
        if call.agent_id != pm.id and "@ 你" in call.prompt:
            rid = re.search(r"req-[0-9a-f]{8}", call.tool("open_request", title="活", expected_outcome="产物")).group()
            call.tool("deliver_request", request_id=rid, summary=f"{call.agent_id} 做完了")
        return "ok"
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    pm, dev, qa, doc = (net.create_agent(n, n, [], [], actor=alice) for n in ("PM", "Dev", "QA", "Doc"))
    p = net.create_project("tasky", actor=alice)
    chats = {}
    for name, a in (("产品组", pm), ("开发组", dev), ("测试组", qa), ("文档组", doc)):
        g = net.create_group(name, "", [a.id], actor=alice)
        net.assign_group(g.id, p.id, actor=alice)
        chats[name] = net.group_chat(g.id, actor=alice)

    plan[pm.id] = [dict(group="开发组", agent="Dev", message="实现"), dict(group="测试组", agent="QA", message="测", after="ho-nope")]
    net.send_message(chats["产品组"].id, "@PM 分派", actor=alice, background=False)
    assert "不是这个项目里可以等待的交接" in out[1]
    h1 = next(iter(net.store.handoffs.values()))
    plan[pm.id] = [dict(group="测试组", agent="QA", message="测", after=h1.id), dict(group="文档组", agent="Doc", message="写")]
    net.send_message(chats["产品组"].id, "@PM 再分派", actor=alice, background=False)
    h2, h3 = sorted((h for h in net.store.handoffs.values() if h.id != h1.id), key=lambda h: h.to_group_id != chats["测试组"].group_id)
    assert h2.after == [h1.id] and h3.after == []

    with pytest.raises(NetworkError, match="互相等待"):
        net.approve_handoff(h1.id, after=[h2.id], actor=alice)
    net.approve_handoff(h3.id, after=[h1.id, h2.id], actor=alice)  # 人在确认时加依赖
    net.approve_handoff(h2.id, actor=alice)
    assert (h2.status, h3.status) == ("scheduled", "scheduled") and not chats["测试组"].messages
    net.approve_handoff(h1.id, actor=alice)
    net.wait_quiet()
    assert h1.status == "sent" and not chats["测试组"].messages  # 开发还没验收

    def accept(agent):
        r = next(r for r in net.store.requests.values() if r.agent_id == agent.id and r.status == "in_review")
        net.review_request(r.id, True, actor=alice)
        net.wait_quiet()
    accept(dev)
    assert h2.status == "sent" and h3.status == "scheduled"
    m = next(m for m in chats["测试组"].messages if m.role == "user")
    assert m.user_id == alice and m.mentions == [qa.id] and f"等 {h1.id} 完成后发出" in m.text
    accept(qa)
    assert h3.status == "sent" and any(m.role == "user" for m in chats["文档组"].messages)


def test_handoff_replaces_and_shows_open_handoffs(tmp_path: Path):
    """不变量 19：进行中的交接写进项目背景；给同一群组重复提交时提醒；确认替换后旧的作废、等旧的改为等新的。"""
    out: list[str] = []
    prompts: dict[str, list[str]] = {}
    plan: dict[str, list] = {}

    def handler(call: ScriptCall) -> str:
        prompts.setdefault(call.agent_id, []).append(call.prompt)
        for kw in plan.pop(call.agent_id, []):
            out.append(call.tool("handoff", **kw))
        return "ok"
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    pm, dev, qa, doc = (net.create_agent(n, n, [], [], actor=alice) for n in ("PM", "Dev", "QA", "Doc"))
    p = net.create_project("tasky", actor=alice)
    chats = {}
    for name, a in (("产品组", pm), ("开发组", dev), ("测试组", qa), ("文档组", doc)):
        g = net.create_group(name, "", [a.id], actor=alice)
        net.assign_group(g.id, p.id, actor=alice)
        chats[name] = net.group_chat(g.id, actor=alice)
    plan[pm.id] = [dict(group="测试组", agent="QA", message="测 v1.1")]
    net.send_message(chats["产品组"].id, "@PM 分派", actor=alice, background=False)
    old = next(iter(net.store.handoffs.values()))
    plan[pm.id] = [dict(group="文档组", agent="Doc", message="写文档", after=old.id)]
    net.send_message(chats["产品组"].id, "@PM 文档", actor=alice, background=False)
    doc_h = next(h for h in net.store.handoffs.values() if h.id != old.id)

    # 开发组的 Dev 被 @ 时，项目背景里能看到进行中的交接
    plan[dev.id] = [dict(group="测试组", agent="QA", message="按交付说明测 1.1.0（含夹具说明）")]
    net.send_message(chats["开发组"].id, "@Dev 交给测试", actor=alice, background=False)
    assert old.id in prompts[dev.id][-1] and "待人确认" in prompts[dev.id][-1]
    net.publish_project_file(p.id, "prd.md", b"v1", actor=alice)
    net.publish_project_file(p.id, "prd.md", b"v2", actor=alice)
    assert "prd.md" in net._recent_files(p)[0] and "第 2 版" in net._recent_files(p)[0]
    assert "已有进行中的交接" in out[-1] and old.id in out[-1]
    new = next(h for h in net.store.handoffs.values() if h.id not in (old.id, doc_h.id))
    with pytest.raises(NetworkError, match="不是可以替换的交接"):
        net.approve_handoff(new.id, replaces=doc_h.id + "x", actor=alice)
    with pytest.raises(NetworkError, match="互相等待"):  # 文档在等旧的；新的不能反过来等文档
        net.approve_handoff(new.id, replaces=old.id, after=[doc_h.id], actor=alice)
    net.approve_handoff(new.id, replaces=old.id, actor=alice)
    net.wait_quiet()
    assert old.status == "superseded" and doc_h.after == [new.id] and new.status == "sent"
    assert pm.id in {a for a in prompts} and any(it["kind"] == "handoff" for it in chats["产品组"].seats[pm.id].inbox)
    # 被替换后，项目背景里不再列旧的
    net.send_message(chats["开发组"].id, "@Dev 再看看", actor=alice, background=False)
    assert old.id not in prompts[dev.id][-1].split("进行中的交接")[-1]


def test_project_decisions_pushed_to_attached_groups(tmp_path: Path):
    """不变量 18：项目决定只由所有者记录，推送给挂载群组（被取代的不再推送）；持久化。"""
    prompts: list[str] = []
    net, alice, bob, a, g, p, research, proto = setup(tmp_path, prompts)
    net.assign_group(g.id, p.id, research.id, actor=alice)
    d1 = net.record_project_decision(p.id, "错误文案按 PRD §5.3 逐字对齐", reason="文档以 PRD 为准", actor=alice)
    with pytest.raises(NetworkError, match="unknown project"):
        net.record_project_decision(p.id, "偷偷定", actor=bob)
    chat = net.group_chat(g.id, actor=alice)
    net.send_message(chat.id, "@A 写", actor=alice, background=False)
    assert "项目决定" in prompts[-1] and "逐字对齐" in prompts[-1]
    net.record_project_decision(p.id, "v1.1.1 修复后再发布文档", supersedes=d1.id, actor=alice)
    net.send_message(chat.id, "@A 再写", actor=alice, background=False)
    assert "v1.1.1 修复后再发布文档" in prompts[-1] and "逐字对齐" not in prompts[-1]
    assert [d.status for d in Store(tmp_path, seed=False).projects[p.id].decisions] == ["superseded", "active"]
