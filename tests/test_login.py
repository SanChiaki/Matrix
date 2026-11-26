"""公司账号登录（docs/login-driver-authoring.md）：登录驱动接口、login.json、通用 OAuth2 驱动、身份怎么对到 Matrix 用户。"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient

from matrix import auth
from matrix.engine import Network
from matrix.login.base import Driver, Flow, Identity, LoginError
from matrix.login.oauth2 import OAuth2Driver
from matrix.login.registry import load_drivers
from matrix.login.service import FLOW_COOKIE, FLOW_TTL_S, LoginService
from matrix.login.testing import assert_driver, check_driver
from matrix.runtime import ScriptedRuntime
from matrix.server import create_app


class FakeDriver(Driver):
    """假的身份系统：回调里的 who 就是人（真的驱动要向身份系统核实，这里省掉）。"""

    kind = "fake"
    title = "假登录"

    def authorize_url(self, flow: Flow) -> str:
        return f"https://sso.test/login?state={flow.state}&back={flow.redirect_uri}"

    def identify(self, flow: Flow, params: dict[str, str]) -> Identity:
        if params.get("who") == "boom":
            raise RuntimeError("secret-token-in-message")
        if not params.get("who"):
            raise LoginError("身份系统说不认识你")
        return Identity(subject=params["who"], email=params.get("email", f"{params['who']}@corp.example"))


def setup(tmp_path: Path, provider: dict | None = None, **top) -> tuple[Network, LoginService, Path]:
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path / "data", seed=False)
    path = tmp_path / "login.json"
    path.write_text(json.dumps({"providers": [{"id": "corp", "name": "公司账号", "driver": "fake", **(provider or {})}],
                                **top}), encoding="utf-8")
    return net, LoginService(path, drivers={"fake": FakeDriver}), path


def browser(net: Network, login: LoginService) -> TestClient:
    return TestClient(create_app(net, login=login), follow_redirects=False)


def start(c: TestClient, provider: str = "corp") -> str:
    """点“用公司账号登录”，返回身份系统要原样带回的 state。"""
    r = c.get(f"/api/auth/sso/{provider}/start")
    assert r.status_code == 303, r.text
    return parse_qs(urlsplit(r.headers["location"]).query)["state"][0]


def sign_in(c: TestClient, who: str, **params: str):
    return c.get("/api/auth/sso/corp/callback", params={"state": start(c), "who": who, **params})


def error_of(r) -> str:
    assert r.status_code == 303 and r.headers["location"].startswith("/#login_error="), r.headers.get("location")
    from urllib.parse import unquote
    return unquote(r.headers["location"].split("=", 1)[1])


# ================= 登录与账号 =================
def test_company_login_creates_account_and_signs_in(tmp_path: Path):
    """第一次用公司账号登录：自动建账号（没有密码）、有助理、发的是 Matrix 自己的登录 cookie；再登录还是同一个人。"""
    net, login, _ = setup(tmp_path, public_url="https://matrix.corp.example")
    c = browser(net, login)
    assert c.get("/api/info").json()["login"] == {"providers": [{"id": "corp", "name": "公司账号"}], "password": True}
    r = c.get("/api/auth/sso/corp/start")
    back = parse_qs(urlsplit(r.headers["location"]).query)["back"][0]
    assert back == "https://matrix.corp.example/api/auth/sso/corp/callback"  # 回调地址按 public_url 拼
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    r = c.get("/api/auth/sso/corp/callback", params={"state": state, "who": "u1001"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    me = c.get("/api/me").json()
    assert me["email"] == "u1001@corp.example" and me["agent_count"] == 1 and me["admin"] is True  # 第一个人是管理员
    user = net.store.users[me["id"]]
    assert user.sso == {"corp": "u1001"} and user.password_hash == ""
    assert c.post("/api/auth/login", json={"email": "u1001@corp.example", "password": "password-123"}).status_code == 400

    other = browser(net, login)
    assert sign_in(other, "u1001").headers["location"] == "/"
    assert other.get("/api/me").json()["id"] == me["id"] and len(net.store.users) == 1
    # 绑定以后只认标识：身份系统里换了邮箱，还是这个人，Matrix 里的邮箱不变
    third = browser(net, login)
    sign_in(third, "u1001", email="renamed@corp.example")
    assert third.get("/api/me").json() == me
    # 重启后绑定还在
    assert Network(ScriptedRuntime(lambda call: "ok"), tmp_path / "data", seed=False).store.users[me["id"]].sso == {"corp": "u1001"}


def test_login_flow_is_bound_to_the_browser_and_single_use(tmp_path: Path):
    """一次登录只认发起它的那个浏览器、只能用一次、10 分钟内有效：别人拿到回调地址、换了 state、重放都进不来。"""
    net, login, _ = setup(tmp_path)
    c, attacker = browser(net, login), browser(net, login)
    state = start(c)
    # 另一个浏览器（没有那个一次性 cookie）拿着同样的回调参数
    assert "不是从这个浏览器发起" in error_of(attacker.get("/api/auth/sso/corp/callback", params={"state": state, "who": "u1"}))
    assert attacker.get("/api/me").status_code == 401
    # state 对不上：这次登录作废
    state = start(c)
    assert "对不上" in error_of(c.get("/api/auth/sso/corp/callback", params={"state": "x" + state, "who": "u1"}))
    assert "过期" in error_of(c.get("/api/auth/sso/corp/callback", params={"state": state, "who": "u1"}))
    # 成功以后同一个回调不能再用
    state = start(c)
    assert c.get("/api/auth/sso/corp/callback", params={"state": state, "who": "u1"}).headers["location"] == "/"
    c.cookies.set(FLOW_COOKIE, "whatever")
    assert "过期" in error_of(c.get("/api/auth/sso/corp/callback", params={"state": state, "who": "u1"}))
    # 过期
    clock = [1000.0]
    login.now = lambda: clock[0]
    late = browser(net, login)
    state = start(late)
    clock[0] += FLOW_TTL_S + 1
    assert "过期" in error_of(late.get("/api/auth/sso/corp/callback", params={"state": state, "who": "u1"}))
    # 没有这个入口
    assert "没有这个登录入口" in error_of(c.get("/api/auth/sso/nope/start"))


def test_driver_failures_do_not_sign_in_or_leak(tmp_path: Path):
    """驱动认不出人就不登录；驱动里没想到的异常只给一句笼统的话，异常消息（可能带令牌）不给浏览器。"""
    net, login, _ = setup(tmp_path)
    c = browser(net, login)
    assert error_of(c.get("/api/auth/sso/corp/callback", params={"state": start(c)})) == "身份系统说不认识你"
    msg = error_of(sign_in(c, "boom"))
    assert "secret-token" not in msg and "登录驱动出错" in msg
    assert c.get("/api/me").status_code == 401 and not net.store.users


def test_existing_account_is_linked_only_when_deployer_trusts_emails(tmp_path: Path):
    """公司账号的邮箱和已有账号相同：默认拒绝（不然能在身份系统里填别人邮箱的人就能进别人的账号）；
    部署方打开 link_by_email 才绑上去，绑过的账号不会再被另一个标识顶替。"""
    net, login, path = setup(tmp_path)
    old = auth.register(net.store, "maya@corp.example", "password-123")[0]
    c = browser(net, login)
    assert "已经有账号" in error_of(sign_in(c, "u7", email="Maya@corp.example"))
    assert c.get("/api/me").status_code == 401 and old.sso == {}

    path.write_text(json.dumps({"providers": [{"id": "corp", "driver": "fake", "link_by_email": True}]}), encoding="utf-8")
    import os
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))  # 改了就生效
    assert sign_in(c, "u7", email="maya@corp.example").headers["location"] == "/"
    assert c.get("/api/me").json()["id"] == old.id and old.sso == {"corp": "u7"}
    assert c.post("/api/auth/logout").status_code == 200
    assert c.post("/api/auth/login", json={"email": "maya@corp.example", "password": "password-123"}).status_code == 200  # 密码还在
    # 另一个人在身份系统里拿到了同一个邮箱：不能顶替
    assert "已经有账号" in error_of(sign_in(browser(net, login), "u8", email="maya@corp.example"))
    assert old.sso == {"corp": "u7"}


def test_who_may_come_in(tmp_path: Path):
    """allow_domains 只放这些邮箱域名；auto_create 关掉时只有已经绑定的人能登录；邮箱不像邮箱的不建账号。"""
    net, login, _ = setup(tmp_path, {"allow_domains": ["corp.example"], "auto_create": True})
    c = browser(net, login)
    assert "域名" in error_of(sign_in(c, "x1", email="x1@elsewhere.io"))
    assert "邮箱" in error_of(sign_in(c, "x2", email="not-an-email"))
    assert sign_in(c, "x3").headers["location"] == "/"
    assert [u.email for u in net.store.users.values()] == ["x3@corp.example"]

    net2, login2, _ = setup(tmp_path / "b", {"auto_create": False})
    c2 = browser(net2, login2)
    assert "还没有你的账号" in error_of(sign_in(c2, "y1"))
    u = auth.register(net2.store, "y1@corp.example", "password-123")[0]
    u.sso["corp"] = "y1"
    assert sign_in(c2, "y1").headers["location"] == "/" and c2.get("/api/me").json()["id"] == u.id


def test_password_login_can_be_turned_off(tmp_path: Path):
    """password_login=false：邮箱密码不能注册，登录只留管理员（应急）；没有可用的入口时不生效，不会把所有人关在外面。"""
    net, login, path = setup(tmp_path, password_login=False)
    admin, member = [auth.register(net.store, e, "password-123")[0] for e in ("admin@corp.example", "bo@corp.example")]
    c = browser(net, login)
    assert c.get("/api/info").json()["login"]["password"] is False
    assert c.post("/api/auth/register", json={"email": "new@corp.example", "password": "password-123"}).status_code == 400
    before = len(net.store.sessions)
    r = c.post("/api/auth/login", json={"email": member.email, "password": "password-123"})
    assert r.status_code == 400 and "公司账号" in r.json()["detail"] and c.get("/api/me").status_code == 401
    assert len(net.store.sessions) == before  # 没留下登录
    assert c.post("/api/auth/login", json={"email": admin.email, "password": "password-123"}).status_code == 200

    path.write_text(json.dumps({"password_login": False, "providers": [{"id": "corp", "driver": "missing"}]}), encoding="utf-8")
    import os
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    c2 = browser(net, login)
    assert c2.get("/api/info").json()["login"] == {"providers": [], "password": True}
    assert "missing" in login.errors["corp"]
    assert c2.post("/api/auth/login", json={"email": member.email, "password": "password-123"}).status_code == 200


def test_login_config_errors_are_contained(tmp_path: Path, monkeypatch):
    """login.json：没有文件 = 只有邮箱密码；某个入口写错只跳过它；整份写坏沿用上一份；密钥经 ${环境变量} 进来，没设就不启用。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path / "data", seed=False)
    assert TestClient(create_app(net)).get("/api/info").json()["login"] == {"providers": [], "password": True}

    seen: list[dict] = []

    class Needs(FakeDriver):
        kind = "needs"

        def __init__(self, config):
            super().__init__(config)
            seen.append(config)
            if not config.get("key"):
                raise LoginError("要写 key")

    path = tmp_path / "login.json"
    path.write_text(json.dumps({"providers": [
        {"id": "ok", "driver": "needs", "config": {"key": "${T_LOGIN_KEY}", "nested": {"k": ["a-${T_LOGIN_KEY}"]}}},
        {"id": "nokey", "driver": "needs"},
        {"id": "noenv", "driver": "needs", "config": {"key": "${T_LOGIN_KEY_UNSET}"}},
        {"id": "badname", "driver": "needs", "config": {"key": "${T_LOGIN_SECRET}"}},
        {"id": "Bad Id", "driver": "needs", "config": {"key": "k"}},
        {"id": "ok", "driver": "needs", "config": {"key": "k"}},
    ]}), encoding="utf-8")
    monkeypatch.setenv("T_LOGIN_KEY", "s3cret")
    monkeypatch.delenv("T_LOGIN_KEY_UNSET", raising=False)
    monkeypatch.setenv("T_LOGIN_SECRET", "x")
    login = LoginService(path, drivers={"needs": Needs})
    assert [p.id for p in login.providers()] == ["ok"]
    assert seen[0] == {"key": "s3cret", "nested": {"k": ["a-s3cret"]}}
    assert set(login.errors) == {"nokey", "noenv", "badname", "Bad Id", "ok"} and "T_LOGIN_KEY_UNSET" in login.errors["noenv"]
    assert "KEY" in login.errors["badname"]  # 变量名不带 KEY 的不收：云侧 Agent 的命令看得到它
    assert "s3cret" not in json.dumps(login.public())

    import os
    path.write_text("{not json", encoding="utf-8")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert [p.id for p in login.providers()] == ["ok"] and "login.json" in login.errors
    path.unlink()
    assert login.providers() == [] and login.password_login


