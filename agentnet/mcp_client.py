"""网络这一侧的远程 MCP 客户端（Streamable HTTP，只用标准库）：云端连接器由网络带上凭证代为调用。

只实现网络要用的三件事：initialize、tools/list、tools/call。响应可以是 JSON，也可以是 SSE 流
（取 id 对得上的那一条）。会话 id（`Mcp-Session-Id`）由服务端发，过期（404）时重新初始化一次。
地址只来自部署方维护的连接器目录，不接受用户填写（避免把网络当跳板访问内网）。
"""

from __future__ import annotations

import itertools
import json
import threading
import urllib.error
import urllib.request
from typing import Any

PROTO = "2025-06-18"


class MCPError(RuntimeError):
    pass


class RemoteMCP:
    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout_s: float = 60):
        self.url = url
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
            resp = urllib.request.urlopen(req, timeout=self.timeout_s)  # noqa: S310 —— 地址来自部署方的目录
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

    def call_tool(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        result = self._call("tools/call", {"name": name, "arguments": args}) or {}
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
