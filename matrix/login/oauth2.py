"""通用 OAuth2 授权码登录（RFC 6749 第 4.1 节，可带 PKCE）。地址与字段都由部署方在 login.json 里写：

    {"id": "corp", "name": "公司账号", "driver": "oauth2", "config": {
       "authorize_url": "https://sso.corp.example/oauth2/authorize",
       "token_url": "https://sso.corp.example/oauth2/token",
       "userinfo_url": "https://sso.corp.example/oauth2/userinfo",
       "client_id": "matrix", "client_secret": "${MATRIX_SSO_KEY}",
       "scope": "profile email",
       "fields": {"subject": "id", "email": "email", "name": "name"}}}

可选：
- `pkce`（默认 true）：带 S256 的 code_challenge。身份系统不认识这个参数而报错时关掉。
- `token_auth`：`body`（默认，client_id / client_secret 放在表单里）或 `basic`（HTTP Basic）。
- `userinfo_auth`：`header`（默认，`Authorization: Bearer <令牌>`）或 `query`（`?access_token=<令牌>`）。
- `userinfo_url` 不写：用户信息直接从换令牌的响应里取（有的身份系统把用户放在那里）。
- `fields`：用户信息 JSON 里各项的位置，可以用点号进到里层（`data.user.id`）。默认 `sub` / `email` / `name`。
  `fields.access_token`：换令牌的响应里访问令牌的位置，默认 `access_token`（有的身份系统包了一层：`data.access_token`）。
- `email_domain`：`email` 字段取到的值没有 @（只是账号名）时补上的域名。
- `authorize_params`：发起登录时额外带的查询参数。
- `proxy`：`direct`（默认，内网直连）或 `env`（照环境变量里的代理）。
- `timeout_s`：每次请求的超时，默认 15 秒。

OIDC 的身份系统也能这样接（它就是 OAuth2 加一层）：scope 里写上 `openid`，`fields.subject` 用 `sub`。
这里不验 id_token：用户信息是服务器拿着刚换到的令牌、直接向身份系统取的，不经过浏览器。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qsl, urlencode

from .base import Driver, Flow, Identity, LoginError

MAX_BODY = 1_000_000


class OAuth2Driver(Driver):
    kind = "oauth2"
    title = "OAuth2 授权码"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        c = config
        self.authorize, self.token = _url(c, "authorize_url"), _url(c, "token_url")
        self.userinfo = _url(c, "userinfo_url") if c.get("userinfo_url") else ""
        self.client_id, self.client_secret = str(c.get("client_id") or ""), str(c.get("client_secret") or "")
        if not self.client_id:
            raise LoginError("要写 client_id")
        self.scope = str(c.get("scope") or "")
        self.pkce = bool(c.get("pkce", True))
        self.token_auth = _choice(c, "token_auth", ("body", "basic"))
        self.userinfo_auth = _choice(c, "userinfo_auth", ("header", "query"))
        self.proxy = _choice(c, "proxy", ("direct", "env"))
        self.timeout = float(c.get("timeout_s", 15))
        fields = c.get("fields") or {}
        extra = c.get("authorize_params") or {}
        if not isinstance(fields, dict) or not isinstance(extra, dict):
            raise LoginError("fields、authorize_params 要是对象")
        self.fields = {"subject": "sub", "email": "email", "name": "name", "access_token": "access_token",
                       **{k: str(v) for k, v in fields.items()}}
        self.extra = {str(k): str(v) for k, v in extra.items()}
        self.email_domain = str(c.get("email_domain") or "").lstrip("@")

    def authorize_url(self, flow: Flow) -> str:
        q = {**self.extra, "response_type": "code", "client_id": self.client_id,
             "redirect_uri": flow.redirect_uri, "state": flow.state}
        if self.scope:
            q["scope"] = self.scope
        if self.pkce:
            flow.data["verifier"] = secrets.token_urlsafe(48)
            digest = hashlib.sha256(flow.data["verifier"].encode()).digest()
            q.update(code_challenge=base64.urlsafe_b64encode(digest).rstrip(b"=").decode(), code_challenge_method="S256")
        return f"{self.authorize}{'&' if '?' in self.authorize else '?'}{urlencode(q)}"

    def identify(self, flow: Flow, params: dict[str, str]) -> Identity:
        if params.get("error"):  # 用户在身份系统那边点了拒绝，或身份系统不认这次请求
            raise LoginError(f"登录没有完成：{(params.get('error_description') or params['error'])[:200]}")
        code = params.get("code")
        if not code:
            raise LoginError("回调里没有授权码")
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": flow.redirect_uri}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.token_auth == "basic":
            pair = f"{self.client_id}:{self.client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
        else:
            form.update(client_id=self.client_id)
            if self.client_secret:
                form["client_secret"] = self.client_secret
        if flow.data.get("verifier"):
            form["code_verifier"] = flow.data["verifier"]
        tok = self._call(self.token, "换令牌", headers, urlencode(form).encode())
        info = tok
        if self.userinfo:
            access = _pick(tok, self.fields["access_token"])
            if not access:
                raise LoginError(f"换令牌的响应里没有 {self.fields['access_token']}")
            info = self._userinfo(access)
        return self._identity(info)

    def verify(self, credential: str) -> Identity:
        """外部系统转来的访问令牌：拿它取用户信息，取得到就是这个人。要配 userinfo_url。"""
        if not self.userinfo:
            raise LoginError("没有配 userinfo_url，核实不了外部系统转来的令牌")
        return self._identity(self._userinfo(credential))

    def _userinfo(self, access: str) -> dict[str, Any]:
        if self.userinfo_auth == "query":
            url = f"{self.userinfo}{'&' if '?' in self.userinfo else '?'}{urlencode({'access_token': access})}"
            return self._call(url, "取用户信息", {})
        return self._call(self.userinfo, "取用户信息", {"Authorization": f"Bearer {access}"})

    def _identity(self, info: dict[str, Any]) -> Identity:
        subject, email = _pick(info, self.fields["subject"]), _pick(info, self.fields["email"])
        if not subject:
            raise LoginError(f"用户信息里没有 {self.fields['subject']}（fields.subject 要指到不会变的用户标识）")
        if email and "@" not in email and self.email_domain:
            email = f"{email}@{self.email_domain}"
        if not email:
            raise LoginError(f"用户信息里没有 {self.fields['email']}（fields.email 要指到邮箱，或账号名加 email_domain）")
        return Identity(subject=subject, email=email, name=_pick(info, self.fields["name"]))

    def _call(self, url: str, what: str, headers: dict[str, str], data: bytes | None = None) -> dict[str, Any]:
        """向身份系统发一次请求，返回 JSON 对象（老式的身份系统换令牌时回表单编码，也认）。出错的消息不带请求内容。"""
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                     headers={"Accept": "application/json", **headers})
        opener = urllib.request.build_opener() if self.proxy == "env" else \
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=self.timeout) as r:
                body = r.read(MAX_BODY + 1)
        except urllib.error.HTTPError as e:
            # OAuth2 的错误是 4xx 加一段 JSON（error / error_description）：有就给人看身份系统说的原因
            try:
                err = json.loads(e.read(MAX_BODY).decode("utf-8", "replace"))
                why = str(err.get("error_description") or err.get("error") or "")[:200] if isinstance(err, dict) else ""
            except (ValueError, OSError):
                why = ""
            raise LoginError(f"{what}失败：{why or f'身份系统返回 {e.code}'}") from e
        except (urllib.error.URLError, OSError) as e:
            raise LoginError(f"{what}失败：连不上身份系统") from e
        if len(body) > MAX_BODY:
            raise LoginError(f"{what}失败：响应太大")
        text = body.decode("utf-8", "replace")
        try:
            out = json.loads(text)
        except ValueError:
            out = dict(parse_qsl(text)) if data is not None else None
        if not isinstance(out, dict) or not out:
            raise LoginError(f"{what}失败：响应不是 JSON 对象")
        if isinstance(out.get("error"), str) and out["error"] and not out.get("access_token"):
            raise LoginError(f"{what}失败：{str(out.get('error_description') or out['error'])[:200]}")
        return out


def _url(c: dict[str, Any], key: str) -> str:
    v = str(c.get(key) or "")
    if not re.match(r"^https?://\S+$", v):
        raise LoginError(f"{key} 要是 http(s) 地址")
    return v


def _choice(c: dict[str, Any], key: str, options: tuple[str, ...]) -> str:
    v = str(c.get(key) or options[0])
    if v not in options:
        raise LoginError(f"{key} 只能是 {' 或 '.join(options)}")
    return v


def _pick(info: dict[str, Any], path: str) -> str:
    """按点号路径取一个值，转成字符串；取不到、不是标量时返回空串。"""
    cur: Any = info
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return ""
        cur = cur[part]
    if isinstance(cur, bool) or not isinstance(cur, (str, int)):
        return ""
    return str(cur).strip()
