"""把看板系统的 HTTP API 包成一个远程 MCP 服务（Streamable HTTP，只用标准库）：业界通行的接入方式。

    uv run python examples/dashboard-system/board_mcp.py --port 8792 --api http://127.0.0.1:8791

部署在看板系统旁边，部署方在 Matrix 的 connectors.json 里登记它（见同目录的 connectors.json）。看板系统的地址只在这里，
技能里不再写死。每个工具带 MCP 规范里的标注（annotations）：只读的标 readOnlyHint；建草稿、改草稿只动草稿；
发布会让任何人都看到，标 openWorldHint——Matrix 据此在每次发布前先问连接的授权人。

看板系统本身不要授权，这里也就不要；如果以后要，按 MCP 的授权方式加在这一层，看板系统不用动。
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

READ = {"readOnlyHint": True}
DRAFT = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
TOOLS = [
    {"name": "list_dashboards", "title": "列出看板", "description": "列出看板系统里的看板：id、标题、状态、链接。",
     "annotations": {**READ, "title": "列出看板"}, "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_dashboard", "title": "查看看板", "description": "一个看板的详情，含草稿的 spec。",
     "annotations": {**READ, "title": "查看看板"},
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "create_dashboard", "title": "建看板草稿",
     "description": "建一个看板草稿（只有草稿，还没人能在已发布的地址看到）。spec 是 JSON 字符串，格式见技能 dashboard-howto。返回 id 与链接。",
     "annotations": {**DRAFT, "title": "建看板草稿"},
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "spec": {"type": "string"}},
                     "required": ["title", "spec"]}},
    {"name": "update_dashboard", "title": "改看板草稿",
     "description": "改一个看板的草稿（title、spec 都可选）。已发布的版本不变，要再发布才更新。",
     "annotations": {**DRAFT, "idempotentHint": True, "title": "改看板草稿"},
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "title": {"type": "string"},
                                                      "spec": {"type": "string"}}, "required": ["id"]}},
    {"name": "publish_dashboard", "title": "发布看板",
     "description": "把看板的草稿发布出去：看板系统是公开的，发布后任何人打开链接都能看到。",
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True,
                     "title": "发布看板（公开）"},
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
]


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _req(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 —— 部署方配置的地址
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(json.loads(e.read().decode("utf-8") or "{}").get("error") or f"HTTP {e.code}") from e

    def call(self, name: str, a: dict[str, Any]) -> Any:
        if "id" in a and not re.fullmatch(r"[A-Za-z0-9_]{1,40}", str(a["id"])):  # id 拼进路径：只收看板系统的 id 格式
            raise RuntimeError(f"看板 id 不对：{a['id']!r}")
        if name == "list_dashboards":
            return self._req("GET", "/api/dashboards")
        if name == "get_dashboard":
            return self._req("GET", f"/api/dashboards/{a['id']}")
        if name == "create_dashboard":
            return self._req("POST", "/api/dashboards", {"title": a.get("title", ""), "spec": a.get("spec")})
        if name == "update_dashboard":
            return self._req("PUT", f"/api/dashboards/{a['id']}", {k: a[k] for k in ("title", "spec") if k in a})
        if name == "publish_dashboard":
            return self._req("POST", f"/api/dashboards/{a['id']}/publish")
        raise RuntimeError(f"没有工具 {name}")


def serve(port: int, api: str) -> ThreadingHTTPServer:
    backend = Api(api)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def _send(self, code: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
            raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:  # noqa: N802
            msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            mid, method = msg.get("id"), msg.get("method")
            if mid is None:
                return self._send(202)
            if method == "initialize":
                return self._send(200, {"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "board-system-mcp", "version": "1.0.0"}}},
                    {"Mcp-Session-Id": secrets.token_hex(8)})
            if method == "tools/list":
                return self._send(200, {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
            if method == "tools/call":
                p = msg.get("params") or {}
                try:
                    text, err = json.dumps(backend.call(str(p.get("name")), p.get("arguments") or {}), ensure_ascii=False), False
                except (RuntimeError, KeyError, OSError, ValueError) as e:
                    text, err = f"看板系统出错：{e}", True
                return self._send(200, {"jsonrpc": "2.0", "id": mid,
                                        "result": {"content": [{"type": "text", "text": text}], "isError": err}})
            return self._send(200, {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"没有方法 {method}"}})

    return ThreadingHTTPServer(("127.0.0.1", port), H)


def main() -> None:
    ap = argparse.ArgumentParser(description="看板系统的 MCP 包装层")
    ap.add_argument("--port", type=int, default=8792)
    ap.add_argument("--api", default="http://127.0.0.1:8791", help="看板系统 HTTP API 的地址")
    args = ap.parse_args()
    print(f"看板系统 MCP：http://127.0.0.1:{args.port}/mcp → {args.api}", flush=True)
    serve(args.port, args.api).serve_forever()


if __name__ == "__main__":
    main()
