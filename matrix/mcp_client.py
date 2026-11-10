"""网络这一侧的远程 MCP 客户端（Streamable HTTP，只用标准库）：云端连接器由网络带上凭证代为调用。

只实现网络要用的三件事：initialize、tools/list、tools/call。响应可以是 JSON，也可以是 SSE 流
（取 id 对得上的那一条）。会话 id（`Mcp-Session-Id`）由服务端发，过期（404）时重新初始化一次。
目录里的地址来自部署方；用户自定义的地址用 `public_only`：只连 https 公网、防 DNS 重绑定、不跟重定向；
部署方在 connectors.json 的 `allow` 里写了的内网主机例外（可以是内网地址、可以用 http），校验与连接同样直连校验过的地址。
"""

from __future__ import annotations

import itertools
import json
import threading
import urllib.error
import urllib.request
from typing import Any, Callable

PROTO = "2025-06-18"


def _listed(host: str, allow: Any) -> bool:
    """host 在部署方给自定义连接器的内网白名单里（connectors.json 的 `allow`）：域名含子域名，或写成 IP 落在网段里。"""
    if allow is None:
        return False
    from .web_fetch import _literal
    literal = _literal(host)
    return allow.named(host) or (literal is not None and allow.listed(literal))


def _public_ip(host: str, port: int, allow: Any = None) -> str:
    """解析 host，返回第一个地址；连接时直连这个地址，防 DNS 重绑定。
    每个地址都得是公网地址，或落在部署方白名单的网段里；白名单里的域名解析到哪里都放行（内网 DNS 由部署方负责）。"""
    import socket
    from .web_fetch import is_public
    host = host.rstrip(".").lower()
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise MCPError(f"解析不了地址 {host}：{e}") from e
    ips = [i[4][0] for i in infos]
    if not ips:
        raise MCPError(f"解析不了地址 {host}")
    if allow is not None and allow.named(host):
        return ips[0]
    for ip in ips:
        if not is_public(ip) and not (allow is not None and allow.listed(ip)):
            raise MCPError(f"{host} 指向内网或保留地址，不在部署方给自定义连接器的白名单里"
                           "（connectors.json 的 allow），只能连公网服务")
    return ips[0]


def _split(url: str, allow: Any) -> tuple[str, str, int]:
    """只看写法：https（或白名单里的主机用 http）、有主机名、不带账号密码。返回（地址, 主机名, 端口）。"""
    from urllib.parse import urlsplit
    u = (url or "").strip()
    try:
        p = urlsplit(u)
        port = p.port
    except ValueError:
        raise MCPError("地址写得不对") from None
    if p.scheme not in ("https", "http") or not p.hostname or p.username or p.password or len(u) > 2000:
        raise MCPError("地址要是 https://… 的公网地址，或部署方白名单里的内网地址")
    host = p.hostname.rstrip(".").lower()
    if p.scheme == "http" and not _listed(host, allow):
        raise MCPError(f"{host} 要用 https（http 只给部署方白名单里的内网地址，见 connectors.json 的 allow）")
    return u, host, port or (443 if p.scheme == "https" else 80)


def check_public_url(url: str, allow: Any = None) -> str:
    """自定义连接器的地址：https 的公网地址（网络代连，不能被当跳板访问内网）。
    allow 是部署方的内网白名单（web_fetch.FetchPolicy，来自 connectors.json 的 `allow`）：
    写进白名单的主机可以是内网、可以用 http。"""
    u, host, port = _split(url, allow)
    _public_ip(host, port, allow)
    return u


def check_url_shape(url: str, allow: Any = None) -> str:
    """不解析域名、只看写法的那一半：https（白名单里的可以 http）、有主机名、不带账号密码，
    写成 IP 的得是公网地址或在白名单里。项目里写下的配置先这样查（写的时候不去连），成员连接时再按 check_public_url 全查。"""
    from .web_fetch import _literal, is_public
    u, host, _ = _split(url, allow)
    if _listed(host, allow):
        return u
    literal = _literal(host)
    if literal is None:
        if host == "localhost" or host.endswith(".localhost"):
            raise MCPError("自定义连接器只能连公网服务（或部署方白名单里的内网地址）")
        return u
    if not is_public(literal):
        raise MCPError("自定义连接器只能连公网服务（或部署方白名单里的内网地址）")
    return u


