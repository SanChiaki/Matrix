"""`/mcp/remote` 的旧版传输（MCP 2024-11-05 的 HTTP+SSE），给只支持它的外部 Agent 平台（docs/remote-agents.md 第 4 节）。

和 Streamable HTTP（`POST /mcp/remote`，server.py）用同一个地址、同一把 Matrix 密钥，走同一个 `Network.remote_mcp`，
鉴权与会话规则一字不差（不变量 28），只是消息怎么来回不一样：

    GET  /mcp/remote                         开一条 SSE 流；第一条是 endpoint 事件，给出这条流专用的消息地址
    POST /mcp/remote/messages?session_id=…   发一条 JSON-RPC，立刻回 202；响应作为 message 事件从流里回来

- 开流要密钥对，不对 401；每个平台 Agent 同时最多 MAX_STREAMS 条流。
- 消息地址里的 session_id 是这条流的随机标识（不是平台会话 id），流断了就作废（404）。
  POST 带了密钥就必须是开流的那个 Agent 的；没带就沿用开流时的密钥（有些平台只在开流时带配置的请求头）。
- 平台会话 id（X-Session-Id 等）先看这次 POST 的请求头，没有再用开流时的；tools/call 照旧必须落到 Matrix 发起的那一轮。
- 没有消息时每 KEEPALIVE_S 秒发一行注释保活，免得中间的代理把空闲连接掐掉。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .engine import Network, NetworkError

PATH = "/mcp/remote"
MESSAGES = "/mcp/remote/messages"
MAX_STREAMS = 20       # 每个平台 Agent 同时开着的流
KEEPALIVE_S = 15.0


@dataclass
class _Stream:
    agent_id: str
    key: str
    session: str                        # 开流时请求头里的平台会话 id（可能为空）
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    tasks: set[asyncio.Task] = field(default_factory=set)


def _bearer(request: Request) -> str:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else ""


def install(app: FastAPI, network: Network) -> None:
    streams: dict[str, _Stream] = {}

    def platform_session(request: Request) -> str:
        return next((v for h in network.remote_session_headers() if (v := request.headers.get(h, ""))), "")

    @app.get(PATH)
    async def remote_mcp_sse(request: Request) -> Response:
        key = _bearer(request)
        agent_id = network.remote_agent_of_key(key)
        if agent_id is None:
            return JSONResponse({"detail": "unknown key"}, status_code=401)
        if sum(1 for s in streams.values() if s.agent_id == agent_id) >= MAX_STREAMS:
            return JSONResponse({"detail": f"同一个 Agent 最多同时开 {MAX_STREAMS} 条 MCP 流"}, status_code=429)
        sid = secrets.token_urlsafe(24)
        st = _Stream(agent_id, key, platform_session(request))
        streams[sid] = st

        async def stream():
            try:
                yield f"event: endpoint\ndata: {MESSAGES}?session_id={sid}\n\n"
                idle = time.monotonic()
                while not network.bus.closed:
                    if await request.is_disconnected():  # 平台断开：立刻收掉，不等下一次保活写失败
                        break
                    try:
                        out = await asyncio.wait_for(st.queue.get(), 1.0)
                    except TimeoutError:
                        if time.monotonic() - idle >= KEEPALIVE_S:
                            idle = time.monotonic()
                            yield ": ping\n\n"
                        continue
                    idle = time.monotonic()
                    yield f"event: message\ndata: {json.dumps(out, ensure_ascii=False)}\n\n"
            finally:
                streams.pop(sid, None)
                for t in st.tasks:
                    t.cancel()

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post(MESSAGES)
    async def remote_mcp_message(request: Request, session_id: str = "") -> Response:
        st = streams.get(session_id)
        if st is None:
            return JSONResponse({"detail": "unknown session：先 GET /mcp/remote 开流，用 endpoint 事件给的地址"}, 404)
        key = _bearer(request) or st.key
        if key != st.key and network.remote_agent_of_key(key) != st.agent_id:
            return JSONResponse({"detail": "unknown key"}, status_code=401)
        try:
            msg = await request.json()
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "不是 JSON"}}, 400)
        if not isinstance(msg, dict):
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "一次一条请求"}}, 400)
        sess = platform_session(request) or st.session

        async def answer() -> None:
            try:
                out = await asyncio.to_thread(network.remote_mcp, key, sess, msg)  # tools/call 可能要等绑定
            except NetworkError:  # 密钥在流开着的时候被重签作废了
                out = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32001, "message": "unknown key"}}
            if out is not None:
                await st.queue.put(out)

        task = asyncio.create_task(answer())
        st.tasks.add(task)
        task.add_done_callback(st.tasks.discard)
        return Response(status_code=202)
