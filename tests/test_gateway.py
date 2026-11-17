"""出口网关（docs/cloud-runtime.md 第 6 节，阶段 2a）：放行决策、模型核对与换密钥、用量、只转沙箱连接这一个端点。"""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from matrix.gateway import Denied, Gateway, decide, usage_of
from matrix.web_fetch import FetchPolicy, parse_policy

PUB = lambda host, port: ["93.184.216.34"]  # noqa: E731
PRIV = lambda host, port: ["10.0.0.5"]  # noqa: E731
META = lambda host, port: ["169.254.169.254"]  # noqa: E731


def test_decide_by_authority_and_host():
    """公网要 web.research、只到 80/443、地址必须都是公网；镜像源要 shell.exec；内网只认 fetch.json 的 allow。"""
    pol = parse_policy({"allow": ["wiki.corp", "10.1.0.0/16"]})
    assert decide(["web.research"], "example.com", 443, None, [], PUB) == ("direct", ["93.184.216.34"])
    for auth, host, port, res, why in [
        ([], "example.com", 443, PUB, "web.research"),
        (["web.research"], "example.com", 22, PUB, "80 / 443"),
        (["web.research"], "evil.example", 443, PRIV, "内网"),
        (["web.research"], "meta.example", 443, META, "内网"),
        (["web.research"], "127.0.0.1", 80, PUB, "内网"),
        (["web.research"], "169.254.169.254", 80, PUB, "内网"),
        (["web.research"], "pypi.org", 443, PUB, None),
        (["web.research"], "pypi.mirror", 443, PRIV, "镜像源"),
        (["shell.exec"], "wiki.corp", 443, PRIV, None),
        ([], "wiki.corp", 443, PRIV, "白名单"),
    ]:
        if why is None:
            assert decide(auth, host, port, pol, ["pypi.mirror"], res)[0] == "direct"
        else:
            with pytest.raises(Denied, match=why):
                decide(auth, host, port, pol, ["pypi.mirror"], res)
    assert decide(["shell.exec"], "pypi.mirror", 8080, None, ["pypi.mirror"], PRIV) == ("direct", ["10.0.0.5"])
    assert decide(["shell.exec"], "files.pypi.mirror", 443, None, ["pypi.mirror"], PRIV)[0] == "direct"
    assert decide(["web.research"], "10.1.2.3", 8080, pol, [], PUB) == ("direct", ["10.1.2.3"])  # allow 的网段
    with pytest.raises(Denied):
        decide(["shell.exec"], "example.com", 443, pol, ["pypi.mirror"], PUB)  # 只有 shell.exec 不能上公网


def test_decide_with_upstream_proxy():
    """网关自己经上游代理出网：公网交给上游代理（本机解析不了的也是）；解析得到内网地址的照样拒。"""
    def fail(host, port):
        from matrix.web_fetch import FetchError
        raise FetchError("no dns")
    assert decide(["web.research"], "example.com", 443, None, [], fail, "http://proxy:3128") == ("proxy", "http://proxy:3128")
    assert decide(["web.research"], "example.com", 443, None, [], PUB, "http://proxy:3128") == ("proxy", "http://proxy:3128")
    with pytest.raises(Denied):
        decide(["web.research"], "evil.example", 443, None, [], PRIV, "http://proxy:3128")
    with pytest.raises(Denied):
        decide(["web.research"], "example.com", 443, None, [], fail)


def test_usage_of_openai_and_anthropic():
    assert usage_of({"usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
                               "prompt_tokens_details": {"cached_tokens": 4}}}) == \
        {"inputTokens": 6, "outputTokens": 3, "totalTokens": 13, "cacheReadTokens": 4}   # 和 dsh 一样：输入不含命中缓存的
    assert usage_of({"usage": {"prompt_tokens": 10, "completion_tokens": 1, "prompt_cache_hit_tokens": 8}}) == \
        {"inputTokens": 2, "outputTokens": 1, "cacheReadTokens": 8}
    assert usage_of({"type": "message_start", "message": {"usage": {"input_tokens": 7}}}) == {"inputTokens": 7}
    assert usage_of({"choices": []}) == {}


