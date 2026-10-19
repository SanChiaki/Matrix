"""FastAPI 服务：REST + SSE 事件流 + 静态页面 + 运行时回调端点。

/api/* 需要登录（邮箱 + 密码，HttpOnly cookie）；每个用户只看得到自己的数据与对自己开放的 Agent。
这仍是最简实现（无 HTTPS、无 CSRF 令牌、无登录限流），只监听 127.0.0.1，不要暴露到公网。
/internal/* 由 MCP 服务与 dsh 审批插件调用，凭运行时调用期间才有效的执行上下文令牌识别调用方。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from pydantic import BaseModel, Field

from . import auth
from .device import BLOCKING, HELLO, PROTOCOL, DeviceRuntime, WebSocketLink, capabilities_from_hello
from .engine import Network, NetworkError
from .models import DESCRIPTION_LIMIT, User
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


class ModelIn(BaseModel):
    provider: str = Field(max_length=80)
    model: str = Field(max_length=120)
    reasoning_effort: str | None = Field(None, max_length=20)


class AgentIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(min_length=2, max_length=DESCRIPTION_LIMIT)
    capabilities: list[str] = Field(default_factory=list, max_length=10)
    authority: list[str] = Field(default_factory=list)
    chat_enabled: bool = True
    model: ModelIn | None = None
    device_runtime: str | None = None   # 绑定的设备 id；None = 平台云侧（默认运行时）


class AgentPatch(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=80)
    description: str | None = Field(None, min_length=2, max_length=DESCRIPTION_LIMIT)
    capabilities: list[str] | None = None
    authority: list[str] | None = None
    chat_enabled: bool | None = None
    model: ModelIn | None = None  # 显式传 null 表示改回运行时默认模型
    device_runtime: str | None = None


class ProposalApprove(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=80)
    description: str | None = Field(None, min_length=2, max_length=DESCRIPTION_LIMIT)
    capabilities: list[str] | None = Field(None, max_length=10)
    authority: list[str] | None = None
    model: ModelIn | None = None
    note: str = Field("", max_length=500)


class MemberIn(BaseModel):
    agent_id: str


class DeviceIn(BaseModel):
    name: str = Field("", max_length=80)


class NoteIn(BaseModel):
    note: str = Field("", max_length=2000)


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field("", max_length=500)
    member_ids: list[str] = Field(default_factory=list)
    kind: str = "internal"                          # internal（只有你和你的 Agent）| joint（联合群组）


class ReviewIn(BaseModel):
    accept: bool
    feedback: str = Field("", max_length=2000)


class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)


class RespondIn(BaseModel):
    accept: bool


class HomeMountIn(BaseModel):
    enabled: bool


class BriefIn(BaseModel):
    text: str = Field("", max_length=4000)


class SuggestIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    reason: str = Field("", max_length=1000)


class DecisionRecordIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    reason: str = Field("", max_length=2000)
    supersedes: str | None = None


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    location: str = Field(min_length=1, max_length=500)
    note: str = Field("", max_length=500)


class FileIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(max_length=200_000)


class GroupPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    description: str | None = Field(None, max_length=500)
    member_ids: list[str] | None = None


class ConversationIn(BaseModel):
    """打开对话：group_id → 这个群组的群聊；agent_id → 和它的私聊（第一次打开时创建）。"""
    group_id: str | None = None
    agent_id: str | None = None
    title: str = Field("", max_length=120)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    mentions: list[str] = Field(default_factory=list, max_length=20)  # 显式 @ 的成员 id；文本里的 @名称 也会识别


class ResumeIn(BaseModel):
    agent_id: str | None = None  # 只续接这个席位；省略则续接所有中断的席位
    note: str = Field("", max_length=2000)


class CredentialsIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class DecisionIn(BaseModel):
    allow: bool
    note: str = Field("", max_length=500)


class ApprovalIn(BaseModel):
    token: str
    tool: str = ""
    reason: str = ""


class ToolCallIn(BaseModel):
    token: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


def current_user(request: Request) -> User:
    """登录用户；没登录 401。"""
    user = auth.user_of(request.app.state.network.store, request.cookies.get(auth.COOKIE))
    if user is None:
        raise HTTPException(401, "请先登录")
    return user


# 模块级定义：文件开头的 `from __future__ import annotations` 让注解变成字符串，FastAPI 要在模块作用域解析它
Me = Annotated[User, Depends(current_user)]


def create_app(network: Network) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        # 事件流阻塞在线程里等事件，uvicorn 取消不了线程：不主动唤醒，进程会卡在关闭阶段，
        # 浏览器那头的连接也一直不断，EventSource 就不会触发重连。
        network.bus.close()

    app = FastAPI(title="Matrix", lifespan=lifespan)
    app.mount("/static", NoCacheStaticFiles(directory=STATIC), name="static")
    store = network.store

    @app.exception_handler(NetworkError)
    async def _net_err(_: Request, e: NetworkError) -> JSONResponse:
        msg = str(e)
        return JSONResponse({"detail": msg}, status_code=404 if msg.startswith("unknown ") else 409)

    @app.exception_handler(auth.AuthError)
    async def _auth_err(_: Request, e: auth.AuthError) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=400)

    app.state.network = network

    def set_cookie(resp: Response, token: str) -> None:
        resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30, path="/")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/about")
    def about() -> FileResponse:
        """产品简介页：静态内容，不需要登录。"""
        return FileResponse(STATIC / "about.html")

    @app.get("/api/info")
    def info() -> dict:
        return {"runtime": network.runtime.name, "limits": network.limits.__dict__,
                "default_model": getattr(network.runtime, "default_model", lambda: None)(),
                "has_users": bool(store.users), "has_unclaimed": not store.users and bool(store.agents)}

    # ---- 用户与登录 ----
    @app.post("/api/auth/register")
    def register(body: CredentialsIn, response: Response) -> dict:
        user, token, first = auth.register(store, body.email, body.password)
        set_cookie(response, token)
        return {**user.public(), "claimed_existing": first and bool(store.agents)}

    @app.post("/api/auth/login")
    def login(body: CredentialsIn, response: Response) -> dict:
        user, token = auth.login(store, body.email, body.password)
        set_cookie(response, token)
        return user.public()

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response) -> dict:
        auth.logout(store, request.cookies.get(auth.COOKIE))
        response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def whoami(user: Me) -> dict:
        own = [a for a in store.agents.values() if a.owner_id == user.id]
        return {**user.public(), "agent_count": len(own)}

    @app.get("/api/network")
    def snapshot(user: Me) -> dict:
        return network.snapshot(user.id)

    @app.get("/api/board")
    def board(user: Me) -> dict:
        return network.board(user.id)

    # ---- Agent ----
    @app.post("/api/agents")
    def create_agent(body: AgentIn, user: Me) -> dict:
        a = network.create_agent(**body.model_dump(), actor=user.id)
        return network.agent_view(a, user.id)

    @app.patch("/api/agents/{agent_id}")
    def update_agent(agent_id: str, body: AgentPatch, user: Me) -> dict:
        a = network.update_agent(agent_id, actor=user.id, **body.model_dump(exclude_unset=True))
        return network.agent_view(a, user.id)

    @app.delete("/api/agents/{agent_id}")
    def delete_agent(agent_id: str, user: Me) -> dict:
        network.delete_agent(agent_id, actor=user.id)
        return {"ok": True}

    @app.get("/api/agents/{agent_id}/ledger")
    def agent_ledger(agent_id: str, user: Me) -> list[dict]:
        network.visible_agent(agent_id, user.id)  # 只有所有者
        return network.ledger(agent_id)

    # ---- 群组 ----
    @app.post("/api/groups")
    def create_group(body: GroupIn, user: Me) -> dict:
        return network.create_group(**body.model_dump(), actor=user.id).to_dict()

    @app.patch("/api/groups/{group_id}")
    def update_group(group_id: str, body: GroupPatch, user: Me) -> dict:
        return network.update_group(group_id, actor=user.id, **body.model_dump(exclude_unset=True)).to_dict()

    @app.post("/api/groups/{group_id}/members")
    def add_member(group_id: str, body: MemberIn, user: Me) -> dict:
        return network.add_member(group_id, body.agent_id, actor=user.id).to_dict()

    @app.delete("/api/groups/{group_id}/members/{agent_id}")
    def remove_member(group_id: str, agent_id: str, user: Me) -> dict:
        return network.remove_member(group_id, agent_id, actor=user.id).to_dict()

    @app.delete("/api/groups/{group_id}")
    def delete_group(group_id: str, user: Me) -> dict:
        network.delete_group(group_id, actor=user.id)
        return {"ok": True}

    @app.get("/api/groups/{group_id}")
    def group_detail(group_id: str, user: Me) -> dict:
        return network.group_detail(group_id, actor=user.id)

    # ---- 联合群组：邀请 → 接受；退出、移出；私有目录挂载 ----
    @app.post("/api/groups/{group_id}/invitations")
    def invite(group_id: str, body: InviteIn, user: Me) -> dict:
        return network.group_detail(network.invite(group_id, body.email, actor=user.id).id, actor=user.id)

    @app.post("/api/groups/{group_id}/invitation")
    def respond(group_id: str, body: RespondIn, user: Me) -> dict:
        g = network.respond_invitation(group_id, body.accept, actor=user.id)
        return network.group_detail(g.id, actor=user.id) if g else {"ok": True}

    @app.post("/api/groups/{group_id}/leave")
    def leave(group_id: str, user: Me) -> dict:
        network.leave_group(group_id, actor=user.id)
        return {"ok": True}

    @app.delete("/api/groups/{group_id}/participants/{user_id}")
    def remove_participant(group_id: str, user_id: str, user: Me) -> dict:
        network.remove_participant(group_id, user_id, actor=user.id)
        return network.group_detail(group_id, actor=user.id)

    @app.put("/api/groups/{group_id}/members/{agent_id}/home")
    def home_mount(group_id: str, agent_id: str, body: HomeMountIn, user: Me) -> dict:
        network.set_home_mount(group_id, agent_id, body.enabled, actor=user.id)
        return network.group_detail(group_id, actor=user.id)

    # ---- 群组档案 ----
    @app.put("/api/groups/{group_id}/brief")
    def set_brief(group_id: str, body: BriefIn, user: Me) -> dict:
        network.set_brief(group_id, body.text, actor=user.id)
        return network.group_detail(group_id, actor=user.id)

    @app.post("/api/groups/{group_id}/brief/confirm")
    def confirm_brief(group_id: str, user: Me) -> dict:
        network.confirm_brief(group_id, actor=user.id)
        return network.group_detail(group_id, actor=user.id)

    @app.post("/api/groups/{group_id}/suggestions")
    def suggest(group_id: str, body: SuggestIn, user: Me) -> dict:
        network.suggest_brief(group_id, body.text, body.reason, actor=user.id)
        return network.group_detail(group_id, actor=user.id)

    @app.post("/api/groups/{group_id}/suggestions/{suggestion_id}")
    def decide_suggestion(group_id: str, suggestion_id: str, body: RespondIn, user: Me) -> dict:
        network.decide_suggestion(group_id, suggestion_id, body.accept, actor=user.id)
        return network.group_detail(group_id, actor=user.id)

    @app.post("/api/groups/{group_id}/decisions")
    def record_decision(group_id: str, body: DecisionRecordIn, user: Me) -> dict:
        network.record_decision(group_id, body.text, body.reason, body.supersedes, actor=user.id)
        return network.group_detail(group_id, actor=user.id)

    @app.delete("/api/groups/{group_id}/decisions/{decision_id}")
    def revoke_decision(group_id: str, decision_id: str, user: Me) -> dict:
        network.revoke_decision(group_id, decision_id, actor=user.id)
        return network.group_detail(group_id, actor=user.id)

    @app.post("/api/groups/{group_id}/projects")
    def add_project(group_id: str, body: ProjectIn, user: Me) -> dict:
        network.add_project(group_id, body.name, body.location, body.note, actor=user.id)
        return network.group_detail(group_id, actor=user.id)

    @app.delete("/api/groups/{group_id}/projects/{project_id}")
    def remove_project(group_id: str, project_id: str, user: Me) -> dict:
        network.remove_project(group_id, project_id, actor=user.id)
        return network.group_detail(group_id, actor=user.id)

    @app.post("/api/groups/{group_id}/files")
    def publish_file(group_id: str, body: FileIn, user: Me) -> dict:
        network.publish_file(group_id, body.name, body.content.encode("utf-8"), actor=user.id)
        return network.group_detail(group_id, actor=user.id)

    @app.get("/api/groups/{group_id}/files/{name}")
    def read_file(group_id: str, name: str, user: Me) -> FileResponse:
        path = network.group_file(group_id, name, actor=user.id)
        # 一律当作附件下载，不在本站点内渲染（文件可能来自其他参与方或 Agent）
        return FileResponse(path, filename=path.name, content_disposition_type="attachment",
                            headers={"X-Content-Type-Options": "nosniff"})

    @app.delete("/api/groups/{group_id}/files/{name}")
    def delete_file(group_id: str, name: str, user: Me) -> dict:
        network.delete_file(group_id, name, actor=user.id)
        return network.group_detail(group_id, actor=user.id)

    # ---- 对话 ----
    @app.post("/api/conversations")
    def create_conversation(body: ConversationIn, user: Me) -> dict:
        return network.create_conversation(body.group_id, body.agent_id, body.title, actor=user.id).to_dict(full=False)

    @app.get("/api/conversations/{conv_id}")
    def conversation(conv_id: str, user: Me) -> dict:
        return network.conversation_detail(conv_id, actor=user.id)

    @app.delete("/api/conversations/{conv_id}")
    def delete_conversation(conv_id: str, user: Me) -> dict:
        network.delete_conversation(conv_id, actor=user.id)  # 只能删除已归档的对话
        return {"ok": True}

    @app.post("/api/conversations/{conv_id}/clear")
    def clear_conversation(conv_id: str, user: Me) -> dict:
        network.clear_conversation(conv_id, actor=user.id)
        return {"ok": True}

    @app.post("/api/conversations/{conv_id}/messages")
    def send(conv_id: str, body: MessageIn, user: Me) -> dict:
        m = network.send_message(conv_id, body.text, body.mentions, actor=user.id)
        return {"id": m.id, "role": m.role, "text": m.text, "mentions": m.mentions, "ts": m.ts}

    @app.post("/api/conversations/{conv_id}/resume")
    def resume(conv_id: str, body: ResumeIn, user: Me) -> dict:
        return {"resumed": network.resume_conversation(conv_id, body.agent_id, body.note, actor=user.id)}

    # ---- 用户任务（看板） ----
    @app.get("/api/requests/{request_id}")
    def request_detail(request_id: str, user: Me) -> dict:
        return network.request_detail(request_id, actor=user.id)

    @app.post("/api/requests/{request_id}/review")
    def review_request(request_id: str, body: ReviewIn, user: Me) -> dict:
        network.review_request(request_id, body.accept, body.feedback, actor=user.id)
        return network.request_detail(request_id, actor=user.id)

    @app.post("/api/requests/{request_id}/cancel")
    def cancel_request(request_id: str, user: Me) -> dict:
        network.cancel_request(request_id, actor=user.id)
        return network.request_detail(request_id, actor=user.id)

    # ---- 委托与知识 ----
    @app.get("/api/tasks/{task_id}")
    def task(task_id: str, user: Me) -> dict:
        return network.task_detail(task_id, actor=user.id)

    @app.post("/api/tasks/{task_id}/retry")
    def retry(task_id: str, body: NoteIn, user: Me) -> dict:
        return network.retry_task(task_id, body.note, actor=user.id).to_dict()

    @app.get("/api/knowledge/{key}")
    def knowledge(key: str, user: Me) -> dict:
        return network.knowledge_item(key, actor=user.id).to_dict(content=True)

    # ---- 人审核：Agent 提议、工作区外溢申请 ----
    @app.post("/api/proposals/{proposal_id}/approve")
    def approve(proposal_id: str, body: ProposalApprove, user: Me) -> dict:
        a = network.approve_proposal(proposal_id, **body.model_dump(), actor=user.id)
        return network.agent_view(a, user.id)

    @app.post("/api/proposals/{proposal_id}/reject")
    def reject(proposal_id: str, body: NoteIn, user: Me) -> dict:
        return network.reject_proposal(proposal_id, body.note, actor=user.id).to_dict()

    @app.post("/api/approvals/{approval_id}")
    def decide(approval_id: str, body: DecisionIn, user: Me) -> dict:
        return network.decide_approval(approval_id, body.allow, actor=user.id, note=body.note).to_dict()

    # ---- 事件流（按用户过滤） ----
    @app.get("/api/events")
    async def events(request: Request, user: Me, since: int = 0) -> StreamingResponse:
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
                    data = network.event_view(ev.type, ev.data, user.id)  # 过滤并裁剪成这个用户能看的部分
                    if data is not None:
                        out = {**ev.to_dict(), "data": data}
                        yield f"id: {ev.seq}\ndata: {json.dumps(out, ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- 端侧连接器 ----
    @app.get("/api/devices")
    def devices(user: Me) -> list[dict]:
        # 在线以连接本身为准：没有绑 Agent 的设备连上来也应该显示在线
        return [{**d.public(), "online": d.id in network._runtimes}
                for d in store.devices.values() if d.user_id == user.id]

    @app.post("/api/devices")
    def create_device(body: DeviceIn, user: Me) -> dict:
        """签发一个连接器令牌。明文只在这里返回一次，之后只能看到设备列表。"""
        dev, token = auth.issue_device_token(store, user.id, body.name)
        return {**dev.public(), "token": token}

    @app.delete("/api/devices/{device_id}")
    def delete_device(device_id: str, user: Me) -> dict:
        auth.revoke_device(store, device_id, user.id)
        network.unregister_runtime(device_id)  # 在线的连接随之失效
        return {"ok": True}

    @app.websocket("/api/devices/connect")
    async def device_connect(ws: WebSocket, token: str = "") -> None:
        """连接器出站连上来。令牌走 query（浏览器的 WebSocket 带不了自定义头）；
        连接只代表这台设备，身份仍由网络按用户与执行上下文认定。"""
        dev = auth.device_of(store, token)
        if dev is None:
            await ws.close(code=1008)
            return
        await ws.accept()
        link = WebSocketLink(ws, asyncio.get_running_loop())  # 运行线程经这个循环发消息
        try:
            await ws.send_json({"type": HELLO, "protocol": PROTOCOL, "device_id": dev.id})
            info = capabilities_from_hello(await ws.receive_json())
        except Exception:  # noqa: BLE001 —— 握手内容不合法 / 断开
            await ws.close(code=1008)
            return
        rt = DeviceRuntime(link, info)
        link.on_message = rt.on_message
        network.register_runtime(dev.id, rt)
        with store.lock:
            dev.last_seen_at = time.time()
            store.save()
        network.bus.emit("device.connected", device_id=dev.id, runtime=info.runtime)
        pending: set[asyncio.Task] = set()

        async def answer(msg: dict) -> None:
            """工具调用 / 审批放到线程里处理：审批要等人，publish_file 还要经这条连接回读文件，
            在接收循环里同步处理会卡住整条连接甚至死锁。"""
            reply = await asyncio.to_thread(rt.on_message, msg)
            if reply is not None:
                try:
                    await ws.send_json(reply)
                except (WebSocketDisconnect, RuntimeError):
                    pass

        try:
            while True:
                msg = await ws.receive_json()
                if msg.get("type") in BLOCKING:
                    task = asyncio.create_task(answer(msg))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                    continue
                reply = rt.on_message(msg)
                if reply is not None:
                    await ws.send_json(reply)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            network.unregister_runtime(dev.id)
            network.bus.emit("device.disconnected", device_id=dev.id)

    # ---- 运行时回调（凭执行上下文令牌，不走登录） ----
    @app.post("/internal/tools")
    async def tools(body: ToolCallIn) -> dict:
        # 工具调用会碰持久化与锁，放到线程里跑，不阻塞事件循环
        text, is_error = await asyncio.to_thread(network.handle_tool, body.token, body.tool, body.args)
        return {"text": text, "is_error": is_error}

    @app.post("/internal/approval")
    async def approval(body: ApprovalIn) -> dict:
        # 阻塞到 Agent 所有者决定或超时；dsh 那头的应答插件一直等着
        outcome = await asyncio.to_thread(network.request_approval, body.token, body.tool, body.reason)
        return {"outcome": outcome}

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


def build_network(runtime_name: str, data: str, port: int, dsh_bin: str = "dsh", seed: bool = False) -> Network:
    """seed：全新数据目录是否预置种子 Agent。服务默认不预置——用户登录后自己创建；CLI 演练时预置。"""
    runtime = DshRuntime(dsh_bin) if runtime_name == "dsh" else ScriptedRuntime(scenario_handler)
    return Network(runtime, Path(data).resolve(), tool_url=f"http://127.0.0.1:{port}/internal/tools", seed=seed)


def main() -> None:
    p = argparse.ArgumentParser(description="Matrix：Agent 协作网络")
    p.add_argument("--runtime", choices=["dsh", "scripted"], default="dsh")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--data", default=os.environ.get("AGENTNET_DATA", ".agentnet"))
    p.add_argument("--dsh-bin", default="dsh")
    # 只在容器里改：容器内监听 0.0.0.0 才能被同一 compose 网络里的反向代理访问；端口不对宿主机发布
    p.add_argument("--host", default=os.environ.get("AGENTNET_HOST", "127.0.0.1"))
    args = p.parse_args()

    network = build_network(args.runtime, args.data, args.port, args.dsh_bin)
    print(f"Matrix → http://127.0.0.1:{args.port}  (runtime={network.runtime.name}, data={args.data})")
    config = uvicorn.Config(create_app(network), host=args.host, port=args.port, log_level="warning",
                            timeout_graceful_shutdown=3)
    AgentNetServer(config, network).run()


if __name__ == "__main__":
    main()
