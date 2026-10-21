"""一台设备多个 harness（docs/device-runtimes.md 第 2.4 节）：握手报告多个运行时，Agent 绑定到“设备 + harness”。"""
import pytest

from agentnet import auth
from agentnet.device import (HELLO_OK, PROTOCOL, RUN_REQUEST, RUN_RESULT, DeviceRuntime, MemoryLink,
                             capabilities_from_hello)
from agentnet.engine import Network, NetworkError
from agentnet.runtime import ADVISORY, ENFORCED, ScriptedRuntime
from tests.test_users import users


def multi_hello():
    """新连接器的握手：claude-code 能强制 shell.exec，codex 只能约定 web.research。"""
    return {"type": HELLO_OK, "protocol": PROTOCOL, "runtime": "claude-code",
            "capabilities": {"enforcement": {"web.research": ENFORCED, "shell.exec": ENFORCED}},
            "runtimes": [
                {"runtime": "claude-code", "capabilities": {"enforcement": {"web.research": ENFORCED,
                                                                            "shell.exec": ENFORCED}}},
                {"runtime": "codex", "capabilities": {"enforcement": {"web.research": ADVISORY,
                                                                      "shell.exec": ENFORCED},
                                                      "sandbox": "workspace-write"}},
            ]}


def device_rt(msg=None, answer="from device"):
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), capabilities_from_hello(msg or multi_hello()))
    sent = []

    def serve(m):
        sent.append(m)
        if m["type"] == RUN_REQUEST:
            rt.on_message({"type": RUN_RESULT, "run_id": m["run_id"], "session_id": "s",
                           "final_text": f"{answer}:{m.get('harness')}"})
        return None

    rt.link = MemoryLink(serve=serve)
    return rt, sent


# ================= 握手 =================
def test_hello_reports_every_harness_with_its_own_capabilities():
    info = capabilities_from_hello(multi_hello())
    assert info.harnesses == ["claude-code", "codex"] and info.runtime == "claude-code"
    assert info.capabilities_for("codex").level("web.research") == ADVISORY
    assert info.capabilities_for("claude-code").enforces("web.research")
    assert info.capabilities_for(None).name == "claude-code"   # 不指定 = 默认 harness
    assert info.capabilities_for("dsh") is None                # 这台设备没有


def test_old_connector_hello_is_a_single_harness():
    """旧连接器只报一个 runtime：照常可用，等于只有这一个 harness。"""
    info = capabilities_from_hello({"type": HELLO_OK, "protocol": PROTOCOL, "runtime": "codex",
                                    "capabilities": {"enforcement": {"file.write": ENFORCED}}})
    assert info.harnesses == ["codex"] and info.capabilities_for("codex").enforces("file.write")


def test_hello_drops_duplicate_and_nameless_harnesses():
    msg = multi_hello()
    msg["runtimes"] += [{"runtime": "codex", "capabilities": {}}, {"capabilities": {}}]
    assert capabilities_from_hello(msg).harnesses == ["claude-code", "codex"]


# ================= 引擎：按 harness 路由与计算能力 =================
def _net(tmp_path):
    net = Network(ScriptedRuntime(lambda call: "cloud"), tmp_path, seed=False)
    alice = users(net, "alice@x.io")[0]
    dev, _ = auth.issue_device_token(net.store, alice, "笔记本")
    return net, alice, dev


def test_agents_on_one_device_run_on_their_own_harness(tmp_path):
    net, alice, dev = _net(tmp_path)
    claude = net.create_agent("C", "x", [], ["web.research"], actor=alice, device_runtime=dev.id, harness="claude-code")
    codex = net.create_agent("X", "x", [], ["web.research"], actor=alice, device_runtime=dev.id, harness="codex")
    rt, sent = device_rt()
    net.register_runtime(dev.id, rt)

    for a in (claude, codex):
        dm = net.direct_chat(a.id, actor=alice)
        net.send_message(dm.id, "在吗", actor=alice, background=False)
        assert net.store.conversations[dm.id].messages[-1].text == f"from device:{a.harness}"
    assert [m["harness"] for m in sent if m["type"] == RUN_REQUEST] == ["claude-code", "codex"]