# ---- 手工编辑的 MCP 配置：自定义连接器的接入方式（目前只有远程 MCP） ----
HTTP_TYPES = {"http", "streamable-http", "streamable_http", "streamablehttp"}
MAX_HEADERS = 10


def parse_server_config(raw: Any) -> tuple[str, str, dict[str, str]]:
    """认常见 MCP 客户端配置的三种写法：`{"mcpServers": {"名字": {...}}}`、`{"名字": {...}}`、直接一项 `{...}`。
    只认远程 MCP（Streamable HTTP）：`url` + 可选 `headers`，其余字段忽略。返回 (名字或空, 地址, 请求头)。
    地址只查是字符串，是否公网由调用方另查；请求头的值可能是凭证，报错信息里不带。"""
    import re
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise MCPError(f"MCP 配置不是合法的 JSON：第 {e.lineno} 行第 {e.colno} 列，{e.msg}") from None
    if not isinstance(raw, dict):
        raise MCPError("MCP 配置要是一个 JSON 对象")
    name, entry = "", raw
    if "mcpServers" in raw:
        servers = raw["mcpServers"]
        if not isinstance(servers, dict) or len(servers) != 1:
            raise MCPError("mcpServers 里要正好写一个服务（一个连接器对应一个 MCP 服务）")
        (name, entry), = servers.items()
    elif not raw.keys() & {"url", "type", "command", "headers"} and len(raw) == 1:
        (name, entry), = raw.items()
    if not isinstance(entry, dict):
        raise MCPError("MCP 服务的配置要是一个 JSON 对象")
    kind = str(entry.get("type") or "").strip().lower()
    if "command" in entry or kind == "stdio":
        raise MCPError("这是在本机启动的 MCP（command）。自定义连接器由网络在云端代连，只能写远程服务的 url；"
                       "本机的服务配在你电脑上的 AI 工具里，Matrix 客户端会自动认出")
    if kind == "sse":
        raise MCPError("只支持 Streamable HTTP（type 写 http），不支持旧的 SSE 传输")
    if kind and kind.replace(" ", "") not in HTTP_TYPES:
        raise MCPError(f"认不出 type「{entry.get('type')}」，远程 MCP 写 http")
    url = entry.get("url")
    if not isinstance(url, str) or not url.strip():
        raise MCPError("要写服务地址 url")
    headers = entry.get("headers") or {}
    if not isinstance(headers, dict):
        raise MCPError("headers 要是一个 JSON 对象：{\"请求头名\": \"值\"}")
    if len(headers) > MAX_HEADERS:
        raise MCPError(f"请求头最多 {MAX_HEADERS} 个")
    clean: dict[str, str] = {}
    for k, v in headers.items():
        if not re.match(r"^[A-Za-z0-9-]{1,64}$", str(k)):
            raise MCPError(f"请求头名「{str(k)[:64]}」只能是字母、数字和 -")
        if not isinstance(v, str) or len(v) > 4000:
            raise MCPError(f"请求头 {k} 的值要是不超过 4000 字的字符串")
        clean[str(k)] = v.strip()
    if not isinstance(name, str):
        name = ""
    return " ".join(name.split())[:60], url.strip(), clean


def server_config(url: str, headers: dict[str, str]) -> dict[str, Any]:
    """一个远程 MCP 服务的配置（和 parse_server_config 认的写法一致），给页面显示与编辑。"""
    out: dict[str, Any] = {"type": "http", "url": url}
    if headers:
        out["headers"] = dict(headers)
    return out


