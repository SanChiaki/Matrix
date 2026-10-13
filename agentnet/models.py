"""Agent Network 领域模型。

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

# 绕过网络私自派生 Agent 的工具：委托必须走网络。
FORBIDDEN_TOOLS = {"subagent", "subagent_fork", "workflow"}

MCP_SERVER = "agentnet"
MCP_PREFIX = f"mcp__{MCP_SERVER}__"


class TaskStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"  # 授权阶段被拒绝（不在群组、成环、越过深度等）


TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.REJECTED}


@dataclass
class AgentNode:
    id: str
    name: str
    description: str
    capabilities: list[str]
    authority: list[str]                 # Agent 自身权限：运行时上限
    chat_enabled: bool = True            # 人是否可以直接与它对话
    created_by: str = "seed"             # seed | human
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
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Message:
    id: str
    role: str                            # user | agent | system
    text: str
    task_ids: list[str] = field(default_factory=list)  # 这一轮里发起的委托
    steps: list[dict[str, Any]] = field(default_factory=list)  # 这一轮的过程：中间文本与工具调用
    ts: float = field(default_factory=time.time)


@dataclass
class Conversation:
    id: str
    agent_id: str
    group_id: str | None                 # 在群组里对话时，Agent 只能委托群组成员
    title: str
    dsh_session_id: str | None = None
    messages: list[Message] = field(default_factory=list)
    status: str = "idle"                 # idle | thinking
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self, full: bool = True) -> dict[str, Any]:
        d = asdict(self)
        if not full:
            d.pop("messages")
            d["message_count"] = len(self.messages)
            d["last_message"] = self.messages[-1].text[:120] if self.messages else ""
        return d


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
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_dict(self, full: bool = False) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["summary"] = (self.result or {}).get("summary")
        if not full:
            for k in ("result", "evidence", "prompt"):
                d.pop(k)
        return d


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
