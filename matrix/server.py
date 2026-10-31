"""FastAPI 服务：REST + SSE 事件流 + 静态页面 + 运行时回调端点。

/api/* 需要登录（邮箱 + 密码，HttpOnly cookie）；每个用户只看得到自己的数据与对自己开放的 Agent。
这仍是最简实现（无 HTTPS、无 CSRF 令牌、无登录限流），只监听 127.0.0.1，不要暴露到公网。
/internal/* 由 MCP 服务与 dsh 审批插件调用，凭运行时调用期间才有效的执行上下文令牌识别调用方。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
import uvicorn
from pydantic import BaseModel, Field, model_validator

from . import auth, compat
from .device import BLOCKING, HELLO, PROTOCOL, DeviceRuntime, WebSocketLink, capabilities_from_hello
from .engine import HookRateLimited, Network, NetworkError
from .connector_catalog import ConnectorCatalog
from .web_fetch import FetchConfig
from .model_catalog import CatalogConflict, ModelCatalog
from .plugins import PluginCatalog
from .models import ATTACHMENT_LIMIT, ATTACHMENTS_PER_MESSAGE, DESCRIPTION_LIMIT, HARNESSES, User
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


class PluginInstallIn(BaseModel):
    name: str = Field(max_length=64)
    confirm: bool = False


class TriggerIn(BaseModel):
    enabled: bool


class FrameIn(BaseModel):
    tab: str = Field(max_length=40)


class BridgeIn(BaseModel):
    op: str = Field(max_length=40)
    args: dict[str, Any] = Field(default_factory=dict)


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
    model_preset: str | None = Field(None, max_length=64)  # 预置模型 id（启用模型目录时云侧只能用它）
    device_runtime: str | None = None   # 绑定的设备 id；None = 平台云侧（默认运行时）
    harness: str | None = Field(None, max_length=40)  # 设备上的哪个 harness；None = 设备默认
    timeout_s: int | None = Field(None, ge=1)        # 每轮运行超时（秒）；None = 不限制
    max_delegations: int | None = Field(None, ge=1)  # 每次被 @ / 每个任务最多委托几次；None = 不限制


class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    body: str = Field("", max_length=64_000)


class SkillFileIn(BaseModel):
    path: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=200_000)


# 手工写的 MCP 配置：JSON 文本或对象（mcpServers 的写法，见 mcp_client.parse_server_config）
McpConfig = str | dict[str, Any]


class CustomConnectorIn(BaseModel):
    name: str = Field("", max_length=60)                  # 不填就用配置里的服务名
    config: McpConfig | None = Field(None)                # 接入方式：远程 MCP，地址只接受 https 公网地址
    url: str = Field("", max_length=2000)                 # 旧写法：单个地址 + 一个请求头
    header: str = Field("", max_length=64)
    value: str = Field("", max_length=4000)               # 只进加密保管，不回显
    agent_ids: list[str] | None = None

    def mcp(self) -> McpConfig:
        if self.config is not None:
            return self.config
        value = self.value.strip()
        return {"url": self.url, "headers": {self.header.strip() or "Authorization": value} if value else {}}


class CustomEditIn(BaseModel):
    name: str = Field("", max_length=60)
    config: McpConfig


class AuthorizeIn(BaseModel):
    connector_id: str = Field(min_length=1, max_length=40)
    values: dict[str, str] = Field(default_factory=dict)  # 授权字段；secret 的只进加密保管，任何接口都不回显
    agent_ids: list[str] | None = None                    # 新授权时勾给哪些 Agent；省略 = 只勾自己的助理


class NeedIn(BaseModel):
    connector_id: str = Field("", max_length=80)          # 目录里的连接器或电脑上的服务名
    purpose: str = Field("", max_length=300)
    name: str = Field("", max_length=60)                  # 自定义连接器：名字与 MCP 配置（请求头的值只能是 ${变量} 模板）
    config: McpConfig | None = None


class NeedEditIn(BaseModel):
    name: str = Field("", max_length=60)
    config: McpConfig
    purpose: str | None = Field(None, max_length=300)


class NeedConnectIn(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)  # 模板里的变量；只进加密保管，不回显
    agent_ids: list[str] | None = None


class GrantIn(BaseModel):
    agent_id: str
    connection_id: str
    grant: Literal["ask", "always", "deny"]


class RefConnectorIn(BaseModel):
    connector_id: str = Field("", max_length=80)


class ConnectorCallIn(BaseModel):
    token: str
    tool: str = ""
    args: dict[str, Any] = Field(default_factory=dict)


class AgentPatch(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=80)
    description: str | None = Field(None, min_length=2, max_length=DESCRIPTION_LIMIT)
    capabilities: list[str] | None = None
    authority: list[str] | None = None
    chat_enabled: bool | None = None
    model: ModelIn | None = None  # 显式传 null 表示改回默认模型（model / model_preset 都没给时不改）
    model_preset: str | None = Field(None, max_length=64)
    device_runtime: str | None = None   # 显式传 null 表示改回平台云侧
    harness: str | None = Field(None, max_length=40)
    timeout_s: int | None = Field(None, ge=1)        # 显式传 null 表示改回不限制
    max_delegations: int | None = Field(None, ge=1)
    skills: list[str] | None = Field(None, max_length=50)       # 勾上的“我的技能”
    connections: list[str] | None = Field(None, max_length=50)  # 勾上的“我的连接器”
    subscriptions: list[str] | None = Field(None, max_length=30)  # 订阅的发布物（不变量 23）


class ReleaseIn(BaseModel):
    """主持人发布（docs/project-publishing.md）：技能包带技能，交付件带档案文件（可带一个插件视图）。读者按邮箱。"""
    name: str = Field(..., min_length=1, max_length=64)
    kind: str = Field(..., pattern="^(method|fact)$")
    title: str = Field(..., min_length=1, max_length=80)
    description: str = Field("", max_length=2000)
    skills: list[str] | None = Field(None, max_length=30)
    files: list[str] | None = Field(None, max_length=30)
    view: dict[str, str] | None = None
    readers: list[str] | None = Field(None, max_length=200)
    public: bool = False
    follow: bool = False


class ReleasePatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=80)
    description: str | None = Field(None, max_length=2000)
    skills: list[str] | None = Field(None, max_length=30)
    files: list[str] | None = Field(None, max_length=30)
    view: dict[str, str] | None = None
    readers: list[str] | None = Field(None, max_length=200)
    public: bool | None = None
    follow: bool | None = None


class SubscribeIn(BaseModel):
    agent_id: str
    on: bool = True


class FeedbackIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class ProposalApprove(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=80)
    description: str | None = Field(None, min_length=2, max_length=DESCRIPTION_LIMIT)
    capabilities: list[str] | None = Field(None, max_length=10)
    authority: list[str] | None = None
    model: ModelIn | None = None
    model_preset: str | None = Field(None, max_length=64)
    note: str = Field("", max_length=500)


class MemberIn(BaseModel):
    agent_id: str


class DeviceIn(BaseModel):
    name: str = Field("", max_length=80)


class PairIn(BaseModel):
    """连接器的配对申请（不需要登录）。字段只用于在批准页展示，超长截断。"""
    name: str = ""
    hostname: str = ""
    harnesses: list[str] = Field(default_factory=list, max_length=16)


class PairPollIn(BaseModel):
    device_code: str = Field("", max_length=200)


class NoteIn(BaseModel):
    note: str = Field("", max_length=2000)


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field("", max_length=500)
    member_ids: list[str] = Field(default_factory=list)


class ReviewIn(BaseModel):
    accept: bool
    feedback: str = Field("", max_length=2000)


class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)


class RespondIn(BaseModel):
    accept: bool


class BriefIn(BaseModel):
    text: str = Field("", max_length=4000)


class SuggestIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    reason: str = Field("", max_length=1000)


class DecisionRecordIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    reason: str = Field("", max_length=2000)
    supersedes: str | None = None


class RefIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    location: str = Field(min_length=1, max_length=500)
    note: str = Field("", max_length=500)


class AssignIn(BaseModel):
    project_id: str | None = None        # None = 移出项目


class FileIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(max_length=200_000)


class ProjectPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    description: str | None = Field(None, max_length=500)
    member_ids: list[str] | None = None


class SessionIn(BaseModel):
    title: str = Field("", max_length=120)


class SessionPatch(BaseModel):
    title: str | None = Field(None, max_length=120)
    archived: bool | None = None


class AssistantIn(BaseModel):
    agent_id: str


class HeldIn(BaseModel):
    action: Literal["send", "grant", "rewrite"]


class DraftSendIn(BaseModel):
    text: str | None = Field(None, max_length=4000)  # 主人在确认卡上改过的文本；不写就照草稿发


class SchedulePatch(BaseModel):
    enabled: bool


class ConversationIn(BaseModel):
    """打开对话：project_id → 这个项目的群聊；agent_id → 和它的私聊（第一次打开时创建）。"""
    project_id: str | None = None
    agent_id: str | None = None
    title: str = Field("", max_length=120)


class AttachmentIn(BaseModel):
    """上传附件时拿到的说明；网络只认 sha 对应的已上传内容，大小以文件为准。"""
    sha: str = Field(min_length=64, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    mime: str = Field("", max_length=100)


class MessageIn(BaseModel):
    text: str = Field("", max_length=8000)  # 只有附件时可以为空
    mentions: list[str] = Field(default_factory=list, max_length=20)  # 显式 @ 的成员 id；文本里的 @名称 也会识别
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=ATTACHMENTS_PER_MESSAGE)

    @model_validator(mode="after")
    def not_empty(self) -> "MessageIn":
        if not self.text.strip() and not self.attachments:
            raise ValueError("消息不能为空")
        return self


class ResumeIn(BaseModel):
    agent_id: str | None = None  # 只续接这个席位；省略则续接所有中断的席位
    note: str = Field("", max_length=2000)


class PresetIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    label: str = Field("", max_length=80)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: str | None = Field(None, max_length=40)
    description: str = Field("", max_length=300)
    context_window: int | None = Field(None, gt=0)
    max_tokens: int | None = Field(None, gt=0)
    image: bool = False


class ProviderIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field("", max_length=80)
    api: str = Field(min_length=1, max_length=40)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str | None = Field(None, max_length=4000)  # 只写：空 = 沿用已存的


class CatalogIn(BaseModel):
    """管理后台整份保存的模型目录；rev 是读到时的版本，文件期间被改过就拒绝。"""
    default: str | None = Field(None, max_length=64)
    models: list[PresetIn] = Field(default_factory=list, max_length=100)
    providers: list[ProviderIn] = Field(default_factory=list, max_length=50)
    rev: str = Field("", max_length=64)


class CredentialsIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class DecisionIn(BaseModel):
    allow: bool
    note: str = Field("", max_length=500)
    scope: Literal["once", "project"] = "once"   # 连接器的申请：只这一轮 / 这个项目里以后都可以


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


async def skill_package(request: Request) -> list[tuple[str, bytes]]:
    """技能包：请求体是 zip（application/zip），或上传文件夹时的 JSON `{files: [{path, data(base64)}]}`。"""
    import base64
    from .models import SKILL_LIMIT
    cap = SKILL_LIMIT * 2  # base64 膨胀
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > cap:
            raise HTTPException(413, f"上传的技能超过 {SKILL_LIMIT // 1_000_000} MB")
    from .skills import SkillError, unzip
    try:
        if "json" in request.headers.get("content-type", ""):
            files = json.loads(bytes(buf)).get("files") or []
            return [(str(f["path"]), base64.b64decode(f["data"], validate=True)) for f in files[:500]]
        return unzip(bytes(buf))
    except (SkillError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise HTTPException(400, str(e) or "上传的技能格式不对") from e


async def plugin_package(request: Request) -> list[tuple[str, bytes]]:
    """插件包：请求体是 zip，或上传文件夹时的 JSON `{files: [{path, data(base64)}]}`。"""
    import base64
    from .models import PLUGIN_FILES, PLUGIN_LIMIT
    from .plugins import PluginError, unzip
    cap = PLUGIN_LIMIT * 2  # base64 膨胀
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > cap:
            raise HTTPException(413, f"插件包超过 {PLUGIN_LIMIT // 1_000_000} MB")
    try:
        if "json" in request.headers.get("content-type", ""):
            files = json.loads(bytes(buf)).get("files") or []
            return [(str(f["path"]), base64.b64decode(f["data"], validate=True)) for f in files[:PLUGIN_FILES + 1]]
        return unzip(bytes(buf))
    except (PluginError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise HTTPException(400, str(e) or "插件包格式不对") from e


# 插件页面的响应头（不变量 20）：直接打开也在沙箱里（不透明来源、没有登录 cookie），不能联网、不能被别的站点嵌入，
# 不带 Referer（地址里有帧令牌）。
PLUGIN_FRAME_HEADERS = {
    "Content-Security-Policy": ("sandbox allow-scripts allow-forms; default-src 'none'; script-src 'self' 'unsafe-inline'; "
                                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
                                "connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'; "
                                "frame-ancestors 'self'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def create_app(network: Network) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        # 事件流阻塞在线程里等事件，uvicorn 取消不了线程：不主动唤醒，进程会卡在关闭阶段，
        # 浏览器那头的连接也一直不断，EventSource 就不会触发重连。
        network.bus.close()
        network.services.stop_all()  # 插件服务进程随服务一起退出（不变量 24）

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

    @app.get("/admin")
    def admin_page() -> FileResponse:
        """管理后台页面：页面本身是静态的，数据经 /api/admin/*，只给管理员。"""
        return FileResponse(STATIC / "admin.html")

    @app.get("/api/info")
    def info() -> dict:
        return {"runtime": network.runtime.name, "limits": network.limits.__dict__,
                "default_model": getattr(network.runtime, "default_model", lambda: None)(),
                "has_users": bool(store.users), "has_unclaimed": not store.users and bool(store.agents),
                "harnesses": HARNESSES, "attachment_limit": ATTACHMENT_LIMIT}

    # ---- 用户与登录 ----
    @app.post("/api/auth/register")
    def register(body: CredentialsIn, response: Response) -> dict:
        user, token, first = auth.register(store, body.email, body.password)
        set_cookie(response, token)
        claimed = first and bool(store.agents)
        network.ensure_assistant(user.id)  # 注册即有助理（认领了旧数据、已有 Agent 的不补建）
        return {**store.users[user.id].public(), "claimed_existing": claimed}

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
        network.ensure_assistant(user.id)  # 有了这个功能之前注册、还没有 Agent 的人：补一个助理
        own = [a for a in store.agents.values() if a.owner_id == user.id]
        home = network.home_of(user.id)
        return {**store.users[user.id].public(), "agent_count": len(own), "home_id": home.id if home else None,
                "admin": network.is_admin(user.id)}

    # ---- 管理后台（只看用量，不看内容；不是管理员按不存在处理） ----
    @app.get("/api/admin/usage")
    def admin_usage(user: Me, days: int = Query(30, ge=1, le=90)) -> dict:
        return network.admin_usage(user.id, days)

    @app.get("/api/admin/models")
    def admin_models(user: Me) -> dict:
        return network.admin_models(user.id)

    @app.put("/api/admin/models")
    def admin_save_models(body: CatalogIn, user: Me) -> dict:
        data = {"default": body.default or None, "models": [m.model_dump() for m in body.models],
                "providers": [p.model_dump(exclude={"api_key"}) for p in body.providers]}
        keys = {p.id.strip(): p.api_key.strip() for p in body.providers if p.api_key and p.api_key.strip()}
        try:
            return network.admin_save_models(user.id, data, body.rev, keys)
        except CatalogConflict as e:
            raise HTTPException(409, str(e)) from e

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

    @app.get("/api/models")
    def models(user: Me) -> dict:
        """云侧预置模型目录（部署方维护 models.json，改了就生效）；active=False 时云侧照旧自由填写。"""
        return {**network.models.public(),
                "runtime_default": getattr(network.runtime, "default_model", lambda: None)()}

    @app.delete("/api/agents/{agent_id}")
    def delete_agent(agent_id: str, user: Me) -> dict:
        network.delete_agent(agent_id, actor=user.id)
        return {"ok": True}

    @app.get("/api/agents/{agent_id}/ledger")
    def agent_ledger(agent_id: str, user: Me) -> list[dict]:
        network.visible_agent(agent_id, user.id)  # 只有所有者
        return network.ledger(agent_id)

    # ---- 项目 ----
    @app.post("/api/projects")
    def create_project(body: ProjectIn, user: Me) -> dict:
        return network.create_project(**body.model_dump(), actor=user.id).to_dict()

    @app.patch("/api/projects/{project_id}")
    def update_project(project_id: str, body: ProjectPatch, user: Me) -> dict:
        return network.update_project(project_id, actor=user.id, **body.model_dump(exclude_unset=True)).to_dict()

    @app.post("/api/projects/{project_id}/members")
    def add_member(project_id: str, body: MemberIn, user: Me) -> dict:
        return network.add_member(project_id, body.agent_id, actor=user.id).to_dict()

    @app.delete("/api/projects/{project_id}/members/{agent_id}")
    def remove_member(project_id: str, agent_id: str, user: Me) -> dict:
        return network.remove_member(project_id, agent_id, actor=user.id).to_dict()

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str, user: Me) -> dict:
        network.delete_project(project_id, actor=user.id)
        return {"ok": True}

    @app.get("/api/projects/{project_id}")
    def project_detail(project_id: str, user: Me) -> dict:
        return network.project_detail(project_id, actor=user.id)

    # ---- 联合项目：邀请 → 接受；退出、移出；私有目录挂载 ----
    @app.post("/api/projects/{project_id}/invitations")
    def invite(project_id: str, body: InviteIn, user: Me) -> dict:
        return network.project_detail(network.invite(project_id, body.email, actor=user.id).id, actor=user.id)

    @app.post("/api/projects/{project_id}/invitation")
    def respond(project_id: str, body: RespondIn, user: Me) -> dict:
        g = network.respond_invitation(project_id, body.accept, actor=user.id)
        return network.project_detail(g.id, actor=user.id) if g else {"ok": True}

    @app.post("/api/projects/{project_id}/leave")
    def leave(project_id: str, user: Me) -> dict:
        network.leave_project(project_id, actor=user.id)
        return {"ok": True}

    @app.delete("/api/projects/{project_id}/participants/{user_id}")
    def remove_participant(project_id: str, user_id: str, user: Me) -> dict:
        network.remove_participant(project_id, user_id, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    # ---- 项目档案 ----
    @app.put("/api/projects/{project_id}/brief")
    def set_brief(project_id: str, body: BriefIn, user: Me) -> dict:
        network.set_brief(project_id, body.text, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.post("/api/projects/{project_id}/brief/confirm")
    def confirm_brief(project_id: str, user: Me) -> dict:
        network.confirm_brief(project_id, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.post("/api/projects/{project_id}/suggestions")
    def suggest(project_id: str, body: SuggestIn, user: Me) -> dict:
        network.suggest_brief(project_id, body.text, body.reason, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.post("/api/projects/{project_id}/suggestions/{suggestion_id}")
    def decide_suggestion(project_id: str, suggestion_id: str, body: RespondIn, user: Me) -> dict:
        network.decide_suggestion(project_id, suggestion_id, body.accept, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.post("/api/projects/{project_id}/decisions")
    def record_decision(project_id: str, body: DecisionRecordIn, user: Me) -> dict:
        network.record_decision(project_id, body.text, body.reason, body.supersedes, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.delete("/api/projects/{project_id}/decisions/{decision_id}")
    def revoke_decision(project_id: str, decision_id: str, user: Me) -> dict:
        network.revoke_decision(project_id, decision_id, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.post("/api/projects/{project_id}/refs")
    def add_ref(project_id: str, body: RefIn, user: Me) -> dict:
        network.add_ref(project_id, body.name, body.location, body.note, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.delete("/api/projects/{project_id}/refs/{ref_id}")
    def remove_ref(project_id: str, ref_id: str, user: Me) -> dict:
        network.remove_ref(project_id, ref_id, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.post("/api/projects/{project_id}/files")
    def publish_file(project_id: str, body: FileIn, user: Me) -> dict:
        network.publish_file(project_id, body.name, body.content.encode("utf-8"), actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.get("/api/projects/{project_id}/files/{name}")
    def read_file(project_id: str, name: str, user: Me) -> FileResponse:
        path = network.project_file(project_id, name, actor=user.id)
        # 一律当作附件下载，不在本站点内渲染（文件可能来自其他参与方或 Agent）
        return FileResponse(path, filename=path.name, content_disposition_type="attachment",
                            headers={"X-Content-Type-Options": "nosniff"})

    @app.delete("/api/projects/{project_id}/files/{name}")
    def delete_file(project_id: str, name: str, user: Me) -> dict:
        network.delete_file(project_id, name, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    # ---- 项目技能（项目档案的一部分）与项目的连接器面板（项目不持有凭证） ----
    @app.get("/api/projects/{project_id}/skills/{name}")
    def project_skill(project_id: str, name: str, user: Me) -> dict:
        return network.project_skill(project_id, name, actor=user.id)

    @app.put("/api/projects/{project_id}/skills")
    def save_project_skill(project_id: str, body: SkillIn, user: Me) -> dict:
        network.save_project_skill(project_id, body.name, body.description, body.body, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.post("/api/projects/{project_id}/skills/upload")
    async def upload_project_skill(project_id: str, request: Request, user: Me) -> dict:
        await run_in_threadpool(network.hosted_project, project_id, user.id)  # 先查权限，不收无权者的内容
        entries = await skill_package(request)
        return await run_in_threadpool(network.upload_project_skill, project_id, entries, user.id)

    @app.post("/api/projects/{project_id}/skills/{name}/files")
    def add_project_skill_file(project_id: str, name: str, body: SkillFileIn, user: Me) -> dict:
        return network.add_project_skill_file(project_id, name, body.path, body.content.encode(), actor=user.id)

    @app.delete("/api/projects/{project_id}/skills/{name}")
    def delete_project_skill(project_id: str, name: str, user: Me) -> dict:
        network.delete_project_skill(project_id, name, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.post("/api/projects/{project_id}/needs")
    def add_need(project_id: str, body: NeedIn, user: Me) -> dict:
        network.add_connector_need(project_id, body.connector_id, body.purpose, actor=user.id,
                                   config=body.config, name=body.name)
        return network.project_detail(project_id, actor=user.id)

    @app.put("/api/projects/{project_id}/needs/{need_id}")
    def edit_need(project_id: str, need_id: str, body: NeedEditIn, user: Me) -> dict:
        network.edit_connector_need(project_id, need_id, body.config, body.purpose, body.name, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.post("/api/projects/{project_id}/needs/{need_id}/connect")
    async def connect_need(project_id: str, need_id: str, body: NeedConnectIn, user: Me) -> dict:
        # 会试连外部服务：放到线程里
        return await asyncio.to_thread(network.connect_custom_need, project_id, need_id, body.values, user.id,
                                       body.agent_ids)

    @app.delete("/api/projects/{project_id}/needs/{need_id}")
    def remove_need(project_id: str, need_id: str, user: Me) -> dict:
        network.remove_connector_need(project_id, need_id, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.put("/api/projects/{project_id}/grants")
    def connector_grant(project_id: str, body: GrantIn, user: Me) -> dict:
        """共享项目里我的 Agent 怎么用我的某个连接器：用到时问（默认）/ 这个项目里都可以 / 不行。"""
        network.set_connector_grant(project_id, body.agent_id, body.connection_id, body.grant, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.put("/api/projects/{project_id}/refs/{ref_id}/tab")
    def ref_tab(project_id: str, ref_id: str, body: TriggerIn, user: Me) -> dict:
        """主持人把资料里的一条链接设成 / 取消项目页上的外部页面标签（`{enabled}`）。"""
        network.set_ref_tab(project_id, ref_id, body.enabled, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.put("/api/projects/{project_id}/refs/{ref_id}/connector")
    def ref_connector(project_id: str, ref_id: str, body: RefConnectorIn, user: Me) -> dict:
        network.set_ref_connector(project_id, ref_id, body.connector_id, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.delete("/api/projects/{project_id}/connector-usage/{connector_id}")
    def clear_usage(project_id: str, connector_id: str, user: Me) -> dict:
        network.clear_connector_usage(project_id, connector_id, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    # ---- 我的技能、我的连接器（只有自己看得到） ----
    @app.get("/api/me/skills")
    def my_skills(user: Me) -> list[dict]:
        return network.my_skills(user.id)

    @app.get("/api/me/skills/{name}")
    def my_skill(name: str, user: Me) -> dict:
        return network.my_skill(name, user.id)

    @app.put("/api/me/skills")
    def save_my_skill(body: SkillIn, user: Me) -> dict:
        return network.save_my_skill(body.name, body.description, body.body, user.id)

    @app.post("/api/me/skills/upload")
    async def upload_my_skill(request: Request, user: Me) -> dict:
        entries = await skill_package(request)
        return await run_in_threadpool(network.upload_my_skill, entries, user.id)

    @app.post("/api/me/skills/{name}/files")
    def add_my_skill_file(name: str, body: SkillFileIn, user: Me) -> dict:
        return network.add_my_skill_file(name, body.path, body.content.encode(), user.id)

    @app.delete("/api/me/skills/{name}")
    def delete_my_skill(name: str, user: Me) -> dict:
        network.delete_my_skill(name, user.id)
        return {"ok": True}

    @app.get("/api/connectors")
    def connector_catalog(user: Me) -> list[dict]:
        """部署方提供的云端连接器（名字、描述、要填什么）。地址与接入方式不对外。"""
        return network.connector_catalog()

    @app.get("/api/me/connections")
    def my_connections(user: Me) -> list[dict]:
        return network.my_connections(user.id)

    @app.post("/api/me/connections")
    async def authorize(body: AuthorizeIn, user: Me) -> dict:
        # 会试连外部服务：放到线程里
        return await asyncio.to_thread(network.authorize_connector, body.connector_id, body.values, user.id,
                                       body.agent_ids)

    @app.post("/api/me/connections/custom")
    async def add_custom(body: CustomConnectorIn, user: Me) -> dict:
        return await asyncio.to_thread(network.add_custom_connector, body.name, body.mcp(), user.id, body.agent_ids)

    @app.put("/api/me/connections/{connection_id}/custom")
    async def edit_custom(connection_id: str, body: CustomEditIn, user: Me) -> dict:
        return await asyncio.to_thread(network.edit_custom_connector, connection_id, body.name, body.config, user.id)

    @app.post("/api/me/connections/{connection_id}/check")
    async def recheck(connection_id: str, user: Me) -> dict:
        return await asyncio.to_thread(network.recheck_connection, connection_id, user.id)

    @app.delete("/api/me/connections/{connection_id}")
    def revoke(connection_id: str, user: Me) -> dict:
        network.revoke_connection(connection_id, user.id)
        return {"ok": True}

    @app.get("/api/me/connections/{connection_id}/calls")
    def connection_calls(connection_id: str, user: Me) -> list[dict]:
        return network.connection_calls(connection_id, user.id)

    # ---- 对话 ----
    # ---- 项目会话：人和自己的助理在项目里的私下对话，只有这个人看得到（不变量 18） ----
    # ---- 项目插件（docs/project-plugins.md） ----
    @app.get("/api/plugins")
    def plugin_catalog(user: Me) -> list[dict]:
        return network.plugin_catalog()

    @app.post("/api/projects/{project_id}/plugins")
    def install_plugin(project_id: str, body: PluginInstallIn, user: Me) -> dict:
        return network.install_plugin(project_id, body.name, body.confirm, actor=user.id)

    @app.post("/api/projects/{project_id}/plugins/upload")
    async def upload_plugin(project_id: str, request: Request, user: Me, confirm: bool = False) -> dict:
        await run_in_threadpool(network.hosted_project, project_id, user.id)  # 先查权限，不收无权者的内容
        entries = await plugin_package(request)
        return await run_in_threadpool(network.upload_plugin, project_id, entries, confirm, user.id)

    @app.delete("/api/projects/{project_id}/plugins/{name}")
    def uninstall_plugin(project_id: str, name: str, user: Me) -> dict:
        network.uninstall_plugin(project_id, name, actor=user.id)
        return network.project_detail(project_id, actor=user.id)

    @app.put("/api/projects/{project_id}/plugins/{name}")
    def set_plugin_enabled(project_id: str, name: str, body: TriggerIn, user: Me) -> dict:
        """启用 / 停用整个插件（只有主持人）；停用保留包、记录与触发设置。"""
        return network.set_plugin_enabled(project_id, name, body.enabled, actor=user.id)

    @app.put("/api/projects/{project_id}/plugins/{name}/triggers/{key}")
    def set_plugin_trigger(project_id: str, name: str, key: str, body: TriggerIn, user: Me) -> dict:
        return network.set_plugin_trigger(project_id, name, key, body.enabled, actor=user.id)

    @app.post("/api/projects/{project_id}/plugins/{name}/triggers/{key}/hook")
    def reset_plugin_hook(project_id: str, name: str, key: str, user: Me) -> dict:
        """明文令牌只在这里出现一次；网络只存哈希。"""
        return {"path": f"/hooks/{network.reset_plugin_hook(project_id, name, key, actor=user.id)}"}

    @app.get("/api/projects/{project_id}/plugins/{name}/records/{table}")
    def plugin_records(project_id: str, name: str, table: str, user: Me, q: str = "", limit: int = 200) -> list[dict]:
        return network.plugin_records(project_id, name, table, q, None, limit, actor=user.id)

    @app.post("/api/projects/{project_id}/plugins/{name}/frame")
    def plugin_frame(project_id: str, name: str, body: FrameIn, user: Me) -> dict:
        return {"src": f"/plugin-frame/{network.plugin_frame(project_id, name, body.tab, actor=user.id)}"}

    @app.post("/api/projects/{project_id}/plugins/{name}/bridge")
    def plugin_bridge(project_id: str, name: str, body: BridgeIn, user: Me) -> dict:
        """插件页面的唯一出口：父页面把 iframe 的 postMessage 原样转到这里，网络按插件申请的能力与看的人的可见性执行。"""
        return {"result": network.plugin_bridge(project_id, name, body.op, body.args, actor=user.id)}

    @app.post("/api/plugin-drafts/{draft_id}/send")
    def send_plugin_draft(draft_id: str, user: Me) -> dict:
        """确认卡上的“发送”：只有父页面能调（插件页面拿不到登录凭证，桥里也没有这个操作）。"""
        msg = network.send_plugin_draft(draft_id, actor=user.id)
        return {"id": msg.id}

    @app.get("/plugin-frame/{token}/{path:path}")
    def plugin_frame_file(token: str, path: str) -> FileResponse:
        try:
            if path == "_matrix/sdk.js":  # 给插件页面用的桥客户端，和插件文件同源（同一个令牌）
                network.plugin_frame_check(token)
                return FileResponse(STATIC / "plugin-sdk.js", headers=PLUGIN_FRAME_HEADERS,
                                    media_type="text/javascript")
            return FileResponse(network.plugin_frame_file(token, path), headers=PLUGIN_FRAME_HEADERS)
        except NetworkError:
            raise HTTPException(404, "not found") from None

    # ---- 发布与订阅（docs/project-publishing.md，不变量 23） ----
    @app.get("/api/releases")
    def releases(user: Me) -> list[dict]:
        return network.releases_for(user.id)

    @app.get("/api/releases/{release_id}")
    def release(release_id: str, user: Me) -> dict:
        return network.release_detail(release_id, actor=user.id)

    @app.get("/api/releases/{release_id}/files/{name}")
    def release_file(release_id: str, name: str, user: Me) -> FileResponse:
        path = network.release_file(release_id, name, actor=user.id)
        # 同项目文件：一律当作附件下载，不在本站点内渲染
        return FileResponse(path, filename=path.name, content_disposition_type="attachment",
                            headers={"X-Content-Type-Options": "nosniff"})

    @app.post("/api/releases/{release_id}/subscribe")
    def subscribe_release(release_id: str, body: SubscribeIn, user: Me) -> dict:
        network.set_subscription(release_id, body.agent_id, body.on, actor=user.id)
        return network.release_detail(release_id, actor=user.id)

    @app.post("/api/releases/{release_id}/feedback")
    def release_feedback(release_id: str, body: FeedbackIn, user: Me) -> dict:
        return network.release_feedback(release_id, body.text, actor=user.id)

    @app.post("/api/releases/{release_id}/frame")
    def release_frame(release_id: str, user: Me) -> dict:
        return {"src": f"/release-frame/{network.release_frame(release_id, actor=user.id)}"}

    @app.post("/api/releases/{release_id}/bridge")
    def release_bridge(release_id: str, body: BridgeIn, user: Me) -> dict:
        """发布物里的插件页面：只读这个版本的快照（父页面代转）。"""
        return {"result": network.release_bridge(release_id, body.op, body.args, actor=user.id)}

    @app.get("/release-frame/{token}/{path:path}")
    def release_frame_file(token: str, path: str) -> FileResponse:
        try:
            if path == "_matrix/sdk.js":
                network.release_frame_check(token)
                return FileResponse(STATIC / "plugin-sdk.js", headers=PLUGIN_FRAME_HEADERS,
                                    media_type="text/javascript")
            return FileResponse(network.release_frame_file(token, path), headers=PLUGIN_FRAME_HEADERS)
        except NetworkError:
            raise HTTPException(404, "not found") from None

    @app.post("/api/projects/{project_id}/releases")
    def create_release(project_id: str, body: ReleaseIn, user: Me) -> dict:
        r = network.create_release(project_id, actor=user.id, **body.model_dump())
        return network.release_detail(r.id, actor=user.id)

    @app.patch("/api/projects/{project_id}/releases/{release_id}")
    def update_release(project_id: str, release_id: str, body: ReleasePatch, user: Me) -> dict:
        r = network.update_release(project_id, release_id, actor=user.id, **body.model_dump(exclude_unset=True))
        return network.release_detail(r.id, actor=user.id)

    @app.post("/api/projects/{project_id}/releases/{release_id}/publish")
    def publish_release(project_id: str, release_id: str, user: Me) -> dict:
        r = network.publish_release_version(project_id, release_id, actor=user.id)
        return network.release_detail(r.id, actor=user.id)

    @app.delete("/api/projects/{project_id}/releases/{release_id}")
    def withdraw_release(project_id: str, release_id: str, user: Me) -> dict:
        r = network.withdraw_release(project_id, release_id, actor=user.id)
        return network.release_detail(r.id, actor=user.id)

    @app.post("/hooks/{token}")
    async def plugin_webhook(token: str, request: Request) -> dict:
        """外部系统的回调：不需要登录，令牌就是授权（主持人在插件页里生成、可重置）。请求体是 JSON，最多 64 KB。"""
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > 64_000:
                raise HTTPException(413, "请求体最多 64 KB")
        try:
            payload = json.loads(bytes(buf) or b"{}")
        except ValueError:
            raise HTTPException(400, "请求体要是 JSON") from None
        try:
            return await run_in_threadpool(network.plugin_webhook, token, payload)
        except HookRateLimited as e:
            raise HTTPException(429, str(e)) from None
        except NetworkError as e:
            raise HTTPException(404 if str(e).startswith("unknown ") else 409, str(e)) from None

    @app.get("/api/projects/{project_id}/sessions")
    def list_sessions(project_id: str, user: Me) -> list:
        return network.sessions(project_id, actor=user.id)

    @app.post("/api/projects/{project_id}/sessions")
    def create_session(project_id: str, body: SessionIn, user: Me) -> dict:
        return network.create_session(project_id, body.title, actor=user.id).to_dict(full=False)

    @app.patch("/api/sessions/{conv_id}")
    def update_session(conv_id: str, body: SessionPatch, user: Me) -> dict:
        return network.update_session(conv_id, body.title, body.archived, actor=user.id).to_dict(full=False)

    # ---- 助理对话：起草的消息（只有主人能发、能丢）与主人的定时 ----
    @app.post("/api/drafts/{draft_id}/send")
    def send_draft(draft_id: str, body: DraftSendIn, user: Me) -> dict:
        msg = network.send_draft(draft_id, body.text, actor=user.id)
        return {"id": msg.id}

    @app.post("/api/drafts/{draft_id}/discard")
    def discard_draft(draft_id: str, user: Me) -> dict:
        network.discard_draft(draft_id, actor=user.id)
        return {"ok": True}

    # ---- 用到别处资料、等所有者确认的回复与交付（不变量 22）：只有 Agent 的所有者能决定，别人一律 404 ----
    @app.post("/api/held/{held_id}")
    def decide_held(held_id: str, body: HeldIn, user: Me) -> dict:
        h = network.decide_held(held_id, body.action, actor=user.id)
        return {"id": h.id, "status": h.status}

    @app.get("/api/schedules")
    def list_schedules(user: Me) -> list:
        return network.schedules(user.id)

    @app.patch("/api/schedules/{schedule_id}")
    def update_schedule(schedule_id: str, body: SchedulePatch, user: Me) -> dict:
        network.set_schedule_enabled(schedule_id, body.enabled, actor=user.id)
        return {"ok": True}

    @app.delete("/api/schedules/{schedule_id}")
    def delete_schedule(schedule_id: str, user: Me) -> dict:
        network.delete_schedule(schedule_id, actor=user.id)
        return {"ok": True}

    @app.put("/api/me/assistant")
    def set_assistant(body: AssistantIn, user: Me) -> dict:
        network.set_assistant(body.agent_id, actor=user.id)
        return network.store.users[user.id].public()

    @app.post("/api/conversations")
    def create_conversation(body: ConversationIn, user: Me) -> dict:
        return network.create_conversation(body.project_id, body.agent_id, body.title, actor=user.id).to_dict(full=False)

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
        m = network.send_message(conv_id, body.text, body.mentions, actor=user.id,
                                 attachments=[a.model_dump() for a in body.attachments])
        return {"id": m.id, "role": m.role, "text": m.text, "mentions": m.mentions, "ts": m.ts,
                "attachments": m.attachments}

    # ---- 对话附件：先上传，再随消息发出；看不到对话的人上传、下载一律 404 ----
    @app.post("/api/conversations/{conv_id}/attachments")
    async def upload_attachment(conv_id: str, request: Request, user: Me,
                                name: str = Query(min_length=1, max_length=200)) -> dict:
        """请求体就是文件内容（不用 multipart，少一个依赖），类型取 Content-Type。超过上限 413。"""
        await run_in_threadpool(network.visible_conv, conv_id, user.id)  # 先查可见性，不收看不到的对话的内容
        too_big = HTTPException(413, f"附件超过上限 {ATTACHMENT_LIMIT // 1_000_000} MB")
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > ATTACHMENT_LIMIT:
            raise too_big
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > ATTACHMENT_LIMIT:
                raise too_big
        return await run_in_threadpool(network.upload_attachment, conv_id, name, bytes(buf),
                                       request.headers.get("content-type", ""), user.id)

    @app.get("/api/conversations/{conv_id}/attachments/{sha}")
    def attachment(conv_id: str, sha: str, user: Me) -> FileResponse:
        path, meta = network.attachment_file(conv_id, sha, actor=user.id)
        # 一律当作附件下载，不在本站点内渲染（内容来自别的参与者）
        return FileResponse(path, filename=meta["name"], media_type=meta["mime"], content_disposition_type="attachment",
                            headers={"X-Content-Type-Options": "nosniff"})

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

    @app.put("/api/requests/{request_id}/project")
    def assign_request(request_id: str, body: AssignIn, user: Me) -> dict:
        network.assign_request(request_id, body.project_id, actor=user.id)
        return network.request_detail(request_id, actor=user.id)

    @app.post("/api/requests/{request_id}/cancel")
    def cancel_request(request_id: str, user: Me) -> dict:
        network.cancel_request(request_id, actor=user.id)
        return network.request_detail(request_id, actor=user.id)

    # ---- 委托与知识 ----
    @app.get("/api/tasks/{task_id}")
    def task(task_id: str, user: Me) -> dict:
        return network.task_detail(task_id, actor=user.id)

    # ---- 工作目录（只读）：Agent 的所有者看产物 ----
    def _download(path) -> FileResponse:
        return FileResponse(path, filename=path.name, content_disposition_type="attachment",
                            headers={"X-Content-Type-Options": "nosniff"})

    @app.get("/api/tasks/{task_id}/files")
    def task_files(task_id: str, user: Me) -> dict:
        return network.workdir_files("task", task_id, actor=user.id)

    @app.get("/api/tasks/{task_id}/files/{path:path}")
    def task_file(task_id: str, path: str, user: Me) -> FileResponse:
        return _download(network.workdir_file("task", task_id, path, actor=user.id))

    @app.get("/api/conversations/{conv_id}/seats/{agent_id}/files")
    def seat_files(conv_id: str, agent_id: str, user: Me) -> dict:
        return network.workdir_files("seat", f"{conv_id}/{agent_id}", actor=user.id)

    @app.get("/api/conversations/{conv_id}/seats/{agent_id}/files/{path:path}")
    def seat_file(conv_id: str, agent_id: str, path: str, user: Me) -> FileResponse:
        return _download(network.workdir_file("seat", f"{conv_id}/{agent_id}", path, actor=user.id))

    @app.post("/api/conversations/{conv_id}/seats/{agent_id}/stop")
    def stop_seat(conv_id: str, agent_id: str, user: Me) -> dict:
        """终止这个席位正在跑的一轮（只有 Agent 的所有者能）。"""
        network.stop_run(f"{conv_id}/{agent_id}", actor=user.id)
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(task_id: str, user: Me) -> dict:
        """终止这个委托正在跑的一轮（只有执行者 Agent 的所有者能）。"""
        network.stop_run(task_id, actor=user.id)
        return {"ok": True}

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
        return network.decide_approval(approval_id, body.allow, actor=user.id, note=body.note, scope=body.scope).to_dict()

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

    # 设备码配对：连接器申请与轮询不需要登录（它还没有令牌）；看与批准需要登录，批准的人就是设备的所有者
    pairings = network.pairings

    def _pairing(code: str, user: User) -> tuple[str, dict]:
        try:
            return pairings.lookup(code, user.id)
        except auth.PairingLimit as e:
            raise HTTPException(429, str(e)) from e
        except auth.AuthError as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/api/devices/pair")
    def pair_start(body: PairIn) -> dict:
        harnesses = list(dict.fromkeys(h for h in body.harnesses if h in HARNESSES))
        try:
            p = pairings.start(body.name.strip()[:80], body.hostname.strip()[:80], harnesses)
        except auth.PairingLimit as e:
            raise HTTPException(429, str(e)) from e
        return {**p, "verify_path": f"/#pair={p['user_code']}"}

    @app.post("/api/devices/pair/poll")
    def pair_poll(body: PairPollIn) -> dict:
        return pairings.poll(body.device_code)

    @app.get("/api/devices/pair/{code}")
    def pair_view(code: str, user: Me) -> dict:
        return pairings.view(_pairing(code, user)[1])

    @app.post("/api/devices/pair/{code}/approve")
    def pair_approve(code: str, body: DeviceIn, user: Me) -> dict:
        _pairing(code, user)  # 统一 404 / 429
        dev = pairings.approve(store, code, user.id, body.name)
        return {**dev.public(), "online": False}

    @app.post("/api/devices/pair/{code}/deny")
    def pair_deny(code: str, user: Me) -> dict:
        _pairing(code, user)
        pairings.deny(code, user.id)
        return {"ok": True}

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
            network.unregister_runtime(dev.id, rt)
            network.bus.emit("device.disconnected", device_id=dev.id)

    # ---- 运行时回调（凭执行上下文令牌，不走登录） ----
    @app.post("/internal/tools")
    async def tools(body: ToolCallIn) -> dict:
        # 工具调用会碰持久化与锁，放到线程里跑，不阻塞事件循环
        text, is_error = await asyncio.to_thread(network.handle_tool, body.token, body.tool, body.args)
        return {"text": text, "is_error": is_error}

    # 云端连接器：运行时经 mcp_connectors.py 回调。网络按令牌认定这一轮能用哪些，带上凭证代为调用
    @app.post("/internal/connectors/list")
    async def connector_list(body: ConnectorCallIn) -> dict:
        return {"tools": await asyncio.to_thread(network.connector_tools, body.token)}

    @app.post("/internal/connectors/call")
    async def connector_call(body: ConnectorCallIn) -> dict:
        text, is_error = await asyncio.to_thread(network.connector_call, body.token, body.tool, body.args)
        return {"text": text, "is_error": is_error}

    @app.post("/internal/approval")
    async def approval(body: ApprovalIn) -> dict:
        # 阻塞到 Agent 所有者决定或超时；dsh 那头的应答插件一直等着
        outcome = await asyncio.to_thread(network.request_approval, body.token, body.tool, body.reason)
        return {"outcome": outcome}

    return app
class MatrixServer(uvicorn.Server):
    """收到退出信号就唤醒事件流。uvicorn 要等所有连接结束才执行 lifespan 关闭，
    只靠 lifespan 的话事件流会一直拖到优雅关闭超时才被强行切断。"""

    def __init__(self, config: uvicorn.Config, network: Network):
        super().__init__(config)
        self.network = network

    def handle_exit(self, sig: int, frame: Any) -> None:
        self.network.bus.close()
        super().handle_exit(sig, frame)


def build_network(runtime_name: str, data: str, port: int, dsh_bin: str = "dsh", seed: bool = False,
                  models: str | None = None, connectors: str | None = None, plugins: str | None = None,
                  admins: str | None = None, fetch: str | None = None) -> Network:
    """seed：全新数据目录是否预置种子 Agent。服务默认不预置——用户登录后自己创建；CLI 演练时预置。
    models：云侧预置模型目录文件；None = `<data>/models.json`（没有这个文件就不启用目录）。
    admins：管理员邮箱，逗号分隔；None = 最早注册的那个人。
    fetch：抓取白名单文件；None = `<data>/fetch.json`（没有这个文件就用 dsh 自带的 web_fetch）。"""
    runtime = DshRuntime(dsh_bin) if runtime_name == "dsh" else ScriptedRuntime(scenario_handler)
    return Network(runtime, Path(data).resolve(), tool_url=f"http://127.0.0.1:{port}/internal/tools", seed=seed,
                   models=ModelCatalog(Path(models).resolve()) if models else None,
                   connectors=ConnectorCatalog(Path(connectors).resolve()) if connectors else None,
                   plugins=PluginCatalog(Path(plugins).resolve()) if plugins else None,
                   admins=(admins or "").split(","),
                   fetch=FetchConfig(Path(fetch).resolve()) if fetch else None)


def main() -> None:
    from . import utf8_stdio
    utf8_stdio()
    p = argparse.ArgumentParser(description="Matrix：Agent 协作网络")
    p.add_argument("--runtime", choices=["dsh", "scripted"], default="dsh")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--data", default=None, help="数据目录；默认 MATRIX_DATA，没有就用 .matrix（只有改名前的 .agentnet 时沿用它）")
    p.add_argument("--dsh-bin", default="dsh")
    p.add_argument("--models", default=compat.env("MODELS"),
                   help="云侧预置模型目录（JSON，改了就生效）；默认 <data>/models.json，没有就不限制")
    p.add_argument("--connectors", default=compat.env("CONNECTORS"),
                   help="云端连接器目录（JSON，改了就生效）；默认 <data>/connectors.json，没有就不提供云端连接器")
    p.add_argument("--plugins", default=compat.env("PLUGINS"),
                   help="项目插件目录（JSON，改了就生效）；默认 <data>/plugins.json，没有就只能上传插件包")
    p.add_argument("--fetch", default=compat.env("FETCH"),
                   help="抓取白名单（JSON，改了就生效）；默认 <data>/fetch.json，没有就用 dsh 自带的 web_fetch（只抓公网）")
    p.add_argument("--admins", default=compat.env("ADMINS"),
                   help="管理员邮箱（逗号分隔），能看 /admin 管理后台；默认最早注册的那个人")
    # 只在容器里改：容器内监听 0.0.0.0 才能被同一 compose 网络里的反向代理访问；端口不对宿主机发布
    p.add_argument("--host", default=compat.env("HOST", "127.0.0.1"))
    args = p.parse_args()
    args.data = args.data or compat.data_dir()

    network = build_network(args.runtime, args.data, args.port, args.dsh_bin, models=args.models,
                            connectors=args.connectors, plugins=args.plugins, admins=args.admins, fetch=args.fetch)
    print(f"Matrix → http://127.0.0.1:{args.port}  (runtime={network.runtime.name}, data={args.data})")
    catalog = network.models
    if catalog.active:
        print(f"预置模型：{catalog.path}（{len(catalog.presets())} 个）")
    if network.connectors.specs():
        print(f"云端连接器：{network.connectors.path}（{len(network.connectors.specs())} 个）")
    if (policy := network.fetch.policy()) is not None:
        print(f"抓取白名单：{network.fetch.path}（{'、'.join(policy.allow_entries()) or '空'}"
              f"{'' if policy.public else '，不抓公网'}{'，替代自带的 web_fetch' if policy.replace_builtin else ''}）")
    if network.plugin_dir_catalog.items():
        print(f"项目插件目录：{network.plugin_dir_catalog.path}（{len(network.plugin_dir_catalog.items())} 个）")
    network.start_scheduler()
    config = uvicorn.Config(create_app(network), host=args.host, port=args.port, log_level="warning",
                            timeout_graceful_shutdown=3)
    MatrixServer(config, network).run()


if __name__ == "__main__":
    main()
