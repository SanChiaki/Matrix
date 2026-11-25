"""登录会话：令牌只存哈希、闲置与最长过期、旧状态里的明文令牌升级后不掉线（docs/control-plane.md 第 8 节，阶段 C0 的一部分）。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from matrix import auth
from matrix.engine import Network
from matrix.runtime import ScriptedRuntime
from matrix.server import create_app


def _net(tmp_path: Path) -> Network:
    return Network(ScriptedRuntime(lambda call: "好的"), tmp_path, seed=False)


def _register(c: TestClient, email: str = "a@example.com") -> str:
    r = c.post("/api/auth/register", json={"email": email, "password": "password-123"})
    assert r.status_code == 200, r.text
    return c.cookies[auth.COOKIE]


def test_state_holds_only_token_hash(tmp_path: Path):
    net = _net(tmp_path)
    c = TestClient(create_app(net))
    token = _register(c)
    text = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert token not in text, "状态文件里不能有登录令牌明文"
    assert hashlib.sha256(token.encode()).hexdigest() in json.loads(text)["sessions"]
    assert c.get("/api/me").status_code == 200
    c.post("/api/auth/logout")
    assert not net.store.sessions and c.get("/api/me").status_code == 401


def test_plaintext_sessions_from_old_state_keep_working(tmp_path: Path):
    """旧版本以明文令牌作键：升级后换成哈希，用户不掉线；旧记录没有“最近用过”，不因此被当成闲置。"""
    net = _net(tmp_path)
    c = TestClient(create_app(net))
    _register(c)
    raw = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    uid = next(iter(raw["sessions"].values()))["user_id"]
    old_token = "old-plaintext-token-from-v0"
    raw["sessions"] = {old_token: {"user_id": uid, "created_at": time.time() - 10 * 24 * 3600}}
    (tmp_path / "state.json").write_text(json.dumps(raw), encoding="utf-8")
    again = _net(tmp_path)
    c2 = TestClient(create_app(again))
    c2.cookies.set(auth.COOKIE, old_token)
    assert c2.get("/api/me").status_code == 200
    again.store.save()
    assert old_token not in (tmp_path / "state.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("field,age", [("last_seen", auth.SESSION_IDLE_S + 60), ("created_at", auth.SESSION_MAX_S + 60)])
def test_sessions_expire(tmp_path: Path, field: str, age: int):
    net = _net(tmp_path)
    c = TestClient(create_app(net))
    token = _register(c)
    s = net.store.sessions[auth.session_hash(token)]
    s[field] = time.time() - age
    if field == "created_at":
        s["last_seen"] = time.time()  # 一直在用，也到最长期限
    assert c.get("/api/me").status_code == 401
    assert auth.session_hash(token) not in net.store.sessions, "过期的顺手删掉"


def test_last_seen_touched_at_most_hourly(tmp_path: Path):
    net = _net(tmp_path)
    c = TestClient(create_app(net))
    token = _register(c)
    s = net.store.sessions[auth.session_hash(token)]
    first = s["last_seen"]
    assert c.get("/api/me").status_code == 200
    assert s["last_seen"] == first, "一小时内不重复记"
    s["last_seen"] = time.time() - auth.SESSION_TOUCH_S - 5
    assert c.get("/api/me").status_code == 200
    assert s["last_seen"] > time.time() - 5


def test_cookie_secure_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MATRIX_COOKIE_SECURE", "on")
    c = TestClient(create_app(_net(tmp_path)), base_url="https://testserver")
    r = c.post("/api/auth/register", json={"email": "a@example.com", "password": "password-123"})
    cookie = r.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and f"Max-Age={auth.SESSION_MAX_S}" in cookie
