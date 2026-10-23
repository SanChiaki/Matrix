"""端侧连接器协议（docs/device-runtimes.md 第 2 节）：一轮的收发、工具回传、审批上行、能力握手。"""
import threading

from matrix.device import (APPROVAL_REQUEST, HELLO_OK, PROTOCOL, RUN_EVENT, RUN_REQUEST, RUN_RESULT, TOOL_CALL,
                             DeviceError, DeviceRuntime, MemoryLink, capabilities_from_hello)
from matrix.runtime import ADVISORY, ENFORCED, RunRequest, RuntimeCapabilities, ToolBridge


def hello(runtime="fake-harness", **caps):
    flat = {"name": runtime, "enforcement": {"web.research": ENFORCED, "file.write": ADVISORY}}
    flat.update(caps)
    return capabilities_from_hello({"type": HELLO_OK, "protocol": PROTOCOL, "runtime": runtime, "capabilities": flat})


def req(**kw):
    base = dict(agent_id="agent-1", context_id="conv-1", prompt="hi", cwd=None, authority=["web.research"])
    base.update(kw)
    return RunRequest(**base)


def connector(rt: DeviceRuntime, on_tool=None, on_approval=None, on_event=None, script=None):
    """一个极简的连接器：收到 run.request 就按 script 回消息。"""
    calls = []

    def serve(msg):
        calls.append(msg)
        if msg["type"] == RUN_REQUEST:
            if script is not None:
                script(msg, deliver)
            else:
                deliver({"type": RUN_RESULT, "run_id": msg["run_id"], "session_id": "s1", "final_text": "done"})
        return None

    def deliver(msg):
        reply = rt.on_message(msg, on_tool=on_tool, on_approval=on_approval, on_event=on_event)
        if reply is not None:
            calls.append(reply)
        return reply

    link = MemoryLink(serve=serve)
    link.on_incoming = lambda m: rt.on_message(m)
    rt.link = link
    return calls


# ================= 一轮 =================
def test_run_round_trip():
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), hello())
    calls = connector(rt)
    res = rt.run(req(session_id=None, tools=ToolBridge(url="", token="t", handler=lambda n, a: ("", False),
                                                       names=["delegate", "list_members"])))
    assert res.ok and res.final_text == "done" and res.session_id == "s1"
    sent = calls[0]
    assert sent["type"] == RUN_REQUEST and sent["agent_id"] == "agent-1" and sent["context_id"] == "conv-1"
    assert sent["tool_names"] == ["delegate", "list_members"]
    assert sent["workdir_ref"] == "agent-1/conv-1"  # 席位续接时同一引用


def test_run_reports_transport_failure_as_retryable_class():
    """连接器不在线：不是任务失败，错误分类要能被上层识别为传输问题。"""

    def boom(msg):
        raise DeviceError("offline")

    rt = DeviceRuntime(MemoryLink(serve=boom), hello())
    res = rt.run(req())
    assert not res.ok and "TRANSPORT" in (res.error or "") and res.error_class == "provider_connection"


def test_run_times_out_and_cancels():
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), hello(), now=lambda: 1.0)
    calls = connector(rt, script=lambda msg, deliver: None)  # 永远不回
    res = rt.run(req(timeout_s=0.05))
    assert not res.ok and "TIMEOUT" in (res.error or "")
    assert calls[-1]["type"] == "cancel" and calls[-1]["run_id"].startswith("run-")


def test_run_without_timeout_waits_and_sends_limit():
    """超时默认不限制：一直等到结果；超时设置随 run.request 发给连接器，端侧 harness 自己执行。"""
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), hello())
    pending = {}
    calls = connector(rt, script=lambda msg, deliver: pending.update(msg=msg, deliver=deliver))
    box = {}
    t = threading.Thread(target=lambda: box.update(res=rt.run(req())))
    t.start()
    t.join(0.2)
    assert t.is_alive() and calls[0]["timeout_s"] is None
    pending["deliver"]({"type": RUN_RESULT, "run_id": pending["msg"]["run_id"], "final_text": "late"})
    t.join(2)
    assert box["res"].ok and box["res"].final_text == "late"
    calls = connector(rt)
    rt.run(req(timeout_s=30))
    assert calls[0]["timeout_s"] == 30