def _public_opener(allow: Any = None) -> urllib.request.OpenerDirector:
    """直连校验过的地址（防 DNS 重绑定）、不跟重定向、不走代理。http 只给白名单里的主机。"""
    import http.client
    import socket
    import ssl

    class Conn(http.client.HTTPSConnection):
        def connect(self) -> None:
            ip = _public_ip(self.host, self.port, allow)
            sock = socket.create_connection((ip, self.port), self.timeout)
            self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)

    class PlainConn(http.client.HTTPConnection):
        def connect(self) -> None:
            if not _listed(self.host.rstrip(".").lower(), allow):
                raise OSError("只允许 https（http 只给部署方白名单里的内网地址）")
            self.sock = socket.create_connection((_public_ip(self.host, self.port, allow), self.port), self.timeout)

    class Https(urllib.request.HTTPSHandler):
        def https_open(self, req: Any) -> Any:
            return self.do_open(Conn, req)

    class Http(urllib.request.HTTPHandler):
        def http_open(self, req: Any) -> Any:
            return self.do_open(PlainConn, req)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a: Any, **k: Any) -> None:
            return None  # 不跟重定向：重定向可以指回内网

    return urllib.request.build_opener(Https, Http, NoRedirect, urllib.request.ProxyHandler({}))


_PUBLIC_OPENER = _public_opener()
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 部署方目录里的连接器怎么走代理（connectors.json 的 `proxy`，也可以每个连接器单写）：
#   auto  内网地址直连，其余照环境变量（HTTPS_PROXY / NO_PROXY）；默认
#   env   全照环境变量（原来的做法）
#   direct 一律直连，不走代理
PROXY_MODES = ("auto", "env", "direct")


def is_intranet(url: str, allow: Any = None) -> bool:
    """这个地址是不是内网：主机在白名单里、写成非公网 IP、或本机解析出的地址里有非公网的。
    本机解析不了的域名不算内网（出网只能走代理的部署：交给代理解析）。"""
    import socket
    from urllib.parse import urlsplit
    from .web_fetch import _literal, is_public
    try:
        p = urlsplit(url)
        host, port = (p.hostname or "").rstrip(".").lower(), p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        return False
    if not host:
        return False
    if _listed(host, allow):
        return True
    literal = _literal(host)
    if literal is not None:
        return not is_public(literal)
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return any(not is_public(i[4][0]) for i in infos)


def goes_direct(url: str, mode: str, allow: Any = None) -> bool:
    """按代理模式决定这一次连接是否绕过环境变量里的代理。"""
    if mode == "direct":
        return True
    if mode == "env":
        return False
    if not any(urllib.request.getproxies().get(k) for k in ("http", "https", "all")):
        return False  # 没配代理：怎么连都一样，不用去解析
    return is_intranet(url, allow)


