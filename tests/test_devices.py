"""端侧连接器令牌与 /api/devices/connect（docs/device-runtimes.md 第 2 节）。"""
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from matrix import auth
from matrix.device import HELLO, HELLO_OK, PROTOCOL
from matrix.engine import Network
from matrix.runtime import ENFORCED, ScriptedRuntime
from matrix.server import create_app
from tests.test_users import client, users


def build(tmp_path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    users(net, "alice@x.io", "bob@x.io")
    return net


def hello(runtime="fake-harness"):
    return {"type": HELLO_OK, "protocol": PROTOCOL, "runtime": runtime,
            "capabilities": {"enforcement": {"web.research": ENFORCED}, "session_resume": True,
                             "approval": "interactive", "sandbox": "workspace-read"}}


# ================= 令牌 =================
def test_issue_and_revoke_device_token(tmp_path):
    net = build(tmp_path)
    alice = next(u for u in net.store.users.values() if u.email == "alice@x.io")
    dev, token = auth.issue_device_token(net.store, alice.id, "笔记本")
    assert dev.public()["name"] == "笔记本" and "token" not in dev.public()
    assert token not in net.store.path.read_text(encoding="utf-8")  # 只存哈希

    assert auth.device_of(net.store, token).id == dev.id
    auth.revoke_device(net.store, dev.id, alice.id)
    assert auth.device_of(net.store, token) is None


def test_multiple_devices_and_revoke_owner_only(tmp_path):
    net = build(tmp_path)
    alice = next(u for u in net.store.users.values() if u.email == "alice@x.io")
    bob = next(u for u in net.store.users.values() if u.email == "bob@x.io")
    d1, t1 = auth.issue_device_token(net.store, alice.id, "一")
    d2, t2 = auth.issue_device_token(net.store, alice.id, "二")
    assert t1 != t2 and auth.device_of(net.store, t1) and auth.device_of(net.store, t2)
    try:
        auth.revoke_device(net.store, d1.id, bob.id)
    except auth.AuthError:
        pass
    else:  # pragma: no cover
        raise AssertionError("别人的设备不能吊销")
    assert auth.device_of(net.store, t1) is not None


# ================= REST =================
def test_devices_api_lists_without_tokens(tmp_path):
    net = build(tmp_path)
    c = client(net, "alice@x.io")
    created = c.post("/api/devices", json={"name": "我的电脑"}).json()
    assert created["token"] and created["name"] == "我的电脑"
    listed = c.get("/api/devices").json()
    assert len(listed) == 1 and "token" not in listed[0] and "token_hash" not in listed[0]

    other = client(net, "bob@x.io")
    assert other.get("/api/devices").json() == []
    assert other.delete(f"/api/devices/{created['id']}").status_code == 400  # 不是自己的
    assert c.delete(f"/api/devices/{created['id']}").json() == {"ok": True}
    assert auth.device_of(net.store, created["token"]) is None


def test_devices_require_login(tmp_path):
    c = TestClient(create_app(build(tmp_path)))
    assert c.get("/api/devices").status_code == 401
    assert c.post("/api/devices", json={}).status_code == 401


# ================= WebSocket =================
def test_connect_without_valid_token_is_refused(tmp_path):
    net = build(tmp_path)
    c = client(net, "alice@x.io")
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/api/devices/connect?token=nope") as ws:
            ws.receive()
    assert e.value.code == 1008
    assert net._runtimes == {}


def _until(cond, timeout=5.0):
    """服务端在自己的线程里处理握手、注册和注销，处理完不回消息：轮询到成立为止（慢的 CI 机器上要一会儿）。"""
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        time.sleep(0.01)
    return cond()


def test_connect_registers_the_runtime(tmp_path):
    net = build(tmp_path)
    c = client(net, "alice@x.io")
    token = c.post("/api/devices", json={"name": "笔记本"}).json()["token"]
    with c.websocket_connect(f"/api/devices/connect?token={token}") as ws:
        assert ws.receive_json()["type"] == HELLO
        ws.send_json(hello())
        dev = auth.device_of(net.store, token)
        assert _until(lambda: dev.id in net._runtimes)
        assert net.runtime_for(_agent_for(net, "alice@x.io")) is None  # 还没绑定到 Agent
        rt = net._runtimes[dev.id]
        assert rt.capabilities().placement == "device" and rt.capabilities().enforces("web.research")
    assert _until(lambda: net._runtimes == {})  # 断开后注销


def test_connect_rejects_bad_handshake(tmp_path):
    net = build(tmp_path)
    c = client(net, "alice@x.io")
    token = c.post("/api/devices", json={"name": "笔记本"}).json()["token"]
    with c.websocket_connect(f"/api/devices/connect?token={token}") as ws:
        ws.receive_json()
        ws.send_json({"type": "hi"})
        assert ws.receive()["type"] == "websocket.close"  # 握手不合法：服务端关闭
    assert net._runtimes == {}


def _agent_for(net, email):
    uid = next(u.id for u in net.store.users.values() if u.email == email)
    a = net.create_agent("L", "端侧", [], ["web.research"], actor=uid)
    a.device_runtime = "whatever"
    return a


def test_agent_binds_only_to_own_device(tmp_path):
    net = build(tmp_path)
    alice = next(u for u in net.store.users.values() if u.email == "alice@x.io")
    bob = next(u for u in net.store.users.values() if u.email == "bob@x.io")
    va, ta = auth.issue_device_token(net.store, alice.id, "alice 的")
    vb, tb = auth.issue_device_token(net.store, bob.id, "bob 的")

    a = net.create_agent("A", "x", [], ["web.research"], actor=alice.id, device_runtime=va.id)
    assert a.device_runtime == va.id
    view = net.agent_view(a, alice.id)
    assert view["device_online"] is False  # 没有连接器连上

    try:
        net.create_agent("B", "x", [], [], actor=alice.id, device_runtime=vb.id)
    except Exception as e:
        assert "属于别人" in str(e)
    else:  # pragma: no cover
        raise AssertionError("不能绑定别人的设备")

    auth.revoke_device(net.store, va.id, alice.id)
    try:
        net.update_agent(a.id, actor=alice.id, device_runtime=va.id)
    except Exception as e:
        assert "不存在或属于别人" in str(e)
    else:  # pragma: no cover
        raise AssertionError("吊销的设备不能绑定")


def test_connected_device_can_use_network_tools_events_and_approvals(tmp_path):
    """真实 /api/devices/connect：连接器发来的工具调用、过程事件、审批申请都要交给这一轮的执行上下文处理
    （不变量 10：身份按执行上下文认定）。审批等人决定期间，同一条连接上的其他消息照常处理。"""
    net = build(tmp_path)
    alice = next(u.id for u in net.store.users.values() if u.email == "alice@x.io")
    c = client(net, "alice@x.io")
    token = c.post("/api/devices", json={"name": "笔记本"}).json()["token"]
    with c.websocket_connect(f"/api/devices/connect?token={token}") as ws:
        ws.receive_json()
        ws.send_json(hello())
        dev = auth.device_of(net.store, token)
        assert _until(lambda: dev.id in net._runtimes)
        a = net.create_agent("L", "端侧", [], ["web.research"], actor=alice, device_runtime=dev.id)
        dm = net.direct_chat(a.id, actor=alice)
        net.send_message(dm.id, "整理一份周报", actor=alice)  # 后台运行：run.request 发到这条连接上
        run = ws.receive_json()
        assert run["type"] == "run.request" and "open_request" in run["tool_names"]
        rid = run["run_id"]
        ws.send_json({"type": "approval.request", "run_id": rid, "call_id": "a1", "tool": "write", "reason": "写外面"})
        ws.send_json({"type": "run.event", "run_id": rid, "event": {"type": "tool_call", "tool": "Read", "input": {}}})
        ws.send_json({"type": "tool.call", "run_id": rid, "call_id": "c1", "name": "open_request",
                      "args": {"title": "周报", "expected_outcome": "一份周报"}})
        reply = ws.receive_json()  # 审批还没决定，工具回执先到
        assert reply["reply_to"] == "c1" and reply["is_error"] is False, reply
        assert any(r.title == "周报" for r in net.store.requests.values())
        pending = lambda: [ap for ap in net.store.approvals.values() if ap.status == "pending"]  # noqa: E731
        assert _until(pending)
        net.decide_approval(pending()[0].id, False, actor=alice)
        assert ws.receive_json() == {"reply_to": "a1", "outcome": "rejected"}
        ws.send_json({"type": "tool.call", "run_id": "run-nope", "call_id": "c2", "name": "open_request", "args": {}})
        assert ws.receive_json()["is_error"] is True  # 不是这台设备上正在跑的一轮：拒绝
        ws.send_json({"type": "run.result", "run_id": rid, "session_id": "s", "final_text": "好的"})
        net.wait_quiet()
    msg = net.store.conversations[dm.id].messages[-1]
    assert msg.text == "好的" and any(s.get("tool") == "Read" for s in msg.steps), msg.steps


def test_materials_over_the_real_connection_are_a_manifest_plus_fetch(tmp_path):
    """真实连接上：声明了 materials-cache 的连接器收到的是清单，内容按 sha 用 material.fetch 取；
    清单以外的 sha 取不到（附件只给这一轮看得到它的席位）。"""
    import base64
    import hashlib
    net = build(tmp_path)
    alice = next(u.id for u in net.store.users.values() if u.email == "alice@x.io")
    c = client(net, "alice@x.io")
    token = c.post("/api/devices", json={"name": "笔记本"}).json()["token"]
    with c.websocket_connect(f"/api/devices/connect?token={token}") as ws:
        ws.receive_json()
        ws.send_json({**hello(), "features": ["materials-cache"]})
        dev = auth.device_of(net.store, token)
        assert _until(lambda: dev.id in net._runtimes)
        a = net.create_agent("L", "端侧", [], ["web.research"], actor=alice, device_runtime=dev.id)
        dm = net.direct_chat(a.id, actor=alice)
        att = net.upload_attachment(dm.id, "data.csv", b"x,y\n1,2\n", actor=alice)
        net.send_message(dm.id, "看看", attachments=[att], actor=alice)
        run = ws.receive_json()
        assert "materials" not in run
        assert run["manifest"] == [{"path": f"attachments/{att['sha'][:8]}/data.csv", "sha": att["sha"], "size": 8}]
        ws.send_json({"type": "material.fetch", "run_id": run["run_id"], "call_id": "m1", "sha": att["sha"], "offset": 0})
        got = ws.receive_json()
        assert got["reply_to"] == "m1" and base64.b64decode(got["data"]) == b"x,y\n1,2\n" and got["total"] == 8
        other = hashlib.sha256(b"not in this run").hexdigest()
        ws.send_json({"type": "material.fetch", "run_id": run["run_id"], "call_id": "m2", "sha": other, "offset": 0})
        assert ws.receive_json() == {"reply_to": "m2", "error": "这一轮没有这份材料"}
        ws.send_json({"type": "run.result", "run_id": run["run_id"], "session_id": "s", "final_text": "看完了"})
        net.wait_quiet()
