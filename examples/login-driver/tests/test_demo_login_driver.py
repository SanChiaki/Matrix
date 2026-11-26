"""演示 SSO 驱动自己的测试：结构检查 + 对着本地的演示 SSO 走完登录、票据不对、密钥不对、票据不能重放；
核实业务系统转来的登录会话 cookie（verify）。

Matrix 这边的规则（只认发起登录的浏览器、身份怎么对到用户、谁能进来）已经由 Matrix 的 tests/test_login.py 对任何驱动
保证了，驱动只测自己和身份系统之间的那一段。接你们的统一登录时照这个写：起一个假的身份系统，把驱动的地址指过去。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from matrix.engine import Network
from matrix.login.base import Flow, LoginError
from matrix.login.service import LoginService
from matrix.login.testing import assert_driver
from matrix.runtime import ScriptedRuntime
from matrix.server import create_app
from matrix_login_demo.devserver import DevServer
from matrix_login_demo.driver import DemoDriver


@pytest.fixture
def sso():
    srv = DevServer().start()
    yield srv
    srv.stop()


def pick(authorize_url: str, uid: str) -> dict[str, str]:
    """扮演用户在演示 SSO 的登录页上点了一个账号：返回跳回应用时带的查询参数。"""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({})).open(f"{authorize_url}&uid={uid}")
    except urllib.error.HTTPError as e:
        assert e.code == 302
        return {k: v[0] for k, v in parse_qs(urlsplit(e.headers["Location"]).query).items()}
    raise AssertionError("演示 SSO 没有跳回应用")


def driver(sso: DevServer, **over) -> DemoDriver:
    return DemoDriver({"url": sso.url, "app_id": "matrix", "app_secret": sso.secret, **over})


def test_structure():
    assert_driver(DemoDriver)
    for bad in ({}, {"url": "ftp://x", "app_id": "a", "app_secret": "s"}, {"url": "http://x", "app_id": "a"}):
        with pytest.raises(LoginError):
            DemoDriver(bad)


def test_ticket_login(sso: DevServer):
    d = driver(sso)
    flow = Flow(state="st4te", redirect_uri="http://matrix.test/api/auth/sso/corp/callback")
    params = pick(d.authorize_url(flow), "u1001")
    assert params["state"] == "st4te"                      # state 跟着回跳地址回来了
    who = d.identify(flow, params)
    assert (who.subject, who.email, who.name) == ("u1001", "maya@corp.example", "Maya")
    with pytest.raises(LoginError):                        # 票据只能用一次
        d.identify(flow, params)


def test_rejects_bad_ticket_secret_and_service(sso: DevServer):
    d = driver(sso)
    flow = Flow(state="s1", redirect_uri="http://matrix.test/api/auth/sso/corp/callback")
    with pytest.raises(LoginError, match="没有票据"):
        d.identify(flow, {})
    with pytest.raises(LoginError):
        d.identify(flow, {"ticket": "made-up"})
    with pytest.raises(LoginError, match="401"):           # 应用密钥不对
        driver(sso, app_secret="wrong").identify(flow, pick(d.authorize_url(flow), "u1001"))
    other = Flow(state="s2", redirect_uri="http://matrix.test/api/auth/sso/corp/callback")
    with pytest.raises(LoginError):                        # 别的登录拿到的票据，用不到这一次上
        d.identify(other, pick(d.authorize_url(flow), "u1001"))
    with pytest.raises(LoginError, match="连不上"):
        DemoDriver({"url": "http://127.0.0.1:9", "app_id": "matrix", "app_secret": "s"}).identify(flow, {"ticket": "t"})


def test_through_matrix(sso: DevServer, tmp_path: Path, monkeypatch):
    """装进 Matrix 走一遍：login.json 里配这个驱动，点登录 → 演示 SSO → 回调 → 已登录。"""
    monkeypatch.setenv("DEMO_SSO_KEY", sso.secret)
    path = tmp_path / "login.json"
    path.write_text(json.dumps({"providers": [{"id": "corp", "name": "公司账号", "driver": "demo-sso", "config": {
        "url": sso.url, "app_id": "matrix", "app_secret": "${DEMO_SSO_KEY}"}}]}), encoding="utf-8")
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path / "data", seed=False)
    c = TestClient(create_app(net, login=LoginService(path, drivers={"demo-sso": DemoDriver})), follow_redirects=False)
    r = c.get("/api/auth/sso/corp/start")
    assert r.status_code == 303
    r = c.get("/api/auth/sso/corp/callback", params=pick(r.headers["location"], "u1002"))
    assert r.headers["location"] == "/"
    assert c.get("/api/me").json()["email"] == "bo@corp.example"


def test_verify_shared_session(sso: DevServer):
    """verify：业务系统的后端转来的登录会话 cookie 的值。有效时认得出人，退出后、编造的、密钥不对的都不认。"""
    d = driver(sso)
    assert DemoDriver.can_verify()
    sid = sso.open_session("u1002")
    who = d.verify(sid)
    assert (who.subject, who.email) == ("u1002", "bo@corp.example")
    with pytest.raises(LoginError, match="401"):
        driver(sso, app_secret="wrong").verify(sid)
    with pytest.raises(LoginError):
        d.verify("made-up")
    sso.close_session(sid)
    with pytest.raises(LoginError):
        d.verify(sid)


def test_backend_calls_matrix_with_the_shared_session(sso: DevServer, tmp_path: Path, monkeypatch):
    """装进 Matrix：login.json 打开 api_auth，业务系统的后端带着用户的登录会话 cookie 调 /api/*，就是那个用户。"""
    monkeypatch.setenv("DEMO_SSO_KEY", sso.secret)
    path = tmp_path / "login.json"
    path.write_text(json.dumps({"providers": [{"id": "corp", "name": "公司账号", "driver": "demo-sso", "api_auth": True,
                                               "config": {"url": sso.url, "app_id": "matrix",
                                                          "app_secret": "${DEMO_SSO_KEY}"}}]}), encoding="utf-8")
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path / "data", seed=False)
    backend = TestClient(create_app(net, login=LoginService(path, drivers={"demo-sso": DemoDriver})))
    sid = sso.open_session("u1001")
    me = backend.get("/api/me", headers={"Authorization": f"Bearer {sid}"})
    assert me.status_code == 200 and me.json()["email"] == "maya@corp.example"
    assert backend.get("/api/me", headers={"Authorization": "Bearer made-up"}).status_code == 401
