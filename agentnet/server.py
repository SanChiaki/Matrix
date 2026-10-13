"""FastAPI 服务：REST + SSE 事件流 + 静态页面 + 网络工具内部端点。

仅监听 127.0.0.1，且管理 API 没有鉴权 —— 这是本地 Demo，不要暴露到公网。
/internal/tools 由 MCP 服务调用，凭运行时调用期间才有效的执行上下文令牌识别调用方。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from pydantic import BaseModel, Field

from .engine import Network, NetworkError
from .runtime import DshRuntime, ScriptedRuntime
from .scripted import scenario_handler

STATIC = Path(__file__).parent / "static"


class NoCacheStaticFiles(StaticFiles):
    """前端仍在快速迭代：不设缓存头时浏览器会拿 ETag 复用旧的 app.js / style.css，
    刷新页面看到的还是改动前的样式。这里一律要求重新校验（仍保留 304 省流量的能力）。"""

    def file_response(self, *args: Any, **kwargs: Any):  # noqa: ANN401
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def resume_seq(since: int, last_event_id: str | None, last_seq: int) -> int:
    """SSE 续接点。浏览器自动重连时带 Last-Event-ID；事件序号不持久化，
    服务重启后序号从 0 重新计数，客户端手里的序号比当前还大时从头补发，否则会一直收不到事件。"""
    seq = since
    if last_event_id and last_event_id.isdigit():
        seq = int(last_event_id)
    return 0 if seq > last_seq else max(0, seq)


class AgentIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(min_length=2, max_length=500)
    capabilities: list[str] = Field(default_factory=list, max_length=10)
    authority: list[str] = Field(default_factory=list)
    chat_enabled: bool = True


class AgentPatch(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=80)
    description: str | None = Field(None, min_length=2, max_length=500)
    capabilities: list[str] | None = None
    authority: list[str] | None = None
    chat_enabled: bool | None = None


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field("", max_length=500)
    member_ids: list[str] = Field(default_factory=list)


class GroupPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    description: str | None = Field(None, max_length=500)
    member_ids: list[str] | None = None


class ConversationIn(BaseModel):
    agent_id: str
    group_id: str | None = None
    title: str = Field("", max_length=120)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class ToolCallIn(BaseModel):
    token: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


def create_app(network: Network) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        # 事件流阻塞在线程里等事件，uvicorn 取消不了线程：不主动唤醒，进程会卡在关闭阶段，
        # 浏览器那头的连接也一直不断，EventSource 就不会触发重连。
        network.bus.close()

    app = FastAPI(title="Agent Network", lifespan=lifespan)
    app.mount("/static", NoCacheStaticFiles(directory=STATIC), name="static")

    @app.exception_handler(NetworkError)
    async def _net_err(_: Request, e: NetworkError) -> JSONResponse:
        msg = str(e)
        return JSONResponse({"detail": msg}, status_code=404 if msg.startswith("unknown ") else 409)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/info")
    def info() -> dict:
        return {"runtime": network.runtime.name, "limits": network.limits.__dict__}

    @app.get("/api/network")
    def snapshot() -> dict:
        return network.snapshot()

    # ---- Agent ----
    @app.post("/api/agents")
    def create_agent(body: AgentIn) -> dict:
        return network.create_agent(**body.model_dump()).to_dict()

    @app.patch("/api/agents/{agent_id}")
    def update_agent(agent_id: str, body: AgentPatch) -> dict:
        return network.update_agent(agent_id, **body.model_dump(exclude_unset=True)).to_dict()

    @app.delete("/api/agents/{agent_id}")
    def delete_agent(agent_id: str) -> dict:
        network.delete_agent(agent_id)
        return {"ok": True}

    # ---- 群组 ----
    @app.post("/api/groups")
    def create_group(body: GroupIn) -> dict:
        return network.create_group(**body.model_dump()).to_dict()

    @app.patch("/api/groups/{group_id}")
    def update_group(group_id: str, body: GroupPatch) -> dict:
        return network.update_group(group_id, **body.model_dump(exclude_unset=True)).to_dict()

    @app.delete("/api/groups/{group_id}")
    def delete_group(group_id: str) -> dict:
        network.delete_group(group_id)
        return {"ok": True}

    # ---- 对话 ----
    @app.post("/api/conversations")
    def create_conversation(body: ConversationIn) -> dict:
        return network.create_conversation(body.agent_id, body.group_id, body.title).to_dict()

    @app.get("/api/conversations/{conv_id}")
    def conversation(conv_id: str) -> dict:
        return network.conversation_detail(conv_id)

    @app.delete("/api/conversations/{conv_id}")
    def delete_conversation(conv_id: str) -> dict:
        network.delete_conversation(conv_id)
        return {"ok": True}

    @app.post("/api/conversations/{conv_id}/messages")
    def send(conv_id: str, body: MessageIn) -> dict:
        m = network.send_message(conv_id, body.text)
        return {"id": m.id, "role": m.role, "text": m.text, "ts": m.ts}

    # ---- 委托与知识 ----
    @app.get("/api/tasks/{task_id}")
    def task(task_id: str) -> dict:
        return network.task_detail(task_id)

    @app.get("/api/knowledge/{key}")
    def knowledge(key: str) -> dict:
        k = network.store.knowledge.get(key)
        if k is None:
            raise HTTPException(404)
        return k.to_dict(content=True)

    # ---- 事件流 ----
    @app.get("/api/events")
    async def events(request: Request, since: int = 0) -> StreamingResponse:
        async def stream():
            seq = resume_seq(since, request.headers.get("last-event-id"), network.bus.last_seq)
            yield "retry: 2000\n\n"
            while not network.bus.closed:
                evs = await asyncio.to_thread(network.bus.wait, seq, 15.0)
                if not evs:
                    yield ": keepalive\n\n"
                    continue
                for ev in evs:
                    seq = ev.seq
                    yield f"id: {ev.seq}\ndata: {json.dumps(ev.to_dict(), ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- 网络工具（MCP 服务回调） ----
    @app.post("/internal/tools")
    async def tools(body: ToolCallIn) -> dict:
        # delegate 会阻塞到子任务结束，放到线程里跑
        text, is_error = await asyncio.to_thread(network.handle_tool, body.token, body.tool, body.args)
        return {"text": text, "is_error": is_error}

    return app


class AgentNetServer(uvicorn.Server):
    """收到退出信号就唤醒事件流。uvicorn 要等所有连接结束才执行 lifespan 关闭，
    只靠 lifespan 的话事件流会一直拖到优雅关闭超时才被强行切断。"""

    def __init__(self, config: uvicorn.Config, network: Network):
        super().__init__(config)
        self.network = network

    def handle_exit(self, sig: int, frame: Any) -> None:
        self.network.bus.close()
        super().handle_exit(sig, frame)


def build_network(runtime_name: str, data: str, port: int, dsh_bin: str = "dsh") -> Network:
    runtime = DshRuntime(dsh_bin) if runtime_name == "dsh" else ScriptedRuntime(scenario_handler)
    return Network(runtime, Path(data).resolve(), tool_url=f"http://127.0.0.1:{port}/internal/tools")


def main() -> None:
    p = argparse.ArgumentParser(description="Agent Network")
    p.add_argument("--runtime", choices=["dsh", "scripted"], default="dsh")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--data", default=os.environ.get("AGENTNET_DATA", ".agentnet"))
    p.add_argument("--dsh-bin", default="dsh")
    args = p.parse_args()

    network = build_network(args.runtime, args.data, args.port, args.dsh_bin)
    print(f"Agent Network → http://127.0.0.1:{args.port}  (runtime={network.runtime.name}, data={args.data})")
    config = uvicorn.Config(create_app(network), host="127.0.0.1", port=args.port, log_level="warning",
                            timeout_graceful_shutdown=3)
    AgentNetServer(config, network).run()


if __name__ == "__main__":
    main()
