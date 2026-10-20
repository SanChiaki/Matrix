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
GROUP_FILES = "group"      # 群组档案的文件区在工作目录里的挂载名（只读副本，运行后丢弃）
PROJECT_FILES = "project"  # 项目文件在挂载群组的工作目录里的挂载名（只读副本，运行后丢弃）

# 群组档案工具：群组成员都有，不需要 delegate。其余网络工具（协作工具）需要 delegate。
ARCHIVE_TOOLS = ("record_decision", "list_decisions", "publish_file", "propose_brief")
# 用户任务工具：席位（群聊、私聊）都有。人交代的事有交付目标时，接任务的 Agent 自己认定、自己交付。
REQUEST_TOOLS = ("open_request", "deliver_request", "accept_request")
# 项目工具：群组挂在项目里时才有，不需要 delegate。跨群组只能提交交接，由人确认后发出（docs/projects.md）。
PROJECT_TOOLS = ("handoff",)
# 负责人工具：项目打开“负责人统筹”时，只有项目负责人 Agent 在它自己群聊的席位里有（不变量 19、20）。
LEAD_TOOLS = ("review_handoff", "review_request", "share_file")
# 负责人统筹的档位：manual 交接由人（项目所有者）确认；lead 交接由项目负责人 Agent 确认；
# lead_accept 再加上：负责人确认的交接引出的用户任务也由负责人验收。超出边界时都转给人。
AUTONOMY = ("manual", "lead", "lead_accept")


class TaskStatus(StrEnum):
    RUNNING = "running"        # 执行者在做，或在等它自己委托出去的子任务
    IN_REVIEW = "in_review"    # 已交付，等委托方验收（accept_task / request_changes）
    COMPLETED = "completed"    # 委托方已接受
    FAILED = "failed"          # 执行失败；委托方可以 retry_task 续接原会话
    REJECTED = "rejected"      # 授权阶段被拒绝（不在群组、成环、越过深度等）
    WAITING = "waiting"        # 端侧运行时不在线：不失败、不消耗重试，上线后继续（docs/device-runtimes.md §5）


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
    # 绑定的运行时：None = 默认运行时（平台云侧）；非 None = 已注册的端侧运行时名（docs/device-runtimes.md）
    device_runtime: str | None = None
    # 该运行时最近一次握手声明的强制能力（权限 -> enforced/advisory/unavailable）。
    # 设备离线时用它，避免把"未知"当成"全都不行"而误收窄委托权限。
    enforcement: dict[str, str] = field(default_factory=dict)
    created_by: str = "seed"             # seed | human | proposal:<id>
    owner_id: str | None = None          # 所有者（用户 id）：只有所有者能用它、改它、审批它在工作目录外的操作；None = 尚未认领
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


GROUP_KINDS = ("internal", "joint")
BRIEF_LIMIT = 4000          # Brief 每轮推送给所有成员，字符上限
DESCRIPTION_LIMIT = 2000    # Agent 描述上限：写进它自己每轮的提示词，也出现在 list_members 里供委托方挑人
DECISIONS_PUSHED = 12       # 每轮推送的最近有效决定条数；更早的用 list_decisions 查
GROUP_FILE_LIMIT = 200_000  # 发布到群组档案的单个文件上限（字节）


@dataclass
class Decision:
    """群组档案里的一条决定：仍然有效的才推送；被取代 / 撤销的保留记录。"""

    id: str
    text: str
    reason: str = ""
    status: str = "active"               # active | superseded | revoked
    supersedes: str | None = None        # 取代了哪一条
    by_user: str | None = None           # 人记录的：用户 id
    by_agent: str | None = None          # Agent 记录的：Agent id
    source: str | None = None            # 来源：消息 id / 任务 id / 席位 id
    created_at: float = field(default_factory=time.time)


@dataclass
class ResourceRef:
    """群组引用的资源（仓库、路径、文档）。只是指针，不授予任何访问权限。旧版叫“项目引用”（Group.projects）。"""

    id: str
    name: str
    location: str
    note: str = ""


@dataclass
class BriefSuggestion:
    """参与者或 Agent 提议的 brief 修改：由主持人采纳或拒绝。"""

    id: str
    text: str
    reason: str = ""
    by_user: str | None = None
    by_agent: str | None = None
    status: str = "pending"              # pending | accepted | rejected
    created_at: float = field(default_factory=time.time)


