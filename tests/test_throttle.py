"""登录限流（docs/control-plane.md 第 8 节、第 11 节 C0 的验收）：按账号与按来源地址分别限流，连续失败后指数退避；
限流期间正确的密码也不放行；过期后恢复；限流的响应对存在与不存在的账号一样。注册、重置链接、改密码同样限流。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from matrix import auth
from matrix.engine import Network
from matrix.runtime import ScriptedRuntime
from matrix.server import create_app


class Clock:
    def __init__(self, monkeypatch):
        self.t = 1_800_000_000.0
        monkeypatch.setattr(auth.time, "time", lambda: self.t)

    def tick(self, s: float) -> None:
        self.t += s


def setup(tmp_path: Path, monkeypatch):
    clock = Clock(monkeypatch)
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    auth.register(net.store, "a@x.io", "password-123")
    app = create_app(net)
    return net, app, clock


def client(app, ip: str = "10.0.0.1") -> TestClient:
    return TestClient(app, client=(ip, 50000))


def attempt(c: TestClient, email: str, password: str):
    return c.post("/api/auth/login", json={"email": email, "password": password})


def test_repeated_failures_lock_the_account_even_for_the_right_password(tmp_path: Path, monkeypatch):
    """连续输错 5 次以后开始退避（2、4、8 … 秒）；限流期间正确的密码也回 429，带 Retry-After；时间到了恢复，成功后清零。"""
    net, app, clock = setup(tmp_path, monkeypatch)
    c = client(app)
    for _ in range(5):
        assert attempt(c, "a@x.io", "wrong-pass").status_code == 400
    assert attempt(c, "a@x.io", "wrong-pass").status_code == 400          # 第 6 次：记下，开始退避 2 秒
    r = attempt(c, "a@x.io", "password-123")
    assert r.status_code == 429 and r.headers["retry-after"] == "2" and "2 秒后再试" in r.json()["detail"]
    clock.tick(2.5)
    assert attempt(c, "a@x.io", "wrong-pass").status_code == 400          # 第 7 次：退避 4 秒
    assert attempt(c, "a@x.io", "password-123").status_code == 429
    clock.tick(3)
    assert attempt(c, "a@x.io", "password-123").status_code == 429        # 还没到
    clock.tick(1.5)
    assert attempt(c, "a@x.io", "password-123").status_code == 200        # 到了：恢复
    for _ in range(5):                                                    # 成功清零：又有 5 次免费的
        assert attempt(c, "a@x.io", "wrong-pass").status_code == 400
    assert attempt(c, "a@x.io", "password-123").status_code == 200


def test_backoff_is_exponential_and_capped():
    t = auth.Throttle(free=2, base=1, cap=10)
    waits = []
    for _ in range(8):
        t.fail("k")
        waits.append(round(t.wait("k")))
    assert waits == [0, 0, 1, 2, 4, 8, 10, 10]


def test_throttle_does_not_reveal_whether_the_account_exists(tmp_path: Path, monkeypatch):
    """不存在的账号：同样的 400 说明，同样的退避与 429 说明。"""
    net, app, clock = setup(tmp_path, monkeypatch)
    c = client(app)
    seen = {}
    for email in ("a@x.io", "ghost@x.io"):
        rs = [attempt(c, email, "wrong-pass") for _ in range(6)] + [attempt(c, email, "password-123")]
        seen[email] = [(r.status_code, r.json()["detail"], r.headers.get("retry-after")) for r in rs]
    assert seen["a@x.io"] == seen["ghost@x.io"]
    assert seen["ghost@x.io"][-1][0] == 429


def test_account_and_address_are_limited_separately(tmp_path: Path, monkeypatch):
    """换来源地址试同一个账号：账号的限流照样生效；同一个地址试很多账号：地址的限流生效，别的地址不受影响。"""
    net, app, clock = setup(tmp_path, monkeypatch)
    for i in range(6):
        attempt(client(app, f"10.0.1.{i}"), "a@x.io", "wrong-pass")
    assert attempt(client(app, "10.0.2.1"), "a@x.io", "password-123").status_code == 429
    clock.tick(3600 * 2)
    auth.register(net.store, "b@x.io", "password-123")
    spray = client(app, "10.9.9.9")
    for i in range(21):
        assert attempt(spray, f"u{i}@x.io", "wrong-pass").status_code == 400
    assert attempt(spray, "b@x.io", "password-123").status_code == 429    # 这个地址被限了
    assert attempt(client(app, "10.9.9.8"), "b@x.io", "password-123").status_code == 200


def test_registration_and_reset_are_limited(tmp_path: Path, monkeypatch):
    net, app, clock = setup(tmp_path, monkeypatch)
    c = client(app)
    for i in range(5):
        assert c.post("/api/auth/register", json={"email": f"n{i}@x.io", "password": "password-123"}).status_code == 200
    r = c.post("/api/auth/register", json={"email": "n9@x.io", "password": "password-123"})
    assert r.status_code == 429 and "n9@x.io" not in [u.email for u in net.store.users.values()]
    for _ in range(11):
        assert c.post("/api/auth/reset/check", json={"token": "guess"}).status_code == 400
    assert c.post("/api/auth/reset/check", json={"token": "guess"}).status_code == 429


def test_change_password_is_limited_by_user(tmp_path: Path, monkeypatch):
    """拿到别人的会话也不能拿改密码去试原密码。"""
    net, app, clock = setup(tmp_path, monkeypatch)
    c = client(app)
    assert attempt(c, "a@x.io", "password-123").status_code == 200
    for _ in range(6):
        assert c.post("/api/auth/password", json={"old": "nope-nope", "new": "new-password-1"}).status_code == 400
    assert c.post("/api/auth/password", json={"old": "password-123", "new": "new-password-1"}).status_code == 429


def test_pairing_limits_are_kept():
    """设备配对已有的上限不动（同时等待的申请、每人输错用户码的次数）。"""
    assert auth.PAIR_MAX_MISSES == 10 and auth.PAIR_MAX_PENDING == 64
