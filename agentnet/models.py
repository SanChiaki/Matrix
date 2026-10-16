"""Matrix（Agent 网络）领域模型。

网络的长期结构：Agent（节点）、Group（成员集合）、Conversation（人与 Agent 的关系边）。
委托（Task）是在对话中由 Agent 通过网络工具发起的动作，执行者只能是同一群组的成员。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# 权限词表：权限属于 Agent 自身；委托只能在执行者自身权限内再收窄。
AUTHORITIES: dict[str, str] = {
    "web.research": "使用 web_search / web_fetch 查阅公开信息",
    "file.write": "在工作目录内创建或修改文件",
    "shell.exec": "执行 shell 命令",
    "delegate": "在所在群组内把子任务委托给其他成员",
}

# DSH 工具 -> 所需权限。None 表示只读、默认允许。
TOOL_AUTHORITY: dict[str, str | None] = {
    "web_search": "web.research",
    "web_fetch": "web.research",
    "write": "file.write",
    "edit": "file.write",
    "bash": "shell.exec",
}

MCP_SERVER = "agentnet"
MCP_PREFIX = f"mcp__{MCP_SERVER}__"

AGENT_HOME = "agent-home"  # Agent 私有目录在每次运行的工作目录里的挂载名


class TaskStatus(StrEnum):
    RUNNING = "running"        # 执行者在做，或在等它自己委托出去的子任务
    IN_REVIEW = "in_review"    # 已交付，等委托方验收（accept_task / request_changes）
    COMPLETED = "completed"    # 委托方已接受
    FAILED = "failed"          # 执行失败；委托方可以 retry_task 续接原会话
    REJECTED = "rejected"      # 授权阶段被拒绝（不在群组、成环、越过深度等）


# 不会再变化的状态。FAILED 不在其中：它可以被续接。
TERMINAL = {TaskStatus.COMPLETED, TaskStatus.REJECTED}


@dataclass
class AgentNode:
    id: str
    name: str
    description: str
    capabilities: list[str]
    authority: list[str]                 # Agent 自身权限：运行时上限
    chat_enabled: bool = True            # 人是否可以直接与它对话
    # 运行时模型：{provider, model, reasoning_effort?}；None 表示用运行时自己的默认模型
    model: dict[str, str] | None = None
    created_by: str = "seed"             # seed | human | proposal:<id>
    owner_id: str | None = None          # 所有者（用户 id）：编辑它、审批它在工作目录外的操作；None = 尚未认领
    visibility: str = "private"          # private：只有所有者 | public：所有用户 | shared：所有者 + shared_with
    shared_with: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    active: int = 0                      # 运行中的会话/任务数（不持久化）

    @property
    def status(self) -> str:
        return "busy" if self.active else "idle"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status
        return d

    def persist(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("active")
        return d


@dataclass
class Group:
    id: str
    name: str
    description: str
    member_ids: list[str]
    owner_id: str | None = None          # 群组只对所有者可见
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Message:
    id: str
    role: str                            # user | agent | system（某个席位的通知）| event（群聊里的成员变动）
    text: str
    agent_id: str | None = None          # agent：谁说的；system：哪个席位的通知（例如这一轮中断）
    mentions: list[str] = field(default_factory=list)  # user：@ 到的 Agent（私聊里就是对方）
    task_ids: list[str] = field(default_factory=list)  # 这一轮里发起的委托
    steps: list[dict[str, Any]] = field(default_factory=list)  # 这一轮的过程：中间文本与工具调用
    wake: list[str] = field(default_factory=list)      # 唤醒这一轮的网络通知（委托回报、提议结果）
    ts: float = field(default_factory=time.time)


@dataclass
class Seat:
    """一个 Agent 在一个对话里的席位：自己的 DSH 会话、收件箱、工作目录。

    群聊里每个被 @ 过的成员各有一个席位，私聊只有对方一个。席位之间上下文隔离，
    对话里的公开消息在席位被唤醒时作为“新消息”送达。
    """

    agent_id: str
    workdir: str                         # 相对 data 目录的工作目录（DSH 会话按目录存放，续接时必须一致）
    dsh_session_id: str | None = None
    inbox: list[dict[str, Any]] = field(default_factory=list)  # 还没送达的 @ 与网络通知
    seen: int = 0                        # 对话消息已送达到第几条
    known: list[str] = field(default_factory=list)  # 已经告诉过它的、别人共享给它的知识 key
    delegations: int = 0                 # 自最近一次被 @ 以来发起的委托数
    blocked: str | None = None           # 上一轮失败的原因；非空时收件箱暂停，等人“继续”或再 @ 它
    created_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("inbox")
        d["pending"] = len(self.inbox)
        return d


@dataclass
class Conversation:
    """参照 Raft：每个群组有且只有一个群聊（频道）；每个（用户, Agent）有且只有一个私聊。
    archived 的对话只读：群组被删除后的群聊、旧版本遗留的多余对话。"""

    id: str
    kind: str                            # group（群聊：属于群组，被 @ 的成员接任务）| dm（私聊：一对一，不能委托）
    group_id: str | None                 # 群聊所在群组；群组被删后为 None
    agent_id: str | None                 # 私聊对象；群聊为 None
    title: str
    owner_id: str | None = None          # 对话属于谁：群聊 = 群组所有者，私聊 = 发起私聊的用户
    seats: dict[str, Seat] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)
    status: str = "idle"                 # idle | thinking（有席位在跑一轮）| waiting（有进行中的委托）
    archived: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self, full: bool = True) -> dict[str, Any]:
        """full=True 用于持久化（含收件箱）；full=False 是列表摘要。"""
        d = asdict(self)
        if not full:
            d.pop("messages")
            d["seats"] = {k: s.public() for k, s in self.seats.items()}
            d["message_count"] = len(self.messages)
            last = next((m for m in reversed(self.messages) if m.role != "event"), None)
            d["last_message"] = last.text[:120] if last else ""
            d["blocked"] = any(s.blocked for s in self.seats.values())
        return d


def seat_id(conv_id: str, agent_id: str) -> str:
    """席位的执行上下文 id；与知识可见性里的共享标签（对话/Agent）一致。"""
    return f"{conv_id}/{agent_id}"


@dataclass
class Delegation:
    """一次授权：目标、权限、约束、输出要求（交付方式由委托方在这里约定）。"""

    mission: str
    authority: list[str]
    constraints: list[str]
    expected_outcome: str
    share: list[str] = field(default_factory=list)  # 显式共享给对方的知识 key


@dataclass
class Task:
    id: str
    conversation_id: str
    group_id: str
    delegator_id: str
    delegatee_id: str | None
    parent_task_id: str | None           # None 表示直接从对话中发起
    depth: int                           # 对话里发起的委托 depth=1
    delegation: Delegation
    status: TaskStatus = TaskStatus.RUNNING
    granted_authority: list[str] = field(default_factory=list)
    denied_authority: list[str] = field(default_factory=list)
    effective_constraints: list[str] = field(default_factory=list)
    dsh_session_id: str | None = None
    result: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    error: str | None = None
    prompt: str = ""
    inbox: list[dict[str, Any]] = field(default_factory=list)  # 还没送达执行者的通知与验收意见
    notes: list[str] = field(default_factory=list)  # 等待子任务时的中间说明（不算交付）
    revisions: int = 0                   # 被委托方退回修改的次数
    auto_accepted: bool = False          # 委托方没有显式验收：送达的那一轮结束后视为接受
    retries: int = 0                     # 失败后被续接的次数
    delegations: int = 0                 # 执行者在这个任务里发起的委托数
    created_at: float = field(default_factory=time.time)
    delivered_at: float | None = None
    finished_at: float | None = None

    def to_dict(self, full: bool = False) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["summary"] = (self.result or {}).get("summary")
        if not full:
            for k in ("result", "evidence", "prompt", "inbox"):
                d.pop(k)
        return d


@dataclass
class Proposal:
    """Agent 提议新建的 Agent。网络不会自己创建：由人审核、可修改后批准，以人的身份创建并加入群组。"""

    id: str
    proposer_id: str
    context_kind: str                    # seat | task：批准或拒绝后通知谁
    context_id: str
    conversation_id: str
    group_id: str
    name: str
    description: str
    capabilities: list[str]
    authority: list[str]
    max_authority: list[str]             # 提议者当时的有效权限：批准时的权限上限
    reason: str = ""
    status: str = "pending"              # pending | approved | rejected
    agent_id: str | None = None
    note: str = ""                       # 人的审核意见
    created_at: float = field(default_factory=time.time)
    decided_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class User:
    id: str
    email: str
    password_hash: str                   # scrypt，hex
    salt: str                            # hex
    created_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "email": self.email}


@dataclass
class Approval:
    """Agent 想在工作目录以外操作（dsh 沙箱升权）时向它的所有者发起的一次性申请。"""

    id: str
    agent_id: str
    owner_id: str | None                 # 由谁审批：Agent 的所有者
    context_id: str                      # 席位 id 或任务 id
    conversation_id: str
    tool: str
    reason: str                          # dsh 给出的原因，含模型写的理由
    status: str = "pending"              # pending | allowed | rejected | cancelled
    requested_by: str | None = None      # 这次运行是替哪个用户做的（对话所有者）
    note: str = ""
    created_at: float = field(default_factory=time.time)
    decided_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class KnowledgeItem:
    key: str
    title: str
    content: str
    group_id: str
    owner_task_id: str
    visible_to: list[str]                # 可见的上下文 id（对话 id 或任务 id）

    def to_dict(self, content: bool = False) -> dict[str, Any]:
        d = asdict(self)
        if not content:
            d.pop("content")
            d["size"] = len(self.content)
        return d
