"""出口网关（docs/cloud-runtime.md 第 6 节，阶段 2a）：放行决策、模型核对与换密钥、用量、只转沙箱连接这一个端点。"""

from __future__ import annotations

import base64
import json
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
    assert b" 403 " in http(addr, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n").split(b"\r\n")[0]
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
