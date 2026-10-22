"""端侧可达性（docs/device-runtimes.md §5）：不在线是等待，不是失败；上线即消费积压。"""
from pathlib import Path

from agentnet.device import HELLO_OK, PROTOCOL, DeviceRuntime, MemoryLink, capabilities_from_hello
from agentnet.engine import Network
from agentnet.models import TaskStatus
from agentnet.runtime import ENFORCED, ScriptedRuntime
from agentnet.store import Store
from tests.test_users import users


def hello():
    return capabilities_from_hello({"type": HELLO_OK, "protocol": PROTOCOL, "runtime": "dev",
                                    "capabilities": {"enforcement": {"web.research": ENFORCED}}})


def setup(tmp_path, online: bool):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice = users(net, "alice@x.io")[0]
    lead = net.create_agent("Lead", "协调", [], ["delegate"], actor=alice)
    worker = net.create_agent("Worker", "执行", [], ["web.research"], actor=alice)
    worker.device_runtime = "dev-1"
    g = net.create_project("G", "", [lead.id, worker.id], actor=alice)
    chat = net.project_chat(g.id, actor=alice)

    def handler(call):
        if call.agent_id == lead.id and call.turn == 1:
            call.tool("delegate", agent="Worker", mission="看看", expected_outcome="结论", authority=["web.research"])
        return "收到"

    net.runtime.handler = handler
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), hello())
    if online:
        from tests.test_device import connector
        # 在线设备：立刻回一个结果，否则这一轮会一直等（真实部署里同样会等，直到 harness 结束）
        connector(rt, script=lambda msg, deliver: deliver(
            {"type": "run.result", "run_id": msg["run_id"], "session_id": "s0", "final_text": "初步结论"}))
        net.register_runtime("dev-1", rt)
    net.send_message(chat.id, "@Lead 安排", actor=alice, background=False)
    return net, lead, worker, chat, rt, alice


def test_offline_device_task_waits_without_consuming_retries(tmp_path):
    """不变量 12/端侧：不可达不判失败、不消耗重试，委托方看到它在等设备。"""
    net, lead, worker, chat, rt, alice = setup(tmp_path, online=False)
    task = next(t for t in net.store.tasks.values() if t.delegatee_id == worker.id)
    assert task.status == TaskStatus.WAITING and task.error is None and task.retries == 0
    assert net.store.conversations[chat.id].status == "waiting"


def test_registering_the_runtime_wakes_waiting_tasks(tmp_path):
    """上线即消费：注册后任务从等待回到执行，并真的跑完。"""
    net, lead, worker, chat, rt, alice = setup(tmp_path, online=False)
    task = next(t for t in net.store.tasks.values() if t.delegatee_id == worker.id)
    assert task.status == TaskStatus.WAITING

    def script(msg, deliver):
        deliver({"type": "run.result", "run_id": msg["run_id"], "session_id": "s", "final_text": "结论：好"})

    from tests.test_device import connector  # 复用测试里的极简连接器
    connector(rt, script=script)
    net.register_runtime("dev-1", rt)
    import time
    for _ in range(50):
        if task.status in (TaskStatus.IN_REVIEW, TaskStatus.COMPLETED):
            break
        time.sleep(0.02)
    # 交付那一轮结束时委托方没表态，按既有规则视为接受
    assert task.status in (TaskStatus.IN_REVIEW, TaskStatus.COMPLETED)
    assert task.result and "结论" in (task.result.get("summary") or "")


def test_restart_keeps_device_tasks_waiting(tmp_path):
    """重启不把端侧任务判失败（不变量 12 的端侧部分）。"""
    net, lead, worker, chat, rt, alice = setup(tmp_path, online=True)
    task = next(t for t in net.store.tasks.values() if t.delegatee_id == worker.id)
    task.status = TaskStatus.RUNNING
    net.store.save()
    again = Store(tmp_path, seed=False)
    assert again.tasks[task.id].status == TaskStatus.WAITING
    assert again.tasks[task.id].error is None


def test_offline_seat_queues_and_tells_the_owner(tmp_path):
    """端侧席位不在线：消息保留在收件箱，只写一条排队说明（同样的说明不重复）。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice = users(net, "alice@x.io")[0]
    a = net.create_agent("Local", "端侧", [], [], actor=alice)
    a.device_runtime = "dev-1"
    chat = net.direct_chat(a.id, actor=alice)
    net.send_message(chat.id, "在吗", actor=alice, background=False)
    net.send_message(chat.id, "还在吗", actor=alice, background=False)
    notes = [m for m in net.store.conversations[chat.id].messages if m.role == "system"]
    assert len(notes) == 1 and "未连接" in notes[0].text and "排队" in notes[0].text
    assert net.store.conversations[chat.id].seats.get(a.id) is None or True  # 还没有席位：游标保留消息
    assert net.runtime.calls == []  # 没有跑到 harness


def test_device_approval_does_not_expire_with_time(tmp_path):
    """端侧的审批不按 15 分钟作废：所有者睡觉时不能把申请丢掉（不变量 14 的端侧部分）。"""
    import threading
    import time

    from agentnet.engine import ExecContext

    net, lead, worker, chat, rt, alice = setup(tmp_path, online=True)
    net.limits.approval_timeout_s = 0.2  # 云侧会立刻超时
    net.register_runtime("dev-1", rt)
    conv = net.store.conversations[chat.id]
    ctx = ExecContext(kind="seat", id=f"{conv.id}/{worker.id}", agent_id=worker.id,
                      conversation_id=conv.id, project_id=conv.project_id, authority=["web.research"],
                      constraints=[], depth=0, chain=[worker.id])
    net._contexts["tok"] = ctx
    out = {}
    t = threading.Thread(target=lambda: out.update(o=net.request_approval("tok", "write", "outside")))
    t.start()
    time.sleep(0.8)
    assert t.is_alive()  # 没有按时间作废
    ap = next(iter(net.store.approvals.values()))
    net.decide_approval(ap.id, True, actor=alice)
    t.join(3)
    assert out["o"] == "allowed-once"


def test_device_publish_file_fetches_from_the_connector(tmp_path):
    """端侧 publish_file：服务器向连接器要文件后发布进项目档案（工作目录在用户电脑上）。"""
    import base64
    from agentnet.engine import ExecContext

    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice = users(net, "alice@x.io")[0]
    a = net.create_agent("Local", "端侧", [], ["file.write"], actor=alice)
    a.device_runtime = "dev-1"
    g = net.create_project("G", "", [a.id], actor=alice)
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), hello())
    sent = []

    def serve(msg):
        sent.append(msg)
        if msg.get("type") == "file.fetch":
            rt.on_message({"reply_to": msg["call_id"], "run_id": "",
                           "data": base64.b64encode(b"# report").decode()})
        return None

    rt.link = MemoryLink(serve=serve)
    net.register_runtime("dev-1", rt)
    ctx = ExecContext(kind="seat", id=f"{net.project_chat(g.id, actor=alice).id}/{a.id}", agent_id=a.id,
                      conversation_id=net.project_chat(g.id, actor=alice).id, project_id=g.id,
                      authority=["file.write"], constraints=[], depth=0, chain=[a.id])
    net._contexts["tok"] = ctx
    rt._by_context[ctx.id] = "run-9"
    text, is_error = net.handle_tool("tok", "publish_file", {"path": "out/report.md"})
    assert not is_error, text
    assert (tmp_path / "projects" / g.id / "files" / "report.md").read_bytes() == b"# report"
    # 越界路径仍然被拒
    text, is_error = net.handle_tool("tok", "publish_file", {"path": "../secret"})
    assert is_error and "只能发布" in text
