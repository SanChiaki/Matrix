"""负责人统筹：项目负责人 Agent 代所有者确认交接，网络强制边界，超出就转给人（docs/projects.md §9）。"""
import re
from pathlib import Path

import pytest

from agentnet.engine import Limits, Network, NetworkError
from agentnet.runtime import ScriptCall, ScriptedRuntime
from tests.test_users import client, users


def build(tmp_path: Path, handler, limits: Limits | None = None):
    net = Network(ScriptedRuntime(handler), tmp_path, limits=limits, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    pm = net.create_agent("PM", "pm", [], [], actor=alice)
    eng = net.create_agent("Eng", "eng", [], [], actor=alice)
    qa = net.create_agent("QA", "qa", [], [], actor=alice)
    groups = {n: net.create_group(n, "", [a.id], actor=alice) for n, a in (("产品组", pm), ("开发组", eng), ("测试组", qa))}
    p = net.create_project("tasky", actor=alice)
    for g in groups.values():
        net.assign_group(g.id, p.id, actor=alice)
    chats = {n: net.group_chat(g.id, actor=alice) for n, g in groups.items()}
    return net, alice, bob, pm, eng, qa, p, chats


def test_lead_confirms_handoffs(tmp_path: Path):
    """不变量 19、10：打开负责人统筹后，交接由项目负责人在它统筹的群聊席位里确认（可修改），以所有者的名义发出并写明谁确认的；
    负责人工具只有它在那个席位里有，身份由网络认定。负责人自己提交的、不重复的交接直接发出。"""
    runs: list[tuple[str, str, list[str]]] = []
    plan: dict[str, list] = {}
    out: list[str] = []

    def handler(call: ScriptCall) -> str:
        runs.append((call.agent_id, call.prompt, call.tool_names))
        for name, kw in plan.pop(call.agent_id, []):
            out.append(call.tool(name, **kw))
        if call.agent_id == pm.id and "待你确认的交接" in call.prompt:
            hid = re.search(r"待你确认的交接 (ho-[0-9a-f]{8})", call.prompt).group(1)
            out.append(call.tool("review_handoff", handoff_id=hid, decision="confirm", message="跑 project/ac.md 的全部用例"))
        return "ok"
    net, alice, bob, pm, eng, qa, p, chats = build(tmp_path, handler)
    with pytest.raises(NetworkError, match="必须是挂进这个项目的群组的成员"):
        net.set_project_lead(p.id, net.create_agent("X", "x", [], [], actor=alice).id, "lead", actor=alice)
    with pytest.raises(NetworkError, match="先指定项目负责人"):
        net.set_project_lead(p.id, None, "lead", actor=alice)
    with pytest.raises(NetworkError):
        net.set_project_lead(p.id, pm.id, "lead", actor=bob)
    net.set_project_lead(p.id, pm.id, "lead", actor=alice)

    plan[eng.id] = [("handoff", dict(group="测试组", agent="QA", message="测一下"))]
    net.send_message(chats["开发组"].id, "@Eng 做完交给测试", actor=alice, background=False)
    net.wait_quiet()
    h = next(iter(net.store.handoffs.values()))
    assert "等项目负责人确认" in out[0]
    assert h.status == "sent" and h.approved_by_agent == pm.id and h.approved_by == alice
    m = next(m for m in chats["测试组"].messages if m.role == "user")
    assert m.user_id == alice and m.mentions == [qa.id] and "跑 project/ac.md" in m.text
    assert "已由项目负责人 PM 确认" in m.text
    assert any(a == qa.id for a, *_ in runs)
    # 提交者收到“已由项目负责人 PM 确认”（不唤醒）；人的待办里从头到尾没有它
    assert chats["开发组"].seats[eng.id].inbox[-1]["kind"] == "handoff"
    assert net.snapshot(alice)["handoffs"] == []
    # 负责人工具：只有负责人在它统筹的席位里有；项目背景里写明负责人
    tools = {a: t for a, _, t in runs}
    assert "mcp__agentnet__review_handoff" in tools[pm.id]
    assert "mcp__agentnet__review_handoff" not in tools[eng.id] and "mcp__agentnet__review_handoff" not in tools[qa.id]
    assert any("项目负责人：PM（「产品组」）" in pr for a, pr, _ in runs if a == eng.id)

    # 负责人自己提交的：不重复就直接发出，不需要再确认
    out.clear()
    plan[pm.id] = [("handoff", dict(group="开发组", agent="Eng", message="做 v1.1"))]
    net.send_message(chats["产品组"].id, "@PM 推进 v1.1", actor=alice, background=False)
    net.wait_quiet()
    h2 = next(x for x in net.store.handoffs.values() if x.id != h.id)
    assert "已直接确认" in out[0] and h2.status == "sent" and h2.approved_by_agent == pm.id
    assert any(m.role == "user" and "做 v1.1" in m.text for m in chats["开发组"].messages)
    # 负责人自己提交、和进行中的交接重复的：不直接发，下一轮由它自己用 review_handoff 确认
    out.clear()
    plan[pm.id] = [("handoff", dict(group="开发组", agent="Eng", message="再补一个导出"))]
    net.send_message(chats["产品组"].id, "@PM 加个导出", actor=alice, background=False)
    net.wait_quiet()
    h3 = max(net.store.handoffs.values(), key=lambda x: x.created_at)
    assert "review_handoff" in out[0] and h3.reviewer == "lead" and h3.status == "sent" and h3.approved_by_agent == pm.id


def test_lead_bounds_escalate_to_owner(tmp_path: Path):
    """不变量 20：负责人确认的交接有 24 小时总数与每对群组的上限，超了转给人；负责人一轮没处理、要求转交时也转给人；
    所有者改回人工确认时，还等负责人确认的转给人，负责人工具随之收回。"""
    runs: list[tuple[str, str, list[str]]] = []
    plan: dict[str, list] = {}
    lead_says: list[str] = []

    def handler(call: ScriptCall) -> str:
        runs.append((call.agent_id, call.prompt, call.tool_names))
        for name, kw in plan.pop(call.agent_id, []):
            call.tool(name, **kw)
        if call.agent_id == pm.id and "待你确认的交接" in call.prompt and lead_says:
            hid = re.search(r"待你确认的交接 (ho-[0-9a-f]{8})", call.prompt).group(1)
            decision = lead_says.pop(0)
            if decision == "switch":  # 所有者在负责人处理之前改回人工确认
                net.set_project_lead(p.id, pm.id, "manual", actor=alice)
            elif decision != "ignore":
                call.tool("review_handoff", handoff_id=hid, decision=decision, note="范围变了，请人定")
        return "ok"
    net, alice, bob, pm, eng, qa, p, chats = build(tmp_path, handler, Limits(lead_pair=1))
    net.set_project_lead(p.id, pm.id, "lead", actor=alice)

    def submit(text: str):
        plan[eng.id] = [("handoff", dict(group="测试组", agent="QA", message=text))]
        net.send_message(chats["开发组"].id, f"@Eng {text}", actor=alice, background=False)
        net.wait_quiet()
        return next(x for x in net.store.handoffs.values() if x.text == text)

    lead_says[:] = ["confirm"]
    assert submit("第一轮").status == "sent"
    # 同一对群组超过上限：不送负责人，直接转给人
    h = submit("第二轮")
    assert h.status == "pending" and h.reviewer == "owner" and "上限 1" in h.escalation
    todo = net.snapshot(alice)["handoffs"]
    assert [x["id"] for x in todo] == [h.id] and todo[0]["awaiting"] == "owner"
    net.reject_handoff(h.id, actor=alice)

    net.limits.lead_pair = 10
    lead_says[:] = ["ignore"]  # 负责人这一轮没处理：转给人
    h = submit("第三轮")
    assert h.reviewer == "owner" and "没有处理" in h.escalation
    net.reject_handoff(h.id, actor=alice)
    lead_says[:] = ["escalate"]
    h = submit("第四轮")
    assert h.reviewer == "owner" and "范围变了" in h.escalation
    net.reject_handoff(h.id, actor=alice)
    lead_says[:] = ["decline"]  # 不采纳：唤醒提交者
    h = submit("第五轮")
    net.wait_quiet()
    assert h.status == "rejected" and h.approved_by_agent == pm.id
    assert runs[-1][0] == eng.id and "没有被采纳（项目负责人 PM）" in runs[-1][1]

    # 总数上限
    net.limits.lead_daily = 1
    assert submit("第六轮").escalation.startswith("负责人 24 小时内已确认")

    # 改回人工（负责人这一轮还没处理时）：等负责人的转给人，负责人工具收回
    net.limits.lead_daily = 20
    lead_says[:] = ["switch"]
    h = submit("第七轮")
    assert h.reviewer == "owner" and "统筹方式改了" in h.escalation
    assert h.id in [x["id"] for x in net.snapshot(alice)["handoffs"]]
    net.send_message(chats["产品组"].id, "@PM 在吗", actor=alice, background=False)
    net.wait_quiet()
    assert runs[-1][0] == pm.id and "mcp__agentnet__review_handoff" not in runs[-1][2]
    assert "待你确认的交接" not in runs[-1][1]


def test_project_lead_api(tmp_path: Path):
    """不变量 13：只有项目所有者能指定负责人、打开统筹；项目页列出可选的负责人。"""
    net, alice, bob, pm, eng, qa, p, chats = build(tmp_path, lambda call: "ok")
    ca, cb = client(net, "alice@x.io"), client(net, "bob@x.io")
    assert cb.put(f"/api/projects/{p.id}/lead", json={"agent_id": pm.id, "autonomy": "lead"}).status_code == 404
    r = ca.put(f"/api/projects/{p.id}/lead", json={"agent_id": pm.id, "autonomy": "lead"})
    assert r.status_code == 200 and r.json()["lead_active"] and r.json()["lead_agent_id"] == pm.id
    assert {c["name"] for c in r.json()["lead_candidates"]} == {"PM", "Eng", "QA"}
    assert ca.put(f"/api/projects/{p.id}/lead", json={"agent_id": pm.id, "autonomy": "auto"}).status_code == 409
    # 负责人离开它统筹的群组：统筹失效，交接回到人
    net.update_group(next(g for g in net.store.groups.values() if g.name == "产品组").id, member_ids=[], actor=alice)
    assert not ca.get(f"/api/projects/{p.id}").json()["lead_active"]



def test_lead_accepts_requests_from_its_handoffs(tmp_path: Path):
    """不变量 20、10：打开 lead_accept 后，负责人确认的交接引出的、不是它自己做的任务交付后由负责人验收（身份由网络认定），
    验收后等它的交接自动发出；负责人退回有上限，超了转给人；负责人一轮没处理、人亲自退回后验收归人；
    开工任务与负责人自己做的仍归人验收；改回只确认交接时，等负责人验收的转给人。"""
    runs: list[tuple[str, str, list[str]]] = []
    plan: dict[str, list] = {}
    lead_says: list[tuple[str, str]] = []

    def handler(call: ScriptCall) -> str:
        runs.append((call.agent_id, call.prompt, call.tool_names))
        last = ""
        for name, kw in plan.pop(call.agent_id, []):
            if kw.get("after") == "$prev":  # 依赖同一轮里上一条交接
                kw = {**kw, "after": re.search(r"ho-[0-9a-f]{8}", last).group()}
            last = call.tool(name, **kw)
        if call.agent_id == pm.id and "待你确认的交接" in call.prompt:
            hid = re.search(r"待你确认的交接 (ho-[0-9a-f]{8})", call.prompt).group(1)
            call.tool("review_handoff", handoff_id=hid, decision="confirm")
        if call.agent_id == pm.id and "待你验收的任务" in call.prompt and lead_says:
            rid = re.search(r"待你验收的任务 (req-[0-9a-f]{8})", call.prompt).group(1)
            decision, fb = lead_says.pop(0)
            if decision == "switch":  # 所有者在负责人验收之前改回只确认交接
                net.set_project_lead(p.id, pm.id, "lead", actor=alice)
            else:
                call.tool("review_request", request_id=rid, decision=decision, feedback=fb)
        if "（@ 你）" in call.prompt or "退回了" in call.prompt:  # 被 @ 就登记并交付；被退回就重新交付
            mine = [r for r in net.store.requests.values() if r.agent_id == call.agent_id and r.status == "in_progress"]
            rid = mine[-1].id if mine else re.search(r"req-[0-9a-f]{8}", call.tool(
                "open_request", title="做事", expected_outcome="结果")).group()
            call.tool("deliver_request", request_id=rid, summary="做完了")
        return "ok"
    net, alice, bob, pm, eng, qa, p, chats = build(tmp_path, handler, Limits(lead_pair=10))
    net.set_project_lead(p.id, pm.id, "lead_accept", actor=alice)

    def kickoff(text: str, lead: list[tuple[str, str]], qa_after: bool = False) -> tuple:
        plan[pm.id] = [("handoff", dict(group="开发组", agent="Eng", message=text))]
        if qa_after:
            plan[pm.id].append(("handoff", dict(group="测试组", agent="QA", message=f"验收 {text}", after="$prev")))
        lead_says[:] = lead
        net.send_message(chats["产品组"].id, f"@PM {text}", actor=alice, background=False)
        net.wait_quiet()
        h = next(x for x in net.store.handoffs.values() if x.text == text)
        return h, next(r for r in net.store.requests.values() if r.message_id == h.message_id)

    # 负责人退回一次、再验收；验收后等它的测试交接自动发出。人只发了开工那一条消息
    h, dev = kickoff("实现 v2", [("changes", "补测试"), ("accept", "")], qa_after=True)
    assert dev.status == "completed" and dev.reviewed_by_agent == pm.id and dev.reviewed_by is None and dev.lead_returns == 1
    assert any("项目负责人 PM 退回了" in pr for a, pr, _ in runs if a == eng.id)
    h_qa = next(x for x in net.store.handoffs.values() if x.text == "验收 实现 v2")
    assert h.returned_at is not None and h_qa.status == "sent" and any(a == qa.id for a, *_ in runs)
    assert any(m.role == "event" and "项目负责人 PM 验收了" in m.text for m in chats["开发组"].messages)
    start = next(r for r in net.store.requests.values() if r.agent_id == pm.id)
    assert start.status == "in_review" and start.reviewer == "owner"  # 开工任务归人
    r_qa = next(r for r in net.store.requests.values() if r.agent_id == qa.id)  # 送到负责人，它这一轮没说话：转给人
    assert r_qa.status == "in_review" and "没有验收" in r_qa.escalation
    assert all("mcp__agentnet__review_request" not in t for a, _, t in runs if a != pm.id)
    assert {r["id"]: r["awaiting"] for r in net.board(alice)["requests"]}[start.id] == "owner"

    # 退回上限：超了转给人
    net.limits.max_revisions = 1
    _, dev = kickoff("实现 v3", [("changes", "再改"), ("accept", "")])
    assert dev.status == "in_review" and dev.reviewer == "owner" and "已经退回 1 次" in dev.escalation
    net.limits.max_revisions = 3

    # 负责人一轮没处理：转给人；人亲自退回后，重新交付也归人
    _, dev = kickoff("实现 v4", [])
    assert dev.reviewer == "owner" and "没有验收" in dev.escalation
    net.review_request(dev.id, False, "人来看", actor=alice)
    net.wait_quiet()
    assert dev.status == "in_review" and dev.reviewer == "owner" and "接手" in dev.escalation

    # 负责人自己做的（别的组交接给它）：归人验收
    plan[eng.id] = [("handoff", dict(group="产品组", agent="PM", message="澄清需求"))]
    net.send_message(chats["开发组"].id, "@Eng 有疑问找 PM", actor=alice, background=False)
    net.wait_quiet()
    h = next(x for x in net.store.handoffs.values() if x.text == "澄清需求")
    mine = next(r for r in net.store.requests.values() if r.message_id == h.message_id)
    assert mine.agent_id == pm.id and mine.status == "in_review" and mine.reviewer == "owner"

    # 改回只确认交接：等负责人验收的转给人
    _, dev = kickoff("实现 v5", [("switch", "")])
    assert dev.reviewer == "owner" and "统筹方式改了" in dev.escalation