def test_stop_cancels_device_run():
    """人终止：网络立刻结束这一轮并通知连接器取消，不等端侧回结果。"""
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), hello())
    calls = connector(rt, script=lambda msg, deliver: None)  # 永远不回
    stop = threading.Event()
    box = {}
    t = threading.Thread(target=lambda: box.update(res=rt.run(req(cancel=stop))))
    t.start()
    t.join(0.1)
    stop.set()
    t.join(3)
    assert not box["res"].ok and "STOPPED" in box["res"].error
    assert calls[-1]["type"] == "cancel"


def test_disconnect_releases_pending_runs():
    """连接断开：等结果的轮次立刻以传输错误结束（引擎据此让端侧任务回到等待），不靠超时。"""
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), hello())
    connector(rt, script=lambda msg, deliver: None)  # 永远不回
    box = {}
    t = threading.Thread(target=lambda: box.update(res=rt.run(req())))
    t.start()
    t.join(0.1)
    rt.abort_all()
    t.join(2)
    res = box["res"]
    assert not res.ok and "TRANSPORT" in res.error and res.error_class == "provider_connection"


def test_events_are_streamed_to_the_engine():
    seen = []
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), hello())
    connector(rt, on_event=seen.append,
              script=lambda msg, deliver: (
                  deliver({"type": RUN_EVENT, "run_id": msg["run_id"], "event": {"type": "text", "text": "think"}}),
                  deliver({"type": RUN_RESULT, "run_id": msg["run_id"], "final_text": "ok"})))
    rt.run(req())
    assert seen == [{"type": "text", "text": "think"}]


# ================= 工具与审批 =================
def test_tool_calls_go_back_through_the_network():
    """不变量 10：连接器不提供身份，工具调用交给网络按执行上下文令牌处理。"""
    got = []

    def on_tool(name, args):
        got.append((name, args))
        return ("已受理", False)

    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), hello())
    calls = connector(rt, on_tool=on_tool,
                      script=lambda msg, deliver: (
                          deliver({"type": TOOL_CALL, "run_id": msg["run_id"], "call_id": "c1",
                                   "name": "delegate", "args": {"agent": "B", "mission": "m"}}),
                          deliver({"type": RUN_RESULT, "run_id": msg["run_id"], "final_text": "ok"})))
    rt.run(req())
    assert got == [("delegate", {"agent": "B", "mission": "m"})]
    reply = next(c for c in calls if isinstance(c, dict) and c.get("reply_to") == "c1")
    assert reply["text"] == "已受理" and reply["is_error"] is False


def test_approval_requests_are_relayed_not_auto_approved():
    """不变量 14：连接器不自行批准；网络把申请交给所有者，这里只转达结果。"""
    asked = []

    def on_approval(tool, reason):
        asked.append((tool, reason))
        return "rejected"

    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), hello())
    calls = connector(rt, on_approval=on_approval,
                      script=lambda msg, deliver: (
                          deliver({"type": APPROVAL_REQUEST, "run_id": msg["run_id"], "call_id": "a1",
                                   "tool": "write", "reason": "写 /etc/hosts"}),
                          deliver({"type": RUN_RESULT, "run_id": msg["run_id"], "final_text": "ok"})))
    rt.run(req())
    assert asked == [("write", "写 /etc/hosts")]
    reply = next(c for c in calls if isinstance(c, dict) and c.get("reply_to") == "a1")
    assert reply["outcome"] == "rejected"


def test_tool_and_approval_without_handlers_are_refused():
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), hello())
    assert rt.on_message({"type": TOOL_CALL, "call_id": "c1", "name": "delegate"})["is_error"] is True
    assert rt.on_message({"type": APPROVAL_REQUEST, "call_id": "a1"})["outcome"] == "unavailable"


