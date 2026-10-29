"""Matrix（Agent 网络）领域模型。

网络的长期结构：Agent（节点）、Project（成员集合 + 项目群聊 + 项目档案）、Conversation（人与 Agent 的关系边）。
委托（Task）是在对话中由 Agent 通过网络工具发起的动作，执行者只能是同一项目的成员。
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
    "delegate": "在所在项目内把子任务委托给其他成员",
    "connector.use": "使用所有者授权、并勾选给它的连接器（外部服务）",
}

# DSH 工具 -> 所需权限。None 表示只读、默认允许。
TOOL_AUTHORITY: dict[str, str | None] = {
    "web_search": "web.research",
    "web_fetch": "web.research",
    "write": "file.write",
    "edit": "file.write",
    "bash": "shell.exec",
    "pwsh": "shell.exec",
}

# 端侧 harness（连接器驱动名 -> 显示名）。一台设备可以同时提供多个；Agent 绑定到“设备 + harness”。
# 连接器的驱动名必须在这里（test_connector_drivers_are_known_harnesses）。
HARNESSES: dict[str, str] = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "dsh": "dsh",
}

MCP_SERVER = "matrix_net"  # 网络工具的 MCP 服务名；不叫 matrix，免得和用户自己配的同名服务（如 matrix.org）撞上
MCP_PREFIX = f"mcp__{MCP_SERVER}__"
# 云端连接器：网络代调用的那一组工具挂在另一个 MCP 服务下，工具名是 <连接器 id>__<工具名>（docs/connectors-and-skills.md）
CONNECTOR_SERVER = "connectors"
CONNECTOR_PREFIX = f"mcp__{CONNECTOR_SERVER}__"
# 共享项目里所有者对自己连接器的授权（Project.connector_grants）：没写的就是 ask
GRANTS = {"ask": "每次问", "always": "这个项目里都可以", "deny": "不行"}

AGENT_HOME = "agent-home"  # Agent 私有目录在每次运行的工作目录里的挂载名
PROJECT_FILES = "project"  # 项目档案的文件区在工作目录里的挂载名（只读副本，运行后丢弃）
ATTACHMENTS = "attachments"  # 对话附件在工作目录里的挂载名（只读副本，每轮刷新，运行后丢弃）
ATTACHMENT_LIMIT = 20_000_000  # 单个附件上限（字节）
ATTACHMENTS_PER_MESSAGE = 10
SKILLS = "skills"  # 本轮生效的技能在工作目录里的挂载名（只读副本，项目技能 > 我的技能，运行后丢弃）

# 技能（docs/connectors-and-skills.md）：一个目录 <名字>/SKILL.md，可带模板、脚本。名字是 kebab-case 英文
# （dsh 的技能名规则），中文写在描述里。
SKILL_NAME_RE = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
SKILL_NAME_MAX = 64
SKILL_LIMIT = 8_000_000    # 单个技能目录的总大小上限（字节）；官方的 docx / pptx / xlsx 技能都在 1 MB 上下，带字体的到 5 MB
SKILLS_TOTAL = 16_000_000  # 每轮下发到端侧的技能合计上限
SKILLS_PER_AGENT = 50


# 项目插件（docs/project-plugins.md）：插件包 = plugin.json + page/（沙箱里跑的页面代码）+ skills/（技能）。
PLUGIN_LIMIT = 2_000_000   # 一个插件包的总大小上限（字节）
PLUGIN_FILES = 300         # 一个插件包最多多少个文件
PLUGINS_PER_PROJECT = 10
PLUGIN_TABS = 5            # 一个插件最多加几个项目页标签
PLUGIN_TABLES = 10
PLUGIN_FIELDS = 40         # 每张表最多多少个字段
PLUGIN_RECORDS = 5000      # 每张表最多多少条记录
PLUGIN_RECORD_LIMIT = 32_000  # 一条记录的字段合计上限（字节）
PLUGIN_TRIGGERS = 20
# 插件页面经桥能申请的能力（实际 = 申请的 ∩ 看页面的人本身有的）；Agent 那一侧只有记录工具
PLUGIN_PAGE_CAPS: dict[str, str] = {
    "records.read": "读插件自己的表",
    "records.write": "写插件自己的表（按清单校验字段）",
    "archive.read": "读纲要、有效决定、项目文件、资源引用",
    "members.read": "读成员 Agent 的名片",
    "chat.read": "读群聊里对所有参与者公开的消息",
    "chat.post": "以看页面的人的名义在群聊里发消息（每次确认）",
    "requests.read": "读看页面的人自己看得到的用户任务",
}
PLUGIN_AGENT_CAPS = ("records.read", "records.write")

# 发布与订阅（docs/project-publishing.md）：项目的内容离开项目只经发布物（不变量 23）
RELEASES = "releases"      # 订阅的交付件在工作目录里的挂载名（只读副本，运行后丢弃）
RELEASE_KINDS = {"method": "技能包", "fact": "交付件"}
RELEASES_PER_PROJECT = 20
RELEASE_READERS = 200      # 一份发布物最多点名多少个读者
RELEASE_ITEMS = 30         # 一份发布物最多带几个技能 / 文件
RELEASE_VERSIONS_KEPT = 3  # 快照保留最近几个版本
RELEASE_FEEDBACK = 200     # 一份发布物最多保留多少条反馈
SUBSCRIPTIONS_PER_AGENT = 30


def attachment_rel(a: dict[str, Any]) -> str:
    """附件在工作目录里的相对路径：attachments/<sha 前 8 位>/<原文件名>。文件名保留扩展名，前缀避免同名冲突。"""
    return f"{ATTACHMENTS}/{a['sha'][:8]}/{a['name']}"


# 项目档案工具：项目成员都有，不需要 delegate。其余网络工具（协作工具）需要 delegate。
ARCHIVE_TOOLS = ("record_decision", "list_decisions", "publish_file", "propose_brief", "propose_skill")
# 用户任务工具：席位（项目群聊、项目会话、私聊）都有。人交代的事有交付目标时，接任务的 Agent 自己认定、自己交付。
REQUEST_TOOLS = ("open_request", "deliver_request", "accept_request")
# 读项目现状的工具：项目会话里的助理（读这个项目）与助理对话（读主人参与的任一项目）有，按主人的可见性过滤。
SESSION_TOOLS = ("read_chat", "read_request")
# 助理对话工具：只有助理对话（人和自己助理的私聊）里有：起草要以主人名义发进项目群聊的消息、主人的定时（不变量 21）。
HOME_TOOLS = ("draft_message", "schedule", "list_schedules", "cancel_schedule")
# 插件记录工具：项目里装了把它们开给 Agent 的插件时才有（档案类工具，不需要 delegate）
PLUGIN_TOOLS = ("plugin_records", "plugin_write")
# 能力工具：任何执行上下文都有。缺连接器时提醒所有者（只弹一张卡片，不授权、不安装）
ABILITY_TOOLS = ("suggest_connector",)
# 私有目录工具：这一轮有没挂上的笔记时才有，读来自别的场合的笔记（不变量 22，matrix/flow.py）
NOTE_TOOLS = ("open_note",)
# 会让别人看到内容的网络工具：会话读过越界的来源时一律不放行（不变量 22）。群聊回复与任务交付另外拦下等所有者确认
OUTBOUND_TOOLS = ("record_decision", "publish_file", "propose_brief", "propose_skill", "plugin_write", "delegate",
                  "share_knowledge", "request_changes", "retry_task", "open_request", "deliver_request", "accept_request")


class TaskStatus(StrEnum):
    RUNNING = "running"        # 执行者在做，或在等它自己委托出去的子任务
    IN_REVIEW = "in_review"    # 已交付，等委托方验收（accept_task / request_changes）
    COMPLETED = "completed"    # 委托方已接受
    FAILED = "failed"          # 执行失败；委托方可以 retry_task 续接原会话
    REJECTED = "rejected"      # 授权阶段被拒绝（不在项目、成环、越过深度等）
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
    # 云侧预置模型 id（model_catalog）：启用目录后云侧 Agent 只能从中选；None = 目录默认。与 model 二选一
    model_preset: str | None = None
    # 绑定的运行时：None = 默认运行时（平台云侧）；非 None = 已注册的端侧运行时名（docs/device-runtimes.md）
    device_runtime: str | None = None
    # 设备上的哪个 harness（HARNESSES 的键）；None = 设备的默认 harness（旧数据、旧连接器只有一个）
    harness: str | None = None
    # 该运行时最近一次握手声明的强制能力（权限 -> enforced/advisory/unavailable）。
    # 设备离线时用它，避免把"未知"当成"全都不行"而误收窄委托权限。
    enforcement: dict[str, str] = field(default_factory=dict)
    # 运行上限（所有者设置）：每轮运行的超时秒数；每次被 @ 引出的工作 / 每个任务最多发起几次委托。None = 不限制
    timeout_s: int | None = None
    max_delegations: int | None = None
    # 所有者勾给它的“我的连接器”（Connection id）与“我的技能”（技能名）。只能是同一所有者的
    connections: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    # 所有者给它勾的订阅：看得到的发布物（Release id）。技能包进技能层，交付件只读挂到 releases/（不变量 23）
    subscriptions: list[str] = field(default_factory=list)
    created_by: str = "seed"             # seed | human | signup（注册时网络给的助理）| proposal:<id>
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


BRIEF_LIMIT = 4000          # 纲要（brief）每轮推送给所有成员，字符上限
DESCRIPTION_LIMIT = 2000    # Agent 描述上限：写进它自己每轮的提示词，也出现在 list_members 里供委托方挑人
DECISIONS_PUSHED = 12       # 每轮推送的最近有效决定条数；更早的用 list_decisions 查
FILE_LIMIT = 200_000       # 发布到项目档案的单个文件上限（字节）


@dataclass
class Decision:
    """项目档案里的一条决定：仍然有效的才推送；被取代 / 撤销的保留记录。"""

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
    """项目资料里的一条链接（仓库、路径、文档）。只是指针，不授予任何访问权限。"""

    id: str
    name: str
    location: str
    note: str = ""
    connector_id: str = ""               # 主持人指定用哪个连接器打开（认不出域名时）；空 = 按域名认


@dataclass
class BriefSuggestion:
    """参与者或 Agent 提议的纲要修改（kind=brief），或 Agent 提议沉淀的项目技能（kind=skill）：由主持人采纳或拒绝。"""

    id: str
    text: str                            # brief：新纲要；skill：技能正文
    reason: str = ""
    by_user: str | None = None
    by_agent: str | None = None
    status: str = "pending"              # pending | accepted | rejected
    kind: str = "brief"                  # brief | skill
    skill_name: str = ""                 # kind=skill：技能名与一句描述
    skill_description: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class ConnectorNeed:
    """主持人在连接器面板里手动添加的一项（没有链接也能写明要用什么）：不持有凭证。
    每个成员 Agent 用它所有者自己的授权（不变量 16）。"""

    id: str
    connector_id: str                    # 连接器目录 id（云端），电脑上的服务名，或自定义的 custom-<名字>
    purpose: str = ""
    ref_id: str | None = None            # 旧数据：关联的链接。迁移时并进那条链接（ResourceRef.connector_id）
    # 自定义连接器（接入方式：远程 MCP）：主持人手工写的配置。请求头的值只是 ${变量} 模板，
    # 每个成员连接时填自己的，项目里没有任何人的凭证（不变量 19）
    name: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Project:
    """项目 = 成员集合 + 一个项目群聊 + 一份项目档案（docs/projects.md）。委托只发给项目成员。

    没有类型：主持人（owner_id）随时可以邀请别人；有参与者或待处理的邀请就是**共享的**（`shared`，
    参照 Raft 的 joint channel：共享对话空间，不共享 Agent）。每个参与者只能加入自己的 Agent；
    群聊与项目档案对所有参与者可见；Agent 仍只代表它的所有者运行。"""

    id: str
    name: str
    description: str
    member_ids: list[str]
    owner_id: str | None = None          # 主持人（建项目的人）
    participants: list[str] = field(default_factory=list)  # 除主持人以外、已接受邀请的用户
    invited: list[str] = field(default_factory=list)       # 已邀请、尚未接受的用户
    brief: str = ""                      # 纲要：目标、范围、成功标准、当前阶段，每轮推送给所有成员
    brief_updated_at: float | None = None
    decisions: list[Decision] = field(default_factory=list)
    refs: list[ResourceRef] = field(default_factory=list)
    suggestions: list[BriefSuggestion] = field(default_factory=list)
    # 项目技能的记录（正文在 projects/<id>/skills/<名字>/）：{名字: {description, by_user, by_agent, approved_by, updated_at}}
    skills: dict[str, dict[str, Any]] = field(default_factory=dict)
    connector_needs: list[ConnectorNeed] = field(default_factory=list)  # 连接器面板里手动添加的
    # 共享项目里所有者对自己连接器的授权：{agent_id: {connection_id: always | deny}}；没写的是 ask（用到时问，不变量 19）。
    # 只给所有者看
    connector_grants: dict[str, dict[str, str]] = field(default_factory=dict)
    # 这个项目里实际调用过的连接器：{connector_id: {count, last_at, last_agent}}（连接器面板的一种来源）
    connector_usage: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 装了的插件（包在 projects/<id>/plugins/<名字>/，记录在 plugin-data/<名字>/）：
    # {名字: {version, title, source: catalog | upload, by_user, installed_at, data_rev,
    #        triggers: {key: {enabled, by_user, failures, paused, last_run, hook_hash}}}}
    plugins: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 所有者允许 Agent 在这个项目里直接用的别处来源：{agent_id: [场合]}（不变量 22，matrix/flow.py）。只给所有者看
    flow_grants: dict[str, list[str]] = field(default_factory=dict)
    archive_rev: int = 0                 # 档案（纲要 / 决定 / 文件 / 资源引用）每变一次 +1；席位与任务据此判断要不要重新推送
    created_at: float = field(default_factory=time.time)

    @property
    def people(self) -> list[str]:
        """看得到这个项目的用户：所有者 / 主持人 + 参与者。"""
        return [u for u in [self.owner_id, *self.participants] if u]

    @property
    def shared(self) -> bool:
        """有别人：已接受的参与者或待处理的邀请。发出第一封邀请时就算，不等对方接受。"""
        return bool(self.participants or self.invited)

    def to_dict(self) -> dict[str, Any]:
        # kind 是推导出来的，只为插件与旧客户端兼容（joint = 共享的）
        return {**asdict(self), "shared": self.shared, "kind": "joint" if self.shared else "internal"}


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
    user_id: str | None = None           # user：哪个用户发的（联合项目里有多个人）
    audience: list[str] = field(default_factory=list)  # 只给这些用户看（空 = 对话里的所有人），如席位的失败说明
    private_to: list[str] = field(default_factory=list)  # steps / wake 只给这些用户看（Agent 的所有者）
    attachments: list[dict[str, Any]] = field(default_factory=list)  # user：附件 {sha, name, size, mime}，文件在对话的附件区
    via: str | None = None               # user：经哪个插件发的（插件页面经确认发出，或插件的触发代表启用它的人发出）、哪个助理起草的
    draft_id: str | None = None          # event：助理对话里起草的一条消息（前端渲染成确认卡）
    held_id: str | None = None           # event：一条等所有者确认的输出（所有者看到确认卡，别人只看到“等确认”）
    ts: float = field(default_factory=time.time)


@dataclass
class Seat:
    """一个 Agent 在一个对话里的席位：自己的 DSH 会话、收件箱、工作目录。

    项目群聊里每个被 @ 过的成员各有一个席位，项目会话与私聊只有对方一个。席位之间上下文隔离，
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
    archive_seen: int = -1               # 已经送达过的项目档案版本（Project.archive_rev）
    abilities_seen: str = ""             # 已经送达过的“本轮可用的连接器 / 技能”说明的指纹，没变就不再送
    state_seen: str = ""                 # 项目会话 / 助理对话：已经送达过的项目现状（摘要的指纹），没变就不再送
    joint_seen: str = ""                 # 助理对话：已经送达过的共享项目总览的指纹（送了新的，这一轮就不能用连接器）
    # 这个模型会话读过的别处来源（场合，matrix/flow.py）：会话续接时一直有效，换新会话才清空（不变量 22）
    session_sources: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("inbox")
        d["pending"] = len(self.inbox)
        return d