class Upstream:
    """假的模型供应商：记下收到的密钥与模型，流式回两段，最后一段带 usage。"""

    def __init__(self):
        self.seen: list[dict] = []
        up = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                up.seen.append({"auth": self.headers.get("Authorization"), "model": body["model"], "path": self.path})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
                self.wfile.write(b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"


def http(addr, raw: bytes) -> bytes:
    s = socket.create_connection(addr, timeout=10)
    s.sendall(raw)
    out = b""
    while chunk := s.recv(65536):
        out += chunk
    s.close()
    return out


def post(addr, path, token, body, header="Authorization", prefix="Bearer "):
    data = json.dumps(body).encode()
    return http(addr, f"POST {path} HTTP/1.1\r\nHost: gw\r\n{header}: {prefix}{token}\r\nContent-Type: application/json\r\n"
                      f"Content-Length: {len(data)}\r\n\r\n".encode() + data)


@pytest.fixture()
def gw(tmp_path, monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(k, raising=False)
    g = Gateway(("127.0.0.1", 9), lambda: None, [], log_dir=tmp_path / "gw", upstream_proxy="")
    addr = g.start("127.0.0.1", 0)
    yield g, addr
    g.stop()


def test_model_proxy_checks_model_swaps_key_and_counts_usage(gw):
    """不变量 10：网关令牌认定这一轮；模型不是这一轮的 403；转发时换成真密钥；用量从流里读出来，两次请求都记上。"""
    g, addr = gw
    up = Upstream()
    tok = g.issue("seat:c/a", [], provider="p1", model="m1", upstream=up.url, key="sk-real")
    assert b" 403 " in post(addr, "/llm/p1/chat/completions", tok, {"model": "other"}).split(b"\r\n")[0]
    assert b" 403 " in post(addr, "/llm/p2/chat/completions", tok, {"model": "m1"}).split(b"\r\n")[0]
    assert b" 403 " in post(addr, "/llm/p1/chat/completions", "mgw-wrong", {"model": "m1"}).split(b"\r\n")[0]
    out = post(addr, "/llm/p1/chat/completions", tok, {"model": "m1", "stream": True})
    assert b" 200 " in out.split(b"\r\n")[0] and b'"content":"hi"' in out
    post(addr, "/llm/p1/chat/completions", tok, {"model": "m1"})      # 生成标题之类的另一个请求
    assert up.seen[0] == {"auth": "Bearer sk-real", "model": "m1", "path": "/v1/chat/completions"}
    assert g.close(tok) == {"inputTokens": 10, "outputTokens": 4, "totalTokens": 14}
    assert b" 403 " in post(addr, "/llm/p1/chat/completions", tok, {"model": "m1"}).split(b"\r\n")[0]  # 这一轮结束了
    up.server.shutdown()


def test_source_address_must_match(gw):
    g, addr = gw
    tok = g.issue("seat:c/a", [], provider="p1", model="m1", upstream="http://127.0.0.1:9")
    g.bind("seat:c/a", "10.9.9.9")
    out = post(addr, "/llm/p1/chat/completions", tok, {"model": "m1"})
    assert b" 403 " in out.split(b"\r\n")[0] and "不是从这个执行单元的沙箱发出的" in out.decode()


def test_only_the_sandbox_endpoint_is_forwarded_and_connect_is_checked(gw, tmp_path):
    """别的 Matrix 接口不转；CONNECT 认令牌、按权限与地址放行，每个请求记一行（不记内容）。"""
    g, addr = gw
    assert b" 404 " in http(addr, b"GET /api/me HTTP/1.1\r\nHost: gw\r\n\r\n").split(b"\r\n")[0]
    first = http(addr, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")   # 没带凭证：质询（git 等着它才带）
    assert b" 407 " in first.split(b"\r\n")[0] and b"Proxy-Authenticate: Basic" in first
    bad = base64.b64encode(b"sbx:mgw-nope").decode()
    assert b" 403 " in http(addr, f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {bad}\r\n\r\n".encode()).split(b"\r\n")[0]
    tok = g.issue("seat:c/a", ["web.research"])
    auth = base64.b64encode(f"sbx:{tok}".encode()).decode()
    out = http(addr, f"CONNECT 169.254.169.254:80 HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n".encode())
    assert b" 403 " in out.split(b"\r\n")[0]
    out = http(addr, f"GET http://127.0.0.1:{addr[1]}/ HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n".encode())
    assert b" 403 " in out.split(b"\r\n")[0]
    rows = [json.loads(x) for x in (tmp_path / "gw" / "egress.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["host"] for r in rows] == ["169.254.169.254", "127.0.0.1"] and all(r["unit"] == "seat:c/a" for r in rows)
    assert all(r["result"].startswith("denied") for r in rows)


def test_sandbox_connect_is_spliced_to_core(tmp_path):
    core = socket.socket()
    core.bind(("127.0.0.1", 0))
    core.listen(1)
    g = Gateway(core.getsockname(), lambda: None, [], upstream_proxy="")
    addr = g.start("127.0.0.1", 0)

    def echo():
        c, _ = core.accept()
        data = c.recv(65536)
        c.sendall(b"HTTP/1.1 101 OK\r\n\r\n" + data[-5:])
        c.close()
    threading.Thread(target=echo, daemon=True).start()
    out = http(addr, b"GET /api/sandbox/connect?token=x HTTP/1.1\r\nUpgrade: websocket\r\n\r\nhello")
    assert out.startswith(b"HTTP/1.1 101") and out.endswith(b"hello")
    g.stop()
    core.close()


# ================= 2b：解开 HTTPS，按方法、URL 与预算放行 =================
from matrix.gateway import check_request, host_kind  # noqa: E402


def test_check_request_tiers():
    """6.4 的四级：镜像源与白名单只读；公网读只 GET / HEAD、不带请求体、URL ≤ 2 KB，算预算；公网写只到写白名单。"""
    W = ["api.partner.example"]
    assert check_request("public", "example.com", "GET", "/a", False, W) == (True, "", True)
    assert check_request("public", "example.com", "HEAD", "/a", False, W)[0]
    assert not check_request("public", "example.com", "POST", "/a", True, W)[0]
    assert not check_request("public", "example.com", "GET", "/a", True, W)[0]        # GET 带请求体也算写
    assert not check_request("public", "example.com", "PUT", "/a", False, W)[0]
    assert "2048" in check_request("public", "example.com", "GET", "/" + "x" * 2100, False, W)[1]
    assert check_request("public", "api.partner.example", "POST", "/a", True, W) == (True, "", False)  # 写白名单不算预算
    assert check_request("mirror", "pypi.org", "GET", "/simple/x/", False, W) == (True, "", False)
    assert "只读" in check_request("mirror", "pypi.org", "POST", "/", True, W)[1]
    assert check_request("mirror", "github.com", "POST", "/o/r.git/git-upload-pack", True, W)[0]   # git 取代码
    assert not check_request("public", "evil.example", "POST", "/o/r.git/git-upload-pack", True, W)[0]
    assert "只读" in check_request("allow", "wiki.corp", "DELETE", "/", False, W)[1]
    pol = parse_policy({"allow": ["wiki.corp"]})
    assert [host_kind(h, pol, ["pypi.org"]) for h in ("files.pypi.org", "wiki.corp", "example.com")] == \
        ["mirror", "allow", "public"]


def test_budget_stops_public_reads_and_notifies_once(tmp_path):
    hit: list[str] = []
    g = Gateway(("127.0.0.1", 9), lambda: None, [], upstream_proxy="", budget=100, on_budget=hit.append)
    tok = g.issue("seat:c/a", ["web.research"])
    grant = g._grants[g._h(tok)]
    assert g._allow(grant, "example.com", "GET", "/a", False, 60) == ""
    assert "出网预算" in g._allow(grant, "example.com", "GET", "/b", False, 60)
    assert "出网预算" in g._allow(grant, "example.com", "GET", "/c", False, 10)
    assert hit == ["seat:c/a"]                                                       # 只通知一次
    g2 = Gateway(("127.0.0.1", 9), lambda: None, ["pypi.org"], upstream_proxy="", budget=10)
    t2 = g2.issue("seat:c/b", ["shell.exec"])
    assert g2._allow(g2._grants[g2._h(t2)], "pypi.org", "GET", "/x", False, 1000) == ""  # 镜像源不算预算


def test_tls_is_terminated_with_per_host_certs(tmp_path, monkeypatch):
    """CONNECT 之后网关用出网 CA 现签的证书终止沙箱的 TLS，逐个请求核对：白名单里的地址 GET 放行、POST 拒绝；
    转给目标时用真的 TLS 并校验目标证书。"""
    import ssl as _ssl
    from http.server import HTTPServer
    from matrix.egress_ca import EgressCA
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(k, raising=False)
    upstream_ca = EgressCA(tmp_path / "up-ca")

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            body = f"hello {self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = HTTPServer(("127.0.0.1", 0), H)
    srv.socket = upstream_ca.context("127.0.0.1").wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    trust = _ssl.create_default_context(cafile=str(tmp_path / "up-ca" / "ca.crt"))
    monkeypatch.setattr("matrix.gateway.ssl.create_default_context", lambda *a, **k: trust)

    ca = EgressCA(tmp_path / "egress-ca")
    if os.name != "nt":   # Windows 不支持 POSIX 权限位
        assert oct((tmp_path / "egress-ca" / "ca.key").stat().st_mode & 0o777) == "0o600"
    g = Gateway(("127.0.0.1", 9), lambda: parse_policy({"allow": ["127.0.0.1/32"]}), [], upstream_proxy="", ca=ca,
                log_dir=tmp_path / "gw")
    addr = g.start("127.0.0.1", 0)
    tok = g.issue("seat:c/a", ["web.research"])
    auth = base64.b64encode(f"sbx:{tok}".encode()).decode()

    def through(method: str) -> bytes:
        s = socket.create_connection(addr, timeout=10)
        s.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n".encode())
        assert b" 200 " in s.recv(4096)
        client = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        client.load_verify_locations(str(tmp_path / "egress-ca" / "ca.crt"))
        t = client.wrap_socket(s, server_hostname="127.0.0.1")
        body = b"x=1" if method == "POST" else b""
        t.sendall(f"{method} /p HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                  + body)
        out = b""
        while chunk := t.recv(65536):
            out += chunk
        t.close()
        return out
    got = through("GET")
    assert got.startswith(b"HTTP/1.1 200") and got.endswith(b"hello /p")
    denied = through("POST")
    assert denied.startswith(b"HTTP/1.1 403") and "只读" in denied.decode()
    rows = [json.loads(x) for x in (tmp_path / "gw" / "egress.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["method"], r["result"][:6]) for r in rows] == [("GET", "ok"), ("POST", "denied")]
    g.stop()
    srv.shutdown()
