"""设备码配对（docs/device-runtimes.md 第 2.5 节）：连接器不再需要人复制令牌，人在已登录的网页里批准。"""
from fastapi.testclient import TestClient

from matrix import auth
from matrix.server import create_app
from tests.test_devices import build, hello
from tests.test_users import client


def start(c: TestClient, **body) -> dict:
    r = c.post("/api/devices/pair", json={"name": "oam 的 MacBook", "hostname": "mbp.local",
                                          "harnesses": ["claude-code", "codex"], **body})
    assert r.status_code == 200, r.text
    return r.json()


def poll(c: TestClient, code: str) -> dict:
    return c.post("/api/devices/pair/poll", json={"device_code": code}).json()


def test_pairing_round_trip(tmp_path):
    net = build(tmp_path)
    anon = TestClient(create_app(net))           # 连接器：没有登录
    p = start(anon)
    assert p["user_code"] and p["device_code"] and p["verify_path"].endswith(p["user_code"])
    assert len(p["user_code"].replace("-", "")) == 8 and p["interval"] >= 1
    assert poll(anon, p["device_code"]) == {"status": "pending"}

    alice = client(net, "alice@x.io")
    seen = alice.get(f"/api/devices/pair/{p['user_code']}").json()
    assert seen["hostname"] == "mbp.local" and seen["harnesses"] == ["claude-code", "codex"]
    assert "device_code" not in seen                     # 批准页拿不到轮询用的密钥
    dev = alice.post(f"/api/devices/pair/{p['user_code']}/approve", json={"name": "笔记本"}).json()
    assert dev["name"] == "笔记本" and "token" not in dev

    got = poll(anon, p["device_code"])
    assert got["status"] == "approved" and got["device_id"] == dev["id"] and got["email"] == "alice@x.io"
    assert auth.device_of(net.store, got["token"]).user_id == next(
        u.id for u in net.store.users.values() if u.email == "alice@x.io")
    assert poll(anon, p["device_code"])["status"] == "expired"  # 令牌只交付一次
    assert any(d["id"] == dev["id"] for d in alice.get("/api/devices").json())

    # 拿到的令牌就是普通设备令牌：照常连接
    with anon.websocket_connect(f"/api/devices/connect?token={got['token']}") as ws:
        ws.receive_json()
        ws.send_json(hello())


def test_user_code_is_case_and_dash_insensitive(tmp_path):
    net = build(tmp_path)
    p = start(TestClient(create_app(net)))
    typed = p["user_code"].replace("-", "").lower()
    assert client(net, "alice@x.io").get(f"/api/devices/pair/{typed}").status_code == 200


def test_denied_and_expired(tmp_path):
    net = build(tmp_path)
    anon = TestClient(create_app(net))
    alice = client(net, "alice@x.io")
    p = start(anon)
    assert alice.post(f"/api/devices/pair/{p['user_code']}/deny", json={}).json() == {"ok": True}
    assert poll(anon, p["device_code"])["status"] == "denied"
    assert alice.post(f"/api/devices/pair/{p['user_code']}/approve", json={}).status_code == 404

    q = start(anon)
    net.pairings.now = lambda: 1e12               # 过期
    assert poll(anon, q["device_code"])["status"] == "expired"
    assert alice.get(f"/api/devices/pair/{q['user_code']}").status_code == 404
    assert poll(anon, "nope")["status"] == "expired"
    assert net.store.devices == {}                 # 没批准就没有设备


def test_approval_requires_login(tmp_path):
    net = build(tmp_path)
    anon = TestClient(create_app(net))
    p = start(anon)
    assert anon.get(f"/api/devices/pair/{p['user_code']}").status_code == 401
    assert anon.post(f"/api/devices/pair/{p['user_code']}/approve", json={}).status_code == 401


def test_guessing_user_codes_is_limited(tmp_path):
    """人输错码有上限：用户码只有 8 位，挡住登录用户逐个试别人的码。"""
    net = build(tmp_path)
    alice = client(net, "alice@x.io")
    codes = [alice.get(f"/api/devices/pair/AAAA{i:04d}").status_code for i in range(auth.PAIR_MAX_MISSES + 1)]
    assert set(codes[:-1]) == {404} and codes[-1] == 429
    p = start(TestClient(create_app(net)))
    assert alice.get(f"/api/devices/pair/{p['user_code']}").status_code == 429  # 超限后正确的码也不受理


def test_pending_pairings_are_bounded(tmp_path):
    """申请不需要登录：同时等待的申请有上限，免得被刷满内存。"""
    net = build(tmp_path)
    anon = TestClient(create_app(net))
    for _ in range(auth.PAIR_MAX_PENDING):
        start(anon)
    assert anon.post("/api/devices/pair", json={}).status_code == 429


def test_pairing_input_is_trimmed(tmp_path):
    net = build(tmp_path)
    anon = TestClient(create_app(net))
    p = start(anon, name="x" * 500, hostname="h" * 500, harnesses=["claude-code", "<script>", "codex", "codex"])
    seen = client(net, "alice@x.io").get(f"/api/devices/pair/{p['user_code']}").json()
    assert len(seen["name"]) <= 80 and len(seen["hostname"]) <= 80
    assert seen["harnesses"] == ["claude-code", "codex"]   # 只认已知 harness，去重