def fetch_public(url: str, limit: int, timeout: float = 30) -> tuple[bytes, str]:
    """下载一个 https 公网地址的内容（IM 消息里的图片、文件），不跟重定向、不连内网；超过 limit 字节报错。
    返回（内容，Content-Disposition 头）。"""
    check_public_url(url)
    with _PUBLIC_OPENER.open(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
        data = resp.read(limit + 1)
        if len(data) > limit:
            raise MCPError(f"文件超过上限 {limit // 1_000_000} MB")
        return data, resp.headers.get("Content-Disposition", "") or ""


class MCPError(RuntimeError):
    pass


class RemoteMCP:
    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout_s: float = 60,
                 public_only: bool = False, allow: Callable[[], Any] | None = None,
                 proxy: str | Callable[[], str] = "auto"):
        self.url = url
        self.proxy = proxy               # 非 public_only 时：代理模式（PROXY_MODES），可以是每次取现值的函数
        self.public_only = public_only   # 用户自己填的地址：只连公网、不跟重定向（check_public_url）
        self.allow = allow               # public_only 时：每次连接取部署方现在的内网白名单（热加载）
        self.headers = dict(headers or {})
        self.timeout_s = timeout_s
        self.session_id: str | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # ---- 传输 ----
    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str]]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTO, **self.headers}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            if self.public_only:
                allow = self.allow() if self.allow else None
                opener = _PUBLIC_OPENER if allow is None else _public_opener(allow)
                resp = opener.open(req, timeout=self.timeout_s)
            else:  # 地址来自部署方的目录：内网地址按代理模式直连（allow 也算内网）
                mode = self.proxy() if callable(self.proxy) else self.proxy
                allow = self.allow() if self.allow else None
                if goes_direct(self.url, mode, allow):
                    resp = _DIRECT_OPENER.open(req, timeout=self.timeout_s)
                else:  # 现建：urlopen 的全局 opener 在第一次用时就定下了代理，之后改环境变量不生效
                    resp = urllib.request.build_opener().open(req, timeout=self.timeout_s)
        except urllib.error.HTTPError as e:
            if e.code == 404 and self.session_id:
                raise _Expired() from e
            detail = e.read().decode(errors="replace")[:300]
            raise MCPError(f"HTTP {e.code}{'（授权无效或已过期）' if e.code in (401, 403) else ''}：{detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise MCPError(f"连不上服务：{e}") from e
        with resp:
            got = {k.lower(): v for k, v in resp.headers.items()}
            if "id" not in payload:  # 通知：不等回应
                return None, got
            ctype = got.get("content-type", "")
            if "text/event-stream" in ctype:
                return _from_sse(resp, payload["id"]), got
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw.strip():
                return None, got
            data = json.loads(raw)
            if isinstance(data, list):  # 批量回应：取对得上的
                data = next((d for d in data if isinstance(d, dict) and d.get("id") == payload["id"]), None)
            return data, got

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}}
        data, _ = self._post(payload)
        if data is None:
            raise MCPError(f"{method} 没有回应")
        if data.get("error"):
            err = data["error"]
            raise MCPError(f"{method} 出错：{err.get('message') if isinstance(err, dict) else err}")
        return data.get("result")

    def _initialize(self) -> None:
        self.session_id = None
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": "initialize",
                   "params": {"protocolVersion": PROTO, "capabilities": {},
                              "clientInfo": {"name": "matrix", "version": "0.1.0"}}}
        data, got = self._post(payload)
        if data is None or data.get("error"):
            raise MCPError(f"初始化失败：{(data or {}).get('error')}")
        self.session_id = got.get("mcp-session-id") or None
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except MCPError:
            pass

    def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            if self.session_id is None:
                self._initialize()
            try:
                return self._rpc(method, params)
            except _Expired:
                self._initialize()
                return self._rpc(method, params)

    # ---- 用法 ----
    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(20):  # 分页：最多取 20 页
            result = self._call("tools/list", {"cursor": cursor} if cursor else {}) or {}
            tools += [t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def list_resources(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = None
        for _ in range(20):
            result = self._call("resources/list", {"cursor": cursor} if cursor else {}) or {}
            out += [r for r in result.get("resources") or [] if isinstance(r, dict) and r.get("uri")]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return out

    def read_resource(self, uri: str) -> str:
        result = self._call("resources/read", {"uri": uri}) or {}
        return "\n".join(str(c.get("text", "")) for c in result.get("contents") or [] if isinstance(c, dict))

    def call_tool(self, name: str, args: dict[str, Any], meta: dict[str, Any] | None = None) -> tuple[str, bool]:
        params: dict[str, Any] = {"name": name, "arguments": args}
        if meta:
            params["_meta"] = meta
        result = self._call("tools/call", params) or {}
        parts = []
        for c in result.get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                parts.append(str(c.get("text", "")))
            elif c.get("type") == "resource" and isinstance(c.get("resource"), dict):
                parts.append(str(c["resource"].get("text") or c["resource"].get("uri") or ""))
            else:
                parts.append(f"[{c.get('type')}]")
        if not parts and result.get("structuredContent") is not None:
            parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
        return "\n".join(parts), bool(result.get("isError"))


class _Expired(Exception):
    pass


def _from_sse(resp: Any, want_id: Any) -> dict[str, Any] | None:
    """读 SSE，返回 id 对得上的 JSON-RPC 回应；中间的通知、进度跳过。"""
    data: list[str] = []
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
            continue
        if line == "" and data:
            try:
                msg = json.loads("\n".join(data))
            except json.JSONDecodeError:
                msg = None
            data = []
            if isinstance(msg, dict) and msg.get("id") == want_id and ("result" in msg or "error" in msg):
                return msg
    if data:
        try:
            msg = json.loads("\n".join(data))
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg
        except json.JSONDecodeError:
            pass
    return None
