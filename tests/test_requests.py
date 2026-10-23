"""对话内代验收：消息授权、席位边界与持久化。"""
import json
import re

import pytest

from matrix.engine import Network, NetworkError
from matrix.models import Task, TaskStatus, Delegation
from matrix.runtime import ScriptedRuntime
from matrix.store import Store
from tests.test_users import users, joint, client


def setup_request(tmp_path, *, dm=False):
    out = []
    def handler(call):
        if call.turn == 1:
            text = call.tool("open_request", title="报告", expected_outcome="报告文件")
            rid = re.search(r"req-[0-9a-f]{8}", text).group()
            call.tool("deliver_request", request_id=rid, summary="报告完成")
        else:
            out.append(call.tool("accept_request", request_id=next(iter(net.store.requests))))
        return "ok"
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "", [], [], actor=alice)
    if dm:
        chat = net.direct_chat(a.id, actor=alice)
        requester = alice
    else:
        _, chat = joint(net, alice, "bob@x.io", bob, [a.id])
        requester = bob
    net.send_message(chat.id, "@A 写报告", actor=requester, background=False)
    return net, chat, next(iter(net.store.requests.values())), alice, requester, out


@pytest.mark.parametrize("dm", [False, True])
def test_accept_request_from_current_requester(tmp_path, dm):
    """不变量 10：当前轮下发人的消息授权，群聊与私聊均推送完成状态并留审计。"""
    net, chat, r, alice, requester, out = setup_request(tmp_path, dm=dm)
    net.send_message(chat.id, "@A 通过了", actor=requester, background=False)
    msg = next(m for m in chat.messages if m.text == "@A 通过了")
    assert r.status == "completed" and r.finished_at and r.accepted_by_message_id == msg.id
    assert r.reviewed_by == requester
    assert net.board(requester)["requests"][0]["status"] == "completed"
    assert any(e.type == "request.updated" and e.data["request"]["status"] == "completed" for e in net.bus.since(0))
    assert any("A 按" in m.text and "的指示验收了" in m.text for m in chat.messages if m.role == "event")
    assert Store(tmp_path, seed=False).requests[r.id].accepted_by_message_id == msg.id


def test_accept_request_rejects_other_or_previous_messages(tmp_path):
    """不变量 10：所有者不是下发人；未 @ 的消息与上一轮消息均不能授权。"""
    net, chat, r, alice, requester, out = setup_request(tmp_path)
    net.send_message(chat.id, "@A 通过了", actor=alice, background=False)
    assert "下发的人没有在这一轮消息里表示验收" in out[-1]
    net.send_message(chat.id, "通过了", actor=requester, background=False)
    net.send_message(chat.id, "@A 继续", actor=alice, background=False)
    assert "下发的人没有在这一轮消息里表示验收" in out[-1]
    assert r.status == "in_review" and r.accepted_by_message_id is None
    # 原始任务消息已在第一轮送达，不能在后续轮次复用。
    assert r.message_id not in [m.id for m in chat.messages if m.text == "@A 继续"]


@pytest.mark.parametrize("status", ["in_progress", "completed", "cancelled"])
def test_accept_request_requires_in_review(tmp_path, status):
    net, chat, r, _, requester, out = setup_request(tmp_path)
    r.status = status
    net.send_message(chat.id, "@A 通过了", actor=requester, background=False)
    assert "只有已交付" in out[-1] and r.status == status


@pytest.mark.parametrize("field", ["agent_id", "conversation_id", "id"])
def test_accept_request_requires_own_seat(tmp_path, field):
    net, chat, r, _, requester, out = setup_request(tmp_path)
    if field == "id":
        net.store.requests = {"missing": r}
        r.id = "missing"
        net.runtime.handler = lambda call: out.append(call.tool("accept_request", request_id="absent")) or "ok"
    else:
        setattr(r, field, "other")
    net.send_message(chat.id, "@A 通过了", actor=requester, background=False)
    assert "不存在，或不是你在这个对话里接下的" in out[-1]
    assert r.status == "in_review"


def test_accept_request_requires_reported_delegations(tmp_path):
    net, chat, r, _, requester, out = setup_request(tmp_path)
    net.store.tasks["pending"] = Task(id="pending", conversation_id=chat.id, project_id=chat.project_id,
        delegator_id=r.agent_id, delegatee_id=r.agent_id, parent_task_id=None, depth=1,
        delegation=Delegation(mission="m", authority=[], constraints=[], expected_outcome="o"),
        status=TaskStatus.RUNNING)
    r.task_ids.append("pending")
    net.send_message(chat.id, "@A 通过了", actor=requester, background=False)
    assert "还有委托没有回报" in out[-1] and r.status == "in_review"


def test_request_manual_review_and_legacy_state(tmp_path):
    net, chat, r, alice, requester, _ = setup_request(tmp_path)
    with pytest.raises(NetworkError, match="只有下发"):
        net.review_request(r.id, True, actor=alice)
    response = client(net, "bob@x.io").post(f"/api/requests/{r.id}/review", json={"accept": True})
    assert response.status_code == 200 and response.json()["status"] == "completed"
    assert r.reviewed_by == requester and r.accepted_by_message_id is None
    assert any("bob@x.io 验收了" in m.text for m in chat.messages)
    raw = json.loads(net.store.path.read_text(encoding="utf-8"))
    for item in raw["requests"]:
        item.pop("reviewed_by")
        item.pop("accepted_by_message_id")
        item["obsolete"] = True
    net.store.path.write_text(json.dumps(raw), encoding="utf-8")
    old = Store(tmp_path, seed=False).requests[r.id]
    assert old.reviewed_by is None and old.accepted_by_message_id is None


def test_accept_request_rejects_no_current_requester_message(tmp_path):
    """上一轮已经送达的明确验收消息不能复用；纯通知轮也不能自行验收。"""
    net, chat, r, _, requester, out = setup_request(tmp_path)
    handler = net.runtime.handler
    net.runtime.handler = lambda call: "收到，尚未验收"
    net.send_message(chat.id, "@A 通过了", actor=requester, background=False)
    net.runtime.handler = handler
    # 模拟委托通知唤醒后的运行：没有新的人的消息。
    net._seat_round(chat, r.agent_id)
    assert "下发的人没有在这一轮消息里表示验收" in out[-1]
    assert r.status == "in_review" and r.accepted_by_message_id is None


@pytest.mark.parametrize("text", ["还没好吗", "继续改", "再看看"])
def test_accept_request_prompt_requires_explicit_acceptance(tmp_path, text):
    """语义交给模型：确定性运行时模拟遵循提示词，对催促、返工和含糊消息不调用。"""
    net, chat, r, _, requester, out = setup_request(tmp_path)
    def cautious(call):
        from matrix.protocol import _requests
        prompt = "\n".join(_requests())
        assert "只有明确说通过、可以了、就这样、验收才调用" in prompt
        assert "含糊、催促、继续改都不算" in prompt
        return "尚未验收"
    net.runtime.handler = cautious
    net.send_message(chat.id, f"@A {text}", actor=requester, background=False)
    assert r.status == "in_review" and out == []
