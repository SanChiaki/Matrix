"""带白名单的网页抓取（matrix/web_fetch.py）：白名单解析、每一跳的地址校验、本机服务上的真实抓取、代理、
stdio MCP、覆盖层（挂上它并关掉 dsh 自带的 web_fetch）、网络只给云侧有 web.research 的 Agent。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from matrix import auth
from matrix.dsh_check import block_of, disabled_in, plugin_ids
from matrix.engine import Network, _violations
from matrix.models import FETCH_TOOL, TOOL_AUTHORITY
from matrix.runtime import FETCH_SCRIPT, ScriptCall, ScriptedRuntime, build_patch_yaml
from matrix.web_fetch import FetchConfig, FetchError, FetchPolicy, fetch, html_to_text, parse_policy, plan

POLICY = parse_policy({"allow": ["wiki.corp.example", "*.svc.internal", "10.20.0.0/16", "192.168.1.8"]})


def resolver(table: dict[str, list[str]]):
    def resolve(host: str, port: int) -> list[str]:
        if host not in table:
            raise FetchError(f"解析不了地址 {host}")
        return table[host]
    return resolve


@pytest.fixture
def no_proxy(monkeypatch: pytest.MonkeyPatch):
    for k in list(os.environ):
        if k.lower().endswith("_proxy"):
            monkeypatch.delenv(k)


# ================= 白名单 =================
def test_policy_parsing():
    assert POLICY.domains == ("wiki.corp.example", "svc.internal")
    assert [str(n) for n in POLICY.networks] == ["10.20.0.0/16", "192.168.1.8/32"]
    assert POLICY.public and POLICY.replace_builtin
    assert POLICY.named("wiki.corp.example") and POLICY.named("a.b.svc.internal") and not POLICY.named("corp.example")
    p = parse_policy({"allow": [], "public": False, "replace_builtin": False})
    assert not p.public and not p.replace_builtin
    assert parse_policy(json.loads(POLICY.to_env())) == FetchPolicy(POLICY.domains, POLICY.networks)
    for bad in ({"allow": "x.com"}, {"allow": ["not a host!"]}, {"allow": [], "public": "yes"}, []):
        with pytest.raises(ValueError):
            parse_policy(bad)


def test_config_hot_reloads_and_keeps_last_good(tmp_path: Path, capsys: pytest.CaptureFixture):
    f = tmp_path / "fetch.json"
    cfg = FetchConfig(f)
    assert cfg.policy() is None  # 没有文件：用 dsh 自带的
    f.write_text(json.dumps({"allow": ["a.example"]}), encoding="utf-8")
    assert cfg.policy().domains == ("a.example",)
    f.write_text("{broken", encoding="utf-8")
    os.utime(f, (1, 1))
    assert cfg.policy().domains == ("a.example",) and cfg.error  # 写坏了沿用上一份
    f.unlink()
    assert cfg.policy() is None


# ================= 每一跳的地址校验 =================
def test_plan_allows_listed_intranet_and_public_only(no_proxy):
    r = resolver({"wiki.corp.example": ["10.9.9.9"], "git.svc.internal": ["172.16.0.5"], "app.corp.example": ["10.20.3.4"],
                  "db.corp.example": ["10.30.0.1"], "news.example": ["93.184.216.34"],
                  "split.example": ["93.184.216.34", "10.0.0.1"], "mapped.example": ["::ffff:10.0.0.1"]})
    assert plan(POLICY, "https://wiki.corp.example/x", r) == ("direct", ["10.9.9.9"])   # 白名单里的域名
    assert plan(POLICY, "http://git.svc.internal/", r) == ("direct", ["172.16.0.5"])    # 含子域名
    assert plan(POLICY, "http://app.corp.example/", r) == ("direct", ["10.20.3.4"])     # 落在白名单网段里
    assert plan(POLICY, "http://192.168.1.8:8080/", r) == ("direct", ["192.168.1.8"])   # 写成 IP
    assert plan(POLICY, "https://news.example/", r) == ("direct", ["93.184.216.34"])    # 公网
    for url in ("http://db.corp.example/", "http://split.example/", "http://mapped.example/", "http://127.0.0.1/",
                "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://10.0.0.1/"):
        with pytest.raises(FetchError, match="白名单"):
            plan(POLICY, url, r)
    closed = parse_policy({"allow": ["wiki.corp.example"], "public": False})
    with pytest.raises(FetchError, match="不在部署方的抓取白名单里"):
        plan(closed, "https://news.example/", r)
    for url, msg in (("ftp://wiki.corp.example/", "http"), ("http://u:p@wiki.corp.example/", "账号密码"),
                     ("http://wiki.corp.example/" + "a" * 3000, "太长")):
        with pytest.raises(FetchError, match=msg):
            plan(POLICY, url, r)


def test_plan_routes_public_through_the_proxy(monkeypatch: pytest.MonkeyPatch, no_proxy):
    """环境配了代理：公网目标走代理（本机解析不了也行）；白名单里的直连；写成内网 IP 的照样拒绝。"""
    monkeypatch.setenv("http_proxy", "http://proxy.example:3128")
    monkeypatch.setenv("https_proxy", "http://proxy.example:3128")
    r = resolver({"wiki.corp.example": ["10.9.9.9"], "app.corp.example": ["10.20.3.4"], "news.example": ["93.184.216.34"],
                  "db.corp.example": ["10.30.0.1"]})
    assert plan(POLICY, "https://news.example/", r) == ("proxy", "http://proxy.example:3128")
    assert plan(POLICY, "https://only-the-proxy-resolves.example/", r) == ("proxy", "http://proxy.example:3128")
    assert plan(POLICY, "https://wiki.corp.example/", r) == ("direct", ["10.9.9.9"])
    assert plan(POLICY, "https://app.corp.example/", r) == ("direct", ["10.20.3.4"])
    with pytest.raises(FetchError, match="白名单"):
        plan(POLICY, "http://10.0.0.1/", r)
    with pytest.raises(FetchError, match="白名单"):  # 本机就解析到了内网：不交给代理
        plan(POLICY, "http://db.corp.example/", r)
    monkeypatch.setenv("no_proxy", "news.example")
    assert plan(POLICY, "https://news.example/", r) == ("direct", ["93.184.216.34"])


# ================= 本机服务上的真实抓取 =================
class Site:
    """127.0.0.1 上的一个小网站：HTML（gbk）、重定向、二进制、404。"""

    def __init__(self):
        outer = self
        self.paths: list[str] = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                outer.paths.append(self.path)
                if self.path == "/":
                    body = ('<html><head><meta charset="gbk"><title>内网首页</title><script>var x=1</script></head>'
                            '<body><h1>周报</h1><p>本周 <a href="/doc">交付</a> 完成</p><ul><li>一</li><li>二</li></ul>'
                            '</body></html>').encode("gbk")
                    self._send(200, "text/html", body)
                elif self.path == "/hop":
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.end_headers()
                elif self.path == "/away":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.2:{outer.port}/")
                    self.end_headers()
                elif self.path == "/img":
                    self._send(200, "image/png", b"\x89PNG....")
                else:
                    self._send(404, "text/plain; charset=utf-8", "没有这一页".encode())

            def _send(self, code: int, ctype: str, body: bytes):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site():
    s = Site()
    yield s
    s.close()


def test_fetch_from_a_listed_intranet_host(site: Site):
    policy = parse_policy({"allow": ["127.0.0.1"]})
    out = fetch(policy, site.url + "/")
    assert out.startswith(f"URL：{site.url}/\n状态：200\n类型：text/html")
    assert "# 内网首页" in out and "# 周报" in out and f"[交付]({site.url}/doc)" in out and "- 一" in out
    assert "var x" not in out
    assert "URL：" + site.url + "/\n" in fetch(policy, site.url + "/hop")  # 同一台主机上的重定向
    assert "状态：404" in fetch(policy, site.url + "/nope")                # 非 2xx 是结果，不是错误
    with pytest.raises(FetchError, match="不支持的内容类型"):
        fetch(policy, site.url + "/img")
    with pytest.raises(FetchError, match="白名单"):  # 重定向到白名单外的地址：重新校验，拒绝
        fetch(policy, site.url + "/away")
    assert site.paths.count("/away") == 1
    seen = len(site.paths)
    with pytest.raises(FetchError, match="白名单"):  # 不在白名单里：连都不连
        fetch(parse_policy({"allow": []}), site.url + "/")
    assert len(site.paths) == seen


def test_fetch_through_a_proxy(monkeypatch: pytest.MonkeyPatch, no_proxy):
    """公网目标走环境里的代理：代理收到的是完整地址，本机不解析。"""
    seen: list[str] = []

    class P(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            seen.append(self.path)
            body = b"hello from the proxy"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), P)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_address[1]}")
        out = fetch(POLICY, "http://public.example/page?q=1", resolver({}))
        assert seen == ["http://public.example/page?q=1"] and out.endswith("hello from the proxy")
    finally:
        proxy.shutdown()
        proxy.server_close()


def test_html_to_text_handles_broken_pages():
    assert html_to_text("<p>a<b>b</p><pre>  x\n  y</pre><a href='#top'>顶</a>") == "ab\n\n```\n  x\n  y\n```\n[顶]"
    assert html_to_text("<div><p>未闭合") == "未闭合"
    assert html_to_text("<title>周报</title><h1>周报</h1><p>x</p>") == "# 周报\n\nx"  # 标题和第一个标题一样时不重复
    assert html_to_text("<table><tr><th>名</th><th>值</th></tr><tr><td>a</td><td>1</td></tr></table>") == \
        "| 名 | 值 |\n| a | 1 |"


# ================= stdio MCP =================
def test_mcp_stdio_lists_the_allowlist_and_refuses_the_rest():
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "fetch",
                                                                          "arguments": {"url": "http://10.0.0.1/"}}}]
    out = subprocess.run([sys.executable, str(FETCH_SCRIPT)], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                         capture_output=True, text=True, encoding="utf-8", timeout=30,
                         env={**os.environ, "MATRIX_FETCH_POLICY": POLICY.to_env()})
    replies = [json.loads(line) for line in out.stdout.splitlines()]
    assert replies[0]["result"]["serverInfo"]["name"] == "matrix-web"
    tool, = replies[1]["result"]["tools"]
    assert tool["name"] == "fetch" and "wiki.corp.example" in tool["description"] and "10.20.0.0/16" in tool["description"]
    assert tool["annotations"]["readOnlyHint"] is True
    assert replies[2]["result"]["isError"] is True and "白名单" in replies[2]["result"]["content"][0]["text"]


# ================= 覆盖层 =================
def test_overlay_mounts_it_and_turns_off_the_builtin_web_fetch():
    y = build_patch_yaml(["web.research"], fetch=POLICY)
    assert "mcp-matrix_web" in y and "MATRIX_FETCH_POLICY" in y and "wiki.corp.example" in y
    tool_web = block_of(y, "tool-web")
    assert "fetch: false" in tool_web and "search: true" in tool_web and "searchTimeoutMs: 60000" in tool_web
    assert not disabled_in(y, "tool-web")
    # 没有 web.research：不挂，tool-web 照旧整个关掉
    y2 = build_patch_yaml([], fetch=POLICY)
    assert "mcp-matrix_web" not in y2 and disabled_in(y2, "tool-web") and "fetch: false" not in y2
    # 部署方要两个都留着：只挂，不动 tool-web
    y3 = build_patch_yaml(["web.research"], fetch=parse_policy({"allow": ["a.example"], "replace_builtin": False}))
    assert "mcp-matrix_web" in y3 and "tool-web" not in plugin_ids(y3)
    # 没有 fetch.json：和以前一样
    assert build_patch_yaml(["web.research"]) == build_patch_yaml(["web.research"], fetch=None)
    assert "matrix_web" not in build_patch_yaml(["web.research"])


def test_fetch_tool_needs_web_research():
    """不变量 11：没有 web.research 却调用了抓取工具，留痕。"""
    assert TOOL_AUTHORITY[FETCH_TOOL] == "web.research"
    assert _violations([{"tool": FETCH_TOOL}], []) == [f"{FETCH_TOOL} 需要 web.research，未授予"]
    assert _violations([{"tool": FETCH_TOOL}], ["web.research"]) == []


def test_network_gives_the_allowlist_to_cloud_runs(tmp_path: Path):
    """fetch.json 在时云侧这一轮带上白名单（运行时据此挂服务、关自带的）；删掉文件下一轮就不带了。"""
    seen: list = []

    def handler(call: ScriptCall) -> str:
        seen.append(call._req.fetch)  # noqa: SLF001
        return "好"
    data = tmp_path / "data"
    data.mkdir()
    (data / "fetch.json").write_text(json.dumps({"allow": ["wiki.corp.example"]}), encoding="utf-8")
    net = Network(ScriptedRuntime(handler), data, seed=False)
    alice = auth.register(net.store, "alice@x.io", "password-123")[0].id
    a = net.create_agent("Helper", "查资料", [], ["web.research"], actor=alice)
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "看一下内网 wiki", actor=alice, background=False)
    assert seen[-1] is not None and seen[-1].domains == ("wiki.corp.example",)
    (data / "fetch.json").unlink()
    net.send_message(dm.id, "再看一下", actor=alice, background=False)
    assert seen[-1] is None
