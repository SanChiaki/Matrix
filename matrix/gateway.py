"""出口网关（docs/cloud-runtime.md 第 6 节；阶段 2a）：云侧沙箱唯一的网络出口。

这一阶段是核心进程里的一个模块、单独的监听地址与端口（以后能拆成独立进程，docs/control-plane.md 第 7 节），所以模型密钥不离开核心进程。
它只认四种请求：

1. `GET /api/sandbox/connect`（WebSocket）：原样转给核心的同一个端点，别的 Matrix 接口一律不转；
2. `/llm/<供应商>/...`：模型代理。认网关令牌（沙箱里 dsh 的“密钥”位置填的就是它），核对供应商与模型是这一轮定的那个，
   换上 vault 里的真密钥转发，流式透传，从响应里读用量（含 dsh 每轮另发的生成标题的小请求）；
3. `CONNECT 主机:端口`：网页与包下载的隧道（2a 不解 TLS），按“这一轮的有效权限 + 目标主机”放行（`decide`），连接固定在校验过的地址上；
4. `GET http://主机/...`（绝对地址）：明文 HTTP 代理，同样按 `decide` 放行。

身份：网关令牌在核心签发、只在这一轮有效（`issue` / `close`）；docker 后端另核对来源地址是管理器登记的那个沙箱的地址（`bind`）。
不信任请求里自报的任何身份（不变量 10）。每个出网请求记一行（执行单元、主机、端口、字节数、结果），不记内容。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import socketserver
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .web_fetch import FetchError, FetchPolicy, _literal, _resolve, is_public

HEAD_MAX = 64 * 1024
URL_MAX = 2048                    # 公网读：URL 不超过 2 KB（6.4）
BUDGET = 64 * 1024                # 每一轮发往非白名单主机的 URL 与请求头累计字节数（6.4）
READ_METHODS = ("GET", "HEAD")
BODY_MAX = 32 * 1024 * 1024      # 一次模型请求体的上限
PUBLIC_PORTS = (80, 443)
PLACEHOLDER = "__GATEWAY__"       # 下发给沙箱的供应商地址里的占位：沙箱客户端换成它连得到的网关地址
HOP = {"connection", "keep-alive", "proxy-authorization", "proxy-connection", "te", "trailer", "transfer-encoding",
       "upgrade", "content-length", "host", "authorization", "x-api-key", "accept-encoding"}


class Denied(Exception):
    pass


class NeedAuth(Exception):
    """代理请求没带凭证：回 407 让客户端带上（git 之类先不带，等质询）。"""


@dataclass
class Grant:
    """一张网关令牌：这一轮、这个执行单元能用的模型与出网权限。真密钥只在这里（核心进程的内存里）。"""
    unit: str
    authority: list[str]
    provider: str = ""
    model: str = ""
    upstream: str = ""            # 供应商的真实地址
    api: str = "openai-completions"
    key: str = field(default="", repr=False)
    usage: dict[str, int] = field(default_factory=dict)
    requests: int = 0
    budget: int = BUDGET          # 出网预算还剩多少字节（2b）
    full_url: bool = False        # 场合里有别人、或会话记过别处来源：公网请求记完整 URL（2b）
    budget_hit: bool = False


# ================= 放行决策（纯函数） =================
def decide(authority: list[str], host: str, port: int, policy: FetchPolicy | None, mirrors: list[str],
           resolver: Callable[[str, int], list[str]] = _resolve, upstream_proxy: str | None = None) -> tuple[str, Any]:
    """这一轮能不能连 host:port：返回 ("direct", [校验过的 IP]) 或 ("proxy", 上游代理)；不放行抛 Denied。

    - 包镜像源（部署方配置的主机）：要 shell.exec；
    - fetch.json 的 allow 里的域名与网段（内网目标只认这里）：要 web.research 或 shell.exec；
    - 别的一律当公网：要 web.research，只到 80 / 443，解析出来的每个地址都得是公网地址（私有、回环、链路本地、元数据地址都拒），
      连接固定在这些地址上（防 DNS 重绑定）。本机解析不了、而网关自己要经上游代理出网时，交给上游代理。"""
    host = host.rstrip(".").lower()
    if not host or not 0 < port < 65536:
        raise Denied("地址不对")
    literal = _literal(host)
    allow = policy is not None and (policy.named(host) or (literal is not None and policy.listed(literal)))
    mirror = any(host == m or host.endswith("." + m) for m in mirrors)
    if mirror:
        if "shell.exec" not in authority:
            raise Denied(f"{host} 是包镜像源，要有 shell.exec")
    elif allow:
        if not ({"web.research", "shell.exec"} & set(authority)):
            raise Denied(f"{host} 在白名单里，但这一轮没有 web.research 或 shell.exec")
    else:
        if "web.research" not in authority:
            raise Denied(f"这一轮没有 web.research，不能访问公网（{host}）")
        if port not in PUBLIC_PORTS:
            raise Denied(f"公网只放行 80 / 443 端口（{host}:{port}）")
    if literal is not None:
        ips = [literal]
    else:
        try:
            ips = resolver(host, port)
        except FetchError:
            if upstream_proxy and not (mirror or allow):
                return "proxy", upstream_proxy
            raise Denied(f"解析不了 {host}") from None
    if not (mirror or allow):
        bad = [ip for ip in ips if not is_public(ip) and not (policy is not None and policy.listed(ip))]
        if bad:
            raise Denied(f"{host} 指向内网或保留地址 {bad[0]}，不在部署方的白名单里")
    # 网关自己要经上游代理出网时：解析出来都是公网地址的（含公网上的镜像源），交给上游代理；内网地址直连
    if upstream_proxy and all(is_public(ip) for ip in ips) and not urllib.request.proxy_bypass(host):
        return "proxy", upstream_proxy
    return "direct", ips


def host_kind(host: str, policy: FetchPolicy | None, mirrors: list[str]) -> str:
    """mirror（部署方配的包镜像源）| allow（fetch.json 的 allow）| public。"""
    host = host.rstrip(".").lower()
    if any(host == m or host.endswith("." + m) for m in mirrors):
        return "mirror"
    literal = _literal(host)
    if policy is not None and (policy.named(host) or (literal is not None and policy.listed(literal))):
        return "allow"
    return "public"


def check_request(kind: str, host: str, method: str, target: str, has_body: bool,
                  write_hosts: list[str]) -> tuple[bool, str, bool]:
    """解开 HTTPS 之后，一个请求放不放（6.4 的四级，纯函数）：（放不放, 原因, 算不算出网预算）。
    主机这一层（权限、地址）已经由 decide 过了；这里按方法分：镜像源与白名单只读；公网读只 GET / HEAD、不带请求体、URL 不超过 2 KB，
    算预算；公网写（带请求体或别的方法）默认不允许，只到部署方的写白名单。"""
    host = host.rstrip(".").lower()
    writable = any(host == w or host.endswith("." + w) for w in write_hosts)
    write = method.upper() not in READ_METHODS or has_body
    if write and kind == "mirror" and method.upper() == "POST" and target.split("?")[0].endswith("/git-upload-pack"):
        write = False   # git 的 smart HTTP 取代码用 POST（只读的取包请求）：只对部署方配的镜像源这样认，公网主机不认
    if write and not writable:
        return False, (f"{method} {host}：网关只放行只读请求（GET / HEAD、不带请求体）；要调外部系统的接口请走连接器"
                       if kind == "public" else f"{method} {host}：{'包镜像源' if kind == 'mirror' else '白名单里的地址'}只读"), False
    if kind == "public" and not writable and len(target) > URL_MAX:
        return False, f"URL 超过 {URL_MAX} 字节", False
    return True, "", kind == "public" and not writable


def usage_of(event: dict[str, Any]) -> dict[str, int]:
    """一段响应里的用量（OpenAI 的 usage；Anthropic 的 message_start / message_delta），换成 dsh 的口径。"""
    out: dict[str, int] = {}
    u = event.get("usage") if isinstance(event.get("usage"), dict) else None
    if u is None and isinstance(event.get("message"), dict):
        u = event["message"].get("usage") if isinstance(event["message"].get("usage"), dict) else None
    if not u:
        return out
    pairs = {"prompt_tokens": "inputTokens", "completion_tokens": "outputTokens", "total_tokens": "totalTokens",
             "input_tokens": "inputTokens", "output_tokens": "outputTokens",
             "cache_read_input_tokens": "cacheReadTokens", "cache_creation_input_tokens": "cacheWriteTokens"}
    for k, v in u.items():
        if k in pairs and isinstance(v, int) and not isinstance(v, bool):
            out[pairs[k]] = out.get(pairs[k], 0) + v
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") if isinstance(u.get("prompt_tokens_details"), dict) else None
    if not isinstance(cached, int):
        cached = u.get("prompt_cache_hit_tokens") if isinstance(u.get("prompt_cache_hit_tokens"), int) else None  # DeepSeek
    if isinstance(cached, int) and "prompt_tokens" in u:
        # OpenAI 口径的 prompt_tokens 含命中缓存的部分；dsh 的 inputTokens 不含：换成同一口径
        out["cacheReadTokens"] = out.get("cacheReadTokens", 0) + cached
        out["inputTokens"] = max(0, out.get("inputTokens", 0) - cached)
    return out


def _add(acc: dict[str, int], part: dict[str, int]) -> None:
    for k, v in part.items():
        acc[k] = acc.get(k, 0) + v


# ================= 网关 =================
class Gateway:
    def __init__(self, core: tuple[str, int], policy: Callable[[], FetchPolicy | None], mirrors: list[str] | None = None,
                 log_dir: Path | None = None, upstream_proxy: str | None = None, ca: Any = None,
                 write_hosts: list[str] | None = None, budget: int = BUDGET,
                 on_budget: Callable[[str], None] | None = None):
        """ca：出网 CA（egress_ca.EgressCA，2b）；有它时 CONNECT 解开 TLS、按方法与预算逐个请求放行，没有时只按主机放行隧道（2a）。"""
        self.core = core                      # 核心的监听地址：/api/sandbox/connect 转到这里
        self.policy = policy
        self.mirrors = [m.strip().lower().lstrip(".") for m in (mirrors or []) if m.strip()]
        self.log_dir = log_dir
        self.upstream_proxy = upstream_proxy if upstream_proxy is not None else (
            os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None)
        self._grants: dict[str, Grant] = {}   # sha256(令牌) → 授权
        self._ips: dict[str, str] = {}        # 执行单元 → 管理器登记的沙箱地址（docker 后端）
        self._lock = threading.Lock()
        self.server: socketserver.ThreadingTCPServer | None = None
        self.ca = ca
        self.write_hosts = [w.strip().lower().lstrip(".") for w in (write_hosts or []) if w.strip()]
        self.budget = budget
        self.on_budget = on_budget

    # ---- 令牌 ----
    @staticmethod
    def _h(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def issue(self, unit: str, authority: list[str], provider: str = "", model: str = "", upstream: str = "",
              api: str = "openai-completions", key: str = "", full_url: bool = False) -> str:
        token = "mgw-" + secrets.token_urlsafe(24)
        with self._lock:
            self._grants[self._h(token)] = Grant(unit, list(authority), provider, model, upstream.rstrip("/"), api, key,
                                                 budget=self.budget, full_url=full_url)
        return token

    def close(self, token: str) -> dict[str, int]:
        """这一轮结束：令牌作废，返回网关记到的用量（含生成标题的请求）。"""
        with self._lock:
            g = self._grants.pop(self._h(token), None)
        return dict(g.usage) if g is not None else {}

    def bind(self, unit: str, ip: str | None) -> None:
        with self._lock:
            if ip:
                self._ips[unit] = ip
            else:
                self._ips.pop(unit, None)

    def _grant(self, token: str, peer: str) -> Grant:
        with self._lock:
            g = self._grants.get(self._h(token)) if token else None
            want = self._ips.get(g.unit) if g is not None else None
        if g is None:
            raise Denied("网关令牌无效或这一轮已经结束")
        if want is not None and peer != want:
            raise Denied("请求不是从这个执行单元的沙箱发出的")
        return g

    def _log(self, row: dict[str, Any]) -> None:
        if self.log_dir is None:
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with (self.log_dir / "egress.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": round(time.time(), 3), **row}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---- 监听 ----
    def start(self, host: str, port: int) -> tuple[str, int]:
        gw = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:  # noqa: D401
                gw._serve(self.request, self.client_address[0])

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True
        self.server = Server((host, port), Handler)
        threading.Thread(target=self.server.serve_forever, name="matrix-gateway", daemon=True).start()
        return self.server.server_address[:2]

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    # ---- 一条连接 ----
    def _serve(self, conn: socket.socket, peer: str) -> None:
        conn.settimeout(120)
        try:
            head, rest = _read_head(conn)
        except (OSError, ValueError):
            conn.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _ = lines[0].split(" ", 2)
        except ValueError:
            return _reply(conn, 400, "bad request")
        headers: dict[str, str] = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        try:
            if method == "CONNECT":
                return self._connect(conn, peer, target, headers)
            if target.startswith("http://"):
                return self._plain(conn, peer, method, target, headers, lines, rest)
            path = urlsplit(target).path
            if path == "/api/sandbox/connect":
                return self._to_core(conn, head, rest)
            if path.startswith("/llm/"):
                return self._llm(conn, peer, method, target, headers, rest)
            return _reply(conn, 404, "网关只转发沙箱连接、模型请求与出网代理")
        except Denied as e:
            return _reply(conn, 403, str(e))
        except NeedAuth:
            return _reply(conn, 407, "要带沙箱的网关令牌", {"Proxy-Authenticate": 'Basic realm="matrix-gateway"'})

    def _proxy_grant(self, headers: dict[str, str], peer: str) -> Grant:
        auth = headers.get("proxy-authorization", "")
        if not auth:
            raise NeedAuth()
        token = ""
        if auth.lower().startswith("basic "):
            try:
                token = base64.b64decode(auth[6:]).decode().partition(":")[2]
            except (ValueError, UnicodeDecodeError):
                token = ""
        return self._grant(token, peer)

    def _route(self, g: Grant, host: str, port: int) -> tuple[str, Any]:
        return decide(g.authority, host, port, self.policy(), self.mirrors, upstream_proxy=self.upstream_proxy)

    def _open(self, route: tuple[str, Any], host: str, port: int, tunnel: bool) -> socket.socket:
        """连上目标：直连校验过的地址；或经上游代理（tunnel 时先 CONNECT）。"""
        kind, val = route
        if kind == "direct":
            last: Exception | None = None
            for ip in val:
                try:
                    return socket.create_connection((ip, port), timeout=30)
                except OSError as e:
                    last = e
            raise OSError(f"连不上 {host}:{port}：{last}")
        p = urlsplit(val)
        s = socket.create_connection((p.hostname, p.port or 80), timeout=30)
        if tunnel:
            s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
            resp, _ = _read_head(s)
            if b" 200" not in resp.split(b"\r\n", 1)[0]:
                s.close()
                raise OSError(f"上游代理拒绝：{resp.split(b'\\r\\n', 1)[0].decode('latin-1')}")
        return s

    def _connect(self, conn: socket.socket, peer: str, target: str, headers: dict[str, str]) -> None:
        g = self._proxy_grant(headers, peer)
        host, _, port_s = target.rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_s)
        except ValueError:
            raise Denied("地址不对") from None
        try:
            route = self._route(g, host, port)
        except Denied as e:
            self._log({"unit": g.unit, "method": "CONNECT", "host": host, "port": port, "result": f"denied: {e}"})
            raise
        try:
            up = self._open(route, host, port, tunnel=True)
        except OSError as e:
            self._log({"unit": g.unit, "method": "CONNECT", "host": host, "port": port, "result": f"error: {e}"})
            return _reply(conn, 502, str(e))
        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        if self.ca is not None:   # 2b：解开 TLS，逐个请求按方法、URL 与预算放行
            return self._intercept(conn, up, g, host, port)
        sent, got = _splice(conn, up)
        self._log({"unit": g.unit, "method": "CONNECT", "host": host, "port": port, "up": sent, "down": got,
                   "result": "ok"})

    def _allow(self, g: Grant, host: str, method: str, target: str, has_body: bool, head_bytes: int) -> str:
        """一个请求放不放：返回拒绝的原因，空串表示放行（并扣掉出网预算）。"""
        kind = host_kind(host, self.policy(), self.mirrors)
        ok, why, budgeted = check_request(kind, host, method, target, has_body, self.write_hosts)
        if not ok:
            return why
        if budgeted:
            with self._lock:
                if g.budget_hit or g.budget - head_bytes < 0:   # 超过一次，这一轮的公网读就停了
                    first, g.budget_hit = not g.budget_hit, True
                else:
                    g.budget -= head_bytes
                    first = None
            if first is not None:
                if first and self.on_budget is not None:
                    self.on_budget(g.unit)
                return f"这一轮发往外部主机的请求超过出网预算（{self.budget // 1024} KB），公网读已停"
        return ""

    def _row(self, g: Grant, host: str, port: int, method: str, target: str, **kw: Any) -> dict[str, Any]:
        row: dict[str, Any] = {"unit": g.unit, "method": method, "host": host, "port": port, **kw}
        if g.full_url:   # 场合里有别人、或会话记过别处来源：记完整 URL（6.4 留痕加细）
            row["url"] = f"https://{host}{target}" if port == 443 else f"https://{host}:{port}{target}"
        return row

    def _intercept(self, conn: socket.socket, up_raw: socket.socket, g: Grant, host: str, port: int) -> None:
        """在隧道里终止沙箱的 TLS（出网 CA 现签的证书），一个一个请求核对后再用真的 TLS 转给目标。
        解不开的（证书固定的客户端、不是 TLS 的协议）这一条连接就断掉。"""
        try:
            tls = self.ca.context(host).wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError) as e:
            self._log({"unit": g.unit, "method": "CONNECT", "host": host, "port": port,
                       "result": f"denied: 解不开的流量（{type(e).__name__}）"})
            conn.close()
            up_raw.close()
            return
        up: ssl.SSLSocket | None = None
        rf = tls.makefile("rb")
        upf = None
        try:
            while True:
                try:
                    head = _read_lines(rf)
                except (OSError, ValueError):
                    break
                if head is None:
                    break
                line, hdrs = head
                method, target = (line.split(" ") + ["", ""])[:2]
                body = _read_body(rf, hdrs)
                has_body = bool(body) or _hget(hdrs, "transfer-encoding").lower() == "chunked"
                head_bytes = len(line) + sum(len(k) + len(v) + 4 for k, v in hdrs)
                why = self._allow(g, host, method, target, has_body, head_bytes)
                if why:
                    self._log(self._row(g, host, port, method, target, result=f"denied: {why}"))
                    _respond(tls, 403, why)
                    break
                if up is None:
                    ctx = ssl.create_default_context()
                    ctx.set_alpn_protocols(["http/1.1"])
                    up = ctx.wrap_socket(up_raw, server_hostname=host)
                    upf = up.makefile("rb")
                up.sendall((line + "\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs) + "\r\n").encode("latin-1") + body)
                status_line, rh = _read_lines(upf) or ("", [])
                tls.sendall((status_line + "\r\n" + "".join(f"{k}: {v}\r\n" for k, v in rh) + "\r\n").encode("latin-1"))
                code = int(status_line.split(" ")[1]) if len(status_line.split(" ")) > 1 else 0
                down, closing = _relay_body(upf, tls, rh, method, code)
                self._log(self._row(g, host, port, method, target, status=code, up=len(body), down=down, result="ok"))
                if closing or _hget(hdrs, "connection").lower() == "close":
                    break
        except (OSError, ValueError, ssl.SSLError):
            pass
        finally:
            for x in (tls, up, up_raw):
                try:
                    if x is not None:
                        x.close()
                except OSError:
                    pass

    def _plain(self, conn: socket.socket, peer: str, method: str, target: str, headers: dict[str, str],
               lines: list[str], rest: bytes) -> None:
        g = self._proxy_grant(headers, peer)
        u = urlsplit(target)
        host, port = (u.hostname or ""), u.port or 80
        try:
            route = self._route(g, host, port)
        except Denied as e:
            self._log({"unit": g.unit, "method": method, "host": host, "port": port, "result": f"denied: {e}"})
            raise
        if self.ca is not None:   # 2b：明文 HTTP 同样按方法、URL 与预算放行
            hdr_bytes = sum(len(x) + 2 for x in lines)
            why = self._allow(g, host, method, target, bool(rest) or "content-length" in headers
                              or headers.get("transfer-encoding", "").lower() == "chunked", hdr_bytes)
            if why:
                self._log({"unit": g.unit, "method": method, "host": host, "port": port, "result": f"denied: {why}"})
                raise Denied(why)
        try:
            up = self._open(route, host, port, tunnel=False)
        except OSError as e:
            return _reply(conn, 502, str(e))
        path = target if route[0] == "proxy" else (u.path or "/") + (f"?{u.query}" if u.query else "")
        keep = [ln for ln in lines[1:] if ln and ln.split(":", 1)[0].strip().lower()
                not in ("proxy-authorization", "proxy-connection", "connection")]
        up.sendall((f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(keep) + "\r\nConnection: close\r\n\r\n").encode("latin-1")
                   + rest)
        sent, got = _splice(conn, up)
        self._log({"unit": g.unit, "method": method, "host": host, "port": port, "up": sent, "down": got, "result": "ok"})

    def _to_core(self, conn: socket.socket, head: bytes, rest: bytes) -> None:
        """沙箱客户端的长连接：转给核心的 /api/sandbox/connect（身份由启动令牌 / 执行单元凭证认定）。
        去掉沙箱带来的转发头（X-Forwarded-*、Forwarded、X-Real-IP）：核心本来就不认这个来源转来的（FORWARDED_ALLOW_IPS 只有反向代理），
        这里再挡一层，免得部署方把 FORWARDED_ALLOW_IPS 设宽了以后沙箱能冒充来源地址或 https。"""
        try:
            up = socket.create_connection(self.core, timeout=30)
        except OSError as e:
            return _reply(conn, 502, f"连不上核心：{e}")
        up.sendall(strip_forwarding(head) + rest)
        conn.settimeout(None)
        up.settimeout(None)
        _splice(conn, up)

    def _llm(self, conn: socket.socket, peer: str, method: str, target: str, headers: dict[str, str],
             rest: bytes) -> None:
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get("x-api-key", "")
        g = self._grant(token, peer)
        parts = urlsplit(target).path.split("/", 3)       # ["", "llm", 供应商, 其余]
        pid, tail = (parts[2] if len(parts) > 2 else ""), (parts[3] if len(parts) > 3 else "")
        if not g.provider or pid != g.provider:
            raise Denied(f"这一轮的模型供应商不是 {pid}")
        n = int(headers.get("content-length") or 0)
        if method != "POST" or not 0 < n <= BODY_MAX:
            raise Denied("模型请求只认带请求体的 POST")
        body = rest + _read_exact(conn, n - len(rest)) if n > len(rest) else rest[:n]
        try:
            req_json = json.loads(body)
        except ValueError:
            raise Denied("请求体不是 JSON") from None
        if req_json.get("model") != g.model:
            raise Denied(f"这一轮的模型不是 {req_json.get('model')!r}")
        q = urlsplit(target).query
        url = f"{g.upstream}/{tail}" + (f"?{q}" if q else "")
        fwd = {k: v for k, v in headers.items() if k not in HOP}
        if g.api.startswith("anthropic"):
            fwd["x-api-key"] = g.key
        else:
            fwd["authorization"] = f"Bearer {g.key}"
        with self._lock:
            g.requests += 1
        try:
            resp = urllib.request.urlopen(urllib.request.Request(url, data=body, headers=fwd, method="POST"), timeout=300)
            status, rh = resp.status, resp.headers
        except urllib.error.HTTPError as e:
            resp, status, rh = e, e.code, e.headers
        except (urllib.error.URLError, OSError) as e:
            return _reply(conn, 502, f"连不上模型供应商：{e}")
        out = [f"HTTP/1.1 {status} {getattr(resp, 'reason', '') or 'OK'}"]
        out += [f"{k}: {v}" for k, v in rh.items() if k.lower() not in HOP]
        out += ["Transfer-Encoding: chunked", "Connection: close", "", ""]
        conn.sendall("\r\n".join(out).encode("latin-1"))
        acc: dict[str, int] = {}
        buf, whole = b"", b""
        try:
            while True:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    break
                conn.sendall(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                buf += chunk
                if len(whole) < 4 * 1024 * 1024:
                    whole += chunk
                while b"\n" in buf:                      # SSE：一行一个 data:
                    line, buf = buf.split(b"\n", 1)
                    if line.startswith(b"data:"):
                        try:
                            _add(acc, usage_of(json.loads(line[5:].strip())))
                        except ValueError:
                            pass
            conn.sendall(b"0\r\n\r\n")
        except OSError:
            pass
        finally:
            resp.close()
        if not acc:                                       # 不是流式：整段 JSON
            try:
                _add(acc, usage_of(json.loads(whole)))
            except ValueError:
                pass
        with self._lock:
            _add(g.usage, acc)
        self._log({"unit": g.unit, "method": "LLM", "host": g.provider, "model": g.model, "status": status,
                   "usage": acc, "result": "ok"})


def _hget(hdrs: list[tuple[str, str]], name: str) -> str:
    return next((v for k, v in hdrs if k.lower() == name), "")


def _read_lines(f: Any) -> tuple[str, list[tuple[str, str]]] | None:
    """读一个 HTTP 头：（起始行, [(名, 值)]）；连接关了返回 None。"""
    first = f.readline(HEAD_MAX)
    if not first:
        return None
    line = first.decode("latin-1").rstrip("\r\n")
    hdrs: list[tuple[str, str]] = []
    total = len(first)
    while True:
        raw = f.readline(HEAD_MAX)
        total += len(raw)
        if total > HEAD_MAX:
            raise ValueError("请求头太大")
        if raw in (b"\r\n", b"\n", b""):
            break
        k, _, v = raw.decode("latin-1").partition(":")
        hdrs.append((k.strip(), v.strip()))
    return line, hdrs


def _read_body(f: Any, hdrs: list[tuple[str, str]]) -> bytes:
    """请求体：按 Content-Length，或原样读完 chunked（含结尾），有上限。"""
    if _hget(hdrs, "transfer-encoding").lower() == "chunked":
        out = b""
        while True:
            size_line = f.readline(1024)
            out += size_line
            n = int(size_line.split(b";")[0].strip() or b"0", 16)
            if n == 0:
                while True:
                    t = f.readline(HEAD_MAX)
                    out += t
                    if t in (b"\r\n", b"\n", b""):
                        return out
            out += f.read(n + 2)
            if len(out) > BODY_MAX:
                raise ValueError("请求体太大")
    n = int(_hget(hdrs, "content-length") or 0)
    if n > BODY_MAX:
        raise ValueError("请求体太大")
    return f.read(n) if n > 0 else b""


def _relay_body(f: Any, out: Any, hdrs: list[tuple[str, str]], method: str, code: int) -> tuple[int, bool]:
    """把响应体原样转回去：（字节数, 之后要不要断开）。"""
    if method == "HEAD" or code in (204, 304) or 100 <= code < 200:
        return 0, False
    total = 0
    if _hget(hdrs, "transfer-encoding").lower() == "chunked":
        while True:
            size_line = f.readline(1024)
            if not size_line:
                return total, True
            out.sendall(size_line)
            n = int(size_line.split(b";")[0].strip() or b"0", 16)
            if n == 0:
                while True:
                    t = f.readline(HEAD_MAX)
                    out.sendall(t)
                    if t in (b"\r\n", b"\n", b""):
                        return total, False
            data = f.read(n + 2)
            out.sendall(data)
            total += n
    if _hget(hdrs, "content-length"):
        left = int(_hget(hdrs, "content-length"))
        while left > 0:
            data = f.read(min(65536, left))
            if not data:
                return total, True
            out.sendall(data)
            total += len(data)
            left -= len(data)
        return total, _hget(hdrs, "connection").lower() == "close"
    while True:                                   # 读到关闭为止
        data = f.read1(65536) if hasattr(f, "read1") else f.read(65536)
        if not data:
            return total, True
        out.sendall(data)
        total += len(data)


def _respond(out: Any, code: int, text: str) -> None:
    body = text.encode()
    try:
        out.sendall(f"HTTP/1.1 {code} Forbidden\r\nContent-Type: text/plain; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    except OSError:
        pass


def _read_head(conn: socket.socket) -> tuple[bytes, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(8192)
        if not chunk:
            raise ValueError("连接关了")
        data += chunk
        if len(data) > HEAD_MAX:
            raise ValueError("请求头太大")
    head, _, rest = data.partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", rest


def _read_exact(conn: socket.socket, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = conn.recv(min(65536, n - len(out)))
        if not chunk:
            break
        out += chunk
    return out


def _reply(conn: socket.socket, code: int, text: str, extra: dict[str, str] | None = None) -> None:
    body = text.encode()
    reason = {400: "Bad Request", 403: "Forbidden", 404: "Not Found", 407: "Proxy Authentication Required",
              502: "Bad Gateway"}.get(code, "Error")
    more = "".join(f"{k}: {v}\r\n" for k, v in (extra or {}).items())
    try:
        conn.sendall(f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n{more}"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    except OSError:
        pass
    finally:
        conn.close()


FORWARDING = ("x-forwarded-", "forwarded", "x-real-ip")


def strip_forwarding(head: bytes) -> bytes:
    """请求头里去掉转发类的头（大小写不敏感），其余原样。head 是到空行为止的请求头（含结尾的空行）。"""
    lines = head.split(b"\r\n")
    keep = [ln for i, ln in enumerate(lines)
            if i == 0 or not ln.split(b":", 1)[0].strip().lower().decode("latin-1").startswith(FORWARDING)]
    return b"\r\n".join(keep)


def _splice(a: socket.socket, b: socket.socket) -> tuple[int, int]:
    """两边对拷到任意一边关掉；返回（a→b 字节数, b→a 字节数）。"""
    counts = [0, 0]
    a.settimeout(None)
    b.settimeout(None)

    def pump(src: socket.socket, dst: socket.socket, i: int) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
                counts[i] += len(data)
        except OSError:
            pass
        finally:
            for s in (dst, src):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
    t = threading.Thread(target=pump, args=(b, a, 1), daemon=True)
    t.start()
    pump(a, b, 0)
    t.join()
    a.close()
    b.close()
    return counts[0], counts[1]