# ================= 握手 =================
def test_hello_unknown_authorities_are_dropped():
    info = capabilities_from_hello({"type": HELLO_OK, "protocol": PROTOCOL, "runtime": "x",
                                    "capabilities": {"enforcement": {"delegate": ENFORCED, "root.everything": ENFORCED}}})
    assert info.capabilities.placement == "device"
    assert info.capabilities.enforces("delegate")
    assert "root.everything" not in info.capabilities.enforcement


def test_hello_rejects_protocol_mismatch():
    try:
        capabilities_from_hello({"type": HELLO_OK, "protocol": PROTOCOL + 1})
    except DeviceError as e:
        assert "protocol mismatch" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected DeviceError")


def test_capabilities_default_to_nothing_enforced():
    """连接器没声明的东西一律按不可用处理：不能因为"没说不能"就认为可以。"""
    caps = capabilities_from_hello({"type": HELLO_OK, "runtime": "x"}).capabilities
    assert not caps.enforces("web.research") and caps.sandbox == "none" and caps.approval == "none"
    assert isinstance(caps, RuntimeCapabilities)


# ================= 引擎接线 =================
def test_network_routes_an_agent_to_its_device_runtime(tmp_path):
    """绑定 device_runtime 的 Agent 走连接器；未绑定的仍走默认运行时。"""
    from matrix.engine import Network
    from matrix.runtime import ScriptedRuntime
    from tests.test_users import users

    default = ScriptedRuntime(lambda call: "cloud")
    net = Network(default, tmp_path, seed=False)
    alice = users(net, "alice@x.io")[0]
    cloud = net.create_agent("Cloud", "云侧", [], ["web.research"], actor=alice)
    local = net.create_agent("Local", "端侧", [], ["web.research"], actor=alice)
    local.device_runtime = "laptop-1"

    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), hello("fake-harness"))
    connector(rt, script=lambda msg, deliver: deliver(
        {"type": RUN_RESULT, "run_id": msg["run_id"], "session_id": "dev-1", "final_text": "from device"}))
    net.register_runtime("laptop-1", rt)

    chat = net.direct_chat(local.id, actor=alice)
    net.send_message(chat.id, "在吗", actor=alice, background=False)
    assert net.runtime_for(local) is rt and net.runtime_for(cloud) is default
    assert any(c.get("type") == RUN_REQUEST for c in rt.link.sent)
    assert chat.messages[-1].text == "from device"


def test_offline_device_runtime_is_a_transport_error_not_a_task_failure(tmp_path):
    from matrix.engine import Network
    from matrix.runtime import ScriptedRuntime
    from tests.test_users import users

    net = Network(ScriptedRuntime(lambda call: "cloud"), tmp_path, seed=False)
    alice = users(net, "alice@x.io")[0]
    a = net.create_agent("Local", "端侧", [], ["web.research"], actor=alice)
    a.device_runtime = "not-connected"
    chat = net.direct_chat(a.id, actor=alice)
    net.send_message(chat.id, "在吗", actor=alice, background=False)
    assert net.runtime_for(a) is None
    failure = net.store.conversations[chat.id].messages[-1]
    assert failure.role == "system" and "未连接" in failure.text
    assert a.active == 0  # 没留下运行中的计数


def test_capabilities_are_per_agent(tmp_path):
    """同一个网络里，端侧 Agent 的有效权限按它自己的运行时要收窄；云侧 Agent 不受影响。"""
    from matrix.engine import Network
    from matrix.runtime import ScriptedRuntime
    from tests.test_users import users

    net = Network(ScriptedRuntime(lambda call: "cloud"), tmp_path, seed=False)
    alice = users(net, "alice@x.io")[0]
    cloud = net.create_agent("Cloud", "云侧", [], ["shell.exec"], actor=alice)
    local = net.create_agent("Local", "端侧", [], ["shell.exec"], actor=alice)
    local.device_runtime = "laptop-1"
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), hello("fake-harness"))  # 只强制 web.research
    net.register_runtime("laptop-1", rt)
    assert net.capabilities(cloud).enforces("shell.exec")
    assert not net.capabilities(local).enforces("shell.exec")
    assert net._effective(local, local.authority) == ([], {"shell.exec": "运行时 fake-harness 无法强制（unavailable）"})
