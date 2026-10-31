"""网页抓取的 stdio MCP 服务（只用标准库）：替代 dsh 自带的 web_fetch，能按部署方的白名单访问内网。

dsh 0.2.1 起，自带的 web_fetch（`@deepseek-ai/dsh-web-fetch-http`）只抓公网地址：解析出的地址有一个不是公网
就整个拒绝，写成内网 IP 的也拒绝，配置里没有白名单。部署方写了 `fetch.json` 时，云侧有 `web.research` 的 Agent 改用这里的
`mcp__matrix_web__fetch`，覆盖层同时关掉自带的 web_fetch（保留 web_search），免得模型挑中那个、在内网地址上碰壁。

文件（默认 `<data>/fetch.json`，`--fetch` / `MATRIX_FETCH` 可改；改了下一轮生效）::

    {
      "allow": ["wiki.corp.example", "10.20.0.0/16", "192.168.1.8"],
      "public": true,
      "replace_builtin": true
    }

- `allow`：可以访问的内网目标。域名含子域名（同 connectors.json 的 `domains`），写 `*.x` 或 `.x` 也一样；
  也可以写 IP 或网段（CIDR）。域名在白名单里时，它解析到哪里都放行（内网 DNS 由部署方负责）；
  不在白名单里的域名，解析出的每个地址都得是公网，或者落在白名单的网段里。
- `public`：是否也抓公网地址（默认是），这样关掉自带的 web_fetch 不少能力。
- `replace_builtin`：是否关掉 dsh 自带的 web_fetch（默认是）。
- 校验过的地址就是实际连接的地址（连接固定在它上面，防 DNS 重绑定）；每一跳重定向都重新校验，最多 5 跳。
- 白名单里的目标总是直连。其余目标在环境配了代理（`HTTPS_PROXY` 等，`NO_PROXY` 照常生效）时走代理：
  代理自己解析域名，这里只拒绝写成内网 IP 的地址，和 dsh 的做法一样，代理那一侧能到哪里由代理的策略管。
- 不带任何凭证：不发 Cookie、不跟 URL 里的账号密码。只读文本（HTML、text/*、JSON、XML），HTML 转成 Markdown 风格的文本。
- 没有这个文件：不挂这个服务，dsh 自带的 web_fetch 照旧。文件写坏了保留上一份能用的。
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

PROTO = "2025-06-18"
TOOL = "fetch"
MAX_URL = 2048
MAX_BYTES = 5_000_000
MAX_CHARS = 100_000
MAX_REDIRECTS = 5
TIMEOUT_S = 30.0
DEADLINE_S = 60.0          # 一次调用（含重定向）最长多久；覆盖层的 toolCallTimeoutMs 比它再宽一点
USER_AGENT = "Matrix-WebFetch/1.0"
TEXT_TYPES = ("application/json", "application/xml", "application/xhtml+xml", "application/javascript",
              "application/x-ndjson")
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9])?)(\.[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9])?)*$")

Resolver = Callable[[str, int], list[str]]


class FetchError(Exception):
    pass


# ================= 白名单 =================
@dataclass(frozen=True)
class FetchPolicy:
    domains: tuple[str, ...] = ()
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    public: bool = True
    replace_builtin: bool = True

    def named(self, host: str) -> bool:
        return any(host == d or host.endswith("." + d) for d in self.domains)

    def listed(self, ip: str) -> bool:
        a = _addr(ip)
        return any(a.version == n.version and a in n for n in self.networks)

    def allow_entries(self) -> list[str]:
        return [*self.domains, *(str(n) for n in self.networks)]

    def to_env(self) -> str:
        """交给 stdio 进程的那一份（覆盖层的 env 里）：只有白名单本身，不是机密。"""
        return json.dumps({"allow": self.allow_entries(), "public": self.public}, ensure_ascii=False)


def parse_policy(data: Any) -> FetchPolicy:
    if not isinstance(data, dict):
        raise ValueError("顶层必须是 {\"allow\": [...]}")
    allow = data.get("allow", [])
    if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
        raise ValueError("allow 要是字符串列表，如 [\"wiki.corp.example\", \"10.0.0.0/8\"]")
    domains: list[str] = []
    networks: list[Any] = []
    for raw in allow:
        entry = raw.strip().lower().removeprefix("*.").removeprefix(".").rstrip(".")
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
            continue
        except ValueError:
            pass
        if not DOMAIN.match(entry):
            raise ValueError(f"allow 里的 {raw!r} 既不是域名，也不是 IP / 网段")
        domains.append(entry)
    for key in ("public", "replace_builtin"):
        if key in data and not isinstance(data[key], bool):
            raise ValueError(f"{key} 要是 true 或 false")
    return FetchPolicy(domains=tuple(dict.fromkeys(domains)), networks=tuple(dict.fromkeys(networks)),
                       public=data.get("public", True), replace_builtin=data.get("replace_builtin", True))


class FetchConfig:
    """部署方的 fetch.json，按修改时间热加载（同 connector_catalog 的做法）。"""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._policy: FetchPolicy | None = None
        self.error: str | None = None

    def policy(self) -> FetchPolicy | None:
        with self._lock:
            if self.path is None:
                return None
            try:
                mtime = self.path.stat().st_mtime
            except OSError:
                self._mtime, self._policy, self.error = None, None, None
                return None
            if mtime != self._mtime:
                self._mtime = mtime
                try:
                    self._policy = parse_policy(json.loads(self.path.read_text(encoding="utf-8")))
                    self.error = None
                except (OSError, ValueError) as e:
                    self.error = str(e)
                    print(f"[matrix] 抓取白名单 {self.path} 无效，沿用上一份：{e}", file=sys.stderr)
            return self._policy


# ================= 地址校验 =================
def _addr(ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    a = ipaddress.ip_address(ip.split("%")[0])
    mapped = getattr(a, "ipv4_mapped", None)
    return mapped if mapped is not None else a


def is_public(ip: str) -> bool:
    a = _addr(ip)
    return a.is_global and not a.is_multicast


def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise FetchError(f"解析不了地址 {host}：{e}") from e
    return list(dict.fromkeys(i[4][0] for i in infos))


def _literal(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def check_url(url: str) -> tuple[str, str, int]:
    """只看写法：返回（scheme, 主机名, 端口）。"""
    u = (url or "").strip()
    try:
        p = urlsplit(u)
        port = p.port
    except ValueError as e:
        raise FetchError(f"地址写得不对：{e}") from e
    if p.scheme not in ("http", "https"):
        raise FetchError("只支持 http:// 与 https:// 地址")
    if p.username or p.password:
        raise FetchError("地址里不能带账号密码")
    if not p.hostname:
        raise FetchError("地址里没有主机名")
    if len(u) > MAX_URL:
        raise FetchError(f"地址太长（上限 {MAX_URL} 字符）")
    return p.scheme, p.hostname.rstrip(".").lower(), port or (443 if p.scheme == "https" else 80)


def _proxy_for(scheme: str, host: str) -> str | None:
    proxy = urllib.request.getproxies().get(scheme)
    if not proxy or urllib.request.proxy_bypass(host):
        return None
    return proxy


def plan(policy: FetchPolicy, url: str, resolver: Resolver = _resolve) -> tuple[str, Any]:
    """决定这一跳怎么连：("direct", [校验过的 IP]) 或 ("proxy", 代理地址)；不放行时抛 FetchError。"""
    scheme, host, port = check_url(url)
    literal = _literal(host)
    if policy.named(host):  # 白名单里的域名：直连，解析到哪里都放行
        return "direct", [literal] if literal else resolver(host, port)
    if literal is not None:
        _check_ip(policy, host, literal)
        proxy = None if policy.listed(literal) else _proxy_for(scheme, host)
        return ("proxy", proxy) if proxy else ("direct", [literal])
    proxy = _proxy_for(scheme, host) if policy.public else None
    try:
        ips = resolver(host, port)
    except FetchError:
        if proxy is None:
            raise
        ips = []  # 本机解析不了的公网域名（出网只能走代理的部署）：交给代理解析
    if ips and all(policy.listed(ip) for ip in ips):
        return "direct", ips
    if not ips and proxy is None:
        raise FetchError(f"解析不了地址 {host}")
    for ip in ips:
        _check_ip(policy, host, ip)
    return ("proxy", proxy) if proxy else ("direct", ips)


def _check_ip(policy: FetchPolicy, host: str, ip: str) -> None:
    if policy.listed(ip) or (policy.public and is_public(ip)):
        return
    if not policy.public:
        raise FetchError(f"{host} 不在部署方的抓取白名单里（{_allow_text(policy)}）")
    raise FetchError(f"{host} 指向内网或保留地址 {ip}，不在部署方的抓取白名单里（{_allow_text(policy)}）")


def _allow_text(policy: FetchPolicy) -> str:
    return "白名单：" + "、".join(policy.allow_entries()) if policy.allow_entries() else "白名单是空的"


# ================= 连接 =================
class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _connection(url: str, route: tuple[str, Any], timeout: float) -> tuple[http.client.HTTPConnection, str, dict]:
    """返回（连接, 请求行里的路径, 额外请求头）。直连时依次试校验过的地址，第一个连得上的为准。"""
    scheme, host, port = check_url(url)
    p = urlsplit(url)
    path = (p.path or "/") + (f"?{p.query}" if p.query else "")
    kind, target = route
    if kind == "direct":
        last: Exception | None = None
        for ip in target:
            conn = (_PinnedHTTPS if scheme == "https" else _PinnedHTTP)(host, port, ip, timeout)
            try:
                conn.connect()
                return conn, path, {}
            except OSError as e:
                conn.close()
                last = e
        raise FetchError(f"连不上 {host}：{last}")
    proxy = urlsplit(target if "://" in target else f"http://{target}")
    extra: dict[str, str] = {}
    if proxy.username:
        import base64
        cred = f"{urllib.request.unquote(proxy.username)}:{urllib.request.unquote(proxy.password or '')}"
        extra["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
    pport = proxy.port or 80
    if scheme == "https":  # CONNECT 隧道，TLS 直接和目标握手（校验目标的证书）
        conn = http.client.HTTPSConnection(proxy.hostname, pport, timeout=timeout, context=ssl.create_default_context())
        conn.set_tunnel(host, port, headers=extra)
        return conn, path, {}
    return http.client.HTTPConnection(proxy.hostname, pport, timeout=timeout), url, extra


def fetch(policy: FetchPolicy, url: str, resolver: Resolver = _resolve) -> str:
    """抓一个地址，返回给模型的文本（非 2xx 也是结果，不是错误）。不放行或出错时抛 FetchError。"""
    deadline = time.monotonic() + DEADLINE_S
    current = (url or "").strip()
    for _hop in range(MAX_REDIRECTS + 1):
        route = plan(policy, current, resolver)
        timeout = max(1.0, min(TIMEOUT_S, deadline - time.monotonic()))
        conn, path, extra = _connection(current, route, timeout)
        try:
            conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "text/html, text/*;q=0.9, */*;q=0.5",
                                               "Accept-Encoding": "identity", **extra})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
                current = urljoin(current, resp.getheader("Location", ""))
                continue
            ctype = resp.getheader("Content-Type", "") or ""
            mime = ctype.split(";")[0].strip().lower()
            if mime and not (mime.startswith("text/") or mime in TEXT_TYPES or mime.endswith(("+json", "+xml"))):
                raise FetchError(f"不支持的内容类型 {mime}：只读文本（HTML、text/*、JSON、XML）")
            body = resp.read(MAX_BYTES + 1)
        except (OSError, http.client.HTTPException) as e:
            raise FetchError(f"抓取 {current} 失败：{e}") from e
        finally:
            conn.close()
        cut_bytes = len(body) > MAX_BYTES
        text = _decode(body[:MAX_BYTES], ctype)
        if mime in ("text/html", "application/xhtml+xml") or (not mime and "<html" in text[:2000].lower()):
            text = html_to_text(text, current)
        cut = cut_bytes or len(text) > MAX_CHARS
        head = [f"URL：{current}", f"状态：{resp.status}", f"类型：{mime or '未声明'}"]
        if cut:
            head.append(f"（内容太长，只保留前 {min(len(text), MAX_CHARS)} 字）")
        return "\n".join(head) + "\n\n" + text[:MAX_CHARS]
    raise FetchError(f"重定向超过 {MAX_REDIRECTS} 次")


def fetch_bytes(policy: FetchPolicy, url: str, limit: int, accept: str = "*/*",
                resolver: Resolver = _resolve) -> tuple[bytes, str, str]:
    """按同样的校验取一个地址的原始字节（插件包、清单用）：返回（内容, 内容类型, 最后的地址）。
    超过 limit 字节、非 2xx 都抛 FetchError。"""
    deadline = time.monotonic() + DEADLINE_S
    current = (url or "").strip()
    for _hop in range(MAX_REDIRECTS + 1):
        route = plan(policy, current, resolver)
        timeout = max(1.0, min(TIMEOUT_S, deadline - time.monotonic()))
        conn, path, extra = _connection(current, route, timeout)
        try:
            conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": accept,
                                               "Accept-Encoding": "identity", **extra})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
                current = urljoin(current, resp.getheader("Location", ""))
                continue
            if not 200 <= resp.status < 300:
                raise FetchError(f"{current} 返回 {resp.status}")
            body = resp.read(limit + 1)
        except (OSError, http.client.HTTPException) as e:
            raise FetchError(f"取 {current} 失败：{e}") from e
        finally:
            conn.close()
        if len(body) > limit:
            raise FetchError(f"{current} 的内容超过 {limit // 1_000_000} MB")
        return body, (resp.getheader("Content-Type", "") or "").split(";")[0].strip().lower(), current
    raise FetchError(f"重定向超过 {MAX_REDIRECTS} 次")


_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_\-]+)""", re.I)


