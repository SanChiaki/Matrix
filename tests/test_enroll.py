"""一次性配对令牌（docs/device-runtimes.md 第 2.8 节）与外挂驱动（docs/device-driver-authoring.md）。"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from matrix import auth, models
from matrix.connector import main as connect_main
from matrix.engine import NetworkError
from matrix.connector import pairing
from matrix.server import create_app
from tests.test_devices import build, hello
from tests.test_users import client

AGENT = {"name": "业务助手", "description": "在这台电脑上处理业务单据", "harness": "codex"}


def redeem(c: TestClient, code: str, **body) -> object:
    return c.post("/api/devices/enroll/redeem",
                  json={"code": code, "hostname": "pc-01", "harnesses": ["codex", "claude-code"], **body})


def test_enroll_round_trip_creates_device_and_agent(tmp_path):
    net = build(tmp_path)
    anon = TestClient(create_app(net))
    alice = client(net, "alice@x.io")
    e = alice.post("/api/devices/enroll", json={"name": "前台电脑", "agent": AGENT}).json()
    assert len(e["code"]) >= 40 and e["expires_in"] == auth.ENROLL_TTL_S

    r = redeem(anon, e["code"])
    assert r.status_code == 200, r.text
    got = r.json()
    dev = net.store.devices[got["device_id"]]
    alice_id = next(u.id for u in net.store.users.values() if u.email == "alice@x.io")
    assert dev.user_id == alice_id and dev.name == "前台电脑" and dev.hostname == "pc-01"
    assert dev.harnesses == ["codex", "claude-code"] and got["email"] == "alice@x.io"
    agent = net.store.agents[got["agent_id"]]
    assert agent.owner_id == alice_id and agent.device_runtime == dev.id and agent.harness == "codex"
    assert auth.device_of(net.store, got["token"]).id == dev.id

    assert redeem(anon, e["code"]).status_code == 404          # 只能用一次
    with anon.websocket_connect(f"/api/devices/connect?token={got['token']}") as ws:
        ws.receive_json()
        ws.send_json(hello())


def test_enroll_without_agent_and_name_uses_connector_name(tmp_path):
    net = build(tmp_path)
    alice = client(net, "alice@x.io")
    code = alice.post("/api/devices/enroll", json={}).json()["code"]
    got = redeem(TestClient(create_app(net)), code, name="mbp").json()
    assert got["agent_id"] is None and net.store.devices[got["device_id"]].name == "mbp"


def test_issuing_requires_login(tmp_path):
    net = build(tmp_path)
    assert TestClient(create_app(net)).post("/api/devices/enroll", json={}).status_code == 401


def test_missing_harness_keeps_the_code(tmp_path):
    net = build(tmp_path)
    anon = TestClient(create_app(net))
    code = client(net, "alice@x.io").post("/api/devices/enroll", json={"agent": AGENT}).json()["code"]
    r = redeem(anon, code, harnesses=["claude-code"])
    assert r.status_code == 400 and "codex" in r.json()["detail"]
    assert not net.store.devices                                  # 没留下半成品设备
    assert redeem(anon, code).status_code == 200                  # 装好之后同一条令牌还能用


def test_unknown_harness_is_refused_when_issuing(tmp_path):
    net = build(tmp_path)
    r = client(net, "alice@x.io").post("/api/devices/enroll", json={"agent": {**AGENT, "harness": "nope"}})
    assert r.status_code == 400


def test_expired_and_bounded(tmp_path):
    net = build(tmp_path)
    clock = [1000.0]
    net.enrollments.now = lambda: clock[0]
    alice = client(net, "alice@x.io")
    code = alice.post("/api/devices/enroll", json={}).json()["code"]
    clock[0] += auth.ENROLL_TTL_S + 1
    assert redeem(TestClient(create_app(net)), code).status_code == 404
    for _ in range(auth.ENROLL_MAX_PER_USER):
        assert alice.post("/api/devices/enroll", json={}).status_code == 200
    assert alice.post("/api/devices/enroll", json={}).status_code == 429
    assert client(net, "bob@x.io").post("/api/devices/enroll", json={}).status_code == 200  # 按人计


def test_connector_enroll_against_the_server(tmp_path):
    net = build(tmp_path / "srv")
    anon = TestClient(create_app(net))
    code = client(net, "alice@x.io").post("/api/devices/enroll", json={"agent": AGENT}).json()["code"]
    post = lambda url, body: anon.post(url.removeprefix("http://testserver"), json=body).json()
    conf = pairing.enroll("http://testserver", code, name="笔记本", harnesses=["codex"], post=post)
    assert conf.email == "alice@x.io" and auth.device_of(net.store, conf.token).id == conf.device_id
    assert any(a.device_runtime == conf.device_id for a in net.store.agents.values())


# ================= 外挂驱动与自定义 harness =================
def test_add_harnesses():
    before = dict(models.HARNESSES)
    try:
        assert models.add_harnesses("biz-agent=业务助手, other ,claude-code=改名") == ["biz-agent", "other"]
        assert models.HARNESSES["biz-agent"] == "业务助手" and models.HARNESSES["other"] == "other"
        assert models.HARNESSES["claude-code"] == "Claude Code"   # 内置的不被覆盖
        with pytest.raises(ValueError):
            models.add_harnesses("Bad Name")
    finally:
        models.HARNESSES.clear()
        models.HARNESSES.update(before)


def test_custom_harness_can_be_bound(tmp_path):
    before = dict(models.HARNESSES)
    try:
        models.add_harnesses("biz-agent=业务助手")
        net = build(tmp_path)
        alice = client(net, "alice@x.io")
        code = alice.post("/api/devices/enroll", json={"agent": {**AGENT, "harness": "biz-agent"}}).json()["code"]
        got = redeem(TestClient(create_app(net)), code, harnesses=["biz-agent"]).json()
        assert net.store.agents[got["agent_id"]].harness == "biz-agent"
        assert net.store.devices[got["device_id"]].harnesses == ["biz-agent"]
    finally:
        models.HARNESSES.clear()
        models.HARNESSES.update(before)


class BizDriver:
    name = "biz-agent"

    def __init__(self, *, bin_path=None, bridge_url=""):
        self.bin_path, self.bridge_url = bin_path, bridge_url


def _ep(name, obj):
    return SimpleNamespace(name=name, load=lambda: obj)


def _broken():
    raise ImportError("boom")


def test_external_drivers_are_loaded(monkeypatch):
    eps = [_ep("biz", BizDriver), _ep("dup", type("D", (), {"name": "codex"})),
           _ep("bad", 42), SimpleNamespace(name="broken", load=_broken)]
    monkeypatch.setattr(connect_main, "entry_points", lambda group: eps if group == connect_main.DRIVER_GROUP else [])
    drivers, errors = connect_main.load_drivers()
    assert next(iter(drivers)) == "biz-agent" and drivers["biz-agent"] is BizDriver  # 外部驱动排第一：成为默认
    assert set(drivers) == {"biz-agent", *connect_main.BUILTIN_DRIVERS}
    assert set(errors) == {"dup", "bad", "broken"}
    monkeypatch.setattr(connect_main, "DRIVERS", drivers)
    d = connect_main.build_driver("biz-agent", bridge_url="http://127.0.0.1:1", bin_path="/opt/biz")
    assert isinstance(d, BizDriver) and d.bin_path == "/opt/biz" and d.bridge_url == "http://127.0.0.1:1"


# ================= 一键创建：设备码配对时带上 Agent =================
def test_pairing_with_agent_creates_it_on_approval(tmp_path):
    net = build(tmp_path)
    anon = TestClient(create_app(net))
    p = anon.post("/api/devices/pair", json={"name": "pc", "hostname": "pc-01", "harnesses": ["codex"],
                                             "agent": AGENT}).json()
    alice = client(net, "alice@x.io")
    seen = alice.get(f"/api/devices/pair/{p['user_code']}").json()
    assert seen["agent"] == {"name": "业务助手", "description": AGENT["description"], "harness": "codex", "authority": []}
    dev = alice.post(f"/api/devices/pair/{p['user_code']}/approve", json={}).json()
    agent = net.store.agents[dev["agent_id"]]
    assert agent.device_runtime == dev["id"] and agent.harness == "codex"
    assert agent.owner_id == net.store.devices[dev["id"]].user_id
    assert anon.post("/api/devices/pair/poll", json={"device_code": p["device_code"]}).json()["status"] == "approved"


def test_pairing_agent_harness_must_be_on_the_device(tmp_path):
    net = build(tmp_path)
    anon = TestClient(create_app(net))
    r = anon.post("/api/devices/pair", json={"harnesses": ["claude-code"], "agent": AGENT})
    assert r.status_code == 400 and "codex" in r.json()["detail"]


def test_denied_pairing_creates_no_agent(tmp_path):
    net = build(tmp_path)
    anon = TestClient(create_app(net))
    p = anon.post("/api/devices/pair", json={"harnesses": ["codex"], "agent": AGENT}).json()
    alice = client(net, "alice@x.io")
    before = len(net.store.agents)
    alice.post(f"/api/devices/pair/{p['user_code']}/deny", json={})
    assert len(net.store.agents) == before


def test_redeem_uses_device_agent_when_issuer_gave_none(tmp_path):
    net = build(tmp_path)
    anon = TestClient(create_app(net))
    alice = client(net, "alice@x.io")
    code = alice.post("/api/devices/enroll", json={}).json()["code"]
    got = redeem(anon, code, agent={**AGENT, "name": "设备说的"}).json()
    assert net.store.agents[got["agent_id"]].name == "设备说的"
    code = alice.post("/api/devices/enroll", json={"agent": AGENT}).json()["code"]
    got = redeem(anon, code, agent={**AGENT, "name": "设备说的"}).json()
    assert net.store.agents[got["agent_id"]].name == "业务助手"   # 签令牌的人指定的优先


def test_setup_agent_flag(tmp_path, monkeypatch):
    seen = {}

    def fake_pair(server, **kw):
        seen.update(kw)
        from matrix.connector.config import Config
        return Config(server="http://x", token="t", device_name="pc", email="a@x.io")

    monkeypatch.setattr(pairing, "pair", fake_pair)
    monkeypatch.setattr(connect_main, "detect_drivers", lambda *a, **k: [])
    connect_main.main(["setup", "http://x", "--name", "pc", "--no-service", "--no-browser",
                       "--config", str(tmp_path / "c.json"), "--agent", "业务助手", "--agent-harness", "codex"])
    assert seen["agent"] == {"name": "业务助手", "description": "业务助手（pc）", "harness": "codex"}


def test_one_click_agent_name_taken_gets_a_suffix(tmp_path):
    """Agent 名全站唯一，而同一个业务系统的每个用户带来的是同一个名字：第二个人起不该批准失败。"""
    net = build(tmp_path)
    anon = TestClient(create_app(net))
    names = []
    for email in ("alice@x.io", "bob@x.io", "bob@x.io"):
        p = anon.post("/api/devices/pair", json={"harnesses": ["codex"], "agent": AGENT}).json()
        dev = client(net, email).post(f"/api/devices/pair/{p['user_code']}/approve", json={}).json()
        polled = anon.post("/api/devices/pair/poll", json={"device_code": p["device_code"]}).json()
        assert polled["agent_id"] == dev["agent_id"] and polled["agent_name"] == dev["agent_name"]
        names.append(dev["agent_name"])
    assert names == ["业务助手", "业务助手·bob", "业务助手·bob 2"]
    # 一次性令牌同样
    code = client(net, "alice@x.io").post("/api/devices/enroll", json={"agent": AGENT}).json()["code"]
    r = anon.post("/api/devices/enroll/redeem", json={"code": code, "harnesses": ["codex"]}).json()
    assert r["agent_name"] == "业务助手·alice" and net.store.agents[r["agent_id"]].name == "业务助手·alice"


def test_failed_one_click_agent_hands_out_no_token(tmp_path):
    """Agent 建不成：设备吊销、申请留在等待中，连接器拿不到一个已经作废的令牌。"""
    net = build(tmp_path)
    anon = TestClient(create_app(net))
    p = anon.post("/api/devices/pair", json={"harnesses": ["codex"], "agent": AGENT}).json()
    alice = client(net, "alice@x.io")
    assert alice.get(f"/api/devices/pair/{p['user_code']}").json()["agent"]["authority"] == AGENT.get("authority", [])
    before = len(net.store.agents)

    def boom(*a, **k):
        raise NetworkError("建不成")
    real, net.create_agent = net.create_agent, boom
    assert alice.post(f"/api/devices/pair/{p['user_code']}/approve", json={}).status_code == 409
    assert all(d.revoked for d in net.store.devices.values()) and len(net.store.agents) == before
    assert anon.post("/api/devices/pair/poll", json={"device_code": p["device_code"]}).json() == {"status": "pending"}
    net.create_agent = real
    assert alice.post(f"/api/devices/pair/{p['user_code']}/approve", json={}).json()["agent_name"] == "业务助手"