@dataclass
class Conversation:
    """参照 Raft：每个项目有且只有一个项目群聊（频道）；每个（用户, Agent）有且只有一个私聊。
    项目会话：一个人在项目里和自己的助理私下对话，可以有多个，只有这个人看得到。
    archived 的对话只读：项目被删除后的群聊、旧版本遗留的多余对话、人归档的项目会话。"""

    id: str
    kind: str                            # project（项目群聊，被 @ 的成员接任务）| session（项目会话）| dm（私聊，不能委托）
    project_id: str | None               # 所在项目（群聊与项目会话）；项目被删后为 None
    agent_id: str | None                 # 私聊对象 / 项目会话里的助理；群聊为 None
    title: str
    owner_id: str | None = None          # 对话属于谁：群聊 = 项目所有者，私聊 / 项目会话 = 那个用户
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
    project_id: str
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
    session_sources: list[str] = field(default_factory=list)  # 同 Seat.session_sources
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
    archive_seen: int = -1               # 已经送达过的项目档案版本
    abilities_seen: str = ""             # 同 Seat.abilities_seen
    attachments: list[dict[str, Any]] = field(default_factory=list)  # 委托方显式转交的对话附件（同 Message.attachments）
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
    """Agent 提议新建的 Agent。网络不会自己创建：由人审核、可修改后批准，以人的身份创建并加入项目。"""

    id: str
    proposer_id: str
    context_kind: str                    # seat | task：批准或拒绝后通知谁
    context_id: str
    conversation_id: str
    project_id: str
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
    project_id: str | None = None        # 所属项目：由网络按所在对话认定，私聊里的由人归类
    revisions: int = 0                   # 被下发人退回修改的次数
    feedback: str = ""                   # 最近一次退回的意见
    created_at: float = field(default_factory=time.time)
    delivered_at: float | None = None
    finished_at: float | None = None
    reviewed_by: str | None = None       # 验收的人（网络认定）
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
    # 最近一次握手报告的 harness（第一个是默认）；设备离线时 UI 仍按它让人选
    harnesses: list[str] = field(default_factory=list)
    hostname: str = ""                   # 设备码配对时连接器报告的主机名，只用于展示

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def public(self) -> dict[str, Any]:
        """对外的设备信息：不含令牌与哈希。"""
        return {"id": self.id, "name": self.name, "created_at": self.created_at,
                "last_seen_at": self.last_seen_at, "revoked": self.revoked,
                "harnesses": list(self.harnesses), "hostname": self.hostname}