def _decode(body: bytes, ctype: str) -> str:
    m = re.search(r"charset=[\"']?([A-Za-z0-9_\-]+)", ctype, re.I)
    charset = m.group(1) if m else None
    if charset is None:
        mm = _META_CHARSET.search(body[:4096])  # 内网老页面常见：只在 <meta> 里写 gbk
        charset = mm.group(1).decode() if mm else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


# ================= HTML → 文本 =================
class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "object", "head", "title"}
    BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "nav", "aside", "table", "ul", "ol",
             "dl", "dt", "dd", "blockquote", "form", "figure", "figcaption", "hr", "br", "h1", "h2", "h3", "h4", "h5",
             "h6", "li", "pre"}

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out: list[str] = []
        self.skip = 0
        self.pre = 0
        self.title = ""
        self._in_title = False
        self._href: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self.skip += 1
            return
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("- ")
        elif tag == "pre":
            self.pre += 1
            self.out.append("```\n")
        elif tag == "tr":
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append("| ")
        elif tag == "a":
            href = dict(attrs).get("href")
            self._href.append(urljoin(self.base, href) if href and not href.startswith(("#", "javascript:")) else None)
            self.out.append("[")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag == "a" and self._href:
            href = self._href.pop()
            self.out.append(f"]({href})" if href else "]")
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("\n```")
        elif tag in ("td", "th"):
            self.out.append(" ")
        elif tag == "tr":
            self.out.append("|")
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self.skip:
            return
        self.out.append(data if self.pre else re.sub(r"\s+", " ", data))