def test_effective_authority_follows_the_bound_harness(tmp_path):
    """不变量 1：同一台设备上，有效权限按 Agent 绑定的那个 harness 能强制的部分计算。"""
    net, alice, dev = _net(tmp_path)
    claude = net.create_agent("C", "x", [], ["web.research"], actor=alice, device_runtime=dev.id, harness="claude-code")
    codex = net.create_agent("X", "x", [], ["web.research"], actor=alice, device_runtime=dev.id, harness="codex")
    net.register_runtime(dev.id, device_rt()[0])
    assert net._effective(claude, claude.authority) == (["web.research"], {})
    eff, narrowed = net._effective(codex, codex.authority)
    assert eff == [] and "codex" in narrowed["web.research"] and "advisory" in narrowed["web.research"]
    assert codex.enforcement == {"web.research": ADVISORY, "shell.exec": ENFORCED}  # 离线时沿用


def test_harness_missing_on_the_device_is_offline_not_failure(tmp_path):
    """设备在线但没装 Agent 要的 harness：按不可达等待，不当成执行失败，装上重连后继续。"""
    net, alice, dev = _net(tmp_path)
    a = net.create_agent("D", "x", [], ["web.research"], actor=alice, device_runtime=dev.id, harness="dsh")
    net.register_runtime(dev.id, device_rt()[0])
    assert net.runtime_for(a) is None and not net.device_online(a)
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "在吗", actor=alice, background=False)
    note = net.store.conversations[dm.id].messages[-1]
    assert note.role == "system" and "dsh" in note.text and "笔记本" in note.text


def test_unset_harness_uses_the_device_default(tmp_path):
    """旧数据没有 harness：走设备的默认 harness（旧连接器只有一个）。"""
    net, alice, dev = _net(tmp_path)
    a = net.create_agent("O", "x", [], ["web.research"], actor=alice, device_runtime=dev.id)
    assert a.harness is None
    rt, sent = device_rt()
    net.register_runtime(dev.id, rt)
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "在吗", actor=alice, background=False)
    assert [m["harness"] for m in sent if m["type"] == RUN_REQUEST] == [None]


def test_device_remembers_reported_harnesses(tmp_path):
    """设备离线时 UI 仍要能选 harness：最近一次握手报告的 harness 记在设备上并持久化。"""
    net, alice, dev = _net(tmp_path)
    net.register_runtime(dev.id, device_rt()[0])
    assert dev.harnesses == ["claude-code", "codex"]
    net.unregister_runtime(dev.id)
    reloaded = Network(ScriptedRuntime(lambda call: ""), tmp_path, seed=False)
    assert reloaded.store.devices[dev.id].harnesses == ["claude-code", "codex"]


def test_binding_checks_the_harness_against_what_the_device_reported(tmp_path):
    net, alice, dev = _net(tmp_path)
    # 还没连过：不知道装了什么，允许先绑定
    a = net.create_agent("A", "x", [], [], actor=alice, device_runtime=dev.id, harness="codex")
    net.register_runtime(dev.id, device_rt()[0])
    with pytest.raises(NetworkError, match="没有 dsh"):
        net.update_agent(a.id, actor=alice, harness="dsh")
    with pytest.raises(NetworkError, match="未知的 harness"):
        net.create_agent("B", "x", [], [], actor=alice, device_runtime=dev.id, harness="rm -rf")
    with pytest.raises(NetworkError, match="云侧"):
        net.create_agent("C", "x", [], [], actor=alice, harness="codex")  # 没选设备不能选 harness


def test_moving_back_to_cloud_clears_the_binding(tmp_path):
    """改回平台云侧：device_runtime 显式传空，harness 一并清掉。"""
    net, alice, dev = _net(tmp_path)
    a = net.create_agent("A", "x", [], [], actor=alice, device_runtime=dev.id, harness="codex")
    net.update_agent(a.id, actor=alice, device_runtime=None)
    assert a.device_runtime is None and a.harness is None
    assert net.runtime_for(a) is net.runtime
