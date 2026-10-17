"""连接器本地小桥：给本机 harness 提供与云侧同形的 `/internal/tools` 与 `/internal/approval`。

云侧这两个端点在 Caddy 后面只对容器内开放；端侧 harness 在本机，直接连网络不可达，
所以连接器在 127.0.0.1 上起一个小 HTTP 服务，收到请求后经出站连接转给服务器：

    harness ──stdio/MCP──▶ 本机桥 ──ws──▶ 服务器 /internal/* 的逻辑（执行上下文令牌仍是 run_id）

桥只监听 127.0.0.1，端口随机；令牌是这一次运行的 run_id，只在本机有效。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

ToolFn = Callable[[str, dict[str, Any]], tuple[str, bool]]
ApprovalFn = Callable[[str, str], str]


class _Handler(BaseHTTPRequestHandler):
    runs: dict[str, tuple[ToolFn | None, ApprovalFn | None]] = {}

    def do_POST(self) -> None:  # noqa: N802 —— http.server 的接口
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"text": "invalid json", "is_error": True}, 400)
        token = str(body.get("token") or "")
        tools, approvals = self.runs.get(token, (None, None))
        if self.path == "/internal/tools":
            if tools is None:
                return self._json({"text": "execution context not found", "is_error": True}, 403)
            try:
                text, is_error = tools(str(body.get("tool") or ""), dict(body.get("args") or {}))
            except Exception as e:  # noqa: BLE001 —— 网络错误原样回给 harness
                return self._json({"text": f"tool error: {e}", "is_error": True})
            return self._json({"text": text, "is_error": is_error})
        if self.path == "/internal/approval":
            if approvals is None:
                return self._json({"outcome": "unavailable"})
            return self._json({"outcome": approvals(str(body.get("tool") or ""), str(body.get("reason") or ""))})
        return self._json({"detail": "not found"}, 404)

    def _json(self, data: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: Any) -> None:
        return


class LocalBridge:
    """一个连接器进程一个桥；每次运行登记 / 注销自己的令牌（run_id）。"""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def register(self, run_id: str, tools: ToolFn | None, approvals: ApprovalFn | None) -> None:
        _Handler.runs[run_id] = (tools, approvals)

    def unregister(self, run_id: str) -> None:
        _Handler.runs.pop(run_id, None)

    def close(self) -> None:
        self._server.shutdown()
        _Handler.runs.clear()
