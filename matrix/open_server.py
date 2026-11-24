"""外部应用的接口（docs/open-api.md，不变量 30）：`/open/v1/*`（查、组、发）与 `/svc/*`（调，标准 HTTP 形状）。

不走登录：只认 `Authorization: Bearer mat_…`（应用令牌）。令牌代表授权它的人或应用的业务系统账号，接口做的事等于
那个主体本来就能做的、再交令牌的 scope 与项目范围；返回的只有配置与用量，不给内容（它自己调用服务的会话除外）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .apps import AppContext
from .engine import Network, NetworkError
from .models import FILE_LIMIT  # 上传资料的上限，与网页上传、Agent 的 publish_file 同一个

SVC_TIMEOUT_S = 900          # 一次 /svc 对话最多等多久
SVC_HEARTBEAT_S = 15         # 这么久流里没有任何事件，发一条 progress 心跳（还有谁在做、过了多久），调用方不至于以为卡住了


class OpenProjectIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    description: str = Field("", max_length=2000)
    brief: str = Field("", max_length=4000)
    members: list[str] = Field(default_factory=list, max_length=50)
    blueprint: str | None = Field(None, max_length=100)
    params: dict[str, Any] | None = None
    roles: dict[str, str] | None = None          # 模板的角色 → 项目成员的 Agent id
    digest: str | None = Field(None, max_length=200)


class OpenProjectPatch(BaseModel):
    description: str | None = Field(None, max_length=2000)
    brief: str | None = Field(None, max_length=4000)
    roles: dict[str, Any] | None = None


class OpenAccessIn(BaseModel):
    path: str = Field(..., min_length=1, max_length=500)
    rule: dict[str, Any] | None = None


class OpenSkillIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(..., min_length=1, max_length=1000)
    body: str = Field(..., min_length=1, max_length=100_000)


class OpenPluginIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    digest: str | None = Field(None, max_length=200)


class OpenServiceIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=80)
    description: str = Field("", max_length=2000)
    entry: str = Field(..., min_length=1, max_length=200)
    staff: list[str] | None = Field(None, max_length=50)
    scope: list[str] | None = Field(None, max_length=50)
    inbox: str | None = Field(None, max_length=500)
    authority: list[str] | None = Field(None, max_length=20)
    plugins: list[str] | None = Field(None, max_length=30)
    readers: list[str] | None = Field(None, max_length=200)
    public: bool = False


class OpenServicePatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=80)
    description: str | None = Field(None, max_length=2000)
    entry: str | None = Field(None, min_length=1, max_length=200)
    staff: list[str] | None = Field(None, max_length=50)
    scope: list[str] | None = Field(None, max_length=50)
    inbox: str | None = Field(None, max_length=500)
    authority: list[str] | None = Field(None, max_length=20)
    plugins: list[str] | None = Field(None, max_length=30)
    readers: list[str] | None = Field(None, max_length=200)
    public: bool | None = None


class SvcChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=20_000)
    session_id: str | None = Field(None, max_length=100)


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def install(app: FastAPI, network: Network) -> None:
    def ctx(request: Request, scope: str | tuple[str, ...]) -> AppContext:
        h = request.headers.get("authorization", "")
        token = h[7:].strip() if h.lower().startswith("bearer ") else ""
        try:
            return network.app_context(token, scope)
        except NetworkError as e:
            msg = str(e)
            if msg == "unknown token":
                raise HTTPException(401, "令牌无效、已撤销或应用已停用") from None
            raise HTTPException(429 if "频繁" in msg else 403, msg) from None

    PROJECT = ("projects.write", "services.publish")

    # ---- 查 ----
    @app.get("/open/v1/agents")
    def open_agents(request: Request) -> list[dict]:
        return network.open_agents(ctx(request, "agents.read"))

    @app.get("/open/v1/blueprints")
    def open_blueprints(request: Request) -> list[dict]:
        return network.open_blueprints(ctx(request, "agents.read"))

    # ---- 组 ----
    @app.get("/open/v1/projects")
    def open_projects(request: Request) -> list[dict]:
        return network.open_projects(ctx(request, PROJECT))

    @app.get("/open/v1/projects/{project_id}")
    def open_project(project_id: str, request: Request) -> dict:
        return network.open_project(ctx(request, PROJECT), project_id)

    @app.post("/open/v1/projects")
    def open_create_project(body: OpenProjectIn, request: Request) -> dict:
        return network.open_create_project(ctx(request, "projects.write"), **body.model_dump())

    @app.delete("/open/v1/projects/{project_id}")
    def open_delete_project(project_id: str, request: Request) -> dict:
        network.open_delete_project(ctx(request, "projects.write"), project_id)
        return {"ok": True}

    @app.patch("/open/v1/projects/{project_id}")
    def open_update_project(project_id: str, body: OpenProjectPatch, request: Request) -> dict:
        return network.open_update_project(ctx(request, "projects.write"), project_id, **body.model_dump(exclude_unset=True))

    @app.put("/open/v1/projects/{project_id}/members/{agent_id}")
    def open_add_member(project_id: str, agent_id: str, request: Request) -> dict:
        return network.open_add_member(ctx(request, "projects.write"), project_id, agent_id)

    @app.delete("/open/v1/projects/{project_id}/members/{agent_id}")
    def open_remove_member(project_id: str, agent_id: str, request: Request) -> dict:
        return network.open_remove_member(ctx(request, "projects.write"), project_id, agent_id)

    @app.put("/open/v1/projects/{project_id}/files/{path:path}")
    async def open_put_file(project_id: str, path: str, request: Request) -> dict:
        c = ctx(request, "projects.write")
        too_big = HTTPException(413, f"文件最多 {FILE_LIMIT // 1_000_000} MB")
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > FILE_LIMIT:
            raise too_big
        buf = bytearray()  # 边收边数，不先把超限的整份读进内存
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > FILE_LIMIT:
                raise too_big
        return await run_in_threadpool(network.open_put_file, c, project_id, path, bytes(buf))

    @app.patch("/open/v1/projects/{project_id}/access")
    def open_file_access(project_id: str, body: OpenAccessIn, request: Request) -> dict:
        return network.open_file_access(ctx(request, "projects.write"), project_id, body.path, body.rule)

    @app.post("/open/v1/projects/{project_id}/skills")
    def open_save_skill(project_id: str, body: OpenSkillIn, request: Request) -> dict:
        return network.open_save_skill(ctx(request, "projects.write"), project_id, body.name, body.description, body.body)

    @app.post("/open/v1/projects/{project_id}/plugins")
    def open_install_plugin(project_id: str, body: OpenPluginIn, request: Request) -> dict:
        return network.open_install_plugin(ctx(request, "projects.write"), project_id, body.name, body.digest)

    # ---- 发 ----
    @app.post("/open/v1/projects/{project_id}/services")
    def open_create_service(project_id: str, body: OpenServiceIn, request: Request) -> dict:
        return network.open_create_service(ctx(request, "services.publish"), project_id, **body.model_dump())

    @app.patch("/open/v1/projects/{project_id}/services/{service_id}")
    def open_update_service(project_id: str, service_id: str, body: OpenServicePatch, request: Request) -> dict:
        return network.open_update_service(ctx(request, "services.publish"), project_id, service_id,
                                           **body.model_dump(exclude_unset=True))

    @app.delete("/open/v1/projects/{project_id}/services/{service_id}")
    def open_withdraw_service(project_id: str, service_id: str, request: Request) -> dict:
        return network.open_withdraw_service(ctx(request, "services.publish"), project_id, service_id)

    @app.get("/open/v1/projects/{project_id}/services/{service_id}/usage")
    def open_service_usage(project_id: str, service_id: str, request: Request,
                           days: int = Query(30, ge=1, le=90)) -> dict:
        return network.open_service_usage(ctx(request, "services.publish"), project_id, service_id, days)

    # ---- 调：标准 HTTP 形状（remote_agents.py 开头），另一个 Matrix 用内置 standard 驱动、url 填 …/svc 就能接 ----
    @app.get("/svc/agents/{service_id}")
    def svc_card(service_id: str, request: Request) -> dict:
        return network.svc_card(ctx(request, "services.call"), service_id)

    @app.post("/svc/agents/{service_id}/chat")
    def svc_chat(service_id: str, body: SvcChatIn, request: Request) -> StreamingResponse:
        c = ctx(request, "services.call")
        conv = network.svc_session(c, service_id, body.session_id)
        seq = network.bus.last_seq
        network.send_message(conv.id, body.message, actor=c.principal)  # 在后台跑；下面读事件流

        def stream():
            yield _sse("session", {"session_id": conv.id})
            last, pos, started = None, seq, time.time()
            deadline, quiet_since, seen = started + SVC_TIMEOUT_S, started, {}
            while time.time() < deadline:
                for ev in network.bus.wait(pos, 1.0):
                    pos = ev.seq
                    d = ev.data
                    for p in network.svc_progress(c, conv.id, ev.type, d, seen):  # 被委托方的进展（委托、工具、中途的话）
                        quiet_since = time.time()
                        yield _sse("progress", p)
                    if ev.type == "run.progress" and d.get("conversation_id") == conv.id \
                            and str(d.get("context_id", "")).startswith(f"{conv.id}/"):
                        step = d.get("step") or {}
                        if step.get("kind") == "text" and str(d.get("context_id")).endswith(f"/{conv.agent_id}"):
                            quiet_since = time.time()
                            yield _sse("delta", {"text": step.get("text", "")})
                        elif step.get("kind") == "tool":
                            quiet_since = time.time()
                            yield _sse("tool", {"name": step.get("tool", ""), "input": {"brief": step.get("brief", "")}})
                    elif ev.type == "message.added" and d.get("conversation_id") == conv.id:
                        m = d.get("message") or {}
                        if m.get("role") == "agent":
                            quiet_since = time.time()
                            if last is not None:  # 前一条是中途的进展（委托出去了还在等）
                                yield _sse("delta", {"text": last + "\n\n"})
                            last = m.get("text", "")
                        elif m.get("role") == "system":
                            yield _sse("error", {"message": m.get("text", "运行失败")})
                            return
                    elif ev.type == "conversation.updated" and (d.get("conversation") or {}).get("id") == conv.id:
                        if last is not None and d["conversation"].get("status") == "idle":
                            yield _sse("done", {"text": last})
                            return
                if network.bus.closed:
                    break
                if time.time() - quiet_since >= SVC_HEARTBEAT_S:
                    quiet_since = time.time()
                    yield _sse("progress", {"kind": "heartbeat", "elapsed_s": int(time.time() - started),
                                            "working": network.svc_working(c, conv.id)})
            yield _sse("error", {"message": "等太久了：这一轮还没结束，可以带着 session_id 再问一次"})

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/svc/sessions/{session_id}/interrupt")
    def svc_interrupt(session_id: str, request: Request) -> dict:
        network.svc_interrupt(ctx(request, "services.call"), session_id)
        return {"ok": True}