@dataclass
class Group:
    """群组 = 一个群聊 + 一份群组档案。

    internal（内部群组）：只有所有者，成员只能是所有者自己的 Agent。
    joint（联合群组，参照 Raft 的 joint channel）：主持人（owner_id）邀请其他用户成为参与者；
    每个参与者只能加入自己的 Agent；群聊与群组档案对所有参与者可见；Agent 仍只代表它的所有者运行。"""

    id: str
    name: str
    description: str
    member_ids: list[str]
    owner_id: str | None = None          # 内部群组的所有者 / 联合群组的主持人
    kind: str = "internal"               # internal | joint；创建后不能转换
    participants: list[str] = field(default_factory=list)  # 联合群组里除主持人以外、已接受邀请的用户
    invited: list[str] = field(default_factory=list)       # 已邀请、尚未接受的用户
    brief: str = ""                      # 目标、范围、成功标准、当前阶段：每轮推送给所有成员
    brief_updated_at: float | None = None
    decisions: list[Decision] = field(default_factory=list)
    refs: list[ResourceRef] = field(default_factory=list)
    suggestions: list[BriefSuggestion] = field(default_factory=list)
    home_enabled: list[str] = field(default_factory=list)  # 联合群组里由所有者打开了私有目录挂载的 Agent
    project_id: str | None = None        # 所属项目（最多一个，docs/projects.md）
    workstream_id: str | None = None     # 在项目里承担的方面
    archive_rev: int = 0                 # 档案（brief / 决定 / 文件 / 资源引用）每变一次 +1；席位与任务据此判断要不要重新推送
    created_at: float = field(default_factory=time.time)

    @property
    def people(self) -> list[str]:
        """看得到这个群组的用户：所有者 / 主持人 + 参与者。"""
        return [u for u in [self.owner_id, *self.participants] if u]

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
    user_id: str | None = None           # user：哪个用户发的（联合群组里有多个人）
    audience: list[str] = field(default_factory=list)  # 只给这些用户看（空 = 对话里的所有人），如席位的失败说明
    private_to: list[str] = field(default_factory=list)  # steps / wake 只给这些用户看（Agent 的所有者）
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
    archive_seen: int = -1               # 已经送达过的群组档案版本（Group.archive_rev）
    project_seen: str = ""               # 已经送达过的项目背景（项目 id:版本:方面；见 Network._project_key）
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
    # 因执行者的运行时无法强制而被收窄的权限：{权限: 原因}（见 docs/device-runtimes.md 第 3 节）
    narrowed: dict[str, str] = field(default_factory=dict)
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
    owners: list[str] = field(default_factory=list)  # 委托两端 Agent 的所有者：只有他们看得到这个委托
    archive_seen: int = -1               # 已经送达过的群组档案版本
    project_seen: str = ""               # 已经送达过的项目背景
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
class Handoff:
    """跨群组交接：项目里一个群组的 Agent 请另一个群组接着做（docs/projects.md）。Agent 不能直接跨群组委托或发言：
    交接由人（项目所有者）确认、可修改后，以人的名义发进目标群聊并 @ 目标成员（不变量 19）。"""

    id: str
    project_id: str
    owner_id: str | None                 # 由谁确认：项目所有者
    from_group_id: str
    from_agent_id: str
    context_kind: str                    # seat | task：处理结果通知谁
    context_id: str
    conversation_id: str
    to_group_id: str                     # 联合群组提交的交接由负责人 / 所有者分派：确认前为空
    to_agent_id: str
    text: str
    after: list[str] = field(default_factory=list)  # 依赖的交接：它们都完成（人验收）后才发出
    replaces: str | None = None          # 要替换的交接：确认这条时旧的作废，依赖它的改为依赖这条
    status: str = "pending"              # pending | scheduled（已确认，等依赖完成）| sent | rejected | superseded
    approved_by: str | None = None       # 以谁的名义发出：项目所有者（人确认，或负责人代为确认）
    approved_by_agent: str | None = None  # 负责人 Agent 确认的：记下是哪个 Agent
    reviewer: str = "owner"              # 待谁确认：owner（人）| lead（项目负责人 Agent）
    escalation: str = ""                 # 转给人的原因（超出预算、负责人要求、负责人没处理……）
    note: str = ""                       # 人的意见（拒绝理由）
    message_id: str | None = None        # 发出的消息
    returned_at: float | None = None     # 由这条消息引出的用户任务验收通过、已通知提交者的时间
    created_at: float = field(default_factory=time.time)
    decided_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    owner_id: str | None = None          # 由谁审核：提议者 Agent 的所有者（新 Agent 归他）
    status: str = "pending"              # pending | approved | rejected
    agent_id: str | None = None
    note: str = ""                       # 人的审核意见
    created_at: float = field(default_factory=time.time)
    decided_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Workstream:
    """项目里的一个方面（市场调研、原型开发……），可以由多个群组承担。"""

    id: str
    name: str
    description: str = ""


