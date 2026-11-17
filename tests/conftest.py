"""测试里的 HTTP 客户端像同站页面一样发请求：带 Origin 与双提交的 CSRF 令牌（app.js 的 api() 就是这样做的）。

跨站防护（matrix/web_guard.py）本身的测试在 test_csrf.py：那里用不带这些的原始客户端。"""

from __future__ import annotations

import secrets

import fastapi.testclient
import starlette.testclient

from matrix.web_guard import CSRF_COOKIE, CSRF_HEADER

RawTestClient = starlette.testclient.TestClient


class SameSiteClient(RawTestClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        token = secrets.token_urlsafe(16)
        self.cookies.set(CSRF_COOKIE, token)
        self.headers.update({"origin": str(self.base_url).rstrip("/"), CSRF_HEADER: token})


fastapi.testclient.TestClient = SameSiteClient
starlette.testclient.TestClient = SameSiteClient