def html_to_text(html: str, base: str = "") -> str:
    p = _Text(base)
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 —— 写坏的页面：能转多少算多少
        pass
    text = "".join(p.out).replace("[]", "")
    text = re.sub(r"\[\s*\]\([^)]*\)", "", text)
    lines, fenced = [], False
    for line in text.split("\n"):  # 代码块（<pre>）里的缩进保留，其余行去掉首尾空白
        if line.strip() == "```":
            fenced = not fenced
        lines.append(line.rstrip() if fenced and line.strip() != "```" else line.strip())
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    title = re.sub(r"\s+", " ", p.title).strip()
    first = text.split("\n", 1)[0].lstrip("# ").strip()
    return f"# {title}\n\n{text}" if title and title != first else text


# ================= stdio MCP =================
def tool_definition(policy: FetchPolicy) -> dict[str, Any]:
    where = "公网地址，以及" if policy.public else "只能访问"
    allow = "、".join(policy.allow_entries()) or "（无）"
    return {
        "name": TOOL,
        "description": f"抓取一个网页（http/https），返回状态码与正文文本（HTML 转成 Markdown 风格）。能访问{where}"
                       f"部署方白名单里的内网地址：{allow}（域名含子域名）。其他内网地址会被拒绝。"
                       "不带任何登录凭证；只读文本，图片、PDF 等二进制内容不支持。",
        "inputSchema": {"type": "object", "properties": {"url": {"type": "string", "description": "要抓取的完整地址"}},
                        "required": ["url"]},
        "annotations": {"title": "抓取网页", "readOnlyHint": True, "openWorldHint": True},
    }


def call(policy: FetchPolicy, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    if name != TOOL:
        return f"未知工具 {name}", True
    try:
        return fetch(policy, str((args or {}).get("url") or "")), False
    except FetchError as e:
        return str(e), True
    except Exception as e:  # noqa: BLE001
        return f"抓取失败：{e!r}", True


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    try:
        policy = parse_policy(json.loads(os.environ.get("MATRIX_FETCH_POLICY") or "{}"))
    except ValueError as e:
        print(f"MATRIX_FETCH_POLICY 无效：{e}", file=sys.stderr)
        sys.exit(2)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": min((msg.get("params") or {}).get("protocolVersion") or PROTO, PROTO),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "matrix-web", "version": "0.1.0"},
            }})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [tool_definition(policy)]}})
        elif method == "tools/call":
            p = msg.get("params") or {}
            text, is_error = call(policy, str(p.get("name")), p.get("arguments") or {})
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
