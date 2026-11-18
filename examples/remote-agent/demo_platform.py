"""演示平台：一个虚构的“公司 AI 平台”，照 Matrix 的标准 HTTP 形状提供云端 Agent（docs/remote-agents.md）。

用来本地验证接入，也是接真实平台时对照的样板：

- `GET  /agents/{agent}`：名称与描述；
- `POST /agents/{agent}/chat`：建会话与发第一条消息是同一次请求（session_id 为空），后续轮次带 session_id 续接；
  返回 SSE，`session` 事件最先；
- `POST /sessions/{id}/interrupt`：会话中断，会话保留；
- `PUT  /agents/{agent}/mcp`：扮演平台的“Agent 级 MCP 配置”页面（真实平台是在网页上填）：{url, headers}；
  之后这个 Agent 每次做事都按这份配置回调 MCP，请求里带 `X-Session-Id`（平台填的，模型改不了）。

演示 Agent 的“脑子”是确定性的：
- 消息里有“成员”：经 MCP 调 list_members，把结果说出来；
- 消息里有“委托”：经 MCP 调 delegate（委托给消息里「」括起来的成员）；
- 消息里有“慢慢来”：一直不答，直到被中断；
- 消息里有“抢跑”：先经 MCP 调 list_members、过一会儿才在流里报出会话 id（建会话与第一条消息同一次请求时的时序）；
- 最后一句回复里写明收没收到调用方的授权（`X-User-Token`），只说有没有，不回显内容。

运行：`uv run python examples/remote-agent/demo_platform.py --port 8801`（只听 127.0.0.1）。
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

API_KEY = "demo-platform-key"
AGENTS = {"expense": {"name": "报销助手（演示平台）", "description": "查报销进度、提交报销单"}}

McpPost = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]


def http_mcp_post(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=120) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


class DemoPlatform:
    def __init__(self, mcp_post: McpPost = http_mcp_post, port: int = 0):
        self.mcp_post = mcp_post
        self.mcp: dict[str, dict[str, Any]] = {}        # agent → {url, headers}（Agent 级 MCP 配置）
        self.sessions: dict[str, list[str]] = {}        # 会话 → 收到过的消息
        self.interrupted: set[str] = set()
        self.seen_headers: list[dict[str, str]] = []
        self.mcp_results: list[dict[str, Any]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # noqa: D401
                pass

            def _json(self, code: int, body: Any) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {API_KEY}":
                    self._json(401, {"error": "bad api key"})
                    return False
                return True

            def _body(self) -> dict[str, Any]:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self) -> None:  # noqa: N802
                m = re.match(r"^/agents/([\w-]+)$", self.path)
                if not self._authed():
                    return
                if m and m.group(1) in AGENTS:
                    self._json(200, AGENTS[m.group(1)])
                else:
                    self._json(404, {"error": "no such agent"})

            def do_PUT(self) -> None:  # noqa: N802
                m = re.match(r"^/agents/([\w-]+)/mcp$", self.path)
                if not self._authed() or not m:
                    return
                outer.mcp[m.group(1)] = self._body()
                self._json(200, {"ok": True})

            def do_POST(self) -> None:  # noqa: N802
                if not self._authed():
                    return
                m = re.match(r"^/sessions/([\w-]+)/interrupt$", self.path)
                if m:
                    outer.interrupted.add(m.group(1))
                    self._json(200, {"ok": True})
                    return
                m = re.match(r"^/agents/([\w-]+)/chat$", self.path)
                if not m or m.group(1) not in AGENTS:
                    self._json(404, {"error": "no such agent"})
                    return
                outer.seen_headers.append(dict(self.headers))
                body = self._body()
                sid = body.get("session_id") or f"s-{uuid.uuid4().hex[:10]}"
                outer.sessions.setdefault(sid, []).append(str(body.get("message", "")))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                outer.chat(m.group(1), sid, str(body.get("message", "")), dict(self.headers), self._send)

            def _send(self, event: str, data: dict[str, Any]) -> bool:
                try:
                    self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    return True
                except OSError:
                    return False

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()

    # ---- 演示 Agent 的“脑子” ----
    def _call(self, agent: str, sid: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg = self.mcp.get(agent)
        if not cfg:
            return {"error": {"message": "没有配置 MCP"}}
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex[:6], "method": method, "params": params or {}}
        out = self.mcp_post(cfg["url"], {**cfg.get("headers", {}), "X-Session-Id": sid}, body)
        self.mcp_results.append({"method": method, "params": params, "out": out})
        return out

    def _tool(self, agent: str, sid: str, name: str, args: dict[str, Any]) -> str:
        out = self._call(agent, sid, "tools/call", {"name": name, "arguments": args})
        if "error" in out:
            return f"（MCP 拒绝：{out['error'].get('message')}）"
        return "".join(c.get("text", "") for c in out.get("result", {}).get("content", []))

    def chat(self, agent: str, sid: str, message: str, headers: dict[str, str],
             send: Callable[[str, dict[str, Any]], bool]) -> None:
        early: list[str] = []
        if "抢跑" in message and agent in self.mcp:
            th = threading.Thread(target=lambda: early.append(self._tool(agent, sid, "list_members", {})))
            th.start()
            time.sleep(0.3)  # 会话 id 晚一点才出现在流里
            send("session", {"session_id": sid})
            th.join(10)
        else:
            send("session", {"session_id": sid})
        if "慢慢来" in message:
            while sid not in self.interrupted:
                if not send("delta", {"text": "."}):
                    return
                time.sleep(0.05)
            return
        parts = []
        if early:
            parts.append(f"抢跑查到：{early[0]}")
        if agent in self.mcp:
            self._call(agent, sid, "initialize")
            if "成员" in message:
                send("tool", {"name": "list_members", "input": {}})
                parts.append("成员：" + self._tool(agent, sid, "list_members", {}))
            m = re.search(r"委托.*?「(.+?)」", message)
            if m:
                send("tool", {"name": "delegate", "input": {"agent": m.group(1)}})
                parts.append(self._tool(agent, sid, "delegate", {"agent": m.group(1), "mission": "演示委托",
                                                                 "expected_outcome": "一句话结论"}))
        token = next((v for k, v in headers.items() if k.lower() == "x-user-token"), "")
        parts.append("收到了调用方的授权" if token else "没有调用方的授权")
        send("delta", {"text": "处理中"})
        send("done", {"text": "；".join(parts)})


def main() -> None:
    p = argparse.ArgumentParser(description="演示平台（虚构的公司 AI 平台）")
    p.add_argument("--port", type=int, default=8801)
    args = p.parse_args()
    d = DemoPlatform(port=args.port)
    print(f"演示平台 → {d.url}  API Key：{API_KEY}  Agent：{', '.join(AGENTS)}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        d.close()


if __name__ == "__main__":
    main()