@dataclass
class Connection:
    """“我的连接器”：一个人授权的一个外部服务（docs/connectors-and-skills.md）。凭证不在这里：
    云端的加密存在 vault/（只有网络代调用时解开），电脑上的留在那台电脑，网络只知道名字与状态。"""

    id: str
    owner_id: str
    connector_id: str                    # 云端：目录 id；电脑上：那个 AI 工具里配置的服务名
    name: str                            # 显示名
    where: str = "cloud"                 # cloud | <设备 id>
    harness: str | None = None           # 电脑上的：哪个 AI 工具里配置的
    status: str = "ok"                   # ok | error | missing（电脑上这次没报告）
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    url: str = ""                        # 自定义的云端 MCP：用户填的 https 公网地址（只连公网，不跟重定向）

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class User:
    id: str
    email: str
    password_hash: str                   # scrypt，hex
    salt: str                            # hex
    assistant_id: str | None = None      # 助理：这个用户自己的一个 Agent，进项目时一定带上，项目会话的对话方
    created_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "email": self.email, "assistant_id": self.assistant_id}


@dataclass
class Draft:
    """助理在助理对话里起草、要以主人的名义发进某个项目群聊的消息（不变量 21）。主人确认（可以改）后才发出，
    目标群聊与 @ 在发出时按项目现状重新认定。"""

    id: str
    owner_id: str                        # 主人：只有他能看、能发、能丢弃
    agent_id: str                        # 起草的助理
    project_id: str
    text: str
    home_id: str                         # 起草所在的助理对话对话
    status: str = "pending"              # pending | sent | discarded
    message_id: str | None = None        # 发出后群聊里的那条消息
    created_at: float = field(default_factory=time.time)
    decided_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Held:
    """一次运行用到了别处的资料、要给当时不在场的人看的输出（不变量 22）：先拦下，由 Agent 的所有者决定。
    发出（send）、以后这个项目里可以直接用这些来源（grant）、不发并让它不用这些重写（rewrite）。"""

    id: str
    agent_id: str
    owner_id: str | None                 # 由谁决定：Agent 的所有者
    project_id: str
    context_kind: str                    # seat | task
    context_id: str                      # 席位 id 或任务 id
    kind: str                            # reply（群聊回复）| delivery（任务交付）
    text: str
    sources: list[str]                   # 越界的场合
    audience: list[str]                  # 会看到它的人
    extra: dict[str, Any] = field(default_factory=dict)  # reply：消息的 task_ids / steps / wake、重写时退回的 seen
    status: str = "pending"              # pending | sent | granted | rewritten
    created_at: float = field(default_factory=time.time)
    decided_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Schedule:
    """主人的一个定时：到点往那个 Agent 的私聊（助理对话）收件箱投一条，唤醒它跑一轮（不变量 21）。
    定时轮次没有人的新消息，不能登记用户任务。cron 与 at 二选一：cron 是五段（本地时间），at 是一次性的到点时间。"""

    id: str
    owner_id: str
    agent_id: str
    title: str
    instruction: str                     # 到点时交给 Agent 的话
    cron: str = ""
    at: float | None = None
    enabled: bool = True
    created_by: str = "user"             # user（主人说的，或注册时的默认早报）
    last_minute: int | None = None       # 同一分钟不重复触发
    last_fired: float | None = None
    day: str = ""                        # fires_today 计数的日期（本地 YYYY-MM-DD）
    fires_today: int = 0
    failures: int = 0                    # 连续没送达（席位停着）的次数，到 3 次自动停用
    error: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    requested_by: str | None = None      # sandbox：这次运行所在对话的所有者；connector：@ 它的人（委托里为空）
    note: str = ""
    # kind=connector：共享项目里第一次要用所有者的连接器（授权是 ask），先问所有者（不变量 19）。
    # tool 是连接器工具名，scope 是同意的范围：once（这一轮）| project（这个项目里以后都可以，写进 connector_grants）
    # kind=connector_write：部署方目录里的连接器、工具按 MCP 标注会对外产生影响（写、且 destructive 或 open world），
    # 每次调用都先给连接的授权人确认（不变量 19）。args 是网络实际要发出的参数，确认卡上照原样显示
    kind: str = "sandbox"                # sandbox | connector | connector_write
    project_id: str | None = None
    connection_id: str | None = None
    scope: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    decided_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Release:
    """项目发出去的一份发布物（docs/project-publishing.md，不变量 23）：只有主持人发布，按版本固定成快照
    （releases/<id>/v<N>/）。技能包（method）是一组技能，可以给所有人；交付件（fact）是一组档案文件（可带一个插件视图），
    只给点名的读者。订阅只在订阅者自己的 Agent 上生效，发布方看不到谁订阅了。"""

    id: str
    project_id: str
    name: str                            # kebab-case，项目内唯一；也是订阅者工作目录里 releases/<name>/ 的名字
    kind: str                            # method | fact
    title: str
    description: str = ""
    skills: list[str] = field(default_factory=list)   # method：项目技能或插件自带的技能的名字
    files: list[str] = field(default_factory=list)    # fact：档案文件名
    view: dict[str, str] | None = None   # fact：{plugin, tab}，插件在清单 release_views 里声明过的标签
    readers: list[str] = field(default_factory=list)  # 点名的读者（用户 id）；项目参与者总是看得到
    public: bool = False                 # method：部署里所有登录用户都看得到
    follow: bool = False                 # fact：跟随档案。列出的文件更新时网络自动切新版本（主持人开的长期许可）
    version: int = 0
    published_at: float | None = None
    published_by: str | None = None      # 这个版本是谁发的（跟随档案切的版本记为开跟随的主持人）
    status: str = "active"               # active | withdrawn
    feedback: list[dict[str, Any]] = field(default_factory=list)  # 读者自己写的反馈：{id, user_id, text, at}
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class KnowledgeItem:
    key: str
    title: str
    content: str
    project_id: str
    owner_task_id: str
    visible_to: list[str]                # 可见的上下文 id（对话 id 或任务 id）

    def to_dict(self, content: bool = False) -> dict[str, Any]:
        d = asdict(self)
        if not content:
            d.pop("content")
            d["size"] = len(self.content)
        return d
