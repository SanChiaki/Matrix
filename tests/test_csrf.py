"""跨站防护（docs/control-plane.md 第 8 节、第 11 节 C0 的验收；matrix/web_guard.py）：改状态的请求要同站来源 +
双提交的 CSRF 令牌；不靠 cookie 的入口（/internal/*、钩子、连接器配对、端侧与沙箱的 WebSocket）不受影响；
经 HTTPS 时 cookie 加 Secure。这里用不带 Origin 与令牌的原始客户端（tests/conftest.py 的 RawTestClient）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from matrix import auth
from matrix.engine import Network
from matrix.runtime import ScriptedRuntime
from matrix.server import create_app
from matrix.web_guard import CSRF_COOKIE, CSRF_HEADER, same_site
from tests.conftest import RawTestClient

ORIGIN = "http://testserver"


def raw(tmp_path: Path) -> tuple[Network, RawTestClient]:
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    auth.register(net.store, "a@x.io", "password-123")
    return net, RawTestClient(create_app(net))


def signed_in(c: RawTestClient) -> str:
    """像页面一样：先 GET 拿到 CSRF cookie，再带上它登录。返回令牌。"""
    c.get("/api/info")
    token = c.cookies.get(CSRF_COOKIE)
    assert token
    r = c.post("/api/auth/login", json={"email": "a@x.io", "password": "password-123"},
               headers={"origin": ORIGIN, CSRF_HEADER: token})
    assert r.status_code == 200, r.text
    return token


def test_cross_site_state_changes_are_refused(tmp_path: Path):
    """没有 Origin、错误的 Origin、缺 CSRF 令牌、令牌对不上：一律 403，状态没变；同站带令牌的照常。"""
    net, c = raw(tmp_path)
    token = signed_in(c)
    body = {"name": "Xa", "description": "xx", "authority": []}
    n = len(net.store.agents)
    for headers in ({CSRF_HEADER: token},                                            # 没有 Origin / Referer
                    {"origin": "https://evil.example", CSRF_HEADER: token},          # 别的站点
                    {"origin": "null", CSRF_HEADER: token},                          # 沙箱化的页面
                    {"origin": "http://testserver.evil.example", CSRF_HEADER: token},
                    {"origin": ORIGIN},                                              # 缺令牌
                    {"origin": ORIGIN, CSRF_HEADER: "forged"}):                      # 令牌对不上
        r = c.post("/api/agents", json=body, headers=headers)
        assert r.status_code == 403 and "跨站请求被拒绝" in r.json()["detail"], headers
    assert len(net.store.agents) == n
    assert c.post("/api/agents", json=body, headers={"origin": ORIGIN, CSRF_HEADER: token}).status_code == 200
    assert c.post("/api/agents", json={**body, "name": "Xb"},
                  headers={"referer": f"{ORIGIN}/#/agents", CSRF_HEADER: token}).status_code == 200
    assert c.delete(f"/api/agents/{next(iter(net.store.agents))}", headers={CSRF_HEADER: token}).status_code == 403
    assert c.get("/api/me").status_code == 200                                      # 读不需要


def test_login_and_logout_are_guarded_too(tmp_path: Path):
    """登录也要同站（防“登录到攻击者的账号”），退出同样（防被人强制退出）。"""
    net, c = raw(tmp_path)
    assert c.post("/api/auth/login", json={"email": "a@x.io", "password": "password-123"}).status_code == 403
    assert c.post("/api/auth/register", json={"email": "b@x.io", "password": "password-123"},
                  headers={"origin": "https://evil.example"}).status_code == 403
    token = signed_in(c)
    assert c.post("/api/auth/logout", headers={"origin": "https://evil.example", CSRF_HEADER: token}).status_code == 403
    assert c.get("/api/me").status_code == 200


def test_entries_without_cookies_are_not_affected(tmp_path: Path):
    """凭令牌而不是 cookie 的入口：/internal/*、插件钩子、连接器配对、端侧与沙箱的 WebSocket 不要 Origin 与 CSRF 令牌。"""
    net, c = raw(tmp_path)
    r = c.post("/internal/tools", json={"token": "bad", "tool": "x", "args": {}})
    assert r.status_code != 403 and "跨站" not in r.text
    for path in ("/internal/connectors/list", "/internal/approval", "/internal/question"):
        r = c.post(path, json={"token": "bad"})
        assert "跨站" not in r.text, path
    assert "跨站" not in c.post("/hooks/nope", json={}).text
    p = c.post("/api/devices/pair", json={"name": "laptop", "hostname": "h", "harnesses": []})
    assert p.status_code == 200
    assert c.post("/api/devices/pair/poll", json={"device_code": p.json()["device_code"]}).json()["status"] == "pending"
    for url in ("/api/devices/connect?token=bad", "/api/sandbox/connect?token=bad"):
        with pytest.raises(WebSocketDisconnect) as e, c.websocket_connect(url) as ws:
            ws.receive_text()
        assert e.value.code != 403


def test_csrf_cookie_is_issued_and_secure_behind_https(tmp_path: Path, monkeypatch):
    """第一次请求就发 CSRF cookie；本机 http 不加 Secure；声明了 https 的公开地址（TLS 反向代理后面）时两个 cookie 都加 Secure。"""
    net, c = raw(tmp_path)
    r = c.get("/api/info")
    assert CSRF_COOKIE in r.headers["set-cookie"] and "Secure" not in r.headers["set-cookie"]
    assert "HttpOnly" not in r.headers["set-cookie"]                               # 页面要读得到
    token = c.cookies.get(CSRF_COOKIE)
    r = c.post("/api/auth/login", json={"email": "a@x.io", "password": "password-123"},
               headers={"origin": ORIGIN, CSRF_HEADER: token})
    assert "Secure" not in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    assert "set-cookie" not in c.get("/api/info").headers                          # 已经有了就不再发
    monkeypatch.setenv("MATRIX_PUBLIC_URL", "https://matrix.example.com")
    assert "Secure" in RawTestClient(create_app(net)).get("/api/info").headers["set-cookie"]
    c2 = RawTestClient(create_app(net), base_url="https://testserver")   # Secure 的 cookie 只经 https 回来
    c2.get("/api/info")
    t2 = c2.cookies.get(CSRF_COOKIE)
    # 公开地址的来源也认（反向代理改了 Host 的部署）
    r = c2.post("/api/auth/login", json={"email": "a@x.io", "password": "password-123"},
                headers={"origin": "https://matrix.example.com", CSRF_HEADER: t2})
    assert r.status_code == 200 and "Secure" in r.headers["set-cookie"]
    https = RawTestClient(create_app(net), base_url="https://testserver")
    monkeypatch.delenv("MATRIX_PUBLIC_URL")
    assert "Secure" in https.get("/api/info").headers["set-cookie"]               # 请求本身是 https


def test_forwarded_proto_alone_is_not_trusted(tmp_path: Path):
    """不无条件信任 X-Forwarded-Proto：测试客户端的来源不在 uvicorn 的 FORWARDED_ALLOW_IPS 里，这个头不改 scheme。"""
    net, c = raw(tmp_path)
    assert "Secure" not in c.get("/api/info", headers={"x-forwarded-proto": "https"}).headers["set-cookie"]


@pytest.mark.parametrize("origin,referer,host,ok", [
    ("http://127.0.0.1:8765", None, "127.0.0.1:8765", True),
    ("http://127.0.0.1:8766", None, "127.0.0.1:8765", False),
    ("https://m.example.com", None, "m.example.com", True),          # 浏览器 https，代理转来的 Host 没端口
    (None, "https://m.example.com/x?y", "m.example.com", True),
    (None, None, "m.example.com", False),
    ("javascript:alert(1)", None, "m.example.com", False),
    ("https://M.Example.com:443", None, "m.example.com", True),
])
def test_same_site_rules(origin, referer, host, ok):
    assert same_site(origin, referer, host, "http") is ok