@dataclass
class Project:
    """项目：目标与汇总层（docs/projects.md）。只有一个所有者；汇总挂载群组里的用户任务，不流动上下文——
    群组、委托、知识、可见性的规则都不因项目改变。"""

    id: str
    name: str
    owner_id: str | None = None
    description: str = ""
    brief: str = ""                      # 目标、范围、成功标准、当前阶段：推送给挂载群组的席位与任务
    workstreams: list[Workstream] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)  # 项目级决定：只有所有者记录，推送给所有挂载群组
    # 共享给联合群组的项目文件：文件名 → 联合群组 id。联合群组只看得到共享给它的文件（不变量 18）
    shares: dict[str, list[str]] = field(default_factory=dict)
    lead_agent_id: str | None = None     # 项目负责人：挂载的内部群组里的一个成员
    lead_group_id: str | None = None     # 负责人在哪个群组的群聊席位里统筹
    autonomy: str = "manual"             # manual | lead | lead_accept（见 AUTONOMY）
    archive_rev: int = 0                 # 推送给 Agent 的部分（名称、brief、方面、挂载群组、文件）每变一次 +1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UserRequest:
    """人交代给 Agent 的一件事（看板上的一张卡）。由接任务的 Agent 认定（open_request）：闲聊、问答不算，
    有交付目标的才算。来源消息与下发人由网络从对话里认定，不信任模型自报。Agent 之间的委托（Task）不在这里。"""

    id: str
    conversation_id: str
    agent_id: str                        # 接任务的 Agent
    requester_id: str | None             # 下发的人：来源消息的发送者
    message_id: str                      # 来源消息
    title: str
    expected_outcome: str
    status: str = "in_progress"          # in_progress | in_review | completed | cancelled
    summary: str = ""                    # 交付说明（Agent 写）
    task_ids: list[str] = field(default_factory=list)  # 为它发起的委托（只在详情里展示）
    project_id: str | None = None        # 所属项目：由网络按所在群组认定，私聊里的由人归类
    workstream_id: str | None = None
    revisions: int = 0                   # 被下发人退回修改的次数
    feedback: str = ""                   # 最近一次退回的意见
    created_at: float = field(default_factory=time.time)
    delivered_at: float | None = None
    finished_at: float | None = None
    reviewed_by: str | None = None       # 验收的人（网络认定）
    reviewed_by_agent: str | None = None  # 项目负责人验收的：记下是哪个 Agent（不变量 20）
    reviewer: str = "owner"              # 交付后待谁验收：owner（下发的人）| lead（项目负责人）
    escalation: str = ""                 # 转回给人验收的原因
    lead_returns: int = 0                # 被负责人退回的次数（有上限，超了转给人）
    accepted_by_message_id: str | None = None  # 代验收依据；看板操作无消息

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Device:
    """一个端侧连接器（用户电脑上的 matrix-connect，见 docs/device-runtimes.md 第 2 节）。
    令牌是高熵随机串，只存 sha256；吊销后立即失效。一个用户可以有多台设备。"""

    id: str
    user_id: str
    name: str
    token_hash: str                      # sha256(token)，hex
    created_at: float = field(default_factory=time.time)
    last_seen_at: float | None = None    # 最近一次握手时间
    revoked_at: float | None = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def public(self) -> dict[str, Any]:
        """对外的设备信息：不含令牌与哈希。"""
        return {"id": self.id, "name": self.name, "created_at": self.created_at,
                "last_seen_at": self.last_seen_at, "revoked": self.revoked}


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
    requested_by: str | None = None      # 这次运行所在对话的所有者（联合群组是主持人）
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