# ================= 外部系统带着用户凭据调接口（api_auth） =================
class SessionDriver(FakeDriver):
    """假的身份系统，带 verify：凭据 "s-<工号>" 是那个人的登录会话；"s-boom" 让驱动出错；别的不认。数一数问了几次。"""

    kind = "fake-session"
    asked: list[str] = []

    def verify(self, credential: str) -> Identity:
        SessionDriver.asked.append(credential)
        if credential == "s-boom":
            raise RuntimeError("secret-token-in-message")
        if not credential.startswith("s-"):
            raise LoginError("身份系统不认这个会话")
        who = credential[2:]
        return Identity(subject=who, email=f"{who}@corp.example")


def api_setup(tmp_path: Path, providers: list[dict], now=None) -> tuple[Network, LoginService, TestClient]:
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path / "data", seed=False)
    path = tmp_path / "login.json"
    path.write_text(json.dumps({"providers": providers}), encoding="utf-8")
    kw = {"now": now} if now else {}
    login = LoginService(path, drivers={"fake": FakeDriver, "fake-session": SessionDriver}, **kw)
    return net, login, TestClient(create_app(net, login=login))


def bearer(cred: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {cred}", **extra}


def test_backend_calls_api_as_the_user(tmp_path: Path):
    """api_auth 打开后，外部系统的后端带着用户的凭据调 /api/*：等同那个用户本人（第一次来自动建账号、有助理），
    写操作也行；对到用户的规则和公司账号登录一样（已有账号默认不绑）。不发 Matrix 自己的登录 cookie。"""
    SessionDriver.asked = []
    net, _, c = api_setup(tmp_path, [{"id": "corp", "driver": "fake-session", "api_auth": True}])
    r = c.get("/api/me", headers=bearer("s-u1001"))
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["email"] == "u1001@corp.example" and me["agent_count"] == 1
    assert "set-cookie" not in r.headers and not c.cookies
    g = c.post("/api/projects", json={"name": "报销"}, headers=bearer("s-u1001")).json()
    assert net.store.projects[g["id"]].owner_id == me["id"]
    # 第二个人只看得到自己的
    assert c.get(f"/api/projects/{g['id']}", headers=bearer("s-u1001")).status_code == 200
    assert c.get(f"/api/projects/{g['id']}", headers=bearer("s-u1002")).status_code == 404
    # 业务系统用的就是项目会话：开会话（不需要助理）、@ 成员发消息
    helper = net.create_agent("报销助手", "答报销问题", [], [], actor=me["id"])
    net.add_member(g["id"], helper.id, actor=me["id"])
    sess = c.post(f"/api/projects/{g['id']}/sessions", json={"agent": "报销助手"}, headers=bearer("s-u1001"))
    assert sess.status_code == 200 and sess.json()["agent_id"] == helper.id
    r = c.post(f"/api/conversations/{sess.json()['id']}/messages", json={"text": "差旅能报多少？"}, headers=bearer("s-u1001"))
    assert r.status_code == 200, r.text
    # 已有同邮箱的账号、没开 link_by_email：不绑
    auth.register(net.store, "u1003@corp.example", "password-123")
    r = c.get("/api/me", headers=bearer("s-u1003"))
    assert r.status_code == 401 and "已经有账号" in r.json()["detail"]
    # 不认的凭据、驱动出错：401，不漏驱动的异常消息
    assert c.get("/api/me", headers=bearer("bad")).status_code == 401
    r = c.get("/api/me", headers=bearer("s-boom"))
    assert r.status_code == 401 and "secret-token" not in r.text


def test_api_auth_is_off_unless_the_deployer_opens_it(tmp_path: Path):
    """默认不认外来凭据；驱动没实现 verify 时打不开（入口不启用，原因进 errors）；管理后台从不认外来凭据。"""
    _, login, c = api_setup(tmp_path, [{"id": "corp", "driver": "fake-session"}])
    assert c.get("/api/me", headers=bearer("s-u1001")).status_code == 401
    _, login, c = api_setup(tmp_path, [{"id": "corp", "driver": "fake", "api_auth": True}])
    assert login.providers() == [] and "verify" in login.errors["corp"]
    assert not FakeDriver.can_verify() and SessionDriver.can_verify()

    net, _, c = api_setup(tmp_path, [{"id": "corp", "driver": "fake-session", "api_auth": True}])
    me = c.get("/api/me", headers=bearer("s-u1001")).json()
    assert net.is_admin(me["id"])                           # 第一个人是管理员……
    r = c.get("/api/admin/usage", headers=bearer("s-u1001"))
    assert r.status_code == 403                             # ……但管理后台只能在网页上登录后用


def test_verified_credentials_are_cached_briefly(tmp_path: Path):
    """核实结果按凭据的哈希缓存：通过的 60 秒内不再问身份系统，过期后重问（用户退出后很快失效）；不通过的也缓存一会儿。"""
    SessionDriver.asked = []
    clock = [1000.0]
    _, login, c = api_setup(tmp_path, [{"id": "corp", "driver": "fake-session", "api_auth": True}], now=lambda: clock[0])
    for _ in range(3):
        assert c.get("/api/me", headers=bearer("s-u1001")).status_code == 200
    assert SessionDriver.asked == ["s-u1001"]
    clock[0] += 61
    c.get("/api/me", headers=bearer("s-u1001"))
    assert SessionDriver.asked == ["s-u1001"] * 2
    for _ in range(3):
        assert c.get("/api/me", headers=bearer("bad")).status_code == 401
    assert SessionDriver.asked.count("bad") == 1
    clock[0] += 11
    c.get("/api/me", headers=bearer("bad"))
    assert SessionDriver.asked.count("bad") == 2
    assert not any("s-u1001" in str(k) for k in login._verified)   # 表里只有哈希，没有凭据明文


def test_several_api_auth_providers_need_to_be_named(tmp_path: Path):
    """开了不止一个入口时，请求头 X-Matrix-Login 写明是哪个；同一个标识在不同入口下是不同的人。"""
    _, _, c = api_setup(tmp_path, [{"id": "corp", "driver": "fake-session", "api_auth": True},
                                   {"id": "lab", "driver": "fake-session", "api_auth": True,
                                    "link_by_email": False}])
    r = c.get("/api/me", headers=bearer("s-u1001"))
    assert r.status_code == 401 and "X-Matrix-Login" in r.json()["detail"]
    a = c.get("/api/me", headers=bearer("s-u1001", **{"X-Matrix-Login": "corp"})).json()
    assert a["email"] == "u1001@corp.example"
    assert c.get("/api/me", headers=bearer("s-u1001", **{"X-Matrix-Login": "nope"})).status_code == 401


def test_builtin_and_template_drivers_pass_the_structure_check():
    """内置驱动在册；一致性检查能认出没写全的驱动（外部驱动包在自己的测试里跑 assert_driver）。"""
    drivers, errors = load_drivers()
    assert drivers["oauth2"] is OAuth2Driver and errors == {}
    assert_driver(OAuth2Driver)
    assert_driver(FakeDriver)

    class Half(Driver):
        kind = "Half!"

    problems = check_driver(Half)
    assert any("kind" in p for p in problems) and any("title" in p for p in problems)
    assert any("authorize_url" in p for p in problems) and any("identify" in p for p in problems)
    with pytest.raises(AssertionError):
        assert_driver(Half)


# ================= 通用 OAuth2 驱动，对着本地的假身份系统 =================
class FakeIdP:
    """最小的 OAuth2 身份系统：/token 换令牌（核对 client、redirect_uri、PKCE），/userinfo 凭令牌给用户信息。"""

    def __init__(self, token_auth: str = "body", userinfo_auth: str = "header", form_token: bool = False):
        self.codes: dict[str, dict] = {}      # 授权码 -> {challenge, redirect_uri, user}
        self.tokens: dict[str, dict] = {}
        self.calls: list[str] = []
        idp = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                idp.calls.append("token")
                form = dict(parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
                if token_auth == "basic":
                    cid, _, secret = base64.b64decode(self.headers.get("Authorization", " ").split(" ")[1]).decode().partition(":")
                else:
                    cid, secret = form.get("client_id"), form.get("client_secret")
                c = idp.codes.pop(form.get("code", ""), None)
                challenge = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
                if (cid, secret) != ("matrix", "s3cret") or c is None or c["redirect_uri"] != form.get("redirect_uri") \
                        or (c["challenge"] and c["challenge"] != challenge):
                    return self._send(400, json.dumps({"error": "invalid_grant", "error_description": "授权码不对"}).encode())
                uid = str(c["user"]["id"])
                idp.tokens["tok-" + uid] = c["user"]
                if form_token:
                    return self._send(200, f"access_token=tok-{uid}&token_type=bearer".encode(), "text/plain")
                self._send(200, json.dumps({"access_token": "tok-" + uid, "token_type": "Bearer"}).encode())

            def do_GET(self):
                idp.calls.append("userinfo")
                if userinfo_auth == "query":
                    tok = parse_qs(urlsplit(self.path).query).get("access_token", [""])[0]
                else:
                    tok = self.headers.get("Authorization", "").removeprefix("Bearer ")
                user = idp.tokens.get(tok)
                if user is None:
                    return self._send(401, b"{}")
                self._send(200, json.dumps({"code": 0, "data": {"user": user}}).encode())

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def approve(self, authorize_url: str, user: dict) -> dict[str, str]:
        """扮演用户在身份系统的登录页上登录并同意：返回回调带的参数。"""
        q = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        assert q["response_type"] == "code" and q["client_id"] == "matrix"
        code = f"code-{len(self.codes)}-{user['id']}"
        self.codes[code] = {"challenge": q.get("code_challenge", ""), "redirect_uri": q["redirect_uri"], "user": user}
        return {"code": code, "state": q["state"]}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def oauth_config(idp: FakeIdP, **extra) -> dict:
    return {"authorize_url": idp.url + "/authorize", "token_url": idp.url + "/token", "userinfo_url": idp.url + "/userinfo",
            "client_id": "matrix", "client_secret": "${T_SSO_KEY}", "scope": "profile email",
            "fields": {"subject": "data.user.id", "email": "data.user.account", "name": "data.user.name"},
            "email_domain": "corp.example", **extra}


@pytest.mark.parametrize("variant", [
    {},
    {"token_auth": "basic", "userinfo_auth": "query", "pkce": False},
    {"form_token": True},
])
def test_oauth2_driver_end_to_end(tmp_path: Path, monkeypatch, variant: dict):
    """通用 OAuth2 驱动走完授权码登录：跳去身份系统（带 state 与 PKCE）→ 回调 → 服务器换令牌、取用户信息 → 登录。
    几种常见的差别都能配：密钥放表单或 Basic、令牌放请求头或查询参数、换令牌回 JSON 或表单编码、账号名补域名。"""
    monkeypatch.setenv("T_SSO_KEY", "s3cret")
    variant = dict(variant)
    idp = FakeIdP(variant.get("token_auth", "body"), variant.get("userinfo_auth", "header"), variant.pop("form_token", False))
    try:
        net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path / "data", seed=False)
        path = tmp_path / "login.json"
        path.write_text(json.dumps({"providers": [{"id": "corp", "name": "公司账号", "driver": "oauth2",
                                                   "config": oauth_config(idp, **variant)}]}), encoding="utf-8")
        login = LoginService(path)
        c = browser(net, login)
        r = c.get("/api/auth/sso/corp/start")
        url = r.headers["location"]
        assert url.startswith(idp.url + "/authorize?") and "s3cret" not in url
        q = parse_qs(urlsplit(url).query)
        assert q["redirect_uri"] == ["http://testserver/api/auth/sso/corp/callback"] and q["scope"] == ["profile email"]
        assert ("code_challenge" in q) == variant.get("pkce", True)
        params = idp.approve(url, {"id": 1001, "account": "maya", "name": "Maya"})
        r = c.get("/api/auth/sso/corp/callback", params=params)
        assert r.headers["location"] == "/", r.headers["location"]
        me = c.get("/api/me").json()
        assert me["email"] == "maya@corp.example" and net.store.users[me["id"]].sso == {"corp": "1001"}
        # 授权码只能用一次；身份系统拒绝时不登录，给人看的是身份系统说的原因
        fresh = browser(net, login)
        again = idp.approve(fresh.get("/api/auth/sso/corp/start").headers["location"], {"id": 1002, "account": "bo"})
        again["code"] = params["code"]
        assert "授权码不对" in error_of(fresh.get("/api/auth/sso/corp/callback", params=again))
        assert fresh.get("/api/me").status_code == 401
        # 用户在身份系统那边点了拒绝
        fresh.get("/api/auth/sso/corp/start")
        state = parse_qs(urlsplit(fresh.get("/api/auth/sso/corp/start").headers["location"]).query)["state"][0]
        assert "access_denied" in error_of(fresh.get("/api/auth/sso/corp/callback",
                                                     params={"state": state, "error": "access_denied"}))
    finally:
        idp.close()


def test_oauth2_driver_checks_its_config_and_the_identity():
    """配置不对在构造时就报（这个入口不出现在登录页上）；用户信息里取不到标识或邮箱就不登录。"""
    good = {"authorize_url": "https://sso.x/a", "token_url": "https://sso.x/t", "client_id": "m"}
    OAuth2Driver(good)
    for bad in ({**good, "authorize_url": "javascript:alert(1)"}, {**good, "token_url": ""}, {**good, "client_id": ""},
                {**good, "token_auth": "magic"}, {**good, "proxy": "socks"}, {**good, "fields": "id"}):
        with pytest.raises(LoginError):
            OAuth2Driver(bad)

    idp = FakeIdP()
    try:
        import os
        os.environ["T_SSO_KEY"] = "s3cret"
        cfg = {k: (v if v != "${T_SSO_KEY}" else "s3cret") for k, v in oauth_config(idp).items()}
        for fields, user, why in (
            ({"subject": "data.user.missing", "email": "data.user.account"}, {"id": 1, "account": "a"}, "data.user.missing"),
            ({"subject": "data.user.id", "email": "data.user.mail"}, {"id": 1, "account": "a"}, "data.user.mail"),
            ({"subject": "data.user", "email": "data.user.account"}, {"id": 1, "account": "a"}, "data.user"),
        ):
            d = OAuth2Driver({**cfg, "fields": fields})
            flow = Flow(state="s", redirect_uri="http://m/cb")
            params = idp.approve(d.authorize_url(flow), user)
            with pytest.raises(LoginError, match=why):
                d.identify(flow, params)
        # 没有 userinfo_url：用户信息从换令牌的响应里取（这个假身份系统不给，所以取不到标识）
        d = OAuth2Driver({**cfg, "userinfo_url": "", "fields": {"subject": "uid"}})
        flow = Flow(state="s", redirect_uri="http://m/cb")
        with pytest.raises(LoginError, match="uid"):
            d.identify(flow, idp.approve(d.authorize_url(flow), {"id": 1}))
        assert idp.calls[-1] == "token"
        # 连不上
        d = OAuth2Driver({**cfg, "token_url": "http://127.0.0.1:9/token", "timeout_s": 2})
        with pytest.raises(LoginError, match="连不上"):
            d.identify(Flow(state="s", redirect_uri="http://m/cb"), {"code": "x"})
    finally:
        os.environ.pop("T_SSO_KEY", None)
        idp.close()


def test_oauth2_driver_verifies_forwarded_access_tokens(tmp_path: Path, monkeypatch):
    """内置 oauth2 驱动的 verify：外部系统转来的访问令牌拿去调 userinfo_url，取得到就是这个人；没配 userinfo_url 打不开。"""
    monkeypatch.setenv("T_SSO_KEY", "s3cret")
    idp = FakeIdP()
    try:
        idp.tokens["tok-42"] = {"id": 42, "account": "lin", "name": "Lin"}
        d = OAuth2Driver(oauth_config(idp, client_secret="s3cret"))
        assert OAuth2Driver.can_verify()
        who = d.verify("tok-42")
        assert (who.subject, who.email) == ("42", "lin@corp.example")
        with pytest.raises(LoginError, match="取用户信息失败"):
            d.verify("tok-unknown")
        cfg = oauth_config(idp, client_secret="s3cret")
        del cfg["userinfo_url"]
        with pytest.raises(LoginError, match="userinfo_url"):
            OAuth2Driver(cfg).verify("tok-42")

        net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path / "data", seed=False)
        path = tmp_path / "login.json"
        path.write_text(json.dumps({"providers": [{"id": "corp", "driver": "oauth2", "api_auth": True,
                                                   "config": oauth_config(idp)}]}), encoding="utf-8")
        c = TestClient(create_app(net, login=LoginService(path)))
        assert c.get("/api/me", headers={"Authorization": "Bearer tok-42"}).json()["email"] == "lin@corp.example"
    finally:
        idp.close()
