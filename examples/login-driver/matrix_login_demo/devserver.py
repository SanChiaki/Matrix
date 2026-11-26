"""本地的演示 SSO：给驱动的测试用，也能起来在浏览器里走一遍登录。

    python -m matrix_login_demo.devserver --port 9300 --secret demo-secret

协议见 driver.py 开头。登录页不问密码，直接列出几个演示账号让你点（它只是一个假的身份系统）。
登录时还给浏览器发一个登录会话 cookie `demo_sso`：真实部署里它设在主域名上、各个子域名的系统共享；
业务系统的后端把它的值转给 Matrix，Matrix 经驱动的 verify 拿 `/session` 核实（协议第 4 条）。
"""

from __future__ import annotations

import argparse
import html
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

USERS = {
    "u1001": {"uid": "u1001", "mail": "maya@corp.example", "name": "Maya"},
    "u1002": {"uid": "u1002", "mail": "bo@corp.example", "name": "Bo"},
}


class DevServer:
    def __init__(self, port: int = 0, app_id: str = "matrix", secret: str = "demo-secret"):
        self.app_id, self.secret = app_id, secret
        self.tickets: dict[str, tuple[str, str]] = {}    # 票据 -> (回跳地址, uid)
        self.sessions: dict[str, str] = {}               # 登录会话 cookie 的值 -> uid（退出即删）
        self.lock = threading.Lock()
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _send(self, code: int, body: bytes, ctype: str, location: str | None = None,
                      cookie: str | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if location:
                    self.send_header("Location", location)
                if cookie:
                    self.send_header("Set-Cookie", f"demo_sso={cookie}; HttpOnly; Path=/; SameSite=Lax")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                url = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path == "/login":
                    if q.get("app") != srv.app_id or not q.get("service"):
                        return self._send(400, "没有登记的应用".encode(), "text/plain; charset=utf-8")
                    if q.get("uid") in USERS:   # 点了某个账号：签一张票据，跳回应用
                        ticket = srv.issue(q["service"], q["uid"])
                        sep = "&" if "?" in q["service"] else "?"
                        return self._send(302, b"", "text/plain", f"{q['service']}{sep}{urlencode({'ticket': ticket})}",
                                          cookie=srv.open_session(q["uid"]))
                    links = "".join(
                        f'<li><a href="/login?{html.escape(urlencode({**q, "uid": u["uid"]}))}">{html.escape(u["name"])}'
                        f'（{html.escape(u["mail"])}）</a></li>' for u in USERS.values())
                    page = f"<!doctype html><meta charset=utf-8><title>演示 SSO</title><h1>演示 SSO</h1><p>用哪个账号登录？</p><ul>{links}</ul>"
                    return self._send(200, page.encode(), "text/html; charset=utf-8")
                if url.path == "/validate":
                    if not secrets.compare_digest(self.headers.get("X-App-Secret", ""), srv.secret):
                        return self._send(401, b'{"ok": false}', "application/json")
                    with srv.lock:
                        service, uid = srv.tickets.pop(q.get("ticket", ""), ("", ""))   # 一次性
                    if not uid or service != q.get("service"):
                        return self._send(200, b'{"ok": false}', "application/json")
                    return self._send(200, json.dumps({"ok": True, "user": USERS[uid]}).encode(), "application/json")
                if url.path == "/session":   # 核实登录会话 cookie 的值：给业务系统与 Matrix 的后端用
                    if not secrets.compare_digest(self.headers.get("X-App-Secret", ""), srv.secret):
                        return self._send(401, b'{"ok": false}', "application/json")
                    with srv.lock:
                        uid = srv.sessions.get(self.headers.get("X-Session", ""), "")
                    if not uid:
                        return self._send(200, b'{"ok": false}', "application/json")
                    return self._send(200, json.dumps({"ok": True, "user": USERS[uid]}).encode(), "application/json")
                self._send(404, b"", "text/plain")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def issue(self, service: str, uid: str) -> str:
        ticket = secrets.token_urlsafe(16)
        with self.lock:
            self.tickets[ticket] = (service, uid)
        return ticket

    def open_session(self, uid: str) -> str:
        """登录会话：返回 cookie 的值。"""
        sid = secrets.token_urlsafe(24)
        with self.lock:
            self.sessions[sid] = uid
        return sid

    def close_session(self, sid: str) -> None:
        """用户在身份系统那边退出。"""
        with self.lock:
            self.sessions.pop(sid, None)

    def start(self) -> "DevServer":
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def main() -> None:
    p = argparse.ArgumentParser(description="演示 SSO（票据式的假身份系统）")
    p.add_argument("--port", type=int, default=9300)
    p.add_argument("--app-id", default="matrix")
    p.add_argument("--secret", default="demo-secret")
    args = p.parse_args()
    srv = DevServer(args.port, args.app_id, args.secret)
    print(f"演示 SSO → {srv.url}（应用 id：{args.app_id}）")
    try:
        srv.httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
