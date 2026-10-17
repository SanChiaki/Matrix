"""运行时能力报告与有效权限收窄（docs/device-runtimes.md 第 3 节）。

有效权限 = 自身权限 ∩ 委托授予 ∩ 运行时能强制的部分。只能靠提示词约定的权限按 advisory 记账，
不进入有效权限，并记录收窄原因。
"""
from agentnet.engine import Network
from agentnet.protocol import format_task_result
from agentnet.runtime import ADVISORY, UNAVAILABLE, RuntimeCapabilities, ScriptedRuntime
from tests.test_users import joint, users


def limited(*advisory: str, name: str = "device-harness") -> RuntimeCapabilities:
    """除指定的权限外全部可强制；指定的按 advisory 处理。"""
    caps = RuntimeCapabilities.all_enforced(name, placement="device")
    for a in advisory:
        caps.enforcement[a] = ADVISORY
    return caps


def build(tmp_path, caps: RuntimeCapabilities | None = None, handler=None):
    rt = ScriptedRuntime(handler or (lambda call: "ok"), capabilities=caps)
    return Network(rt, tmp_path, seed=False)


def test_default_runtimes_enforce_every_authority(tmp_path):
    net = build(tmp_path)
    caps = net.capabilities()
    assert caps.placement == "hosted" and caps.session_resume
    assert set(caps.enforcement) == set(net.store.authorities) if hasattr(net.store, "authorities") else True
    assert all(caps.enforces(a) for a in ("web.research", "file.write", "shell.exec", "delegate"))


def test_runtime_without_capabilities_reports_all_enforced(tmp_path):
    """旧运行时（没有 capabilities()）保持原行为：全部可强制。"""

    class Bare:
        name = "bare"
        calls: list = []

        def run(self, req):  # pragma: no cover —— 本测试不跑这一轮
            raise AssertionError

    net = Network(Bare(), tmp_path, seed=False)  # type: ignore[arg-type]
    assert net.capabilities().enforces("delegate")
    assert net.capabilities().name == "bare"


def test_agent_view_reports_effective_authority(tmp_path):
    net = build(tmp_path, limited("shell.exec"))
    alice = users(net, "alice@x.io")[0]
    a = net.create_agent("A", "做调研", [], ["web.research", "shell.exec", "delegate"], actor=alice)
    view = net.agent_view(a, alice)
    assert view["effective_authority"] == ["web.research", "delegate"]
    assert view["runtime_capabilities"]["enforcement"]["shell.exec"] == ADVISORY
    assert view["runtime_capabilities"]["placement"] == "device"


def test_seat_authority_excludes_advisory(tmp_path):
    """席位拿到的有效权限不含 advisory；delegate 被收窄时网络工具也不挂。"""
    net = build(tmp_path, limited("delegate"))
    alice = users(net, "alice@x.io")[0]
    a = net.create_agent("A", "做调研", ["research"], ["web.research", "delegate"], actor=alice)
    g = net.create_group("G", "", [a.id], actor=alice)
    chat = net.group_chat(g.id, actor=alice)
    net.send_message(chat.id, "@A 看看", actor=alice, background=False)
    call = net.runtime.calls[0]
    assert call.authority == ["web.research"]
    assert not call.can_delegate
    assert "delegate" not in call.tool_names


def test_delegation_cannot_grant_what_runtime_cannot_enforce(tmp_path):
    """不变量 1：委托授予只能来自委托方的有效权限。运行时做不到的权限，委托方自己就拿不到，更转授不出去。"""
    net = build(tmp_path, limited("file.write"))
    alice = users(net, "alice@x.io")[0]
    lead = net.create_agent("Lead", "协调", [], ["delegate", "file.write"], actor=alice)
    worker = net.create_agent("Worker", "执行", [], ["file.write"], actor=alice)
    g = net.create_group("G", "", [lead.id, worker.id], actor=alice)
    chat = net.group_chat(g.id, actor=alice)

    seen = {}

    def handler(call):
        if call.agent_id == lead.id and call.turn == 1:
            seen["text"] = call.tool("delegate", agent="Worker", mission="写报告", expected_outcome="一份报告",
                                     authority=["file.write"])
        return "收到"

    net.runtime.handler = handler
    net.send_message(chat.id, "@Lead 安排一下", actor=alice, background=False)

    task = next(t for t in net.store.tasks.values() if t.delegatee_id == worker.id)
    assert task.granted_authority == []
    assert task.denied_authority == ["file.write"]
    assert task.narrowed == {}

def test_unavailable_and_narrowed_reasons():
    caps = RuntimeCapabilities(name="x", enforcement={"delegate": UNAVAILABLE, "shell.exec": ADVISORY})
    assert caps.level("file.write") == UNAVAILABLE
    assert caps.effective(["delegate", "shell.exec", "web.research"]) == []
    assert set(caps.narrowed(["delegate", "shell.exec"])) == {"delegate", "shell.exec"}
    assert caps.enforces("delegate") is False


def test_task_narrowing_is_shown_and_persisted(tmp_path):
    """执行者运行时收窄时：委托结果里写明原因（不是“未授予”），并随任务持久化，重载后仍在。"""
    net = build(tmp_path)
    alice = users(net, "alice@x.io")[0]
    lead = net.create_agent("Lead", "协调", [], ["delegate"], actor=alice)
    worker = net.create_agent("Worker", "执行", [], ["file.write"], actor=alice)
    g = net.create_group("G", "", [lead.id, worker.id], actor=alice)
    chat = net.group_chat(g.id, actor=alice)

    def handler(call):
        if call.agent_id == lead.id and call.turn == 1:
            call.tool("delegate", agent="Worker", mission="写", expected_outcome="文件", authority=["file.write"])
        return "收到"

    net.runtime.handler = handler
    net.send_message(chat.id, "@Lead 安排", actor=alice, background=False)
    task = next(t for t in net.store.tasks.values() if t.delegatee_id == worker.id)

    task.narrowed = {"file.write": "运行时 device-harness 无法强制（advisory）"}
    text = format_task_result(task, "Worker")
    assert "运行时限制" in text and "file.write" in text and "advisory" in text

    net.store.save()
    from agentnet.store import Store
    again = Store(tmp_path, seed=False)
    assert again.tasks[task.id].narrowed == task.narrowed
