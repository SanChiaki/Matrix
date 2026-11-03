"""dsh 的长期目标（create_goal）由网络在同一会话里续跑；后台任务守护挂在覆盖层上（research: deepseek-harness#headless-tools）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from matrix import auth
from matrix.engine import Network, _goal_of
from matrix.models import TaskStatus
from matrix.runtime import JOBS_PLUGIN, RuntimeCapabilities, ScriptCall, ScriptedRuntime, build_patch_yaml


def users(net: Network, *emails: str) -> list[str]:
    return [auth.register(net.store, e, "password-123")[0].id for e in emails]


def goal_json(phase: str, max_rounds: int | None = 5, gid: str = "goal-1", objective: str = "写完三节报告") -> str:
    return json.dumps({"goal": {"id": gid, "revision": 1, "objective": objective, "phase": phase, "roundsStarted": 0,
                                "maxGoalRounds": max_rounds}, "activation": "armed"}, ensure_ascii=False)


def goal_agent(finish_on: int | None, max_rounds: int | None = 5, log: list[str] | None = None) -> Callable[[ScriptCall], str]:
    """第一轮 create_goal；之后每轮记下提示词，第 finish_on 轮 update_goal complete（None：一直不完成）。"""
    def handler(call: ScriptCall) -> str:
        if log is not None:
            log.append(call.prompt)
        if call.turn == 1:
            call.use("create_goal", {"objective": "写完三节报告"}, result=goal_json("active", max_rounds))
            return "第 1 节写好了"
        if finish_on is not None and call.turn == finish_on:
            call.use("update_goal", {"action": "complete"}, result=goal_json("complete", max_rounds))
            return "三节都写完了"
        return f"第 {call.turn} 节写好了"
    return handler


def dm_of(net: Network, handler: Callable[[ScriptCall], str], caps: RuntimeCapabilities | None = None):
    net.runtime = ScriptedRuntime(handler, caps)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    return alice, a, net.direct_chat(a.id, actor=alice)


def test_goal_continues_in_same_session_until_complete(tmp_path: Path):
    """create_goal 之后网络在同一会话里一轮接一轮地唤醒，直到 update_goal complete；每轮的回复照常发出。"""
    log: list[str] = []
    net = Network(ScriptedRuntime(lambda c: "x"), tmp_path, seed=False)
    alice, a, dm = dm_of(net, goal_agent(3, log=log))
    net.send_message(dm.id, "写一份三节的报告", background=False, actor=alice)
    replies = [m for m in dm.messages if m.role == "agent"]
    assert [m.text for m in replies] == ["第 1 节写好了", "第 2 节写好了", "三节都写完了"]
    assert replies[1].wake == ["目标续行 第 1/5 轮"] and replies[2].wake == ["目标续行 第 2/5 轮"]
    assert "接着推进你的目标（第 1/5 轮）" in log[1] and "写完三节报告" in log[1] and "complete" in log[1]
    seat = dm.seats[a.id]
    assert seat.goal["phase"] == "complete" and seat.goal["rounds"] == 2 and not seat.inbox
    sessions = {c.session_id for c in net.runtime.calls}
    assert len(sessions) == 1  # 同一会话续接


def test_goal_stops_at_round_cap(tmp_path: Path):
    """一直不标记完成：续到上限就停，告诉所有者。"""
    net = Network(ScriptedRuntime(lambda c: "x"), tmp_path, seed=False)
    alice, a, dm = dm_of(net, goal_agent(None, max_rounds=2))
    net.send_message(dm.id, "go", background=False, actor=alice)
    assert len([m for m in dm.messages if m.role == "agent"]) == 3  # 第一轮 + 续 2 轮
    assert dm.seats[a.id].goal["phase"] == "limit"
    note = [m for m in dm.messages if m.role == "event" and "上限" in m.text]
    assert note and note[0].audience == [alice]


def test_goal_rounds_are_capped_even_if_goal_asks_for_more(tmp_path: Path):
    from matrix.engine import GOAL_ROUNDS_CAP, _goal_cap
    assert _goal_cap({"max": 1000}) == GOAL_ROUNDS_CAP and _goal_cap({"max": None}) == 10 and _goal_cap({"max": "x"}) == 10


def test_no_goal_no_continuation_and_failure_stops(tmp_path: Path):
    """没有目标就只跑一轮；续行中这一轮失败就不再续（席位停着，等人“继续”）。"""
    net = Network(ScriptedRuntime(lambda c: "只回一句"), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "hi", background=False, actor=alice)
    assert len(net.runtime.calls) == 1 and dm.seats[a.id].goal == {}

    def flaky(call: ScriptCall) -> str:
        if call.turn == 1:
            call.use("create_goal", {}, result=goal_json("active"))
            return "开始"
        call.fail("SERVER boom")
        return ""
    net2 = Network(ScriptedRuntime(flaky), tmp_path / "2", seed=False)
    bob, = users(net2, "bob@x.io")
    b = net2.create_agent("B", "b", [], [], actor=bob)
    dm2 = net2.direct_chat(b.id, actor=bob)
    net2.send_message(dm2.id, "go", background=False, actor=bob)
    seat = dm2.seats[b.id]
    assert seat.blocked and len(net2.runtime.calls) == 2


def test_paused_goal_stops_and_human_message_joins_next_round(tmp_path: Path):
    """人插话时和下一轮续行一起送达；模型按人的要求 pause 后不再续。"""
    log: list[str] = []

    def handler(call: ScriptCall) -> str:
        log.append(call.prompt)
        if call.turn == 1:
            call.use("create_goal", {}, result=goal_json("active"))
            net.send_message(dm.id, "先停一下", actor=alice)  # 这一轮还在跑时人插话
            return "开始"
        call.use("update_goal", {"action": "pause"}, result=goal_json("paused"))
        return "好的，先停下"
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "go", background=False, actor=alice)
    net.wait_quiet()
    assert len(log) == 2 and "先停一下" in log[1] and "接着推进你的目标" in log[1]
    assert dm.seats[a.id].goal["phase"] == "paused"


def test_task_goal_delivers_only_when_done(tmp_path: Path):
    """委托里的目标：中间几轮只记进展，目标完成那一轮才交付给委托方。"""
    ids: dict[str, str] = {}

    def handler(call: ScriptCall) -> str:
        if call.agent_id == ids.get("worker"):
            return goal_agent(3)(call)
        if call.turn == 1:
            call.tool("delegate", agent="Worker", mission="写报告", expected_outcome="三节报告")
            return "委托了"
        return "收到"
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    lead = net.create_agent("Lead", "l", [], ["delegate"], actor=alice)
    worker = net.create_agent("Worker", "w", [], [], actor=alice)
    ids["worker"] = worker.id
    g = net.create_project("G", "", [lead.id, worker.id], actor=alice)
    net.send_message(net.project_chat(g.id, actor=alice).id, "@Lead 安排", background=False, actor=alice)
    t = next(iter(net.store.tasks.values()))
    assert t.status in (TaskStatus.IN_REVIEW, TaskStatus.COMPLETED)
    assert t.result["report"] == "三节都写完了" and t.notes == ["第 1 节写好了", "第 2 节写好了"]
    assert t.goal["phase"] == "complete"


def test_goal_result_parsing():
    assert _goal_of(goal_json("active", 4)) == {"id": "goal-1", "objective": "写完三节报告", "phase": "active", "max": 4}
    assert _goal_of('{"goal":null}') == {} and _goal_of("Error: nope") is None
    cut = goal_json("complete", 3)[:-25]  # 截断了，按字段名取
    assert _goal_of(cut)["phase"] == "complete"


def test_prompt_mentions_goals_only_if_runtime_has_them(tmp_path: Path):
    for caps, expect in ((RuntimeCapabilities.all_enforced("scripted"), True),
                         (RuntimeCapabilities.all_enforced("scripted", goals=False), False)):
        seen: list[str] = []
        net = Network(ScriptedRuntime(lambda c: seen.append(c.prompt) or "ok", caps), tmp_path / str(expect), seed=False)
        alice, = users(net, "alice@x.io")
        a = net.create_agent("A", "a", [], [], actor=alice)
        net.send_message(net.direct_chat(a.id, actor=alice).id, "hi", background=False, actor=alice)
        assert ("create_goal" in seen[0]) is expect


def test_overlay_mounts_jobs_keeper():
    y = build_patch_yaml([])
    assert "id: matrix-jobs" in y and json.dumps(str(JOBS_PLUGIN)) in y and "maxNudges: 3" in y
    src = JOBS_PLUGIN.read_text(encoding="utf-8")
    # 空闲时还有自己名下在跑的后台任务：追加一条消息让它收完（headless 退出会结束它们）
    assert '"agent/status"' in src and "followup" in src and "job_output" in src and "job_kill" in src
