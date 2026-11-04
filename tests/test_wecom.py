"""企业微信驱动（matrix/im/wecom.py）：对着本地的假企业微信长连接服务跑订阅、心跳、收发、被顶掉、断线重连；
帧的解析、媒体解密、驱动一致性检查。"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from typing import Any

import pytest
from websockets.sync.server import ServerConnection, serve

from matrix.im import wecom
from matrix.im.base import Action, Card, Button, Chat, DriverError, Inbound
from matrix.im.testing import check_driver
from matrix.im.wecom import WecomDriver, decrypt_media, encrypt_media


class FakeWecom:
    """假的企业微信长连接服务：认 secret == "good"；回 ping 与每一帧的回执；frames 记下收到的帧。"""

    def __init__(self):
        self.frames: list[dict[str, Any]] = []
        self.conns: list[ServerConnection] = []
        self.reject_send = False
        self._srv = serve(self._handle, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self._srv.socket.getsockname()[1]}"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def _handle(self, ws: ServerConnection) -> None:
        self.conns.append(ws)
        for raw in ws:
            f = json.loads(raw)
            self.frames.append(f)
            rid = f["headers"]["req_id"]
            if f["cmd"] == "aibot_subscribe":
                ok = f["body"]["secret"] == "good"
                ws.send(json.dumps({"headers": {"req_id": rid}, "errcode": 0 if ok else 40001,
                                    "errmsg": "ok" if ok else "invalid secret"}))
            elif f["cmd"] == "aibot_send_msg" and self.reject_send:
                ws.send(json.dumps({"headers": {"req_id": rid}, "errcode": 846609, "errmsg": "no session"}))
            else:
                ws.send(json.dumps({"headers": {"req_id": rid}, "errcode": 0, "errmsg": "ok"}))

    def push(self, frame: dict[str, Any]) -> None:
        self.conns[-1].send(json.dumps(frame, ensure_ascii=False))

    def cmds(self, cmd: str) -> list[dict[str, Any]]:
        return [f for f in self.frames if f["cmd"] == cmd]

    def close(self) -> None:
        self._srv.shutdown()


class Sink:
    def __init__(self):
        self.statuses: list[tuple[str, str]] = []
        self.messages: list[Inbound] = []
        self.actions: list[Action] = []
        self.got = threading.Event()

    def on_message(self, msg: Inbound) -> None:
        self.messages.append(msg)
        self.got.set()

    def on_action(self, action: Action) -> str | None:
        self.actions.append(action)
        self.got.set()
        return "已发送"

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
    s = FakeWecom()
    monkeypatch.setenv("MATRIX_WECOM_URL", s.url)
    yield s
    s.close()


def driver(secret: str = "good") -> tuple[WecomDriver, Sink]:
    sink = Sink()
    d = WecomDriver({"bot_id": "bot-1"}, {"secret": secret}, sink)
    d.start()
    return d, sink


def callback(body: dict[str, Any], rid: str = "cb-1", cmd: str = "aibot_msg_callback") -> dict[str, Any]:
    return {"cmd": cmd, "headers": {"req_id": rid}, "body": {"aibotid": "bot-1", **body}}


def test_driver_passes_the_consistency_check():
    assert check_driver(WecomDriver) == []


def test_subscribe_receive_reply_and_push(server: FakeWecom):
    d, sink = driver()
    try:
        until(lambda: ("online", "") in sink.statuses)
        sub = server.cmds("aibot_subscribe")[0]
        assert sub["body"] == {"bot_id": "bot-1", "secret": "good"}
        server.push(callback({"msgid": "m1", "chattype": "single", "from": {"userid": "u1"}, "msgtype": "text",
                              "text": {"content": "你好"}}, rid="cb-1"))
        until(lambda: sink.messages)
        m = sink.messages[0]
        assert (m.msg_id, m.chat, m.sender, m.text, m.handle) == ("m1", Chat("single", "u1"), "u1", "你好", "cb-1")
        r = d.open_reply(m.handle, m.chat)
        r.update("想一想")
        r.finish("好了")
        frames = server.cmds("aibot_respond_msg")
        assert [f["headers"]["req_id"] for f in frames] == ["cb-1", "cb-1"]  # 回复透传 req_id
        assert [(f["body"]["stream"]["content"], f["body"]["stream"]["finish"]) for f in frames] == \
            [("想一想", False), ("好了", True)]
        assert frames[0]["body"]["stream"]["id"] == frames[1]["body"]["stream"]["id"]
        d.push(Chat("single", "u1"), "早报")
        send = server.cmds("aibot_send_msg")[-1]["body"]
        assert send == {"chatid": "u1", "chat_type": 1, "msgtype": "markdown", "markdown": {"content": "早报"}}
        server.reject_send = True
        with pytest.raises(DriverError, match="no session"):
            d.push(Chat("single", "u1"), "又一条")
    finally:
        d.stop()


def test_card_click_comes_back_as_an_action_and_updates_the_card(server: FakeWecom):
    d, sink = driver()
    try:
        until(lambda: ("online", "") in sink.statuses)
        d.push_card(Chat("single", "u1"), Card("起草了一条", "正文", (Button("draft:d1:send", "发送", "发送 1"),)))
        card = server.cmds("aibot_send_msg")[-1]["body"]["template_card"]
        assert card["card_type"] == "button_interaction" and card["button_list"][0]["key"] == "draft:d1:send"
        server.push(callback({"msgid": "e1", "chattype": "single", "from": {"userid": "u1"}, "msgtype": "event",
                              "event": {"eventtype": "template_card_event", "event_key": "draft:d1:send",
                                        "task_id": card["task_id"]}}, rid="ev-1", cmd="aibot_event_callback"))
        until(lambda: server.cmds("aibot_respond_update_msg"))
        assert sink.actions[0].key == "draft:d1:send" and sink.actions[0].sender == "u1"
        upd = server.cmds("aibot_respond_update_msg")[0]
        assert upd["headers"]["req_id"] == "ev-1" and upd["body"]["template_card"]["task_id"] == card["task_id"]
    finally:
        d.stop()


def test_bad_secret_is_reported_and_not_retried(server: FakeWecom):
    d, sink = driver("wrong")
    try:
        until(lambda: any(s == "auth_failed" for s, _ in sink.statuses))
        time.sleep(0.3)
        assert len(server.cmds("aibot_subscribe")) == 1
    finally:
        d.stop()


def test_kicked_by_another_connection_does_not_reconnect(server: FakeWecom):
    d, sink = driver()
    try:
        until(lambda: ("online", "") in sink.statuses)
        server.push(callback({"msgtype": "event", "event": {"eventtype": "disconnected_event"}},
                             cmd="aibot_event_callback"))
        server.conns[-1].close()
        until(lambda: any(s == "kicked" for s, _ in sink.statuses))
        time.sleep(0.5)
        assert len(server.cmds("aibot_subscribe")) == 1
    finally:
        d.stop()


def test_dropped_connection_reconnects_and_heartbeats(server: FakeWecom, monkeypatch):
    monkeypatch.setattr(WecomDriver, "heartbeat", 0.1)
    d, sink = driver()
    try:
        until(lambda: ("online", "") in sink.statuses)
        until(lambda: len(server.cmds("ping")) >= 2)
        server.conns[-1].close()
        until(lambda: len(server.cmds("aibot_subscribe")) == 2)
        until(lambda: sink.statuses[-1] == ("online", ""))
    finally:
        d.stop()


def test_parse_mixed_group_quote_and_skip_non_user_messages():
    d = WecomDriver({"bot_id": "b"}, {"secret": "s"}, Sink())
    m = d.parse_message({"msgid": "m", "chattype": "group", "chatid": "g1", "from": {"userid": "u1"},
                         "msgtype": "mixed", "mixed": {"msg_item": [
                             {"msgtype": "text", "text": {"content": "@助理 看图"}},
                             {"msgtype": "image", "image": {"url": "https://x/i", "aeskey": "k"}}]},
                         "quote": {"msgtype": "text", "text": {"content": "原话"}}}, "r")
    assert m.chat == Chat("group", "g1") and m.text == "看图" and m.quote == "原话" and len(m.files) == 1
    v = d.parse_message({"msgid": "v", "chattype": "single", "from": {"userid": "u1"}, "msgtype": "voice",
                         "voice": {"content": "语音转的字"}}, "r")
    assert v.text == "语音转的字"
    assert d.parse_message({"msgid": "x", "chattype": "single", "from": {"userid": "u1"}, "msgtype": "text",
                            "text": {"content": "hi"}, "compat_info": {"source": "channel"}}, "r") is None


def test_media_decrypt_round_trip_and_file_names(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode().rstrip("=")
    png = b"\x89PNG\r\n\x1a\n" + os.urandom(100)
    assert decrypt_media(encrypt_media(png, key), key) == png
    # 填充不对一定报错。不用随机字节：随机密文解出来末字节是 0x01 的概率是 1/256，会恰好像合法填充（偶发不报错）
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    raw = base64.b64decode(key + "=" * (-len(key) % 4))

    def cipher_of(plain: bytes) -> bytes:  # 不加填充直接加密：解出来就是 plain
        e = Cipher(algorithms.AES(raw), modes.CBC(raw[:16])).encryptor()
        return e.update(plain) + e.finalize()
    for tail in (b"\x00", b"\x21", b"\x01\x02"):  # 填充长度 0、超过块长 32、填充字节不一致
        with pytest.raises(DriverError):
            decrypt_media(cipher_of(b"x" * (64 - len(tail)) + tail), key)
    import matrix.mcp_client as mc
    monkeypatch.setattr(mc, "fetch_public", lambda url, limit, timeout=30: (encrypt_media(png, key), ""))
    d = WecomDriver({"bot_id": "b"}, {"secret": "s"}, Sink())
    f = d._file({"url": "https://x/i", "aeskey": key}, "image")
    assert f.fetch() == png and (f.name, f.mime) == ("图片.png", "image/png")
    monkeypatch.setattr(mc, "fetch_public",
                        lambda url, limit, timeout=30: (encrypt_media(b"doc", key),
                                                        "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.pdf"))
    f = d._file({"url": "https://x/f", "aeskey": key}, "file")
    assert f.fetch() == b"doc" and (f.name, f.mime) == ("报告.pdf", "application/pdf")


def test_media_downloads_refuse_private_addresses():
    """不变量 25 的边界：消息里的下载地址只连 https 公网（网络不能被当跳板）。"""
    from matrix.mcp_client import MCPError, fetch_public
    for url in ("http://example.com/a", "https://127.0.0.1/a", "https://10.0.0.1/a"):
        with pytest.raises(MCPError):
            fetch_public(url, 100)
