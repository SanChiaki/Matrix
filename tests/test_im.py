"""IM 入口（docs/reference/im.md）：绑定码、只认绑定的人、回复回到 IM、哪些消息推出去、草稿卡片、按能力降级、
密钥只进保管库、驱动注册。都用内存里的假驱动（matrix/im/fake.py），任何驱动都经同一个桥，所以都受这些约束。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from matrix.engine import Network, NetworkError
from matrix.im import bridge as bridge_mod
from matrix.im import registry
from matrix.im.base import Capabilities
from matrix.im.bridge import GROUP_TEXT, LATER_TEXT, UNBOUND_TEXT, WORKING_TEXT, ImBridge
from matrix.im.fake import FakeDriver, PlainFakeDriver
from matrix.models import MCP_PREFIX
from matrix.runtime import ScriptCall, ScriptedRuntime
from matrix.server import create_app
from tests.test_users import users

MONDAY_9 = time.mktime((2026, 10, 26, 9, 0, 0, 0, 0, -1))  # 默认早报：工作日 9:00


class Home:
    """助理对话里的脚本：回一句“回复：<主人的话>”；几个特殊的话触发进度、起草、慢回复、长回复。"""

    def __call__(self, call: ScriptCall) -> str:
        if MCP_PREFIX + "draft_message" not in call.tool_names:
            return "收到"
        if "到点了" in call.prompt:
            return "早报：一切正常"
        asked = [line.split("（@ 你）：", 1)[1].strip() for line in call.prompt.splitlines() if "（@ 你）：" in line]
        last = asked[-1] if asked else ""
        if last == "想一想":
            call.say("先查一下资料")
            time.sleep(0.1)
            return "想好了"
        if last == "起草":
            call.tool("draft_message", project="周报", text="开始写本周周报")
            return "起草好了，等你确认"
        if last == "慢":
            time.sleep(1.6)
            return "慢慢做完了"
        if last == "长":
            return "长" * 150  # 450 字节：假驱动一条最多 200
        return f"回复：{last}"


class QuickTTL(FakeDriver):
    kind = "fake-ttl"
    caps = Capabilities(stream=True, cards=True, proactive=True, max_text_bytes=200, reply_ttl=31,
                        min_update_interval=0, rate_per_minute=1000)


@pytest.fixture
def make(tmp_path: Path):
    made: list[ImBridge] = []

    def _make(data: Path | None = None, drivers=(FakeDriver, PlainFakeDriver, QuickTTL)) -> tuple[Network, ImBridge]:
        d = data or tmp_path / "data"
        d.mkdir(exist_ok=True)
        net = Network(ScriptedRuntime(Home()), d, seed=False)
        b = ImBridge(net, {x.kind: x for x in drivers})
        b.start()
        made.append(b)
        return net, b

    FakeDriver.instances.clear()
    yield _make
    for b in made:
        b.stop()


def personal(net: Network, b: ImBridge, owner: str, kind: str = "fake", sender: str = "u-alice") -> tuple[FakeDriver, str]:
    """主人接一个个人机器人并在 IM 里发绑定码。返回（驱动，渠道 id）。"""
    net.ensure_assistant(owner)
    out = b.create_channel(kind, {"bot": "b1", "token": "tok-123"}, owner)
    drv = FakeDriver.instances[-1]
    drv.say(sender, out["code"])
    b.flush()
    return drv, out["channel"]["id"]


def home_texts(net: Network, user: str) -> list[str]:
    return [m.text for m in net.home_of(user).messages if m.role == "user"]


# ================= 绑定 =================
def test_personal_bot_binds_only_its_owner_with_a_one_time_code(make):
    """不变量 25：个人机器人只给主人发码（别人 404）；没绑定的人发什么都不进网络；码只能用一次，绑的是发码的那个人。"""
    net, b = make()
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    net.ensure_assistant(alice)
    out = b.create_channel("fake", {"bot": "b1", "token": "tok-123"}, alice)
    cid, code = out["channel"]["id"], out["code"]
    drv = FakeDriver.instances[-1]
    assert out["channel"]["status"] == "online" and drv.running
    with pytest.raises(NetworkError, match="unknown im channel"):
        b.issue_code(cid, bob)
    drv.say("u-stranger", "帮我查一下明天的会")
    drv.say("u-stranger", "WRONG123")
    b.flush()
    assert drv.texts("respond") == [UNBOUND_TEXT, "绑定码不对或已过期：在 Matrix 里重新拿一个。"]
    assert home_texts(net, alice) == []
    drv.say("u-alice", code.lower())
    b.flush()
    assert "绑定好了" in drv.texts("respond")[-1]
    [bd] = net.store.im_bindings.values()
    assert (bd.user_id, bd.sender, bd.channel_id) == (alice, "u-alice", cid)
    drv.say("u-other", code)  # 用过的码
    b.flush()
    assert "不对或已过期" in drv.texts("respond")[-1] and len(net.store.im_bindings) == 1
    assert b.my_channels(alice)["personal"][0]["binding"]["id"] == bd.id
    assert b.my_channels(bob)["personal"] == []


def test_bind_codes_expire_and_guessing_is_capped(make):
    """不变量 25：绑定码有期限；同一个人错 5 次就先停 10 分钟；个人机器人上一直有人猜，待用的码作废。"""
    net, b = make()
    alice, = users(net, "alice@x.io")
    net.ensure_assistant(alice)
    out = b.create_channel("fake", {"bot": "b1", "token": "tok"}, alice)
    drv = FakeDriver.instances[-1]
    for c in b._codes.values():
        c.expires = time.time() - 1
    drv.say("u-alice", out["code"])
    b.flush()
    assert "已过期" in drv.texts("respond")[-1] and not net.store.im_bindings
    code = b.issue_code(out["channel"]["id"], alice)["code"]
    for i in range(5):
        drv.say("u-guess", f"GUESS{i:03d}")
    drv.say("u-guess", code)
    b.flush()
    assert "太多" in drv.texts("respond")[-1] and not net.store.im_bindings
    drv.say("u-alice", code)  # 有人一直在猜：这个码也作废了
    b.flush()
    assert "不对或已过期" in drv.texts("respond")[-1] and not net.store.im_bindings


def test_shared_bot_is_set_up_by_admins_and_bound_by_many(make):
    """不变量 25、13：部署方机器人只有管理员能建、能改；人人能拿码绑定自己；一个 IM 身份只对应一个人。"""
    net, b = make()
    admin, bob, carol = users(net, "admin@x.io", "bob@x.io", "carol@x.io")  # 最早注册的是管理员
    for u in (bob, carol):
        net.ensure_assistant(u)
    with pytest.raises(NetworkError, match="unknown admin"):
        b.create_channel("fake", {"bot": "corp", "token": "t"}, bob, shared=True)
    out = b.create_channel("fake", {"bot": "corp", "token": "t"}, admin, title="公司机器人", shared=True)
    cid = out["channel"]["id"]
    assert "code" not in out
    with pytest.raises(NetworkError, match="unknown im channel"):
        b.update_channel(cid, bob, title="改名")
    drv = FakeDriver.instances[-1]
    drv.say("u-bob", b.issue_code(cid, bob)["code"])
    drv.say("u-carol", b.issue_code(cid, carol)["code"])
    b.flush()
    assert {(x.user_id, x.sender) for x in net.store.im_bindings.values()} == {(bob, "u-bob"), (carol, "u-carol")}
    # 已绑定的人再发别人的码：只是一条普通消息，进的是他自己的助理对话
    code = b.issue_code(cid, carol)["code"]
    drv.say("u-bob", code)
    b.flush()
    assert home_texts(net, bob) == [code] and home_texts(net, carol) == []
    assert b.admin_channels(admin)["channels"][0]["bound"] == 2
    assert b.my_channels(bob)["shared"][0]["binding"] is not None


# ================= 入站 =================
def test_only_the_bound_person_in_a_single_chat_reaches_the_network(make):
    """不变量 25：群里的消息、别人的消息只回固定的话；绑定的人的单聊以他的身份进他的助理对话，重推的只进一次。"""
    net, b = make()
    alice, = users(net, "alice@x.io")
    drv, _ = personal(net, b, alice)
    drv.say("u-alice", "群里说的", group="g1")
    drv.say("u-x", "你好")
    drv.say("u-alice", "你好", msg_id="dup")
    drv.say("u-alice", "你好", msg_id="dup")
    b.flush()
    assert GROUP_TEXT in drv.texts("respond") and UNBOUND_TEXT in drv.texts("respond")
    home = net.home_of(alice)
    mine = [m for m in home.messages if m.role == "user"]
    assert [(m.text, m.user_id, m.via) for m in mine] == [("你好", alice, "来自测试 IM")]
    assert drv.texts("finish") == ["回复：你好"]


def test_attachments_and_quotes_come_in_with_the_message(make):
    net, b = make()
    alice, = users(net, "alice@x.io")
    drv, _ = personal(net, b, alice)
    drv.say("u-alice", "看看这个", files=[("a.txt", "text/plain", b"hello")], quote="昨天说的事")
    b.flush()
    m = next(m for m in net.home_of(alice).messages if m.role == "user")
    assert m.text == "> 昨天说的事\n\n看看这个"
    assert [(a["name"], a["size"]) for a in m.attachments] == [("a.txt", 5)]


# ================= 出站 =================
def test_reply_goes_back_to_the_im_it_came_from_and_web_messages_stay_on_the_web(make):
    """不变量 25：经 IM 发来的，回复先占位、刷新进度、收尾成正式回复；网页里说的话，回复不推到 IM。"""
    net, b = make()
    alice, = users(net, "alice@x.io")
    drv, _ = personal(net, b, alice)
    bridge_mod.PROGRESS_INTERVAL, old = 0.0, bridge_mod.PROGRESS_INTERVAL
    try:
        drv.say("u-alice", "想一想")
        b.flush()
    finally:
        bridge_mod.PROGRESS_INTERVAL = old
    assert drv.texts("update")[0] == WORKING_TEXT and "先查一下资料" in drv.texts("update")[-1]
    assert drv.texts("finish") == ["想好了"]
    before = list(drv.sent)
    net.send_message(net.home_of(alice).id, "在网页里问的", actor=alice, background=False)
    b.flush()
    assert drv.sent == before


def test_long_replies_are_split_and_slow_ones_are_finished_then_pushed(make):
    net, b = make()
    alice, = users(net, "alice@x.io")
    drv, _ = personal(net, b, alice)
    drv.say("u-alice", "长")
    b.flush()
    parts = drv.texts("finish") + drv.texts("push")
    assert "".join(parts) == "长" * 150 and all(len(p.encode()) <= 200 for p in parts)
    drv2, _ = personal(net, b, users(net, "carol@x.io")[0], kind="fake-ttl", sender="u-carol")
    drv2.say("u-carol", "慢")
    b.flush()
    assert drv2.texts("finish")[-1] == LATER_TEXT and drv2.texts("push")[-1] == "慢慢做完了"


def test_messages_nobody_asked_for_are_pushed_only_if_switched_on(make):
    """不变量 25、21：定时、早报这类没人说话时的消息，推到开了推送的绑定；关掉就不推。"""
    net, b = make()
    alice, = users(net, "alice@x.io")
    drv, _ = personal(net, b, alice)
    assert net.run_schedules(MONDAY_9) == 1
    b.flush()
    assert drv.texts("push") == ["早报：一切正常"]
    [bd] = net.store.im_bindings.values()
    b.update_binding(bd.id, alice, push=False)
    assert net.run_schedules(MONDAY_9 + 86400) == 1
    b.flush()
    assert drv.texts("push") == ["早报：一切正常"]


def test_notices_reach_only_the_owner(make):
    """不变量 25：等你处理的事只推给它的所有者，别人的不推。"""
    net, b = make()
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    drv, _ = personal(net, b, alice)
    agent = net.home_of(alice).agent_id
    net.bus.emit("approval.updated", approval={"id": "a1", "owner_id": bob, "status": "pending", "agent_id": agent,
                                               "tool": "write", "kind": "sandbox"})
    net.bus.emit("approval.updated", approval={"id": "a2", "owner_id": alice, "status": "pending", "agent_id": agent,
                                               "tool": "write", "kind": "sandbox"})
    net.bus.emit("held.updated", user_id=bob, held={"status": "pending", "agent_name": "X"})
    b.flush()
    assert len(drv.texts("push")) == 1 and "等你批准" in drv.texts("push")[0]


def test_drafts_are_confirmed_from_the_im_only_by_the_owner(make):
    """不变量 25、21：草稿推成卡片；按钮只认绑定的那个人；不能发卡片的 IM 用文字指令。"""
    net, b = make()
    alice, = users(net, "alice@x.io")
    drv, cid = personal(net, b, alice)
    writer = net.create_agent("Writer", "写作", [], [], actor=alice)
    p = net.create_project("周报", "", [writer.id], actor=alice)
    drv.say("u-alice", "起草")
    b.flush()
    [d] = net.store.drafts.values()
    [card] = drv.texts("card")
    assert "周报" in card and f"draft:{d.id}:send" in card
    assert drv.click("u-x", f"draft:{d.id}:send") == "这张卡片不是给你的。" and d.status == "pending"
    assert "已发到「周报」群聊" in drv.click("u-alice", f"draft:{d.id}:send")
    assert d.status == "sent"
    chat = net.project_chat(p.id, actor=alice)
    assert any(m.role == "user" and m.text == "开始写本周周报" and m.user_id == alice for m in chat.messages)
    # 简易 IM：文字指令
    b.delete_channel(cid, alice)
    plain, _ = personal(net, b, alice, kind="fake-plain")
    plain.say("u-alice", "起草")
    b.flush()
    d2 = next(x for x in net.store.drafts.values() if x.status == "pending")
    # 简易 IM 不能主动推送：卡片发不出去，记在绑定上
    [bd] = net.store.im_bindings.values()
    assert "不能主动发消息" in bd.error
    n = b._short_of(alice, d2.id)
    plain.say("u-alice", f"放弃 {n}")
    b.flush()
    assert d2.status == "discarded" and plain.texts("respond")[-1] == "这条草稿不发了。"


def test_drivers_without_streaming_get_a_single_reply(make):
    net, b = make()
    alice, = users(net, "alice@x.io")
    drv, _ = personal(net, b, alice, kind="fake-plain")
    drv.say("u-alice", "你好")
    b.flush()
    assert drv.texts("update") == [] and drv.texts("respond")[-1] == "回复：你好"


def test_kicked_channel_stops_until_reconnected(make):
    net, b = make()
    alice, = users(net, "alice@x.io")
    drv, cid = personal(net, b, alice)
    drv.kick()
    b.flush()
    c = net.store.im_channels[cid]
    assert c.status == "kicked" and cid not in b._links
    b.reconnect(cid, alice)
    assert c.status == "online" and cid in b._links


def test_bindings_survive_a_restart(make, tmp_path: Path):
    net, b = make()
    alice, = users(net, "alice@x.io")
    personal(net, b, alice)
    b.stop()
    net2, b2 = make(tmp_path / "data")
    drv = FakeDriver.instances[-1]
    drv.say("u-alice", "重启后")
    b2.flush()
    assert home_texts(net2, alice) == ["重启后"]


# ================= 密钥与可见性 =================
def test_secrets_stay_in_the_vault_and_other_peoples_bots_are_404(make, tmp_path: Path):
    """不变量 25、13：密钥不出现在任何返回、事件、state.json 里；别人的机器人、绑定一律 404；删掉连密钥一起删。"""
    net, b = make()
    users(net, "alice@x.io", "bob@x.io")
    app = create_app(net, b)
    a, o = TestClient(app), TestClient(app)
    a.post("/api/auth/login", json={"email": "alice@x.io", "password": "password-123"})
    o.post("/api/auth/login", json={"email": "bob@x.io", "password": "password-123"})
    secret = "s3cr3t-token-value"
    r = a.post("/api/me/im", json={"kind": "fake", "values": {"bot": "b1", "token": secret}})
    assert r.status_code == 200 and secret not in r.text and r.json()["code"]
    cid = r.json()["channel"]["id"]
    listing = a.get("/api/me/im")
    assert secret not in listing.text and listing.json()["personal"][0]["secrets_set"] == ["token"]
    assert {d["kind"] for d in listing.json()["drivers"]} >= {"fake", "fake-plain"}
    assert a.patch(f"/api/me/im/{cid}", json={"values": {"bot": "b2"}}).status_code == 200
    assert net.vault.get(f"im-{cid}") == {"token": secret}  # 密钥留空 = 沿用
    drv = FakeDriver.instances[-1]
    drv.say("u-alice", r.json()["code"])
    b.flush()
    bid = next(iter(net.store.im_bindings))
    for path in (f"/api/me/im/{cid}", f"/api/me/im/{cid}/reconnect", f"/api/me/im/{cid}/code"):
        assert o.post(path).status_code in (404, 405)
    assert o.patch(f"/api/me/im/{cid}", json={"title": "x"}).status_code == 404
    assert o.delete(f"/api/me/im/{cid}").status_code == 404
    assert o.patch(f"/api/me/im-bindings/{bid}", json={"push": False}).status_code == 404
    assert o.get("/api/admin/im").status_code == 404
    events = json.dumps([e.to_dict() for e in net.bus.since(0)], ensure_ascii=False)
    state = (tmp_path / "data" / "state.json").read_text(encoding="utf-8")
    assert secret not in events and secret not in state
    assert net.event_view("im.updated", {"user_id": net.store.im_channels[cid].owner_id}, "someone") is None
    assert a.delete(f"/api/me/im/{cid}").status_code == 200
    assert net.vault.get(f"im-{cid}") == {} and not net.store.im_bindings


# ================= 驱动注册 =================
class _EP:
    def __init__(self, name: str, obj=None, err: Exception | None = None):
        self.name, self._obj, self._err = name, obj, err

    def load(self):
        if self._err:
            raise self._err
        return self._obj


def test_external_drivers_are_discovered_by_entry_point(monkeypatch):
    class Acme(PlainFakeDriver):
        kind = "acme"
        title = "Acme IM"

    class FakeWecom(PlainFakeDriver):
        kind = "wecom"

    eps = [_EP("acme", Acme), _EP("broken", err=ImportError("no module")), _EP("bad", object),
           _EP("clash", FakeWecom)]
    monkeypatch.setattr(registry, "entry_points", lambda group: eps if group == registry.GROUP else [])
    drivers, errors = registry.load_drivers()
    assert drivers["acme"] is Acme and drivers["wecom"] is registry.BUILTIN["wecom"]
    assert set(errors) == {"broken", "bad", "clash"}


def wait(cond, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while not cond():
        if time.time() > deadline:
            return False
        time.sleep(0.02)
    return True


def test_the_driver_template_works_through_the_bridge(make, monkeypatch):
    """不变量 25 对样板驱动（examples/im-driver，企业内部 IM 照它改）同样成立：经同一个桥，先绑定才进网络，
    群里只回“请私聊”；能改消息的 IM 先发占位、再改成正式回复。"""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "im-driver"))
    from matrix.im.testing import assert_driver
    from matrix_im_demo.devserver import DevServer
    from matrix_im_demo.driver import DemoDriver

    assert_driver(DemoDriver)
    srv = DevServer()
    monkeypatch.setenv("DEMO_IM_URL", srv.url)
    try:
        net, b = make(drivers=(DemoDriver,))
        [alice] = users(net, "alice@x.io")
        net.ensure_assistant(alice)
        out = b.create_channel("demo-im", {"app_id": "app-1", "app_secret": "demo-secret"}, alice)
        cid = out["channel"]["id"]
        assert wait(lambda: net.store.im_channels[cid].status == "online")
        srv.say("bob", "你好")
        assert wait(lambda: srv.texts("bob") == [UNBOUND_TEXT])
        srv.say("alice-im", out["code"])
        assert wait(lambda: any("绑定好了" in t for t in srv.texts("alice-im")))
        srv.say("alice-im", "你好")
        assert wait(lambda: srv.texts("alice-im")[-1] == "回复：你好")
        assert any(f["op"] == "send" and f["text"] == WORKING_TEXT for f in srv.sent)  # 先占位，后改成回复
        assert home_texts(net, alice) == ["你好"]
        srv.say("alice-im", "@助理 看一下", group="研发群")
        assert wait(lambda: srv.texts("研发群") == [GROUP_TEXT])
        assert home_texts(net, alice) == ["你好"]
    finally:
        srv.close()
