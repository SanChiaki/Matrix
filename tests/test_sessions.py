"""登录加固（docs/control-plane.md 第 8 节与第 11 节 C0 的验收）：令牌只存哈希、会话过期与轮换、我的会话、
旧 state.json 的明文会话迁移。CSRF 与限流的测试在 test_csrf.py、test_throttle.py。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from matrix import auth
from matrix.engine import Network
from matrix.runtime import ScriptedRuntime
from matrix.server import create_app
from matrix.store import Store


def net_of(tmp_path: Path) -> Network:
    return Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)


def accounts(net: Network, *emails: str) -> None:
    """建账号（auth.register 顺带登录一次：那个会话不算，清掉）。"""
    for e in emails:
        auth.register(net.store, e, "password-123")
    net.store.sessions.clear()


def login(net: Network, email: str = "a@x.io", password: str = "password-123") -> TestClient:
    c = TestClient(create_app(net))
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return c


def test_state_has_no_plaintext_session_tokens(tmp_path: Path):
    """令牌只存 sha256：直接读 state.json 找不到 cookie 里的令牌，但哈希在。"""
    net = net_of(tmp_path)
    auth.register(net.store, "a@x.io", "password-123")
    c = login(net)
    token = c.cookies.get(auth.COOKIE)
    raw = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert token and token not in raw
    assert auth.token_hash(token) in raw
    assert c.get("/api/me").status_code == 200


def test_old_state_with_plaintext_sessions_migrates_in_place(tmp_path: Path):
    """旧版 state.json 以明文令牌作键：能读，读的时候就地换成哈希写回，原来的令牌照样能用（不用重新登录）。"""
    net = net_of(tmp_path)
    uid = auth.register(net.store, "a@x.io", "password-123")[0].id
    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    import time
    old = "x" * 10 + "OldPlainToken_-123456789012345678901"   # 旧版的令牌：token_urlsafe(32)，43 个字符
    data["sessions"] = {old: {"user_id": uid, "created_at": time.time() - 3600}}
    (tmp_path / "state.json").write_text(json.dumps(data), encoding="utf-8")
    store = Store(tmp_path, seed=False)
    raw = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert old not in raw and auth.token_hash(old) in raw          # 加载时已经写回成哈希
    assert auth.user_of(store, old).id == uid
    net2 = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    c = TestClient(create_app(net2))
    c.cookies.set(auth.COOKIE, old)
    assert c.get("/api/me").json()["email"] == "a@x.io"
    row = c.get("/api/auth/sessions").json()[0]
    assert row["current"] and row["id"] and "token" not in json.dumps(row) and old not in json.dumps(row)


def test_sessions_expire_when_idle_or_too_old(tmp_path: Path, monkeypatch):
    """闲置 7 天过期、最长 30 天（都可配）；过期后请求按没登录处理。"""
    net = net_of(tmp_path)
    auth.register(net.store, "a@x.io", "password-123")
    c = login(net)
    token = c.cookies.get(auth.COOKIE)
    now = auth.time.time()
    day = 86400
    assert auth.user_of(net.store, token, now + 6 * day) is not None       # 6 天没用：还在，最近使用刷新
    assert auth.user_of(net.store, token, now + 12 * day) is not None      # 离上次使用 6 天
    assert auth.user_of(net.store, token, now + 31 * day) is None          # 超过最长期限
    assert auth.token_hash(token) not in net.store.sessions                 # 过期的顺手删掉
    c = login(net)
    monkeypatch.setattr(auth.time, "time", lambda: now + 8 * day)          # 闲置 8 天
    assert c.get("/api/me").status_code == 401
    monkeypatch.undo()
    monkeypatch.setenv("MATRIX_SESSION_IDLE_DAYS", "1")
    c = login(net)
    monkeypatch.setattr(auth.time, "time", lambda: now + 2 * day)
    assert c.get("/api/me").status_code == 401


def test_change_password_rotates_and_revokes_other_sessions(tmp_path: Path):
    """改密码：换新令牌，这个人其余的会话全部作废；别人的会话不受影响。"""
    net = net_of(tmp_path)
    auth.register(net.store, "a@x.io", "password-123")
    auth.register(net.store, "b@x.io", "password-123")
    laptop, phone, bob = login(net), login(net), login(net, "b@x.io")
    before = laptop.cookies.get(auth.COOKIE)
    assert laptop.post("/api/auth/password", json={"old": "wrong-pass", "new": "new-password-1"}).status_code == 400
    assert laptop.post("/api/auth/password", json={"old": "password-123", "new": "new-password-1"}).status_code == 200
    assert laptop.cookies.get(auth.COOKIE) != before                       # 换了新令牌
    assert laptop.get("/api/me").status_code == 200
    assert phone.get("/api/me").status_code == 401                        # 其余会话作废
    stale = TestClient(create_app(net))
    stale.cookies.set(auth.COOKIE, before)
    assert stale.get("/api/me").status_code == 401                        # 发起改密码的旧令牌也作废
    assert bob.get("/api/me").status_code == 200
    assert TestClient(create_app(net)).post("/api/auth/login", json={"email": "a@x.io", "password": "new-password-1"}).status_code == 200


def test_login_rotates_the_token_it_came_with(tmp_path: Path):
    """带着旧 cookie 再登录：拿到新令牌，旧的那个会话作废，不叠着留。"""
    net = net_of(tmp_path)
    accounts(net, "a@x.io")
    c = login(net)
    old = c.cookies.get(auth.COOKIE)
    assert c.post("/api/auth/login", json={"email": "a@x.io", "password": "password-123"}).status_code == 200
    assert c.cookies.get(auth.COOKIE) != old and auth.token_hash(old) not in net.store.sessions
    assert len([s for s in net.store.sessions.values()]) == 1


def test_my_sessions_list_and_revoke(tmp_path: Path):
    """我的会话：只看得到自己的，不含令牌；逐个下线、一键下线其他所有。"""
    net = net_of(tmp_path)
    accounts(net, "a@x.io", "b@x.io")
    a1, a2, a3, b = login(net), login(net), login(net), login(net, "b@x.io")
    rows = a1.get("/api/auth/sessions", headers={"user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) Chrome/120"}).json()
    assert len(rows) == 3 and rows[0]["current"] and sum(r["current"] for r in rows) == 1
    assert all(set(r) == {"id", "created_at", "last_used", "ip", "ua", "current"} for r in rows)
    other = next(r for r in rows if not r["current"])
    bob_sid = b.get("/api/auth/sessions").json()[0]["id"]
    assert a1.delete(f"/api/auth/sessions/{bob_sid}").status_code == 400   # 别人的会话：不存在
    assert b.get("/api/me").status_code == 200
    assert a1.delete(f"/api/auth/sessions/{other['id']}").status_code == 200
    assert sum(c.get("/api/me").status_code == 200 for c in (a2, a3)) == 1
    assert a1.post("/api/auth/sessions/others").json() == {"revoked": 1}
    assert a2.get("/api/me").status_code == 401 and a3.get("/api/me").status_code == 401
    assert a1.get("/api/me").status_code == 200 and b.get("/api/me").status_code == 200


def test_session_meta_is_a_summary(tmp_path: Path):
    from matrix.server import ua_summary
    assert ua_summary("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36") == "Chrome · Windows"
    assert ua_summary("curl/8.5.0") == "curl"


@pytest.mark.parametrize("bad", ["", "0", "-3", "abc"])
def test_bad_session_settings_fall_back_to_defaults(monkeypatch, bad):
    monkeypatch.setenv("MATRIX_SESSION_MAX_DAYS", bad)
    assert auth.session_limits()[1] == 30 * 86400
