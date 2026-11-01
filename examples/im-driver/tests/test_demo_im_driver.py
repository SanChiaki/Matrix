"""演示 IM 驱动自己的测试：结构检查 + 对着本地的演示 IM 跑登录、收发、改消息、凭证不对、被顶掉、断线重连。

桥的规则（只认绑定的人、推给谁）已经由 Matrix 的 tests/test_im.py 对任何驱动保证了，驱动只测自己的收发。
接你们的 IM 时照这个写：起一个假的 IM 服务（devserver.py），把驱动的地址指过去。
"""

from __future__ import annotations

import threading
import time

import pytest

from matrix.im.base import Chat, DriverError, Inbound
from matrix.im.testing import assert_driver
from matrix_im_demo.devserver import DevServer
from matrix_im_demo.driver import DemoDriver


class Sink:
    def __init__(self):
        self.statuses: list[tuple[str, str]] = []
        self.messages: list[Inbound] = []
        self.lock = threading.Lock()

    def on_message(self, msg: Inbound) -> None:
        with self.lock:
            self.messages.append(msg)

    def on_action(self, action) -> None:
        return None

    def on_status(self, status: str, error: str = "") -> None:
        self.statuses.append((status, error))


def until(cond, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("等不到")


@pytest.fixture
def server(monkeypatch):
    s = DevServer()
    monkeypatch.setenv("DEMO_IM_URL", s.url)
    yield s
    s.close()


def online(secret: str = "demo-secret") -> tuple[DemoDriver, Sink]:
    sink = Sink()
    d = DemoDriver({"app_id": "app-1"}, {"app_secret": secret}, sink)
    d.start()
    return d, sink


def test_driver_shape():
    assert_driver(DemoDriver)


def test_login_receive_reply_edit_and_push(server: DevServer):
    d, sink = online()
    try:
        until(lambda: ("online", "") in sink.statuses)
        server.say("alice", "你好")
        server.say("bob", "@助理  帮我看看", group="研发群")
        until(lambda: len(sink.messages) == 2)
        single, group = sink.messages
        assert (single.chat, single.sender, single.text) == (Chat("single", "alice"), "alice", "你好")
        assert (group.chat, group.text) == (Chat("group", "研发群"), "帮我看看")  # 开头的 @ 去掉了
        r = d.open_reply(single.handle, single.chat)
        r.update("收到，正在处理…")
        r.finish("好了")
        assert server.texts("alice") == ["好了"]  # 先发一条，再改成正式回复
        assert [f["op"] for f in server.sent] == ["send", "edit"] and server.sent[0]["reply_to"] == single.handle
        d.push(Chat("single", "alice"), "早报")
        assert server.texts("alice") == ["好了", "早报"]
        with pytest.raises(DriverError, match="没有这条消息"):
            d._edit("b-nope", "x")
    finally:
        d.stop()


def test_bad_secret_is_reported_and_not_retried(server: DevServer):
    d, sink = online("wrong")
    try:
        until(lambda: any(s == "auth_failed" for s, _ in sink.statuses))
        time.sleep(0.3)
        assert server.logins == []
    finally:
        d.stop()


def test_kicked_does_not_reconnect(server: DevServer):
    d, sink = online()
    try:
        until(lambda: ("online", "") in sink.statuses)
        server.kick()
        until(lambda: any(s == "kicked" for s, _ in sink.statuses))
        time.sleep(0.5)
        assert server.logins == ["app-1"]
    finally:
        d.stop()


def test_dropped_connection_reconnects(server: DevServer, monkeypatch):
    monkeypatch.setattr(DemoDriver, "heartbeat", 0.1)
    d, sink = online()
    try:
        until(lambda: ("online", "") in sink.statuses)
        time.sleep(0.3)  # 心跳有回执，连接不会被当成死的
        assert server.logins == ["app-1"]
        server.conns["app-1"].close()
        until(lambda: server.logins == ["app-1", "app-1"])
        until(lambda: sink.statuses[-1] == ("online", ""))
    finally:
        d.stop()
