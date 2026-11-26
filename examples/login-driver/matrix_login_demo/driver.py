"""演示 SSO 的登录驱动：票据式的统一登录（不是 OAuth2）接进 Matrix 的样板。照它改成你们公司的。

「协议」标出的三处是演示 SSO 自己的约定，换成你们的：登录页地址怎么拼、回调带回什么、怎么向身份系统核实。
其余（只认发起登录的那个浏览器、身份怎么对到 Matrix 用户、谁能进来、登录状态）都在 Matrix 里，驱动不用管。

演示 SSO 的协议（devserver.py 实现了它）：
1. 浏览器打开 `{url}/login?app=<应用 id>&service=<回跳地址>`，用户登录；
2. 登录后跳回 `<回跳地址>&ticket=<一次性票据>`；
3. 应用的服务器拿票据去问：`GET {url}/validate?ticket=…&service=…`，请求头 `X-App-Secret: <应用密钥>`，
   得到 `{"ok": true, "user": {"uid": "…", "mail": "…", "name": "…"}}`。票据只能用一次，只对签发时的回跳地址有效。
4. 登录后浏览器还有一个主域名下共享的登录会话 cookie（`demo_sso`）。持有它的值的后端可以问：
   `GET {url}/session`，请求头 `X-App-Secret` 与 `X-Session: <cookie 的值>`，回答同第 3 条；用户退出后不再认。
   业务系统的后端把这个值转给 Matrix（`Authorization: Bearer <值>`），Matrix 经 verify 走这一条（login.json 里 `api_auth`）。

login.json 里这样写（密钥放环境变量）：

    {"id": "corp", "name": "公司账号", "driver": "demo-sso", "api_auth": true,
     "config": {"url": "http://127.0.0.1:9300", "app_id": "matrix", "app_secret": "${DEMO_SSO_KEY}"}}
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

from matrix.login.base import Driver, Flow, Identity, LoginError


class DemoDriver(Driver):
    kind = "demo-sso"
    title = "演示 SSO（票据式）"
    # state_param 用默认的 "state"：演示 SSO 不会单独带回 state，所以把 flow.return_url（回调地址 + state）当回跳地址

    def __init__(self, config: dict[str, Any]):
        # 只校验配置，不连网。配置不对抛 LoginError：这个入口不会出现在登录页上，原因打在 Matrix 的日志里
        super().__init__(config)
        self.url = str(config.get("url") or "").rstrip("/")
        self.app_id, self.app_secret = str(config.get("app_id") or ""), str(config.get("app_secret") or "")
        if not re.match(r"^https?://", self.url):
            raise LoginError("url 要是演示 SSO 的 http(s) 地址")
        if not self.app_id or not self.app_secret:
            raise LoginError("要写 app_id 与 app_secret")

    def authorize_url(self, flow: Flow) -> str:
        # 协议：登录页地址。回跳地址必须让 flow.state 跟着回来
        return f"{self.url}/login?{urlencode({'app': self.app_id, 'service': flow.return_url})}"

    def identify(self, flow: Flow, params: dict[str, str]) -> Identity:
        # 协议：回调带回一次性票据
        ticket = params.get("ticket")
        if not ticket:
            raise LoginError("回调里没有票据")
        # 协议：服务器直接向身份系统核实票据。只信这里拿到的用户，不信浏览器带来的任何用户信息
        return self._ask(f"{self.url}/validate?{urlencode({'ticket': ticket, 'service': flow.return_url})}", {}, "票据")

    def verify(self, credential: str) -> Identity:
        # 协议：业务系统的后端转来的登录会话 cookie 的值，向身份系统核实它现在还有效、是谁（login.json 里 api_auth 打开才用）
        return self._ask(f"{self.url}/session", {"X-Session": credential}, "登录会话")

    def _ask(self, url: str, headers: dict[str, str], what: str) -> Identity:
        req = urllib.request.Request(url, headers={"X-App-Secret": self.app_secret, "Accept": "application/json", **headers})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 内网直连，不走环境变量里的代理
        try:
            with opener.open(req, timeout=15) as r:
                body = json.loads(r.read(1_000_000).decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise LoginError(f"{what}没有通过核实（{e.code}）") from e   # 消息给人看：不要带票据、cookie、密钥
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LoginError("连不上演示 SSO") from e
        user = body.get("user") if isinstance(body, dict) and body.get("ok") is True else None
        if not isinstance(user, dict) or not user.get("uid"):
            raise LoginError(f"演示 SSO 不认这个{what}")
        # subject 用不会变的标识（工号、用户 id），不要用会改的名字；email 是 Matrix 里邀请、认管理员用的
        return Identity(subject=str(user["uid"]), email=str(user.get("mail") or ""), name=str(user.get("name") or ""))
