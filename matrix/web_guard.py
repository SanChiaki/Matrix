"""网页入口的跨站防护（docs/control-plane.md 第 8 节，C0）：改状态的请求校验来源站点 + 双提交的 CSRF 令牌，
经 HTTPS 时 cookie 加 Secure。

- 范围：`/api/*` 上的 POST / PUT / PATCH / DELETE，除了不靠 cookie 的连接器配对（`EXEMPT`）。
  `/internal/*`（凭执行上下文令牌）、`/hooks/*`（凭钩子令牌）、端侧与沙箱的 WebSocket（凭设备令牌 / 启动令牌）不经 cookie，
  不在范围里。登录、注册、重置密码也在范围里（防“登录到攻击者的账号”）。
- 来源：`Origin`（没有时退到 `Referer`）的主机要等于这次请求的 `Host`，或部署方声明的 `MATRIX_PUBLIC_URL` 的主机；
  两个都没有、或是 `null`，拒绝。
- 令牌：`matrix_csrf` cookie（不是 HttpOnly，页面读得到）与请求头 `X-Matrix-CSRF` 要相同。没有这个 cookie 的请求，
  响应里顺手发一个，所以老的登录升级后打开页面就有。
- 经 HTTPS：请求本身是 https（uvicorn 只在来源是 `FORWARDED_ALLOW_IPS` 里的代理时才认 `X-Forwarded-Proto`），
  或部署方声明了 `MATRIX_PUBLIC_URL=https://…` / `MATRIX_SECURE_COOKIE=on`（在 TLS 反向代理后面）。本机 http 开发不受影响。
"""

from __future__ import annotations

import hmac
import json
import secrets
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlsplit

from . import compat

CSRF_COOKIE = "matrix_csrf"
CSRF_HEADER = "x-matrix-csrf"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
EXEMPT = {"/api/devices/pair", "/api/devices/pair/poll"}   # 连接器申请配对：没有登录，也不带 cookie


def public_url() -> str:
    return (compat.env("PUBLIC_URL") or "").strip().rstrip("/")


def secure_cookies(scheme: str) -> bool:
    """这次响应发的 cookie 要不要加 Secure。"""
    flag = (compat.env("SECURE_COOKIE") or "").strip().lower()
    if flag in ("off", "0", "false", "no"):
        return False
    return scheme in ("https", "wss") or flag in ("on", "1", "true", "yes") or public_url().lower().startswith("https://")


def _netloc(url: str) -> str:
    try:
        p = urlsplit(url)
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.hostname:
        return ""
    default = {"http": 80, "https": 443}[p.scheme]
    port = p.port if p.port is not None else default
    return f"{p.hostname.lower()}:{port}"


def _host_netloc(host: str, scheme: str) -> set[str]:
    """请求的 Host 头 → 可接受的“主机:端口”。在 TLS 反向代理后面时代理到 Matrix 是 http，浏览器那边是 https：两种默认端口都认。"""
    host = (host or "").strip().lower()
    if not host:
        return set()
    if host.startswith("["):
        name, _, rest = host.partition("]")
        name, port = name + "]", rest.lstrip(":")
    else:
        name, _, port = host.partition(":")
    if port:
        return {f"{name.strip('[]')}:{port}"}
    return {f"{name.strip('[]')}:80", f"{name.strip('[]')}:443"}


def same_site(origin: str | None, referer: str | None, host: str, scheme: str) -> bool:
    src = origin if origin is not None else referer
    if not src or src == "null":
        return False
    got = _netloc(src)
    if not got:
        return False
    allowed = _host_netloc(host, scheme)
    if (pub := public_url()):
        allowed.add(_netloc(pub))
    return got in allowed


def csrf_cookie_header(token: str, scheme: str) -> bytes:
    attrs = f"{CSRF_COOKIE}={token}; Path=/; SameSite=Lax; Max-Age={400 * 86400}"
    return (attrs + ("; Secure" if secure_cookies(scheme) else "")).encode("latin-1")


class CsrfGuard:
    """ASGI 中间件（不包 BaseHTTPMiddleware：SSE 的流式响应原样过）。"""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        jar = SimpleCookie()
        try:
            jar.load(headers.get("cookie", ""))
        except Exception:  # noqa: BLE001 —— 坏 cookie 当没有
            jar = SimpleCookie()
        have = jar[CSRF_COOKIE].value if CSRF_COOKIE in jar else ""
        scheme = scope.get("scheme", "http")
        path = scope.get("path", "")
        if scope.get("method", "GET").upper() in UNSAFE and path.startswith("/api/") and path not in EXEMPT:
            why = ""
            if not same_site(headers.get("origin"), headers.get("referer"), headers.get("host", ""), scheme):
                why = "来源站点不对"
            elif not have or not hmac.compare_digest(have, headers.get(CSRF_HEADER, "")):
                why = "缺少 CSRF 令牌"
            if why:
                body = json.dumps({"detail": f"跨站请求被拒绝（{why}）：请从 Matrix 的页面操作，或刷新页面后重试"},
                                  ensure_ascii=False).encode()
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        if have:
            await self.app(scope, receive, send)
            return
        token = secrets.token_urlsafe(24)

        async def send_with_cookie(msg: dict) -> None:
            if msg["type"] == "http.response.start":
                msg = {**msg, "headers": [*msg.get("headers", []), (b"set-cookie", csrf_cookie_header(token, scheme))]}
            await send(msg)
        await self.app(scope, receive, send_with_cookie)
