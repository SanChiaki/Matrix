"""Agent 在运行中提问（dsh 的 ask_user_question，research: deepseek-harness#user-questions）：
席位里问这一轮交代事情的人；委托里先问委托方 Agent，它决定不了、没回答或跑不起来才转给人。"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from matrix import auth
from matrix.device import HELLO_OK, QUESTION_REQUEST, DeviceRuntime, capabilities_from_hello
from matrix.engine import Limits, Network, NetworkError
from matrix.models import TaskStatus
from matrix.runtime import (QUESTION_PLUGIN, QuestionBridge, RunRequest, RuntimeCapabilities, ScriptCall,
                            ScriptedRuntime, build_patch_yaml)
from matrix.server import create_app

FMT = {"id": "fmt", "question": "报告用什么格式？", "header": "格式",
       "options": [{"label": "Markdown (Recommended)", "description": "方便改"}, {"label": "PDF"}]}


def users(net: Network, *emails: str) -> list[str]:
    return [auth.register(net.store, e, "password-123")[0].id for e in emails]


def client(net: Network, email: str) -> TestClient:
    c = TestClient(create_app(net))
    assert c.post("/api/auth/login", json={"email": email, "password": "password-123"}).status_code == 200
    return c


def when(cond: Callable[[], Any], timeout: float = 5) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = cond()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("条件没有满足")


def answer_when_pending(net: Network, actor: str | None, **kw: Any) -> threading.Thread:
    def run() -> None:
        q = when(lambda: net.pending_questions(actor))[0]
        net.answer_question(q.id, actor=actor, **kw)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


def asking(out: dict[str, Any], *questions: dict[str, Any]) -> Callable[[ScriptCall], str]:
    """第一轮提问并把结果记下；之后的轮次记下提示词。"""
    def handler(call: ScriptCall) -> str:
        if call.turn == 1:
            out["result"] = call.ask(*(questions or (FMT,)))
            return "按回答做完了"
        out.setdefault("later", []).append(call.prompt)
        return "收到"
    return handler


# ================= 席位：问交代事情的人 =================
def test_seat_question_goes_to_the_person_who_asked(tmp_path: Path):
    """不变量 10、13：共享项目里 bob @ alice 的 Agent，它的问题交给 bob（交代事情的人），不是所有者；
    只有 bob 看得到卡片、答得了；回答是工具结果。"""
    out: dict[str, Any] = {}
    net = Network(ScriptedRuntime(asking(out)), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    pub = net.create_agent("Pub", "p", [], [], actor=alice)
    g = net.create_project("J", "联合", [], actor=bob)
    net.invite(g.id, "alice@x.io", actor=bob)
    net.respond_invitation(g.id, True, actor=alice)
    chat = net.project_chat(g.id, actor=bob)
    assert pub.id in g.member_ids
    net.send_message(chat.id, "@Pub 写个报告", actor=bob)
    q = when(lambda: net.pending_questions(bob))[0]
    assert q.agent_id == pub.id and q.target_user_id == bob and q.items[0]["options"][1]["label"] == "PDF"
    assert net.pending_questions(alice) == []
    assert [x["id"] for x in net.conversation_detail(chat.id, bob)["questions"]] == [q.id]
    assert net.conversation_detail(chat.id, alice)["questions"] == []
    assert net.board(bob)["questions"][0]["id"] == q.id
    assert net.event_visible("question.updated", {"question": q.to_dict()}, bob)
    assert not net.event_visible("question.updated", {"question": {**q.to_dict(), "owner_id": "x"}}, alice)
    with pytest.raises(NetworkError, match="unknown question"):
        net.answer_question(q.id, [{"id": "fmt", "selected": ["PDF"]}], actor=alice)
    net.answer_question(q.id, [{"id": "fmt", "selected": ["PDF", "Markdown (Recommended)", "不存在"]}], actor=bob)
    net.wait_quiet()
    assert out["result"] == {"answers": [{"id": "fmt", "selected": ["PDF"]}]}  # 单选只留一个，不认识的选项丢掉
    assert q.status == "answered" and q.answered_by == bob
    record = [m for m in chat.messages if m.role == "event" and "回答：PDF" in m.text]
    assert record and set(record[0].audience) == {alice, bob}
    with pytest.raises(NetworkError, match="回答过了"):
        net.answer_question(q.id, [{"id": "fmt", "selected": ["PDF"]}], actor=bob)


def test_reply_in_chat_answers_the_question(tmp_path: Path):
    """人不用找卡片：在私聊里直接回复就是回答，不再排一轮；下一轮的新消息里也不再出现这条回答。"""
    out: dict[str, Any] = {}
    net = Network(ScriptedRuntime(asking(out)), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "写个报告", actor=alice)
    q = when(lambda: net.pending_questions(alice))[0]
    msg = net.send_message(dm.id, "pdf", actor=alice)
    net.wait_quiet()
    assert msg.answers == q.id
    assert out["result"] == {"answers": [{"id": "fmt", "selected": ["PDF"]}]}  # 文字正好是选项：按选中
    assert "later" not in out  # 没有为这条回答再跑一轮
    net.send_message(dm.id, "再加一节", background=False, actor=alice)
    assert "再加一节" in out["later"][0] and "pdf" not in out["later"][0]


def test_reply_without_mention_in_group_answers(tmp_path: Path):
    """群聊里回答不必 @：没 @ 任何人的回复交给等这个人回答的问题（原来没 @ 就不会唤醒，Agent 卡住）。"""
    out: dict[str, Any] = {}
    net = Network(ScriptedRuntime(asking(out, {"id": "who", "question": "给谁看？"})), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    g = net.create_project("G", "", [a.id], actor=alice)
    chat = net.project_chat(g.id, actor=alice)
    net.send_message(chat.id, "@A 写个报告", actor=alice)
    when(lambda: net.pending_questions(alice))
    net.send_message(chat.id, "给管理层看，一页以内", actor=alice)
    net.wait_quiet()
    assert out["result"] == {"answers": [{"id": "who", "selected": [], "custom": "给管理层看，一页以内"}]}


def test_decline_timeout_and_end_of_run(tmp_path: Path):
    """不回答、等待超时：模型拿到“没有得到回答”，不会被当成同意；坏令牌直接拒绝。"""
    out: dict[str, Any] = {}
    net = Network(ScriptedRuntime(asking(out)), tmp_path, Limits(question_timeout_s=0.3), seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "go", background=False, actor=alice)
    assert "没有得到回答" in out["result"]["error"] and "不要当成同意" in out["result"]["error"]
    q = next(iter(net.store.questions.values()))
    assert q.status == "cancelled" and "没有人回答" in q.note
    assert net.request_question("bad-token", [FMT]) == {"error": "执行上下文无效或已结束"}

    out2: dict[str, Any] = {}
    net2 = Network(ScriptedRuntime(asking(out2)), tmp_path / "2", seed=False)
    bob, = users(net2, "bob@x.io")
    b = net2.create_agent("B", "b", [], [], actor=bob)
    th = answer_when_pending(net2, bob, decline=True, note="自己定")
    net2.send_message(net2.direct_chat(b.id, actor=bob).id, "go", background=False, actor=bob)
    th.join(5)
    assert "bob@x.io 不回答：自己定" in out2["result"]["error"]


def test_answer_validation(tmp_path: Path):
    out: dict[str, Any] = {}
    multi = {"id": "secs", "question": "要哪几节？", "multi_select": True,
             "options": [{"label": "背景"}, {"label": "结论"}, {"label": "附录"}]}
    net = Network(ScriptedRuntime(asking(out, multi, {"id": "", "question": "截止？"}, {"question": " "})),
                  tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    net.send_message(net.direct_chat(a.id, actor=alice).id, "go", actor=alice)
    q = when(lambda: net.pending_questions(alice))[0]
    assert [i["id"] for i in q.items] == ["secs", "q2"]  # 空问题丢掉，缺 id 的补上
    with pytest.raises(NetworkError, match="至少回答一个"):
        net.answer_question(q.id, [], actor=alice)
    net.answer_question(q.id, [{"id": "secs", "selected": ["结论", "背景"], "custom": "再加摘要"}], actor=alice)
    net.wait_quiet()
    assert out["result"]["answers"] == [{"id": "secs", "selected": ["结论", "背景"], "custom": "再加摘要"},
                                        {"id": "q2", "selected": []}]  # 没答的项按跳过


# ================= 委托：先问委托方，决定不了才给人 =================
def _delegation(tmp_path: Path, mode: str, limits: Limits | None = None):
    """Lead（席位）委托 Worker；Worker 做事时提问。mode：委托方收到问题时 answer / escalate / ignore。"""
    seen: dict[str, Any] = {"lead_prompts": []}
    ids: dict[str, str] = {}

    def handler(call: ScriptCall) -> str:
        if call.agent_id == ids.get("worker"):
            if call.turn == 1:
                seen["result"] = call.ask(FMT)
            return f"交付：{json.dumps(seen['result'], ensure_ascii=False)}"
        seen["lead_prompts"].append(call.prompt)
        m = re.search(r"question_id: (q-[0-9a-f]{8})", call.prompt)
        if m:
            if mode == "answer":
                seen["tool"] = call.tool("answer_question", question_id=m.group(1), answer="PDF")
            elif mode == "escalate":
                seen["tool"] = call.tool("escalate_question", question_id=m.group(1), note="我倾向 PDF")
            return "处理了问题"
        if call.turn == 1:
            call.tool("delegate", agent="Worker", mission="写报告", expected_outcome="报告")
            return "委托了"
        return "收到"

    net = Network(ScriptedRuntime(handler), tmp_path, limits, seed=False)
    alice, = users(net, "alice@x.io")
    lead = net.create_agent("Lead", "lead", [], ["delegate"], actor=alice)
    worker = net.create_agent("Worker", "w", [], [], actor=alice)
    ids.update(lead=lead.id, worker=worker.id)
    g = net.create_project("G", "", [lead.id, worker.id], actor=alice)
    chat = net.project_chat(g.id, actor=alice)
    return net, alice, chat, seen


def test_task_question_answered_by_delegator(tmp_path: Path):
    """委托里的问题先交给委托方 Agent：它在通知里看到问题，用 answer_question 回答，执行者在原来的调用里拿到。"""
    net, alice, chat, seen = _delegation(tmp_path, "answer")
    net.send_message(chat.id, "@Lead 安排写报告", background=False, actor=alice)
    net.wait_quiet()
    assert seen["result"] == {"answers": [{"id": "fmt", "selected": ["PDF"]}]}
    assert "已回答" in seen["tool"]
    notice = next(p for p in seen["lead_prompts"] if re.search(r"question_id: q-", p))
    assert "报告用什么格式" in notice and "Markdown (Recommended)" in notice and "escalate_question" in notice
    q = next(iter(net.store.questions.values()))
    assert q.status == "answered" and q.answered_by == net.store.agent_by_name("Lead").id and q.target_user_id is None
    assert net.pending_questions(alice) == []
    t = next(iter(net.store.tasks.values()))
    assert t.status in (TaskStatus.IN_REVIEW, TaskStatus.COMPLETED) and "PDF" in t.result["report"]


def test_delegator_escalates_to_the_human(tmp_path: Path):
    """委托方决定不了：escalate_question 转给委托链起点那个席位交代事情的人，带上委托方的补充。"""
    net, alice, chat, seen = _delegation(tmp_path, "escalate")
    th = answer_when_pending(net, alice, answers=[{"id": "fmt", "selected": [], "custom": "Word"}])
    net.send_message(chat.id, "@Lead 安排写报告", background=False, actor=alice)
    th.join(5)
    net.wait_quiet()
    assert seen["result"] == {"answers": [{"id": "fmt", "selected": [], "custom": "Word"}]}
    q = next(iter(net.store.questions.values()))
    assert q.answered_by == alice and "决定不了" in q.escalated and "我倾向 PDF" in q.escalated
    assert "转给 alice@x.io" in seen["tool"]


def test_unanswered_question_escalates_after_delegators_turn(tmp_path: Path):
    """委托方这一轮结束了既没回答也没转：网络自动转给人，执行者不会一直卡着。"""
    net, alice, chat, seen = _delegation(tmp_path, "ignore")
    th = answer_when_pending(net, alice, answers=[{"id": "fmt", "selected": ["PDF"]}])
    net.send_message(chat.id, "@Lead 安排写报告", background=False, actor=alice)
    th.join(5)
    net.wait_quiet()
    q = next(iter(net.store.questions.values()))
    assert q.escalated == "委托方这一轮没有回答" and q.answered_by == alice
    assert seen["result"] == {"answers": [{"id": "fmt", "selected": ["PDF"]}]}


def test_only_the_delegator_context_can_answer(tmp_path: Path):
    """不变量 10：answer_question 只认问题送到的那个委托方上下文（按令牌），别的 Agent 拿到 id 也答不了。"""
    net, alice, chat, seen = _delegation(tmp_path, "ignore")
    net.send_message(chat.id, "@Lead 安排写报告", actor=alice)
    q = when(lambda: net.pending_questions(alice))[0]  # 已经自动转给人
    from matrix.engine import ExecContext
    ctx = ExecContext(kind="seat", id=f"{chat.id}/{net.store.agent_by_name('Lead').id}",
                      agent_id=net.store.agent_by_name("Lead").id, conversation_id=chat.id, project_id=chat.project_id,
                      authority=["delegate"], constraints=[], depth=0, chain=[])
    with pytest.raises(NetworkError, match="没有等你回答的问题"):
        net._tool_answer_question(ctx, {"question_id": q.id, "answer": "PDF"})
    net.answer_question(q.id, [{"id": "fmt", "selected": ["PDF"]}], actor=alice)
    net.wait_quiet()


def test_delegator_that_cannot_run_hands_over_to_the_human(tmp_path: Path):
    """委托方的席位上一轮失败、停着：问题不等它，直接转给人。"""
    net, alice, chat, seen = _delegation(tmp_path, "answer")
    lead = net.store.agent_by_name("Lead")

    real = net.runtime.handler

    def handler(call: ScriptCall) -> str:
        if call.agent_id == lead.id and call.turn == 1:
            out = real(call)
            chat.seats[lead.id].blocked = "测试：委托方停着"
            return out
        return real(call)
    net.runtime.handler = handler
    th = answer_when_pending(net, alice, answers=[{"id": "fmt", "selected": ["Markdown (Recommended)"]}])
    net.send_message(chat.id, "@Lead 安排写报告", background=False, actor=alice)
    th.join(5)
    net.wait_quiet()
    q = next(iter(net.store.questions.values()))
    assert q.escalated == "委托方这时跑不起来" and q.answered_by == alice


# ================= 运行时、端侧、重启 =================
def test_patch_mounts_ask_tool_and_answerer():
    y = build_patch_yaml(["file.write"], questions=QuestionBridge(url="http://127.0.0.1:1/internal/question",
                                                                  token="t", handler=lambda q: {}))
    assert "id: tool-ask-user" in y and '"@deepseek-ai/dsh-tool-ask-user"' in y
    assert "id: matrix-questions" in y and json.dumps(str(QUESTION_PLUGIN)) in y and '"t"' in y
    assert "tool-ask-user" not in build_patch_yaml(["file.write"])  # 没有提问通道就不挂提问工具
    src = QUESTION_PLUGIN.read_text(encoding="utf-8")
    assert "user-questions/request" in src and "throw new Error" in src  # 没得到回答时报错，不当成回答


def test_prompt_tells_when_to_ask_only_if_runtime_can(tmp_path: Path):
    prompts: list[str] = []

    def handler(call: ScriptCall) -> str:
        prompts.append(call.prompt)
        return "ok"
    for caps, expect in ((RuntimeCapabilities.all_enforced("scripted"), True),
                         (RuntimeCapabilities.all_enforced("scripted", questions=False), False)):
        prompts.clear()
        net = Network(ScriptedRuntime(handler, caps), tmp_path / str(expect), seed=False)
        alice, = users(net, "alice@x.io")
        a = net.create_agent("A", "a", [], [], actor=alice)
        net.send_message(net.direct_chat(a.id, actor=alice).id, "hi", background=False, actor=alice)
        assert ("ask_user_question" in prompts[0]) is expect


def test_device_relays_question_request():
    """端侧：连接器发来的 question.request 只交给这一轮的提问通道（按 run_id 认），旧客户端不报告就当不能提问。"""
    sent: list[dict[str, Any]] = []

    class Link:
        def send(self, msg: dict[str, Any]) -> None:
            sent.append(msg)

    rt = DeviceRuntime(Link(), capabilities_from_hello({"type": HELLO_OK, "runtime": "dsh", "capabilities": {"name": "dsh"}}))
    assert rt.capabilities().questions is False
    got: list[Any] = []
    req = RunRequest(agent_id="a", context_id="c", prompt="p", cwd=Path("."), authority=[],
                     questions=QuestionBridge(url="", token="t", handler=lambda q: got.append(q) or {"answers": []}))
    with rt._lock:
        rt._reqs["run-1"] = req
    reply = rt.on_message({"type": QUESTION_REQUEST, "run_id": "run-1", "call_id": "c1", "questions": [FMT]})
    assert reply == {"reply_to": "c1", "answers": []} and got == [[FMT]]
    assert rt.on_message({"type": QUESTION_REQUEST, "run_id": "nope", "call_id": "c2", "questions": [FMT]}) == \
        {"reply_to": "c2", "error": "这一轮不能提问"}


def test_pending_question_cancelled_on_restart(tmp_path: Path):
    from matrix.models import Question
    from matrix.store import Store
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    net.store.questions["q-1"] = Question(id="q-1", agent_id="a", owner_id=None, context_kind="seat",
                                          context_id="c/a", conversation_id="c", items=[FMT])
    net.store.save()
    q = Store(tmp_path, seed=False).questions["q-1"]
    assert q.status == "cancelled" and "重启" in q.note


def test_question_api(tmp_path: Path):
    out: dict[str, Any] = {}
    net = Network(ScriptedRuntime(asking(out)), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    ca, cb = client(net, "alice@x.io"), client(net, "bob@x.io")
    dm = net.direct_chat(a.id, actor=alice)
    ca.post(f"/api/conversations/{dm.id}/messages", json={"text": "go"})
    qid = when(lambda: ca.get("/api/board").json()["questions"])[0]["id"]
    assert ca.get("/api/network").json()["questions"][0]["id"] == qid
    assert cb.post(f"/api/questions/{qid}", json={"answers": [{"id": "fmt", "selected": ["PDF"]}]}).status_code == 404
    r = ca.post(f"/api/questions/{qid}", json={"answers": [{"id": "fmt", "custom": "两种都要"}]})
    assert r.status_code == 200 and r.json()["status"] == "answered"
    net.wait_quiet()
    assert out["result"] == {"answers": [{"id": "fmt", "selected": [], "custom": "两种都要"}]}
    assert TestClient(create_app(net)).post("/internal/question", json={"token": "bad", "questions": [FMT]}).json() == \
        {"error": "执行上下文无效或已结束"}
