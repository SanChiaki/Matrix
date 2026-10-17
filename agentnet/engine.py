"""Network：Agent 网络的运行核心。

- 对话：人与有权限（chat_enabled）的 Agent 持续对话，每个对话续接同一个 DSH 会话。
- 群组：只是成员集合。在群组内对话时，Agent 通过网络工具（MCP）只能委托群组成员；网络不会自己创建 Agent，
  只接受 Agent 的提议（propose_agent），由人审核后创建。
- 权限：执行者有效权限 = 执行者自身权限 ∩ 委托授予；委托授予只能是委托方有效权限的子集；约束沿链累加。
- 上下文：默认隔离。每个委托任务独立会话与工作目录；知识只对产生者与直接委托方可见，其余需显式共享。
- 异步：委托立即返回任务 id。每个执行上下文（对话、任务）有一个收件箱，消息与通知进收件箱，
  有内容且没在运行时网络就续接它的会话跑一轮；同一上下文同时只跑一轮。一轮失败只影响这一轮。
- 验收：交付后任务进入 in_review，由委托方接受或退回；失败的任务可以续接原会话。
"""

from __future__ import annotations

import re
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .events import EventBus
from .models import (AGENT_HOME as HOME_DIR, ARCHIVE_TOOLS, REQUEST_TOOLS, AUTHORITIES, BRIEF_LIMIT, GROUP_FILE_LIMIT,
                     GROUP_FILES, GROUP_KINDS, MCP_PREFIX, TOOL_AUTHORITY, AgentNode, Approval, Conversation, Decision,
                     Delegation, Group, KnowledgeItem, Message, ProjectRef, Proposal, Seat, Task, TaskStatus,
                     UserRequest, new_id, seat_id)
from .protocol import (build_notice_prompt, build_seat_prompt, build_task_prompt, format_archive, format_decision,
                       format_delivery, format_failure, format_failure_notice, format_ledger, format_open_requests,
                       format_proposal_notice, format_request_changes,
                       format_shared_knowledge, format_task_result, format_transcript, retry_prompt)
from .runtime import AgentRuntime, ApprovalBridge, RunRequest, RunResult, RuntimeCapabilities, ToolBridge
from .store import Store


@dataclass
class Limits:
    max_depth: int = 3              # 对话直接发起的委托 depth=1；depth >= max_depth 的任务不能再委托
    max_delegations: int = 8        # 每条用户消息（对话）/ 每个任务最多发起几次委托
    max_revisions: int = 3          # 一个任务最多被退回修改几次
    max_retries: int = 3            # 一个失败任务最多被续接几次
    turn_timeout_s: float = 1800
    task_timeout_s: float = 900
    approval_timeout_s: float = 900  # 所有者多久没审批就作废（dsh 那次调用按“取消”处理）


@dataclass
class ExecContext:
    """一次运行时调用的执行上下文；令牌只在调用期间有效。"""

    kind: str                       # seat（对话里的一个席位）| task
    id: str                         # 席位 id（对话/Agent）或任务 id
    agent_id: str
    conversation_id: str
    group_id: str | None
    authority: list[str]            # 有效权限
    constraints: list[str]
    depth: int                      # 对话为 0
    chain: list[str]                # 从对话 Agent 到当前 Agent 的链（防环）
    started_at: float = field(default_factory=time.time)
    created: list[str] = field(default_factory=list)  # 这一轮发起的委托
    wake: list[str] = field(default_factory=list)     # 这一轮送达的交付（本轮结束仍未处理即视为接受）
    addressed: list[str] = field(default_factory=list)  # 席位：这一轮送达的、@ 它的人的消息（open_request 的来源）
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def in_group(self) -> bool:
        """在群组里：挂载网络工具（至少有群组档案工具）。"""
        return self.group_id is not None

    @property
    def can_delegate(self) -> bool:
        return self.group_id is not None and "delegate" in self.authority

    @property
    def tools(self) -> list[str]:
        return network_tools_for(self.kind, self.in_group, self.can_delegate)

    @property
    def share_tag(self) -> str:
        return seat_id(self.conversation_id, self.agent_id)


class NetworkError(ValueError):
    pass


class Network:
    def __init__(self, runtime: AgentRuntime, data_dir: Path, limits: Limits | None = None,
                 tool_url: str = "http://127.0.0.1:8765/internal/tools", seed: bool = True):
        self.runtime = runtime
        self.limits = limits or Limits()
        self.tool_url = tool_url
        self.approval_url = tool_url.rsplit("/", 1)[0] + "/approval"
        self.store = Store(data_dir, seed=seed)
        self.bus = EventBus()
        self._contexts: dict[str, ExecContext] = {}
        self._live: dict[str, list[dict[str, Any]]] = {}  # 运行中的执行上下文 id → 过程（不持久化）
        self._running: set[str] = set()   # 正在跑一轮的执行上下文 id
        self._approval_waits: dict[str, threading.Event] = {}
        self._home_locks: dict[str, threading.Lock] = {}
        self._lock = self.store.lock
        self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="agentnet-round")

    # ================= 归属与可见性 =================
    # actor 是发起操作的用户 id；None 表示系统内部或测试，不做归属检查。
    # 信任域（不变量 16）：Agent 只属于它的所有者，只有所有者能用它（私聊、拉进群组）、改它；它只代表所有者运行。
    # 跨用户协作只走联合群组：群聊与群组档案对所有参与者可见，委托只给两端 Agent 的所有者看。
    def can_use_agent(self, a: AgentNode, actor: str | None) -> bool:
        return actor is None or a.owner_id in (None, actor)

    can_edit_agent = can_use_agent

    def can_see_agent(self, a: AgentNode, actor: str | None) -> bool:
        """看得到（名称、描述、能力）：自己的，或和自己在同一个联合群组里的。"""
        return self.can_use_agent(a, actor) or any(
            a.id in g.member_ids and actor in g.people for g in self.store.groups.values() if g.kind == "joint")

    def can_see_group(self, g: Group, actor: str | None) -> bool:
        return actor is None or g.owner_id is None or actor in g.people

    def can_see_conv(self, c: Conversation, actor: str | None) -> bool:
        if actor is None or c.owner_id is None:
            return True
        if c.kind == "group" and c.group_id:
            g = self.store.groups.get(c.group_id)
            if g is not None:
                return actor in g.people
        return c.owner_id == actor

    def can_see_task(self, t: Task, actor: str | None) -> bool:
        """委托只给两端 Agent 的所有者看（内部群组里两端都是所有者自己）。"""
        if actor is None:
            return True
        conv = self.store.conversations.get(t.conversation_id)
        if conv is None or conv.owner_id is None:
            return True
        return actor in t.owners if t.owners else conv.owner_id == actor

    def can_see_message(self, m: Message, actor: str | None) -> bool:
        return actor is None or not m.audience or actor in m.audience

    def _check(self, ok: bool, what: str) -> None:
        if not ok:
            raise NetworkError(f"unknown {what}")  # 对没有权限的人，假装不存在（404），不泄露

    def visible_agent(self, agent_id: str, actor: str | None) -> AgentNode:
        a = self._agent(agent_id)
        self._check(self.can_use_agent(a, actor), f"agent {agent_id}")
        return a

    owned_agent = visible_agent

    def visible_group(self, group_id: str, actor: str | None) -> Group:
        g = self._group(group_id)
        self._check(self.can_see_group(g, actor), f"group {group_id}")
        return g

    def hosted_group(self, group_id: str, actor: str | None) -> Group:
        """只有所有者 / 主持人能做的操作（改名、brief、项目、邀请、删除）。"""
        g = self.visible_group(group_id, actor)
        if actor is not None and g.owner_id not in (None, actor):
            raise NetworkError("只有主持人能这样做")
        return g

    def visible_conv(self, conv_id: str, actor: str | None) -> Conversation:
        c = self._conv(conv_id)
        self._check(self.can_see_conv(c, actor), f"conversation {conv_id}")
        return c

    def visible_task(self, task_id: str, actor: str | None) -> Task:
        t = self.store.tasks.get(task_id)
        self._check(t is not None and self.can_see_task(t, actor), f"task {task_id}")
        return t

    def _agents_used_by(self, actor: str) -> list[AgentNode]:
        return [a for a in self.store.agents.values() if self.can_use_agent(a, actor)]

    # ================= Agent 管理 =================
    def create_agent(self, name: str, description: str, capabilities: list[str], authority: list[str],
                     chat_enabled: bool = True, model: dict[str, str] | None = None,
                     actor: str | None = None) -> AgentNode:
        with self._lock:
            a = self._new_agent(name, description, capabilities, authority, chat_enabled, model, "human", actor)
        self.bus.emit("agent.updated", agent=a.to_dict())
        return a

    def _new_agent(self, name: str, description: str, capabilities: list[str], authority: list[str],
                   chat_enabled: bool, model: dict[str, str] | None, created_by: str,
                   owner_id: str | None = None) -> AgentNode:
        """调用方持有锁。"""
        if self.store.agent_by_name(name):
            raise NetworkError(f"Agent 名称已存在：{name}")
        a = AgentNode(id=new_id("agent"), name=name.strip(), description=description.strip(),
                      capabilities=_clean_caps(capabilities), authority=_clean_auth(authority),
                      chat_enabled=chat_enabled, model=_clean_model(model), created_by=created_by,
                      owner_id=owner_id)
        self.store.agents[a.id] = a
        self.store.save()
        return a

    def update_agent(self, agent_id: str, actor: str | None = None, **fields: Any) -> AgentNode:
        with self._lock:
            a = self.owned_agent(agent_id, actor)
            if "name" in fields and fields["name"] != a.name and self.store.agent_by_name(fields["name"]):
                raise NetworkError(f"Agent 名称已存在：{fields['name']}")
            for k, v in fields.items():
                if k == "model":  # None / {} 表示改回运行时默认模型
                    a.model = _clean_model(v)
                    continue
                if v is None:
                    continue
                if k == "authority":
                    v = _clean_auth(v)
                elif k == "capabilities":
                    v = _clean_caps(v)
                elif k not in UPDATABLE_AGENT_FIELDS:
                    raise NetworkError(f"不能修改 {k}")
                setattr(a, k, v)
            self.store.save()
        self.bus.emit("agent.updated", agent=a.to_dict())
        return a

    def delete_agent(self, agent_id: str, actor: str | None = None) -> None:
        with self._lock:
            a = self.owned_agent(agent_id, actor)
            if a.active:
                raise NetworkError("Agent 正在运行，不能删除")
            for g in self.store.groups.values():
                if agent_id in g.member_ids:
                    self._member_events(g, [], [agent_id])
                    g.member_ids.remove(agent_id)
            for c in self.store.conversations.values():
                if c.kind == "dm" and c.agent_id == agent_id:
                    c.archived = True  # 私聊记录保留，只读
            del self.store.agents[agent_id]
            self.store.save()
        shutil.rmtree(self.store.agent_dir(agent_id), ignore_errors=True)  # 私有目录随 Agent 删除
        self.bus.emit("agent.deleted", agent_id=agent_id)

    # ================= 群组管理 =================
    # 参照 Raft 的频道：群组就是一个群聊。建群组即有群聊，成员随时可以加减；成员变动作为事件出现在群聊里。
    # 内部群组：只有所有者，成员只能是所有者自己的 Agent。
    # 联合群组（参照 Raft 的 joint channel）：主持人邀请用户，接受后成为参与者；每个人只能加减自己的 Agent，
    # 主持人可以移出任何 Agent 与参与者。参与者退出 / 被移出时，他的 Agent 一并移出，之后看不到群组与群聊。
    def _check_members(self, member_ids: list[str], actor: str | None) -> list[str]:
        for m in member_ids:
            self.visible_agent(m, actor)  # 只能加入自己的 Agent
        return list(dict.fromkeys(member_ids))

    def create_group(self, name: str, description: str, member_ids: list[str], actor: str | None = None,
                     kind: str = "internal") -> Group:
        if kind not in GROUP_KINDS:
            raise NetworkError(f"未知群组类型：{kind}（可选 {' / '.join(GROUP_KINDS)}）")
        with self._lock:
            g = Group(id=new_id("group"), name=name.strip(), description=description.strip(),
                      member_ids=self._check_members(member_ids, actor), owner_id=actor, kind=kind)
            self.store.groups[g.id] = g
            conv = self._group_chat(g)
            self.store.save()
        self.bus.emit("group.updated", group=g.to_dict())
        self.bus.emit("conversation.updated", conversation=conv.to_dict(full=False))
        return g

    def update_group(self, group_id: str, name: str | None = None, description: str | None = None,
                     member_ids: list[str] | None = None, actor: str | None = None) -> Group:
        """改名、描述只有主持人能做；成员列表的增减按 add_member / remove_member 的规则逐个检查。"""
        with self._lock:
            g = self.visible_group(group_id, actor)
            if (name is not None and name.strip() != g.name) or description is not None:
                self.hosted_group(group_id, actor)
            if member_ids is not None:
                new = list(dict.fromkeys(member_ids))
                added = [m for m in new if m not in g.member_ids]
                removed = [m for m in g.member_ids if m not in new]
                self._check_members(added, actor)
                for m in removed:
                    self._check_removable(g, m, actor)
                self._member_events(g, added, removed)
                g.member_ids = new
            if name is not None and name.strip() != g.name:
                g.name = name.strip()
                self._group_chat(g).title = g.name
            if description is not None:
                g.description = description.strip()
            self.store.save()
        self.bus.emit("group.updated", group=g.to_dict())
        self._emit_group_chat(g)
        return g

    def _check_removable(self, g: Group, agent_id: str, actor: str | None) -> None:
        """调用方持有锁。主持人能移出任何成员；参与者只能移出自己的 Agent。"""
        a = self.store.agents.get(agent_id)
        if actor is None or g.owner_id in (None, actor) or (a is not None and a.owner_id == actor):
            return
        raise NetworkError(f"{self._name(agent_id)} 不是你的 Agent，只有它的所有者或主持人能把它移出")

    def add_member(self, group_id: str, agent_id: str, actor: str | None = None) -> Group:
        with self._lock:
            g = self.visible_group(group_id, actor)
            self.visible_agent(agent_id, actor)
            if agent_id in g.member_ids:
                raise NetworkError(f"{self._name(agent_id)} 已经在群组里")
            members = g.member_ids + [agent_id]
        return self.update_group(group_id, member_ids=members, actor=actor)

    def remove_member(self, group_id: str, agent_id: str, actor: str | None = None) -> Group:
        with self._lock:
            g = self.visible_group(group_id, actor)
            if agent_id not in g.member_ids:
                raise NetworkError(f"{self._name(agent_id)} 不在群组里")
            members = [m for m in g.member_ids if m != agent_id]
        return self.update_group(group_id, member_ids=members, actor=actor)

    def _member_events(self, g: Group, added: list[str], removed: list[str], why: str = "") -> None:
        """调用方持有锁。成员变动写进群聊：人看得到，之后被 @ 的成员也看得到。
        移出群组不打断它手上已经接下的任务，只是不能再被 @、不能再被委托。"""
        conv = self._group_chat(g)
        for verb, ids in (("加入了群组", added), ("离开了群组", removed)):
            for aid in ids:
                conv.messages.append(Message(id=new_id("msg"), role="event", agent_id=aid,
                                             text=f"{self._name(aid)} {verb}{why}"))
        if added or removed:
            conv.updated_at = time.time()
            if removed:
                g.home_enabled = [a for a in g.home_enabled if a not in removed]

    def _people_event(self, g: Group, text: str) -> None:
        """调用方持有锁。参与者变动写进群聊。"""
        conv = self._group_chat(g)
        conv.messages.append(Message(id=new_id("msg"), role="event", text=text))
        conv.updated_at = time.time()

    def _email(self, user_id: str | None) -> str:
        u = self.store.users.get(user_id or "")
        return u.email if u else "（未知用户）"

    def _emit_group_chat(self, g: Group) -> None:
        conv = self._group_chat_of(g.id)
        if conv is not None:
            self.bus.emit("conversation.updated", conversation=conv.to_dict(full=False))

    def delete_group(self, group_id: str, actor: str | None = None) -> None:
        with self._lock:
            self.hosted_group(group_id, actor)
            if any(c.group_id == group_id and c.status != "idle" for c in self.store.conversations.values()):
                raise NetworkError("群组内有进行中的对话，不能删除")
            del self.store.groups[group_id]
            for c in self.store.conversations.values():
                if c.group_id == group_id:
                    c.group_id, c.archived = None, True  # 群聊记录保留，只读（只有主持人看得到）
            self.store.save()
        self.bus.emit("group.deleted", group_id=group_id)

    # ---- 联合群组的参与者：邀请 → 接受；退出 / 移出 ----
    def invite(self, group_id: str, email: str, actor: str | None = None) -> Group:
        with self._lock:
            g = self.hosted_group(group_id, actor)
            if g.kind != "joint":
                raise NetworkError("内部群组只有你自己；要和别人协作请新建联合群组")
            user = next((u for u in self.store.users.values() if u.email == email.strip().lower()), None)
            if user is None:
                raise NetworkError(f"没有用户 {email.strip()}")
            if user.id in g.people:
                raise NetworkError(f"{user.email} 已经是参与者")
            if user.id in g.invited:
                raise NetworkError(f"已经邀请过 {user.email}")
            g.invited.append(user.id)
            self.store.save()
        self.bus.emit("group.invited", group_id=g.id, user_id=user.id, invitation=self.invitation_view(g))
        self.bus.emit("group.updated", group=g.to_dict())
        return g

    def invitations(self, actor: str) -> list[dict[str, Any]]:
        with self._lock:
            return [self.invitation_view(g) for g in self.store.groups.values() if actor in g.invited]

    def invitation_view(self, g: Group) -> dict[str, Any]:
        """接受之前只给群组名、描述与主持人，不给群聊和档案。"""
        return {"group_id": g.id, "name": g.name, "description": g.description, "host": self._email(g.owner_id)}

    def respond_invitation(self, group_id: str, accept: bool, actor: str) -> Group | None:
        with self._lock:
            g = self.store.groups.get(group_id)
            self._check(g is not None and actor in g.invited, f"invitation {group_id}")
            g.invited.remove(actor)
            if accept:
                g.participants.append(actor)
                self._people_event(g, f"{self._email(actor)} 加入了联合群组")
            self.store.save()
        self.bus.emit("group.updated", group=g.to_dict())
        self._emit_group_chat(g)
        return g if accept else None

    def leave_group(self, group_id: str, actor: str) -> None:
        """参与者退出：他的 Agent 一并移出（已接下的任务照常完成），之后看不到群组与群聊。主持人不能退出，只能删除。"""
        with self._lock:
            g = self.visible_group(group_id, actor)
            if g.owner_id == actor:
                raise NetworkError("主持人不能退出；不再需要这个群组可以删除它")
            self._drop_participant(g, actor, f"{self._email(actor)} 退出了联合群组")
            self.store.save()
        self.bus.emit("group.updated", group=g.to_dict())
        self.bus.emit("group.left", group_id=g.id, user_id=actor)
        self._emit_group_chat(g)

    def remove_participant(self, group_id: str, user_id: str, actor: str | None = None) -> Group:
        with self._lock:
            g = self.hosted_group(group_id, actor)
            if user_id in g.invited:  # 撤回邀请
                g.invited.remove(user_id)
            elif user_id in g.participants:
                self._drop_participant(g, user_id, f"{self._email(user_id)} 被主持人移出了联合群组")
            else:
                raise NetworkError("不是这个群组的参与者")
            self.store.save()
        self.bus.emit("group.updated", group=g.to_dict())
        self.bus.emit("group.left", group_id=g.id, user_id=user_id)
        self._emit_group_chat(g)
        return g

    def _drop_participant(self, g: Group, user_id: str, text: str) -> None:
        """调用方持有锁。"""
        theirs = [m for m in g.member_ids if (a := self.store.agents.get(m)) is not None and a.owner_id == user_id]
        self._member_events(g, [], theirs, "（所有者已退出）")
        g.member_ids = [m for m in g.member_ids if m not in theirs]
        g.participants.remove(user_id)
        self._people_event(g, text)

    # ---- 联合群组里的私有目录：默认不挂载，由 Agent 的所有者按群组打开 ----
    def set_home_mount(self, group_id: str, agent_id: str, enabled: bool, actor: str | None = None) -> Group:
        with self._lock:
            g = self.visible_group(group_id, actor)
            self.visible_agent(agent_id, actor)
            if agent_id not in g.member_ids:
                raise NetworkError(f"{self._name(agent_id)} 不在群组里")
            g.home_enabled = [a for a in g.home_enabled if a != agent_id] + ([agent_id] if enabled else [])
            self.store.save()
        self.bus.emit("group.updated", group=g.to_dict())
        return g

    def home_mounted(self, g: Group | None, agent_id: str) -> bool:
        """内部群组、私聊总是挂载；联合群组只在所有者打开时挂载。"""
        return g is None or g.kind != "joint" or agent_id in g.home_enabled

    # ================= 群组档案 =================
    # 群组的工作档案，归群组所有、不放进任何项目仓库：brief（每轮推送）、决定（推送仍然有效的）、
    # 文件（只读挂载到工作目录的 group/）、项目引用（只是指针）。写入只走人的操作或网络工具，记入 log.jsonl（不变量 17）。
    # 联合群组的档案对所有参与者可见：写进去就等于对所有参与方公开。
    def _archive_changed(self, g: Group, action: str, by_user: str | None = None, by_agent: str | None = None,
                         **detail: Any) -> None:
        """调用方持有锁。"""
        import json
        g.archive_rev += 1
        entry = {"ts": time.time(), "action": action, "by_user": by_user, "by_agent": by_agent, **detail}
        log = self.store.group_dir(g.id).parent / "log.jsonl"
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _archive_saved(self, g: Group, event: str | None = None) -> None:
        """调用方不持有锁。档案变化的公开事件写进群聊，人和之后被 @ 的成员都看得到。"""
        with self._lock:
            if event:
                self._people_event(g, event)
            self.store.save()
        self.bus.emit("group.updated", group=g.to_dict())
        self._emit_group_chat(g)

    def set_brief(self, group_id: str, text: str, actor: str | None = None) -> Group:
        text = text.strip()
        if len(text) > BRIEF_LIMIT:
            raise NetworkError(f"brief 最多 {BRIEF_LIMIT} 字")
        with self._lock:
            g = self.hosted_group(group_id, actor)
            g.brief, g.brief_updated_at = text, time.time()
            self._archive_changed(g, "brief.set", by_user=actor, size=len(text))
        self._archive_saved(g, "主持人更新了 brief")
        return g

    def confirm_brief(self, group_id: str, actor: str | None = None) -> Group:
        """brief 还准确：只刷新确认时间。"""
        with self._lock:
            g = self.hosted_group(group_id, actor)
            g.brief_updated_at = time.time()
        self._archive_saved(g)
        return g

    def suggest_brief(self, group_id: str, text: str, reason: str = "", actor: str | None = None,
                      agent_id: str | None = None) -> Group:
        text = text.strip()
        if not text or len(text) > BRIEF_LIMIT:
            raise NetworkError(f"建议的 brief 不能为空，最多 {BRIEF_LIMIT} 字")
        from .models import BriefSuggestion
        with self._lock:
            g = self.visible_group(group_id, actor)
            g.suggestions.append(BriefSuggestion(id=new_id("sugg"), text=text, reason=reason.strip()[:1000],
                                                 by_user=actor, by_agent=agent_id))
        who = self._name(agent_id) if agent_id else self._email(actor)
        self._archive_saved(g, f"{who} 建议修改 brief，等主持人决定")
        return g

    def decide_suggestion(self, group_id: str, suggestion_id: str, accept: bool, actor: str | None = None) -> Group:
        with self._lock:
            g = self.hosted_group(group_id, actor)
            sug = next((x for x in g.suggestions if x.id == suggestion_id and x.status == "pending"), None)
            self._check(sug is not None, f"suggestion {suggestion_id}")
            sug.status = "accepted" if accept else "rejected"
            if accept:
                g.brief, g.brief_updated_at = sug.text, time.time()
                self._archive_changed(g, "brief.set", by_user=actor, suggestion=sug.id, size=len(sug.text))
        self._archive_saved(g, "主持人采纳了 brief 修改建议" if accept else "主持人没有采纳 brief 修改建议")
        return g

    def record_decision(self, group_id: str, text: str, reason: str = "", supersedes: str | None = None,
                        actor: str | None = None, agent_id: str | None = None, source: str | None = None) -> Decision:
        text = text.strip()
        if not text:
            raise NetworkError("决定不能为空")
        with self._lock:
            g = self.visible_group(group_id, actor)
            old = None
            if supersedes:
                old = next((d for d in g.decisions if d.id == supersedes and d.status == "active"), None)
                if old is None:
                    raise NetworkError(f"没有仍然有效的决定 {supersedes}")
                old.status = "superseded"
            d = Decision(id=new_id("dec"), text=text[:2000], reason=reason.strip()[:2000], supersedes=supersedes,
                         by_user=actor, by_agent=agent_id, source=source)
            g.decisions.append(d)
            self._archive_changed(g, "decision.record", by_user=actor, by_agent=agent_id, decision=d.id,
                                  supersedes=supersedes)
        who = self._name(agent_id) if agent_id else self._email(actor)
        self._archive_saved(g, f"{who} 记录了决定 {d.id}：{_short(d.text, 120)}"
                               + (f"（取代 {supersedes}）" if supersedes else ""))
        return d

    def revoke_decision(self, group_id: str, decision_id: str, actor: str | None = None) -> Group:
        """人可以撤销任何决定（包括 Agent 记录的）。"""
        with self._lock:
            g = self.visible_group(group_id, actor)
            d = next((x for x in g.decisions if x.id == decision_id and x.status == "active"), None)
            self._check(d is not None, f"decision {decision_id}")
            d.status = "revoked"
            self._archive_changed(g, "decision.revoke", by_user=actor, decision=d.id)
        self._archive_saved(g, f"{self._email(actor)} 撤销了决定 {d.id}")
        return g

    def add_project(self, group_id: str, name: str, location: str, note: str = "",
                    actor: str | None = None) -> ProjectRef:
        if not name.strip() or not location.strip():
            raise NetworkError("项目名称与位置不能为空")
        with self._lock:
            g = self.hosted_group(group_id, actor)
            ref = ProjectRef(id=new_id("proj"), name=name.strip()[:120], location=location.strip()[:500],
                             note=note.strip()[:500])
            g.projects.append(ref)
            self._archive_changed(g, "project.add", by_user=actor, project=ref.id)
        self._archive_saved(g, f"主持人引用了项目「{ref.name}」")
        return ref

    def remove_project(self, group_id: str, project_id: str, actor: str | None = None) -> Group:
        with self._lock:
            g = self.hosted_group(group_id, actor)
            ref = next((r for r in g.projects if r.id == project_id), None)
            self._check(ref is not None, f"project {project_id}")
            g.projects.remove(ref)
            self._archive_changed(g, "project.remove", by_user=actor, project=ref.id)
        self._archive_saved(g, f"主持人移除了项目引用「{ref.name}」")
        return g

    def publish_file(self, group_id: str, name: str, content: bytes, actor: str | None = None,
                     agent_id: str | None = None, source: str | None = None) -> str:
        """把一份文件发布进群组档案。同名覆盖（日志里留着谁、什么时候）。返回文件名。"""
        fname = _safe_filename(name)
        if len(content) > GROUP_FILE_LIMIT:
            raise NetworkError(f"文件太大：最多 {GROUP_FILE_LIMIT // 1000} KB")
        with self._lock:
            g = self.visible_group(group_id, actor)
            (self.store.group_dir(g.id) / fname).write_bytes(content)
            self._archive_changed(g, "file.publish", by_user=actor, by_agent=agent_id, file=fname,
                                  size=len(content), source=source)
        who = self._name(agent_id) if agent_id else self._email(actor)
        self._archive_saved(g, f"{who} 发布了文件 {fname}")
        return fname

    def delete_file(self, group_id: str, name: str, actor: str | None = None) -> Group:
        with self._lock:
            g = self.hosted_group(group_id, actor)
            path = self.store.group_dir(g.id) / _safe_filename(name)
            self._check(path.is_file(), f"file {name}")
            path.unlink()
            self._archive_changed(g, "file.delete", by_user=actor, file=path.name)
        self._archive_saved(g, f"主持人删除了文件 {path.name}")
        return g

    def group_files(self, g: Group) -> list[dict[str, Any]]:
        d = self.store.group_dir(g.id)
        return [{"name": p.name, "size": p.stat().st_size, "updated_at": p.stat().st_mtime}
                for p in sorted(d.iterdir()) if p.is_file()]

    def group_file(self, group_id: str, name: str, actor: str | None = None) -> Path:
        g = self.visible_group(group_id, actor)
        path = self.store.group_dir(g.id) / _safe_filename(name)
        self._check(path.is_file(), f"file {name}")
        return path

    def group_detail(self, group_id: str, actor: str | None = None) -> dict[str, Any]:
        with self._lock:
            g = self.visible_group(group_id, actor)
            d = g.to_dict()
            d["files"] = self.group_files(g)
            d["host"] = self._email(g.owner_id)
            d["participant_emails"] = {u: self._email(u) for u in g.people}
            d["invited_emails"] = {u: self._email(u) for u in g.invited} if actor in (None, g.owner_id) else {}
            if actor not in (None, g.owner_id):
                d.pop("invited")
            d["is_host"] = actor is None or g.owner_id in (None, actor)
            return d

    # ================= 对话：群聊与私聊 =================
    # 群聊（kind=group）就是群组本身：每个群组一个，没有“主 Agent”，人在消息里 @ 谁，谁就接这个任务。
    # 被 @ 的成员在群聊里有自己的席位（独立 DSH 会话、收件箱、工作目录），自己做，或在群组里找合适的成员委托。
    # 私聊（kind=dm）是所有者和自己的一个 Agent 的会话：每个 Agent 一个，不用 @，不能委托。
    def _group_chat_of(self, group_id: str) -> Conversation | None:
        return next((c for c in self.store.conversations.values()
                     if c.kind == "group" and c.group_id == group_id and not c.archived), None)

    def _group_chat(self, g: Group) -> Conversation:
        """调用方持有锁。群组的群聊，不存在就建。"""
        conv = self._group_chat_of(g.id)
        if conv is None:
            conv = Conversation(id=new_id("conv"), kind="group", group_id=g.id, agent_id=None, title=g.name,
                                owner_id=g.owner_id)
            self.store.conversations[conv.id] = conv
        return conv

    def _dm_of(self, agent_id: str, user_id: str | None) -> Conversation | None:
        return next((c for c in self.store.conversations.values()
                     if c.kind == "dm" and c.agent_id == agent_id and c.owner_id == user_id and not c.archived), None)

    def group_chat(self, group_id: str, actor: str | None = None) -> Conversation:
        with self._lock:
            conv = self._group_chat(self.visible_group(group_id, actor))
            self.store.save()
        return conv

    def direct_chat(self, agent_id: str, actor: str | None = None) -> Conversation:
        """当前用户和一个 Agent 的私聊：第一次打开时创建。"""
        with self._lock:
            a = self.visible_agent(agent_id, actor)
            conv = self._dm_of(a.id, actor)
            created = conv is None
            if created:
                if not a.chat_enabled:
                    raise NetworkError(f"{a.name} 未开放私聊")
                conv = Conversation(id=new_id("conv"), kind="dm", group_id=None, agent_id=a.id, title=a.name,
                                    owner_id=actor)
                self.store.conversations[conv.id] = conv
                self.store.save()
        if created:
            self.bus.emit("conversation.updated", conversation=conv.to_dict(full=False))
        return conv

    def create_conversation(self, group_id: str | None = None, agent_id: str | None = None,
                            title: str = "", actor: str | None = None) -> Conversation:
        """兼容入口：群组 → 它的群聊；Agent → 当前用户和它的私聊。都不会重复创建。"""
        if group_id is not None:
            return self.group_chat(group_id, actor)
        if agent_id is not None:
            return self.direct_chat(agent_id, actor)
        raise NetworkError("群聊需要 group_id，私聊需要 agent_id")

    def clear_conversation(self, conv_id: str, actor: str | None = None) -> None:
        """清空聊天记录与所有席位（会话从头开始）。委托、知识不受影响。群聊只有主持人能清空。"""
        with self._lock:
            conv = self.visible_conv(conv_id, actor)
            if conv.kind == "group" and conv.group_id:
                self.hosted_group(conv.group_id, actor)
            if conv.status != "idle":
                raise NetworkError("对话进行中或还有进行中的委托，不能清空")
            conv.messages, conv.seats = [], {}
            conv.updated_at = time.time()
            self.store.save()
        self.bus.emit("conversation.updated", conversation=conv.to_dict(full=False))

    def delete_conversation(self, conv_id: str, actor: str | None = None) -> None:
        """只能删除已归档的对话；群聊随群组存在，私聊随 Agent 存在，要清掉记录用 clear_conversation。"""
        with self._lock:
            conv = self.visible_conv(conv_id, actor)
            if not conv.archived:
                raise NetworkError("群聊随群组存在、私聊随 Agent 存在，不能删除；可以清空聊天记录")
            del self.store.conversations[conv_id]
            self.store.save()
        self.bus.emit("conversation.deleted", conversation_id=conv_id)

    def resolve_mentions(self, conv: Conversation, text: str, explicit: list[str] | None = None) -> list[str]:
        """消息里 @ 到的群组成员（按名称匹配，最长优先），加上调用方显式给出的成员 id。私聊总是对方。"""
        if conv.kind == "dm":
            return [conv.agent_id] if conv.agent_id else []
        g = self.store.groups.get(conv.group_id or "")
        members = [self.store.agents[m] for m in self._members(conv)]
        found: list[str] = []
        rest = text
        for a in sorted(members, key=lambda a: -len(a.name)):  # 长名字优先，匹配过的部分抹掉，避免前缀误配
            pat = re.compile(rf"(?<![\w.])@{re.escape(a.name)}(?![\w-])", re.I)
            if pat.search(rest):
                found.append(a.id)
                rest = pat.sub(" ", rest)
        for mid in explicit or []:
            if any(a.id == mid for a in members) and mid not in found:
                found.append(mid)
            elif not any(a.id == mid for a in members):
                raise NetworkError(f"{self._name(mid)} 不是群组「{g.name if g else ''}」的成员")
        return found

    def send_message(self, conv_id: str, text: str, mentions: list[str] | None = None,
                     background: bool = True, actor: str | None = None) -> Message:
        """人发消息。群聊里只唤醒被 @ 的成员；没 @ 任何人的消息留在对话里，之后被 @ 的成员能看到。
        联合群组里任何参与者都能 @ 任何成员；被 @ 的 Agent 仍以它的所有者的名义、用所有者的资源运行。"""
        with self._lock:
            conv = self.visible_conv(conv_id, actor)
            if conv.archived:
                raise NetworkError("这个对话已归档（群组或 Agent 已被删除，或是旧版本遗留的对话），只读")
            if conv.kind == "dm":
                peer = self._agent(conv.agent_id or "")
                if not peer.chat_enabled:
                    raise NetworkError(f"{peer.name} 未开放直接对话")
                if not self.can_use_agent(peer, actor):
                    raise NetworkError(f"{peer.name} 已不再对你开放")
            targets = self.resolve_mentions(conv, text, mentions)
            msg = Message(id=new_id("msg"), role="user", text=text.strip(), mentions=targets, user_id=actor)
            conv.messages.append(msg)
            for aid in targets:
                seat = self._seat(conv, aid)
                if seat.blocked:  # 上一轮失败后再次被 @：同一会话接着做，不让人重发原需求
                    self._unblock(seat)
                seat.delegations = 0  # 委托次数按“一次被 @ 引出的工作”计
                seat.inbox.append({"kind": "mention", "message_id": msg.id})
            conv.updated_at = time.time()
            self.store.save()
        self.bus.emit("message.added", conversation_id=conv.id, message=_msg(msg))
        for aid in targets:
            self._kick("seat", seat_id(conv.id, aid))
        if not background:
            self.wait_quiet()
        return msg

    def _seat(self, conv: Conversation, agent_id: str) -> Seat:
        """调用方持有锁。席位第一次被用到时创建。"""
        seat = conv.seats.get(agent_id)
        if seat is None:
            workdir = self.store.seat_dir(conv.id, agent_id if conv.kind == "group" else None)
            seat = Seat(agent_id=agent_id, workdir=str(workdir.relative_to(self.store.root)))
            conv.seats[agent_id] = seat
        return seat

    def wait_quiet(self, timeout: float = 60) -> None:
        """等到网络里没有任何执行上下文在运行（收件箱里的工作都已处理）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._running:
                    return
            time.sleep(0.02)
        raise TimeoutError(f"网络未在 {timeout}s 内静止：{sorted(self._running)}")

    def wait_idle(self, conv_id: str, timeout: float = 60) -> Conversation:
        self.wait_quiet(timeout)
        return self._conv(conv_id)

    # ================= 调度：收件箱 → 一轮 =================
    # 执行上下文有两种：席位（对话里的一个 Agent，id = 对话/Agent）和委托任务（id = task-…）。
    def _holder(self, kind: str, cid: str) -> Seat | Task | None:
        if kind == "task":
            return self.store.tasks.get(cid)
        conv_id, _, agent_id = cid.partition("/")
        conv = self.store.conversations.get(conv_id)
        return conv.seats.get(agent_id) if conv else None

    def _eligible(self, kind: str, cid: str) -> bool:
        """调用方持有锁。收件箱有内容，且上下文处于可以运行的状态。"""
        h = self._holder(kind, cid)
        if h is None or not h.inbox:
            return False
        if isinstance(h, Seat):
            return not h.blocked and h.agent_id in self.store.agents
        return h.status == TaskStatus.RUNNING

    def _kick(self, kind: str, cid: str) -> None:
        """收件箱有内容就跑一轮；同一上下文同时只跑一轮，运行中到达的内容等这一轮结束再送达。"""
        with self._lock:
            if cid in self._running or not self._eligible(kind, cid):
                return
            self._running.add(cid)
        self._refresh_conv(self._conv_id_of(kind, cid))
        self._pool.submit(self._round_safe, kind, cid)

    def _round_safe(self, kind: str, cid: str) -> None:
        try:
            if kind == "seat":
                conv_id, _, agent_id = cid.partition("/")
                self._seat_round(self.store.conversations[conv_id], agent_id)
            else:
                self._task_round(self.store.tasks[cid])
        except Exception as e:  # noqa: BLE001 —— 引擎缺陷也要落到对话或任务里
            self._internal_error(kind, cid, e)
        finally:
            with self._lock:  # 判断与出队放在同一把锁里，wait_quiet 不会在两轮之间误判为静止
                again = self._eligible(kind, cid)
                if not again:
                    self._running.discard(cid)
            if again:
                self._pool.submit(self._round_safe, kind, cid)
            self._refresh_conv(self._conv_id_of(kind, cid))

    def _internal_error(self, kind: str, cid: str, e: Exception) -> None:
        if kind == "seat":
            conv_id, _, agent_id = cid.partition("/")
            conv = self.store.conversations[conv_id]
            with self._lock:
                conv.seats[agent_id].blocked = f"网络内部错误：{e!r}"
                owner = self.store.agents[agent_id].owner_id if agent_id in self.store.agents else None
            self._append(conv, Message(id=new_id("msg"), role="system", agent_id=agent_id,
                                       text=f"（{self._name(agent_id)}：网络内部错误 {e!r}）",
                                       audience=[owner] if owner else []))
        else:
            self._fail_task(self.store.tasks[cid], f"网络内部错误：{e!r}")

    def _conv_id_of(self, kind: str, cid: str) -> str | None:
        if kind == "seat":
            return cid.partition("/")[0]
        t = self.store.tasks.get(cid)
        return t.conversation_id if t else None

    def _refresh_conv(self, conv_id: str | None) -> None:
        """对话状态：thinking（有席位在跑一轮）> waiting（还有进行中的委托）> idle。"""
        with self._lock:
            conv = self.store.conversations.get(conv_id or "")
            if conv is None:
                return
            if any(seat_id(conv.id, a) in self._running for a in conv.seats):
                status = "thinking"
            elif any(t.conversation_id == conv.id and t.status == TaskStatus.RUNNING
                     for t in self.store.tasks.values()):
                status = "waiting"
            else:
                status = "idle"
            if status == conv.status:
                return
            conv.status = status
            self.store.save()
        self.bus.emit("conversation.updated", conversation=conv.to_dict(full=False))

    def _notify_delegator(self, task: Task, kind: str) -> None:
        """把委托回报放进委托方的收件箱：委托方在对话里的席位，或上一级任务。"""
        item = {"kind": kind, "task_id": task.id}
        with self._lock:
            if task.parent_task_id:
                hk, hid = "task", task.parent_task_id
            else:
                hk, hid = "seat", seat_id(task.conversation_id, task.delegator_id)
            holder = self._holder(hk, hid)
            if holder is None:
                return
            holder.inbox.append(item)
            self.store.save()
        self._kick(hk, hid)

    def _drain(self, holder: Seat | Task) -> list[dict[str, Any]]:
        with self._lock:
            items, holder.inbox = list(holder.inbox), []
            self.store.save()
        return items

    def _requeue(self, holder: Seat | Task, items: list[dict[str, Any]]) -> None:
        """这一轮失败：没处理完的通知放回收件箱，下次运行时再送达。@ 与对话消息由 seen 游标保证不丢。"""
        keep = [i for i in items if i["kind"] not in ("mention", "start")]
        with self._lock:
            holder.inbox = keep + holder.inbox
            self.store.save()

    # ================= 一轮：对话里的席位 =================
    def capabilities(self) -> RuntimeCapabilities:
        """当前运行时的能力。运行时没实现 capabilities() 时按全部可强制处理（保持旧行为）。"""
        get = getattr(self.runtime, "capabilities", None)
        return get() if callable(get) else RuntimeCapabilities.all_enforced(getattr(self.runtime, "name", "runtime"))

    def _effective(self, agent: AgentNode, authority: list[str]) -> tuple[list[str], dict[str, str]]:
        """有效权限 = 自身权限 ∩ 运行时能强制的部分；返回 (有效权限, 被收窄的权限与原因)。
        委托这一侧再交委托授予，见 _tool_delegate。"""
        owned = list(dict.fromkeys(authority))
        return self.capabilities().effective(owned), self.capabilities().narrowed(owned)

    def _seat_round(self, conv: Conversation, agent_id: str) -> None:
        agent = self._agent(agent_id)
        seat = conv.seats[agent_id]
        group = self.store.groups.get(conv.group_id) if conv.group_id else None
        items = self._drain(seat)
        sid = seat_id(conv.id, agent_id)
        ctx = ExecContext(kind="seat", id=sid, agent_id=agent.id, conversation_id=conv.id,
                          group_id=group.id if group else None, authority=self._effective(agent, agent.authority)[0],
                          constraints=[], depth=0, chain=[agent.id])
        with self._lock:
            upto = len(conv.messages)
            fresh = conv.messages[seat.seen:upto]  # 上次运行之后对话里的新消息：公开上下文
            shared = [k for k in self.store.knowledge.values()
                      if sid in k.visible_to and k.key not in seat.known and k.owner_task_id not in self._own_tasks(sid)]
            fresh = [m for m in fresh if not m.audience or agent.owner_id in m.audience]  # 只给别人看的不送
            ctx.addressed = [m.id for m in fresh if m.role == "user" and agent.id in m.mentions]
            archive_rev = group.archive_rev if group else -1
            archive = self._archive_text(group) if group and seat.archive_seen != archive_rev else ""
        joint = bool(group and group.kind == "joint")
        transcript = format_transcript(fresh, agent.id, self._name, self._email if joint else None)
        notices, wake = self._render_inbox(items, ctx)
        with self._lock:
            open_reqs = [r for r in self.store.requests.values() if r.conversation_id == conv.id
                         and r.agent_id == agent.id and r.status in ("in_progress", "in_review")]
        if open_reqs:
            notices = "\n\n".join(filter(None, [notices, format_open_requests(open_reqs)]))
        if shared:
            notices = "\n\n".join(filter(None, [notices, format_shared_knowledge(shared)]))
        first = seat.dsh_session_id is None
        mount_home = self.home_mounted(group, agent.id)
        prompt = build_seat_prompt(agent, conv, group, ctx.can_delegate, transcript, notices, first,
                                   home_writable="file.write" in ctx.authority, archive=archive,
                                   home=mount_home, joint=joint)
        res = self._run(ctx, agent, prompt, self.store.root / seat.workdir, seat, self.limits.turn_timeout_s,
                        mount_home=mount_home)
        steps = self._live.pop(sid, [])
        owner_only = [agent.owner_id] if agent.owner_id else []
        if res.ok:
            with self._lock:
                seat.seen = upto
                seat.known = _dedup(seat.known + [k.key for k in shared])
                seat.archive_seen = archive_rev
            self._auto_accept(ctx)
            self._append(conv, Message(id=new_id("msg"), role="agent", agent_id=agent.id,
                                       text=res.final_text.strip() or "（空回复）", task_ids=ctx.created,
                                       steps=steps, wake=wake, private_to=owner_only))
            return
        self._requeue(seat, items)
        with self._lock:
            seat.blocked = res.error or "运行失败"
            if seat.dsh_session_id:  # 消息已经进了会话：续接时靠会话历史，不重复送；没拿到会话则下次重发
                seat.seen = upto
            tasks = [t for t in self.store.tasks.values() if t.conversation_id == conv.id and t.delegator_id == agent.id]
            knowledge = [k for k in self.store.knowledge.values() if sid in k.visible_to]
        text = format_failure(agent.name, res.error or "", res.error_class or "runtime_error", tasks, knowledge,
                              self._name)
        self._append(conv, Message(id=new_id("msg"), role="system", agent_id=agent.id, text=text,
                                   task_ids=ctx.created, steps=steps, wake=wake, audience=owner_only,
                                   private_to=owner_only))

    def _own_tasks(self, sid: str) -> set[str]:
        """调用方持有锁。这个席位直接发起的任务：它们的结果是委托回报，不算“别人共享的知识”。"""
        conv_id, _, agent_id = sid.partition("/")
        return {t.id for t in self.store.tasks.values()
                if t.conversation_id == conv_id and t.delegator_id == agent_id and t.parent_task_id is None}

    def _append(self, conv: Conversation, msg: Message) -> None:
        with self._lock:
            conv.messages.append(msg)
            conv.updated_at = time.time()
            self.store.save()
        self.bus.emit("message.added", conversation_id=conv.id, message=_msg(msg))
        self.bus.emit("conversation.updated", conversation=conv.to_dict(full=False))
    # ================= 一轮：委托任务 =================
    def _task_round(self, task: Task) -> None:
        agent = self.store.agents.get(task.delegatee_id or "")
        delegator = self.store.agents.get(task.delegator_id)
        g = self.store.groups.get(task.group_id)
        if agent is None or delegator is None or g is None:
            self._drain(task)
            self._fail_task(task, "执行者、委托方或群组已不存在")
            return
        items = self._drain(task)
        ctx = self._task_ctx(task, agent, g)
        inbox, _ = self._render_inbox(items, ctx)
        home_writable = "file.write" in ctx.authority
        mount_home = self.home_mounted(g, agent.id)
        workdir = self.store.task_dir(task.id)
        with self._lock:
            archive_rev = g.archive_rev
            archive = self._archive_text(g) if task.archive_seen != archive_rev else ""
        if task.dsh_session_id is None:  # 第一次运行（或在拿到会话之前就失败了）：发完整委托
            knowledge = self.visible_knowledge(ctx)
            task.prompt = build_task_prompt(task, agent, delegator, g, ctx.can_delegate, knowledge, home_writable,
                                            archive=archive, home=mount_home)
            workdir = self._write_task_files(task, knowledge)
            prompt = task.prompt + (f"\n\n{inbox}" if inbox else "")
        else:
            prompt = build_notice_prompt("\n\n".join(filter(None, [inbox, archive])))
        res = self._run(ctx, agent, prompt, workdir, task, self.limits.task_timeout_s, mount_home=mount_home)
        if res.ok:
            with self._lock:
                task.archive_seen = archive_rev
        self._live.pop(task.id, None)
        violations = _violations(res.tool_calls, ctx.authority)
        with self._lock:
            task.violations = _dedup(task.violations + violations)
            task.evidence += [{"tool": c["tool"], "input": _short(c["input"], 300), "status": c["status"]}
                              for c in res.tool_calls]
        for v in violations:
            self.bus.emit("authority.violation", task_id=task.id, agent_id=agent.id, detail=v)
        if not res.ok:
            self._requeue(task, items)
            self._fail_task(task, res.error or "运行失败")
            return
        self._auto_accept(ctx)
        text = res.final_text.strip()
        if self._waiting_on(task):
            # 还有子任务或提议没有回音：这一轮只是进展，等回报唤醒后再交付
            with self._lock:
                if text:
                    task.notes.append(text)
            self._save_task(task)
            return
        if not text:
            self._fail_task(task, "执行者没有交回任何内容")
            return
        self._deliver(task, agent, g, text)

    def _task_ctx(self, task: Task, agent: AgentNode, g: Group) -> ExecContext:
        ctx = ExecContext(kind="task", id=task.id, agent_id=agent.id, conversation_id=task.conversation_id,
                          group_id=g.id, authority=list(task.granted_authority),
                          constraints=list(task.effective_constraints), depth=task.depth,
                          chain=self._chain(task) + [agent.id])
        if ctx.depth >= self.limits.max_depth and "delegate" in ctx.authority:
            ctx.authority.remove("delegate")  # 到达深度上限：不再挂载网络工具
        return ctx

    def _waiting_on(self, task: Task) -> bool:
        with self._lock:
            return any(t.parent_task_id == task.id and t.status == TaskStatus.RUNNING
                       for t in self.store.tasks.values()) or \
                any(p.context_id == task.id and p.status == "pending" for p in self.store.proposals.values())

    def _deliver(self, task: Task, agent: AgentNode, g: Group, text: str) -> None:
        key = f"{task.id}:result"
        with self._lock:
            old = self.store.knowledge.get(key)
            delegator_ctx = task.parent_task_id or seat_id(task.conversation_id, task.delegator_id)
            visible = _dedup((old.visible_to if old else []) + [task.id, delegator_ctx])
            self.store.knowledge[key] = KnowledgeItem(key=key, title=f"{agent.name}：{_first_line(text)[:60]}",
                                                      content=text, group_id=g.id, owner_task_id=task.id,
                                                      visible_to=visible)
            task.result = {"summary": _first_line(text)[:200], "report": text, "knowledge_key": key}
            task.status, task.error, task.delivered_at = TaskStatus.IN_REVIEW, None, time.time()
        self._save_task(task)
        self._notify_delegator(task, "delivered")

    def _fail_task(self, task: Task, error: str) -> None:
        with self._lock:
            task.status, task.error, task.finished_at = TaskStatus.FAILED, error, time.time()
        self._save_task(task)
        self._notify_delegator(task, "failed")

    def _auto_accept(self, ctx: ExecContext) -> None:
        """这一轮送达、委托方却既没接受也没退回的交付：一轮正常结束即视为接受（提示词里写明了）。"""
        for tid in ctx.wake:
            t = self.store.tasks.get(tid)
            if t is not None and t.status == TaskStatus.IN_REVIEW:
                self._complete(t, auto=True)

    def _complete(self, task: Task, auto: bool = False) -> None:
        with self._lock:
            task.status, task.finished_at, task.auto_accepted = TaskStatus.COMPLETED, time.time(), auto
        self._save_task(task)

    # ================= 收件箱渲染 =================
    def _render_inbox(self, items: list[dict[str, Any]], ctx: ExecContext) -> tuple[str, list[str]]:
        """收件箱 → 提示词文本，以及唤醒摘要（展示在回复上）。"""
        blocks, wake = [], []
        for it in items:
            kind = it["kind"]
            if kind in ("mention", "start"):  # @ 的内容已在对话新消息里；委托内容已在任务提示词里
                continue
            elif kind == "changes":
                blocks.append(f"## 委托方要求修改（第 {it['round']} 次）\n{it['feedback']}\n\n"
                              "在原有工作基础上修改，然后重新交付完整结果。")
            elif kind == "retry":
                blocks.append(retry_prompt(it.get("note"), it.get("error")))
            elif kind in ("delivered", "failed"):
                t = self.store.tasks.get(it["task_id"])
                if t is None:
                    continue
                name = self._name(t.delegatee_id)
                if kind == "delivered" and t.status == TaskStatus.IN_REVIEW:
                    blocks.append(format_delivery(t, name))
                    ctx.wake.append(t.id)
                    wake.append(f"{name} 交付了 {t.id}")
                elif kind == "failed" and t.status == TaskStatus.FAILED:
                    blocks.append(format_failure_notice(t, name, self.limits.max_retries))
                    wake.append(f"{name} 的 {t.id} 失败")
            elif kind == "request_changes":
                r = self.store.requests.get(it["request_id"])
                if r is not None and r.status == "in_progress":
                    blocks.append(format_request_changes(r, self._who(r.requester_id).strip()))
                    wake.append(f"{self._who(r.requester_id).strip()} 退回了 {r.id}")
            elif kind == "proposal":
                p = self.store.proposals.get(it["proposal_id"])
                if p is None:
                    continue
                blocks.append(format_proposal_notice(p, self._name(p.agent_id) if p.agent_id else ""))
                wake.append(f"提议 {p.name} 已{'批准' if p.status == 'approved' else '拒绝'}")
        return "\n\n".join(blocks), wake

    # ================= 运行时调用 =================
    def _run(self, ctx: ExecContext, agent: AgentNode, prompt: str, workdir: Path, holder: Seat | Task,
             timeout_s: float, mount_home: bool = True) -> RunResult:
        """跑一轮；可恢复的运行时错误（如工具参数 JSON 损坏）用同一个会话自动续接一次。"""
        others, hidden = self._ledger_for(ctx)
        if others or hidden:  # 工作台账：同时在做的其他工作（联合群组里不列出别处的细节）
            prompt = f"{prompt}\n\n{format_ledger(others, hidden)}"
        self._mount_group_files(ctx, workdir)
        before = self._mount_home(agent, workdir) if mount_home else None
        on_session = lambda sid: setattr(holder, "dsh_session_id", sid)  # noqa: E731
        try:
            res = self._invoke(ctx, agent, prompt, workdir, holder.dsh_session_id, timeout_s, on_session)
            holder.dsh_session_id = res.session_id or holder.dsh_session_id
            if res.retryable:
                self.bus.emit("run.retry", context=ctx.kind, context_id=ctx.id, agent_id=agent.id,
                              error_class=res.error_class, error=_short(res.error, 300))
                again = retry_prompt(None, res.error) if holder.dsh_session_id else prompt
                res2 = self._invoke(ctx, agent, again, workdir, holder.dsh_session_id, timeout_s, on_session)
                holder.dsh_session_id = res2.session_id or holder.dsh_session_id
                res2.tool_calls = res.tool_calls + res2.tool_calls
                res = res2
        finally:
            shutil.rmtree(workdir / GROUP_FILES, ignore_errors=True)  # 只读副本：运行里的改动一律丢弃
            if before is not None:
                conflicts = self._sync_home(agent, workdir, before, ctx.id)
                if conflicts:
                    self.bus.emit("home.conflict", agent_id=agent.id, context_id=ctx.id, files=conflicts)
            else:
                shutil.rmtree(workdir / HOME_DIR, ignore_errors=True)
        return res

    def _mount_group_files(self, ctx: ExecContext, workdir: Path) -> None:
        """群组档案的文件区拷进工作目录的 group/（每轮刷新）。发布只能经 publish_file。"""
        mount = workdir / GROUP_FILES
        shutil.rmtree(mount, ignore_errors=True)
        g = self.store.groups.get(ctx.group_id or "")
        if g is not None:
            shutil.copytree(self.store.group_dir(g.id), mount, symlinks=False)

    def _archive_text(self, g: Group) -> str:
        """调用方持有锁。"""
        return format_archive(g, self._active_decisions(g), [f["name"] for f in self.group_files(g)],
                              self._name, self._email)

    @staticmethod
    def _active_decisions(g: Group) -> list[Decision]:
        return [d for d in g.decisions if d.status == "active"]

    # ================= Agent 私有目录 =================
    # 每个 Agent 一个跨对话、跨任务持久的目录（data/agents/<id>/）。网络不读、不注入，只负责让它在每次运行时出现在
    # 工作目录的 agent-home/ 下。沙箱只允许写工作目录，所以采用“运行前拷入、运行后按文件同步回去”。
    # 同一 Agent 会并行运行（多人同时找它）：写回时按文件三方比较——这次运行改了、而 home 里同一文件在这期间
    # 也被别的运行改了，就把这次的版本另存为冲突副本，不覆盖别人的修改。
    def agent_home(self, agent_id: str) -> Path:
        return self.store.agent_dir(agent_id)

    def _mount_home(self, agent: AgentNode, workdir: Path) -> HomeMount:
        mount, home = workdir / HOME_DIR, self.agent_home(agent.id)
        with self._home_lock(agent.id):
            shutil.rmtree(mount, ignore_errors=True)
            shutil.copytree(home, mount, symlinks=False)
            return HomeMount(base=_fingerprint(mount), home=_fingerprint(home))

    def _sync_home(self, agent: AgentNode, workdir: Path, before: HomeMount, ctx_id: str = "") -> list[str]:
        """写回；返回产生的冲突副本（相对路径）。"""
        mount, home = workdir / HOME_DIR, self.agent_home(agent.id)
        if not mount.is_dir():
            return []
        conflicts: list[str] = []
        with self._home_lock(agent.id):
            after, now = _fingerprint(mount), _fingerprint(home)
            for rel, fp in after.items():
                if before.base.get(rel) == fp:
                    continue  # 这次运行没改
                dest = rel
                if now.get(rel) != before.home.get(rel):  # 别的运行在这期间也改过（或删过 / 新建过）
                    p = Path(rel)
                    dest = str(p.with_name(f"{p.stem}.conflict-{_safe(ctx_id)[-24:]}{p.suffix}"))
                    conflicts.append(dest)
                (home / dest).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(mount / rel, home / dest)
            for rel in before.base.keys() - after.keys():
                if now.get(rel) == before.home.get(rel):  # 别人没动过才删
                    (home / rel).unlink(missing_ok=True)
            shutil.rmtree(mount, ignore_errors=True)
        return conflicts

    def _home_lock(self, agent_id: str) -> threading.Lock:
        with self._lock:
            return self._home_locks.setdefault(agent_id, threading.Lock())

    # ================= 工作台账 =================
    # 同一个 Agent 可以同时做多件事（不限并发）。每次运行前告诉它“你同时还在做什么”，
    # 让它避免改动别的工作的产物、不把别的结论当成这次的输入。看板与 Agent 详情也展示这份台账。
    # 信任域（不变量 16）：联合群组里的运行只列出同一群组里的条目，其余只给数量——否则别的参与者 @ 它就能问出
    # 所有者在别处做什么。
    def _ledger_for(self, ctx: ExecContext) -> tuple[list[dict[str, Any]], int]:
        items = self.ledger(ctx.agent_id, exclude=ctx.id)
        g = self.store.groups.get(ctx.group_id or "")
        if g is None or g.kind != "joint":
            return items, 0
        same = [it for it in items if it.get("group_id") == g.id]
        return same, len(items) - len(same)

    def ledger(self, agent_id: str, exclude: str | None = None) -> list[dict[str, Any]]:
        out = []
        with self._lock:
            for ctx in self._contexts.values():
                if ctx.agent_id != agent_id or ctx.id == exclude or any(o["context_id"] == ctx.id for o in out):
                    continue
                conv = self.store.conversations.get(ctx.conversation_id)
                owner = self.store.users.get(conv.owner_id or "") if conv else None
                item = {"context_id": ctx.id, "kind": ctx.kind, "conversation_id": ctx.conversation_id,
                        "group_id": ctx.group_id, "user": owner.email if owner else None, "since": ctx.started_at}
                if ctx.kind == "task":
                    t = self.store.tasks.get(ctx.id)
                    item.update(what=_short(t.delegation.mission, 80) if t else "",
                                for_agent=self._name(t.delegator_id) if t else "")
                else:
                    item.update(what=("私聊" if conv and conv.kind == "dm" else f"群聊「{conv.title}」") if conv else "")
                out.append(item)
        return out

    def _invoke(self, ctx: ExecContext, agent: AgentNode, prompt: str, workdir: Path, session_id: str | None,
                timeout_s: float, on_session: Any = None) -> RunResult:
        token = secrets.token_urlsafe(18)
        tools = None
        if ctx.tools:  # 席位有用户任务工具；群组里有档案工具；协作工具（委托等）另外要求 delegate
            tools = ToolBridge(url=self.tool_url, token=token,
                               handler=lambda name, args: self.handle_tool(token, name, args),
                               timeout_s=self.limits.task_timeout_s, names=ctx.tools)
        # 审批通道总是挂载：Agent 要在工作目录以外操作时（dsh 沙箱升权），向它的所有者申请
        approvals = ApprovalBridge(url=self.approval_url, token=token,
                                   handler=lambda tool, reason: self.request_approval(token, tool, reason),
                                   timeout_s=self.limits.approval_timeout_s)
        with self._lock:
            self._contexts[token] = ctx
            self._live[ctx.id] = []
            agent.active += 1
        self.bus.emit("agent.updated", agent=agent.to_dict())
        self.bus.emit("runtime.invoke", context=ctx.kind, context_id=ctx.id, agent_id=agent.id,
                      resumed=bool(session_id), authority=ctx.authority, network_tools=tools is not None,
                      model=agent.model)

        def on_event(ev: dict[str, Any]) -> None:
            t = ev.get("type")
            if t == "session" and ev.get("sessionId") and on_session:
                on_session(ev["sessionId"])
            elif t == "tool_call":
                self.bus.emit("tool.call", context=ctx.kind, context_id=ctx.id, agent_id=agent.id,
                              tool=ev.get("tool"), input=_short(ev.get("input"), 160))
            step = _step(ev)
            if step is None:
                return
            with self._lock:
                live = self._live.setdefault(ctx.id, [])
                if step["kind"] == "result":  # 工具结果只更新对应调用的状态
                    for s in reversed(live):
                        if s.get("call_id") == step["call_id"]:
                            s["status"] = step["status"]
                            break
                    else:
                        return
                else:
                    live.append(step)
                    del live[:-MAX_STEPS]
            self.bus.emit("run.progress", context=ctx.kind, context_id=ctx.id, agent_id=agent.id,
                          conversation_id=ctx.conversation_id, step=step)

        try:
            res = self.runtime.run(RunRequest(agent_id=agent.id, context_id=ctx.id, prompt=prompt, cwd=workdir,
                                              authority=ctx.authority, session_id=session_id, tools=tools,
                                              on_event=on_event, timeout_s=timeout_s, model=agent.model,
                                              approvals=approvals))
        except Exception as e:  # noqa: BLE001
            res = RunResult(session_id or "", "", ok=False, error=f"runtime error: {e!r}")
        finally:
            with self._lock:
                self._contexts.pop(token, None)
                agent.active = max(0, agent.active - 1)
            self.bus.emit("agent.updated", agent=agent.to_dict())
        return res

    # ================= 网络工具（MCP） =================
    def handle_tool(self, token: str, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        ctx = self._contexts.get(token)
        if ctx is None:
            return "执行上下文无效或已结束", True
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return f"未知工具 {name}", True
        if name not in ctx.tools:
            return (f"{name} 只能在对话里使用" if name in REQUEST_TOOLS else
                    f"{name} 只能在群组里使用" if name in ARCHIVE_TOOLS else f"{name} 需要 delegate 权限"), True
        try:
            return handler(self, ctx, args)
        except NetworkError as e:
            return str(e), True

    # ================= 工作区外溢审批 =================
    # dsh 沙箱：没有 file.write 是只读，有则只能写工作目录（和平台临时目录）。工作目录以外的写被拒后，模型可以带理由
    # 申请升权重试同一调用一次；dsh 把申请交给审批应答者——我们挂载的插件把它转到这里，等 Agent 的所有者决定。
    # 获批只对那一次调用生效（以 danger-full-access 运行），不是放行某个路径。读不受沙箱限制。
    def request_approval(self, token: str, tool: str, reason: str) -> str:
        """返回 dsh 的结果词：allowed-once | rejected | cancelled | unavailable。阻塞到所有者决定或超时。"""
        ctx = self._contexts.get(token)
        if ctx is None:
            return "unavailable"
        agent = self.store.agents.get(ctx.agent_id)
        if agent is None:
            return "unavailable"
        conv = self.store.conversations.get(ctx.conversation_id)
        ap = Approval(id=new_id("appr"), agent_id=agent.id, owner_id=agent.owner_id, context_id=ctx.id,
                      conversation_id=ctx.conversation_id, tool=str(tool or ""), reason=str(reason or "")[:2000],
                      requested_by=conv.owner_id if conv else None)
        done = threading.Event()
        with self._lock:
            self.store.approvals[ap.id] = ap
            self._approval_waits[ap.id] = done
            self.store.save()
        self.bus.emit("approval.updated", approval=ap.to_dict())
        if conv is not None:
            # 申请理由可能带着所有者的内部信息：联合群组里只给所有者看
            self._append(conv, Message(id=new_id("msg"), role="system", agent_id=agent.id,
                                       text=f"{agent.name} 申请在工作目录以外操作，等 {self._email(ap.owner_id)} 批准："
                                            f"{_short(ap.reason, 200)}",
                                       audience=[ap.owner_id] if ap.owner_id else []))
        if not done.wait(self.limits.approval_timeout_s):
            with self._lock:
                if ap.status == "pending":
                    ap.status, ap.note, ap.decided_at = "cancelled", "等待超时", time.time()
                    self.store.save()
            self.bus.emit("approval.updated", approval=ap.to_dict())
        with self._lock:
            self._approval_waits.pop(ap.id, None)
        return {"allowed": "allowed-once", "rejected": "rejected"}.get(ap.status, "cancelled")

    def decide_approval(self, approval_id: str, allow: bool, actor: str | None = None, note: str = "") -> Approval:
        """只有 Agent 的所有者能审批（actor=None 为系统 / 测试）。"""
        with self._lock:
            ap = self.store.approvals.get(approval_id)
            self._check(ap is not None and (actor is None or ap.owner_id in (None, actor)), f"approval {approval_id}")
            if ap.status != "pending":
                raise NetworkError(f"这个申请已经{APPROVAL_CN.get(ap.status, ap.status)}")
            ap.status, ap.note, ap.decided_at = ("allowed" if allow else "rejected"), note.strip(), time.time()
            self.store.save()
            done = self._approval_waits.get(ap.id)
        if done is not None:
            done.set()
        self.bus.emit("approval.updated", approval=ap.to_dict())
        return ap

    def pending_approvals(self, actor: str | None) -> list[Approval]:
        with self._lock:
            return [a for a in self.store.approvals.values()
                    if a.status == "pending" and (actor is None or a.owner_id in (None, actor))]

    def _require_group(self, ctx: ExecContext, delegate: bool = True) -> Group:
        if delegate and not ctx.can_delegate:
            raise NetworkError("当前执行上下文没有委托能力（不在群组中或没有 delegate 权限）")
        g = self.store.groups.get(ctx.group_id or "")
        if g is None:
            raise NetworkError("群组已不存在")
        return g

    # ---- 群组档案工具：群组成员都有，不需要 delegate。联合群组里写进去的内容对所有参与方可见 ----
    def _tool_record_decision(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        g = self._require_group(ctx, delegate=False)
        sup = str(args.get("supersedes", "")).strip() or None
        d = self.record_decision(g.id, str(args.get("text", "")), str(args.get("reason", "")), sup,
                                 agent_id=ctx.agent_id, source=ctx.id)
        return f"已记录决定 {d.id}，群组所有成员此后都会看到。", False

    def _tool_list_decisions(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        g = self._require_group(ctx, delegate=False)
        everything = str(args.get("include_inactive", "")).lower() in ("true", "1", "yes")
        with self._lock:
            ds = [d for d in g.decisions if everything or d.status == "active"]
        if not ds:
            return "群组档案里还没有决定。", False
        return "\n".join(format_decision(d, self._name, self._email) for d in ds), False

    def _tool_publish_file(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        """发布当前工作目录里的一个文件（不是 group/ 或 agent-home/ 下的）。"""
        g = self._require_group(ctx, delegate=False)
        holder = self._holder(ctx.kind, ctx.id)
        root = (self.store.root / holder.workdir) if isinstance(holder, Seat) else self.store.task_dir(ctx.id)
        rel = str(args.get("path", "")).strip()
        src = (root / rel).resolve()
        top = Path(rel).parts[0] if Path(rel).parts else ""
        if not rel or root.resolve() not in src.parents or top in (GROUP_FILES, HOME_DIR) or not src.is_file():
            return f"找不到 {rel}：只能发布当前工作目录里的文件（不含 {GROUP_FILES}/ 与 {HOME_DIR}/）", True
        name = self.publish_file(g.id, str(args.get("name", "")).strip() or src.name, src.read_bytes(),
                                 agent_id=ctx.agent_id, source=ctx.id)
        return f"已发布到群组档案：{GROUP_FILES}/{name}。群组所有成员此后都能读到。", False

    def _tool_propose_brief(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        g = self._require_group(ctx, delegate=False)
        self.suggest_brief(g.id, str(args.get("text", "")), str(args.get("reason", "")), agent_id=ctx.agent_id)
        return "已提交 brief 修改建议，主持人决定前 brief 不变。", False

    def _tool_list_members(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        g = self._require_group(ctx)
        lines = [f"群组「{g.name}」成员（你自己除外）："]
        for mid in g.member_ids:
            a = self.store.agents.get(mid)
            if a is None or a.id == ctx.agent_id:
                continue
            note = "；已在本委托链上，不能再委托" if a.id in ctx.chain else ""
            whose = f" | 属于 {self._email(a.owner_id)}" if g.kind == "joint" else ""
            lines.append(f"- {a.name}：{a.description} | 能力：{', '.join(a.capabilities)} | "
                         f"自身权限：{', '.join(a.authority) or '无'} | {a.status}{whose}{note}")
        if len(lines) == 1:
            lines.append("（没有其他成员）")
        lines.append(f"你当前的有效权限：{', '.join(ctx.authority)}；授予对方的权限只能是它的子集。")
        lines.append("没有合适的成员时，可以用 propose_agent 提议新建，由人审核。")
        return "\n".join(lines), False

    def _resolve_member(self, g: Group, name: str) -> AgentNode | None:
        a = self.store.agent_by_name(name) or self.store.agents.get(name.strip())
        return a if a and a.id in g.member_ids else None

    def _count_delegation(self, ctx: ExecContext) -> str | None:
        """委托次数按“一次被 @ 引出的工作 / 每个任务”累计，跨轮次有效（异步后一个上下文会跑很多轮）。"""
        holder = self._holder(ctx.kind, ctx.id)
        with self._lock:
            if holder is None:
                return "执行上下文已不存在"
            if holder.delegations >= self.limits.max_delegations:
                return f"委托次数已达上限 {self.limits.max_delegations}"
            holder.delegations += 1
        return None

    def _tool_delegate(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        g = self._require_group(ctx)
        delegator = self._agent(ctx.agent_id)
        req_auth = args.get("authority")
        delegation = Delegation(
            mission=str(args.get("mission", "")).strip(),
            authority=_as_list(req_auth) if req_auth is not None else list(ctx.authority),
            constraints=_as_lines(args.get("constraints")),
            expected_outcome=str(args.get("expected_outcome", "")).strip(),
            share=_as_list(args.get("share")),
        )
        target = self._resolve_member(g, str(args.get("agent", "")))
        if target is not None and target.id == ctx.agent_id:
            raise NetworkError("不能委托给自己；自己能做的直接做")
        parent_task_id = ctx.id if ctx.kind == "task" else None
        task = Task(id=new_id("task"), conversation_id=ctx.conversation_id, group_id=g.id,
                    delegator_id=delegator.id, delegatee_id=target.id if target else None,
                    parent_task_id=parent_task_id, depth=ctx.depth + 1, delegation=delegation,
                    effective_constraints=_dedup(ctx.constraints + delegation.constraints),
                    owners=_dedup([o for o in (delegator.owner_id, target.owner_id if target else None) if o]))

        reason = None
        if target is None:
            reason = f"「{args.get('agent', '')}」不是群组「{g.name}」的成员；只能委托 list_members 返回的成员"
        elif target.id in ctx.chain:
            reason = f"{target.name} 已在本委托链上（{' → '.join(self._name(i) for i in ctx.chain)}），不能成环"
        elif not delegation.mission or not delegation.expected_outcome:
            reason = "mission 与 expected_outcome 不能为空"
        elif ctx.depth + 1 > self.limits.max_depth:
            reason = f"委托深度超过上限 {self.limits.max_depth}"
        else:
            reason = self._count_delegation(ctx)
        ctx.created.append(task.id)
        if reason:
            task.status, task.error, task.finished_at = TaskStatus.REJECTED, reason, time.time()
            self._save_task(task, created=True)
            return format_task_result(task, target.name if target else str(args.get("agent", ""))), True

        assert target is not None
        # 有效权限 = 执行者自身权限 ∩ 委托授予 ∩ 运行时能强制的部分；委托授予只能来自委托方的有效权限
        offered = [a for a in delegation.authority if a in ctx.authority]
        narrowed = self.capabilities().narrowed([a for a in target.authority if a in offered])
        task.granted_authority = [a for a in target.authority if a in offered and a not in narrowed]
        task.denied_authority = [a for a in _dedup(delegation.authority) if a not in task.granted_authority]
        task.narrowed = narrowed
        task.inbox.append({"kind": "start"})
        with self._lock:
            shared, denied_keys = self._share_into(ctx, task.id, delegation.share)
            req = self._open_request_of(ctx) if ctx.kind == "seat" else None
            if req is not None:  # 为用户任务发起的委托：看板上不单独成卡，只在任务详情里展示
                req.task_ids.append(task.id)
        self._save_task(task, created=True)
        if shared or denied_keys:
            self.bus.emit("knowledge.shared", task_id=task.id, from_agent=delegator.id, to_agent=target.id,
                          keys=[k.key for k in shared], denied=denied_keys)
        self._kick("task", task.id)
        return format_task_result(task, target.name), False

    def _own_task(self, ctx: ExecContext, args: dict[str, Any]) -> Task:
        """只有直接委托方能验收、退回、续接一个任务。"""
        tid = str(args.get("task_id", "")).strip()
        t = self.store.tasks.get(tid)
        mine = t is not None and t.delegator_id == ctx.agent_id and t.conversation_id == ctx.conversation_id \
            and (t.parent_task_id == (ctx.id if ctx.kind == "task" else None))
        if not mine:
            raise NetworkError(f"任务 {tid} 不存在，或不是你在当前上下文中发起的委托")
        return t

    def _tool_list_tasks(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        parent = ctx.id if ctx.kind == "task" else None
        with self._lock:
            mine = [t for t in self.store.tasks.values() if t.delegator_id == ctx.agent_id
                    and t.conversation_id == ctx.conversation_id and t.parent_task_id == parent]
        if not mine:
            return "你在当前上下文中还没有发起委托。", False
        return "\n".join(f"- {t.id} → {self._name(t.delegatee_id)}：{t.status.value} · {t.delegation.mission[:60]}"
                         + (f"（{t.error[:80]}）" if t.error else "") for t in mine), False

    def _tool_accept(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        t = self._own_task(ctx, args)
        if t.status != TaskStatus.IN_REVIEW:
            return f"任务 {t.id} 当前是 {t.status.value}，只有 in_review 的任务可以验收", True
        self._complete(t)
        return f"已接受 {t.id}。结果知识 `{t.id}:result` 可在后续委托的 share 中引用。", False

    def _tool_request_changes(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        t = self._own_task(ctx, args)
        feedback = str(args.get("feedback", "")).strip()
        if t.status != TaskStatus.IN_REVIEW:
            return f"任务 {t.id} 当前是 {t.status.value}，只有 in_review 的任务可以退回", True
        if not feedback:
            return "feedback 不能为空", True
        if t.revisions >= self.limits.max_revisions:
            return f"任务 {t.id} 已退回 {t.revisions} 次，达到上限；请接受，或换一个成员重新委托", True
        with self._lock:
            t.revisions += 1
            t.status, t.delivered_at = TaskStatus.RUNNING, None
            t.inbox.append({"kind": "changes", "feedback": feedback, "round": t.revisions})
        self._save_task(t)
        self._kick("task", t.id)
        return f"已退回 {t.id}（第 {t.revisions} 次）。{self._name(t.delegatee_id)} 修改后会重新交付，结果会通知你。", False

    def _tool_retry(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        t = self._own_task(ctx, args)
        if t.status != TaskStatus.FAILED:
            return f"任务 {t.id} 当前是 {t.status.value}，只有 failed 的任务可以续接", True
        reason = self._resume_task(t, str(args.get("note", "")).strip())
        if reason:
            return reason, True
        return f"已续接 {t.id}：{self._name(t.delegatee_id)} 会在原会话里接着做，结果会通知你。", False

    def _resume_task(self, t: Task, note: str) -> str | None:
        if t.retries >= self.limits.max_retries:
            return f"任务 {t.id} 已续接 {t.retries} 次，达到上限"
        with self._lock:
            t.retries += 1
            error, t.status, t.error, t.finished_at = t.error, TaskStatus.RUNNING, None, None
            t.inbox.append({"kind": "retry", "note": note, "error": error})
        self._save_task(t)
        self._kick("task", t.id)
        return None

    def _tool_share(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        key, agent_name = str(args.get("key", "")), str(args.get("agent", ""))
        g = self._require_group(ctx)
        target = self._resolve_member(g, agent_name)
        if target is None:
            return f"「{agent_name}」不是群组成员", True
        with self._lock:
            item = self.store.knowledge.get(key)
            if item is None or not self._can_see(ctx, item):
                return f"知识 `{key}` 不存在或对你不可见", True
            tag = f"{ctx.conversation_id}/{target.id}"
            if tag not in item.visible_to:
                item.visible_to.append(tag)
            self.store.save()
        self.bus.emit("knowledge.shared", task_id=None, context_id=ctx.id, from_agent=ctx.agent_id,
                      to_agent=target.id, keys=[key], denied=[])
        return f"已把 `{key}` 共享给 {target.name}：它在本对话后续收到的委托中可见。", False

    # ================= 提议新建 Agent（Agent 起草，人提交） =================
    def _tool_propose_agent(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        g = self._require_group(ctx)
        name = str(args.get("name", "")).strip()
        description = str(args.get("description", "")).strip()
        reason = str(args.get("reason", "")).strip()
        authority = _as_list(args.get("authority"))
        if len(name) < 2 or len(description) < 2 or not reason:
            return "name、description、reason 都不能为空", True
        unknown = [a for a in authority if a not in AUTHORITIES]
        if unknown:
            return f"未知权限：{', '.join(unknown)}", True
        over = [a for a in authority if a not in ctx.authority]
        if over:
            return f"提议的权限超出你当前的有效权限：{', '.join(over)}", True
        with self._lock:
            if self.store.agent_by_name(name):
                return f"Agent「{name}」已存在；如果它不在群组里，请让人把它加入群组", True
            if any(p.status == "pending" and p.name.lower() == name.lower() for p in self.store.proposals.values()):
                return f"已经有一个待审核的「{name}」提议", True
            p = Proposal(id=new_id("prop"), proposer_id=ctx.agent_id, context_kind=ctx.kind, context_id=ctx.id,
                         conversation_id=ctx.conversation_id, group_id=g.id, name=name, description=description,
                         capabilities=_clean_caps(_as_list(args.get("capabilities"))), authority=_dedup(authority),
                         max_authority=list(ctx.authority), reason=reason,
                         owner_id=self._agent(ctx.agent_id).owner_id)
            self.store.proposals[p.id] = p
            self.store.save()
        self.bus.emit("proposal.updated", proposal=p.to_dict())
        return (f"已提交提议 {p.id}（{name}），等人审核。批准后它会加入群组「{g.name}」，结果会作为通知送给你；"
                "在那之前不要假设它存在。"), False

    def approve_proposal(self, proposal_id: str, name: str | None = None, description: str | None = None,
                         capabilities: list[str] | None = None, authority: list[str] | None = None,
                         model: dict[str, str] | None = None, note: str = "", actor: str | None = None) -> AgentNode:
        """提议者 Agent 的所有者批准提议：可以修改字段，但权限不能超出提议者提议时的有效权限；
        以批准人的身份创建（新 Agent 归批准人）并作为他的成员加入群组。"""
        with self._lock:
            p = self._proposal(proposal_id, actor)
            if p.status != "pending":
                raise NetworkError(f"提议 {p.id} 已经{'批准' if p.status == 'approved' else '拒绝'}")
            g = self.store.groups.get(p.group_id)
            if g is None:
                raise NetworkError("提议所在的群组已不存在")
            auth = _clean_auth(authority if authority is not None else p.authority)
            over = [a for a in auth if a not in p.max_authority]
            if over:
                raise NetworkError(f"权限超出提议者的有效权限：{', '.join(over)}")
            a = self._new_agent(name or p.name, description or p.description,
                                capabilities if capabilities is not None else p.capabilities, auth,
                                chat_enabled=True, model=model, created_by=f"proposal:{p.id}",
                                owner_id=actor or p.owner_id or g.owner_id)
            g.member_ids.append(a.id)
            p.status, p.agent_id, p.note, p.decided_at = "approved", a.id, note.strip(), time.time()
            self.store.save()
        self.bus.emit("agent.updated", agent=a.to_dict())
        self.bus.emit("group.updated", group=g.to_dict())
        self._proposal_decided(p)
        return a

    def reject_proposal(self, proposal_id: str, note: str = "", actor: str | None = None) -> Proposal:
        with self._lock:
            p = self._proposal(proposal_id, actor)
            if p.status != "pending":
                raise NetworkError(f"提议 {p.id} 已经处理过")
            p.status, p.note, p.decided_at = "rejected", note.strip(), time.time()
            self.store.save()
        self._proposal_decided(p)
        return p

    def _proposal_decided(self, p: Proposal) -> None:
        self.bus.emit("proposal.updated", proposal=p.to_dict())
        holder = self._holder(p.context_kind, p.context_id)
        if holder is None:
            return
        with self._lock:
            holder.inbox.append({"kind": "proposal", "proposal_id": p.id})
            self.store.save()
        self._kick(p.context_kind, p.context_id)
        self._refresh_conv(p.conversation_id)

    def _proposal(self, proposal_id: str, actor: str | None = None) -> Proposal:
        """提议归提议者 Agent 的所有者审核（旧数据没有记录时归对话所有者）。"""
        p = self.store.proposals.get(proposal_id)
        self._check(p is not None and self.can_review_proposal(p, actor), f"proposal {proposal_id}")
        return p

    def can_review_proposal(self, p: Proposal, actor: str | None) -> bool:
        if actor is None:
            return True
        if p.owner_id:
            return p.owner_id == actor
        conv = self.store.conversations.get(p.conversation_id)
        return conv is None or conv.owner_id in (None, actor)

    # ================= 用户任务（看板） =================
    # 看板只展示人交代的事。不是每条消息都算：闲聊、问答不算，有交付目标的才算——由接任务的 Agent 判断，
    # 用 open_request 登记（来源消息与下发人由网络从这一轮送达的消息里认定），做完用 deliver_request 交付，
    # 下发人验收或退回。为它发起的委托只在任务详情里展示，不单独成卡。
    def _open_request_of(self, ctx: ExecContext) -> UserRequest | None:
        """调用方持有锁。这个席位当前在做的用户任务（最近登记、还没交付的那个）。"""
        agent_id = ctx.agent_id
        mine = [r for r in self.store.requests.values() if r.conversation_id == ctx.conversation_id
                and r.agent_id == agent_id and r.status == "in_progress"]
        return max(mine, key=lambda r: r.created_at) if mine else None

    def _tool_open_request(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        title = str(args.get("title", "")).strip()
        outcome = str(args.get("expected_outcome", "")).strip()
        if not title or not outcome:
            return "title 与 expected_outcome 不能为空：说不出要交付什么，就不是任务，直接回答即可", True
        with self._lock:
            conv = self.store.conversations.get(ctx.conversation_id)
            msgs = {m.id: m for m in conv.messages} if conv else {}
            sources = [msgs[i] for i in ctx.addressed if i in msgs]
            if not sources:
                return "这一轮没有人交代给你的新消息：只有人交代的事才能登记为任务", True
            src = sources[-1]
            dup = next((r for r in self.store.requests.values()
                        if r.message_id == src.id and r.agent_id == ctx.agent_id), None)
            if dup is not None:
                return f"这条消息已经登记为任务 {dup.id}", True
            r = UserRequest(id=new_id("req"), conversation_id=ctx.conversation_id, agent_id=ctx.agent_id,
                            requester_id=src.user_id or (conv.owner_id if conv else None), message_id=src.id,
                            title=title[:120], expected_outcome=outcome[:1000])
            self.store.requests[r.id] = r
            self.store.save()
        self._request_event(r, f"{self._name(r.agent_id)} 接下了「{r.title}」")
        return f"已登记任务 {r.id}。做完后用 deliver_request 交付（request_id: {r.id}），由下发的人验收。", False

    def _tool_deliver_request(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        rid = str(args.get("request_id", "")).strip()
        summary = str(args.get("summary", "")).strip()
        with self._lock:
            r = self.store.requests.get(rid)
            if r is None or r.agent_id != ctx.agent_id or r.conversation_id != ctx.conversation_id:
                return f"任务 {rid} 不存在，或不是你在这个对话里接下的", True
            if r.status != "in_progress":
                return f"任务 {rid} 当前是 {r.status}，只有进行中的任务可以交付", True
            if not summary:
                return "summary 不能为空：写清交付了什么、在哪里（文件、结论），下发的人据此验收", True
            running = [t for t in r.task_ids if (t2 := self.store.tasks.get(t)) and t2.status == TaskStatus.RUNNING]
            if running:
                return f"还有委托没有回报（{', '.join(running)}）：等它们交付或失败后再交付", True
            r.status, r.summary, r.delivered_at = "in_review", summary[:4000], time.time()
            self.store.save()
        self._request_event(r, f"{self._name(r.agent_id)} 完成了「{r.title}」，等{self._who(r.requester_id)}验收")
        return f"已交付 {r.id}，等下发的人验收；要修改会通知你。", False

    def _tool_accept_request(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        rid = str(args.get("request_id", "")).strip()
        with self._lock:
            r = self.store.requests.get(rid)
            if r is None or r.agent_id != ctx.agent_id or r.conversation_id != ctx.conversation_id:
                return f"任务 {rid} 不存在，或不是你在这个对话里接下的", True
            if r.status != "in_review":
                return "只有已交付（待验收）的任务可以验收", True
            conv = self.store.conversations.get(ctx.conversation_id)
            sources = [m for m in conv.messages if m.id in ctx.addressed and m.role == "user"
                       and m.user_id == r.requester_id and r.requester_id is not None
                       and ctx.agent_id in m.mentions] if conv else []
            if not sources:
                return "下发的人没有在这一轮消息里表示验收：需要本人这一轮实际送达、@ 你的消息", True
            running = [t for t in r.task_ids if (t2 := self.store.tasks.get(t)) and t2.status == TaskStatus.RUNNING]
            if running:
                return f"还有委托没有回报（{', '.join(running)}）：等它们交付或失败后再验收", True
            self._complete_request(r, r.requester_id, sources[-1].id)
            self.store.save()
        self._request_event(r, f"{self._name(r.agent_id)} 按{self._who(r.requester_id)}的指示验收了「{r.title}」")
        return f"已按下发人的指示验收 {r.id}，任务已完成。", False

    def _complete_request(self, r: UserRequest, reviewer: str | None, message_id: str | None = None) -> None:
        """调用方持有锁。看板与对话代验收共用完成状态与审计字段。"""
        r.status, r.finished_at = "completed", time.time()
        r.reviewed_by, r.accepted_by_message_id = reviewer, message_id

    def _request_event(self, r: UserRequest, text: str | None = None) -> None:
        """调用方不持有锁。任务状态变化写进对话（公开的进展），推送给看板。"""
        conv = self.store.conversations.get(r.conversation_id)
        if conv is not None and text:
            self._append(conv, Message(id=new_id("msg"), role="event", agent_id=r.agent_id, text=text))
        self.bus.emit("request.updated", conversation_id=r.conversation_id, request=r.to_dict())

    def _who(self, user_id: str | None) -> str:
        """事件文本里的人：有邮箱写邮箱（两侧留空格），没有就写“下发的人”。"""
        return f" {self._email(user_id)} " if user_id in self.store.users else "下发的人"

    def can_see_request(self, r: UserRequest, actor: str | None) -> bool:
        """看板：下发的人和接任务的 Agent 的所有者（内部群组、私聊里是同一个人）。"""
        if actor is None:
            return True
        a = self.store.agents.get(r.agent_id)
        conv = self.store.conversations.get(r.conversation_id)
        return conv is not None and self.can_see_conv(conv, actor) and (
            r.requester_id == actor or (a is not None and a.owner_id == actor))

    def _request(self, request_id: str, actor: str | None) -> UserRequest:
        r = self.store.requests.get(request_id)
        self._check(r is not None and self.can_see_request(r, actor), f"request {request_id}")
        return r

    def review_request(self, request_id: str, accept: bool, feedback: str = "", actor: str | None = None) -> UserRequest:
        """下发的人验收：接受，或写明要改什么退回（接任务的 Agent 在原会话里返工后重新交付）。"""
        feedback = feedback.strip()
        with self._lock:
            r = self._request(request_id, actor)
            if actor is not None and r.requester_id not in (None, actor):
                raise NetworkError("只有下发这个任务的人能验收")
            if r.status != "in_review":
                raise NetworkError("只有已交付（待验收）的任务可以验收")
            if accept:
                self._complete_request(r, actor or r.requester_id)
            else:
                if not feedback:
                    raise NetworkError("退回时写明要改什么")
                r.status, r.revisions, r.feedback, r.delivered_at = "in_progress", r.revisions + 1, feedback[:2000], None
                seat = self._seat(self._conv(r.conversation_id), r.agent_id)
                if seat.blocked:
                    self._unblock(seat)
                seat.inbox.append({"kind": "request_changes", "request_id": r.id})
            self.store.save()
        who = self._who(actor or r.requester_id)
        self._request_event(r, f"{who}验收了「{r.title}」" if accept else f"{who}退回了「{r.title}」：{_short(feedback, 80)}")
        if not accept:
            self._kick("seat", seat_id(r.conversation_id, r.agent_id))
        return r

    def cancel_request(self, request_id: str, actor: str | None = None) -> UserRequest:
        with self._lock:
            r = self._request(request_id, actor)
            if actor is not None and r.requester_id not in (None, actor):
                raise NetworkError("只有下发这个任务的人能取消")
            if r.status in ("completed", "cancelled"):
                raise NetworkError("任务已经结束")
            r.status, r.finished_at = "cancelled", time.time()
            self.store.save()
        self._request_event(r, f"{self._who(actor or r.requester_id)}取消了「{r.title}」")
        return r

    def request_detail(self, request_id: str, actor: str | None = None) -> dict[str, Any]:
        with self._lock:
            r = self._request(request_id, actor)
            conv = self.store.conversations.get(r.conversation_id)
            src = next((m for m in conv.messages if m.id == r.message_id), None) if conv else None
            return {**self.request_card(r), "message": src.text if src else "",
                    "tasks": [self.store.tasks[t].to_dict() for t in r.task_ids
                              if t in self.store.tasks and self.can_see_task(self.store.tasks[t], actor)]}

    def request_card(self, r: UserRequest) -> dict[str, Any]:
        """调用方持有锁。"""
        conv = self.store.conversations.get(r.conversation_id)
        return {**r.to_dict(), "agent_name": self._name(r.agent_id), "requester": self._email(r.requester_id),
                "conversation_title": conv.title if conv else "", "conversation_kind": conv.kind if conv else "",
                "delegations": len(r.task_ids)}

    # ================= 人：续接 =================
    def resume_conversation(self, conv_id: str, agent_id: str | None = None, note: str = "",
                            actor: str | None = None) -> list[str]:
        """某个席位这一轮失败后，人点“继续”：同一个会话接着做，已完成的委托与知识都还在。
        不指定 agent_id 时续接这个对话里所有中断的席位。"""
        with self._lock:
            conv = self.visible_conv(conv_id, actor)
            seats = [s for a, s in conv.seats.items() if s.blocked and (agent_id is None or a == agent_id)
                     and (actor is None or (self.store.agents.get(a) and self.store.agents[a].owner_id in (None, actor)))]
            if not seats:
                raise NetworkError("对话没有中断，不需要继续")
            for s in seats:
                self._unblock(s, note)
            self.store.save()
        for s in seats:
            self._kick("seat", seat_id(conv_id, s.agent_id))
        return [s.agent_id for s in seats]

    def _unblock(self, seat: Seat, note: str = "") -> None:
        """调用方持有锁。"""
        if seat.dsh_session_id:
            seat.inbox.insert(0, {"kind": "retry", "note": note, "error": seat.blocked})
        seat.blocked = None

    def retry_task(self, task_id: str, note: str = "", actor: str | None = None) -> Task:
        """委托方 Agent 的所有者续接一个失败的委托（人的权限在所有 Agent 之上）。"""
        t = self.visible_task(task_id, actor)
        delegator = self.store.agents.get(t.delegator_id)
        if actor is not None and delegator is not None and delegator.owner_id not in (None, actor):
            raise NetworkError("只有委托方 Agent 的所有者能续接这个委托")
        if t.status != TaskStatus.FAILED:
            raise NetworkError(f"任务 {t.id} 当前是 {t.status.value}，只有 failed 的任务可以续接")
        reason = self._resume_task(t, note)
        if reason:
            raise NetworkError(reason)
        return t

    # ================= 上下文与知识 =================
    def _can_see(self, ctx: ExecContext, item: KnowledgeItem) -> bool:
        return item.group_id == ctx.group_id and (ctx.id in item.visible_to or ctx.share_tag in item.visible_to)

    def visible_knowledge(self, ctx: ExecContext) -> list[KnowledgeItem]:
        with self._lock:
            return [k for k in self.store.knowledge.values() if self._can_see(ctx, k)]

    def _share_into(self, ctx: ExecContext, task_id: str, keys: list[str]) -> tuple[list[KnowledgeItem], list[str]]:
        shared, denied = [], []
        for key in keys:
            item = self.store.knowledge.get(key)
            if item is None or not self._can_see(ctx, item):
                denied.append(key)
                continue
            if task_id not in item.visible_to:
                item.visible_to.append(task_id)
            shared.append(item)
        return shared, denied

    def _write_task_files(self, task: Task, knowledge: list[KnowledgeItem]) -> Path:
        import json
        d = self.store.task_dir(task.id)
        (d / "DELEGATION.json").write_text(json.dumps({
            "task_id": task.id, "delegator": task.delegator_id, "delegatee": task.delegatee_id,
            "mission": task.delegation.mission, "expected_outcome": task.delegation.expected_outcome,
            "granted_authority": task.granted_authority, "effective_constraints": task.effective_constraints,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        if knowledge:
            kd = d / "shared"
            kd.mkdir(exist_ok=True)
            for k in knowledge:
                (kd / f"{_safe(k.key)}.md").write_text(f"# {k.title}\n\n{k.content}\n", encoding="utf-8")
        return d

    def _chain(self, task: Task) -> list[str]:
        """从对话 Agent 到委托方的 Agent 链。"""
        chain: list[str] = []
        t: Task | None = task
        while t is not None:
            chain.insert(0, t.delegator_id)
            t = self.store.tasks.get(t.parent_task_id) if t.parent_task_id else None
        return chain

    def _save_task(self, task: Task, created: bool = False) -> None:
        with self._lock:
            self.store.tasks[task.id] = task
            self.store.save()
        self.bus.emit("task.created" if created else "task.updated", task=task.to_dict())

    # ================= 查询 =================
    def _agent(self, agent_id: str) -> AgentNode:
        a = self.store.agents.get(agent_id)
        if a is None:
            raise NetworkError(f"unknown agent {agent_id}")
        return a

    def _name(self, agent_id: str | None) -> str:
        a = self.store.agents.get(agent_id or "")
        return a.name if a else (agent_id or "（群组外）")

    def _group(self, group_id: str) -> Group:
        g = self.store.groups.get(group_id)
        if g is None:
            raise NetworkError(f"unknown group {group_id}")
        return g

    def _conv(self, conv_id: str) -> Conversation:
        c = self.store.conversations.get(conv_id)
        if c is None:
            raise NetworkError(f"unknown conversation {conv_id}")
        return c

    def _tasks_of(self, actor: str | None) -> list[Task]:
        """调用方持有锁。用户能看到的委托：两端有自己的 Agent。"""
        return [t for t in self.store.tasks.values() if self.can_see_task(t, actor)]

    def relations(self, actor: str | None = None) -> list[dict[str, Any]]:
        """长期关系：按 (委托方, 执行方) 聚合所有委托。"""
        agg: dict[tuple[str, str], dict[str, Any]] = {}
        bucket = {"completed": "completed", "running": "running", "in_review": "running"}
        for t in self._tasks_of(actor):
            if not t.delegatee_id:
                continue
            r = agg.setdefault((t.delegator_id, t.delegatee_id), {
                "from": t.delegator_id, "to": t.delegatee_id, "count": 0, "completed": 0, "failed": 0,
                "running": 0, "last_at": 0.0, "groups": []})
            r["count"] += 1
            r[bucket.get(t.status.value, "failed")] += 1
            r["last_at"] = max(r["last_at"], t.created_at)
            if t.group_id not in r["groups"]:
                r["groups"].append(t.group_id)
        return sorted(agg.values(), key=lambda r: -r["count"])

    def agent_view(self, a: AgentNode, actor: str | None) -> dict[str, Any]:
        d = a.to_dict()
        d["mine"] = self.can_edit_agent(a, actor)
        d["owner_email"] = self._email(a.owner_id) if a.owner_id else None
        caps = self.capabilities()
        d["runtime_capabilities"] = {"name": caps.name, "placement": caps.placement,
                                     "enforcement": {x: caps.level(x) for x in a.authority}}
        d["effective_authority"] = caps.effective(a.authority)
        if not d["mine"]:  # 联合群组里别人的 Agent：只给名片（名称、描述、能力、自身权限）
            d = {k: d[k] for k in ("id", "name", "description", "capabilities", "authority", "owner_email", "mine",
                                   "chat_enabled")}
        return d

    def snapshot(self, actor: str | None = None) -> dict[str, Any]:
        with self._lock:
            agents = [a for a in self.store.agents.values() if self.can_see_agent(a, actor)]
            groups = [g for g in self.store.groups.values() if self.can_see_group(g, actor)]
            convs = [c for c in self.store.conversations.values() if self.can_see_conv(c, actor)]
            tasks = self._tasks_of(actor)
            conv_ids = {c.id for c in convs}
            return {
                "agents": [self.agent_view(a, actor) for a in agents],
                "groups": [self.group_brief(g, actor) for g in groups],
                "conversations": [self.conversation_view(c, actor) for c in sorted(convs, key=lambda c: -c.updated_at)],
                "relations": self.relations(actor),
                "active_tasks": [t.to_dict() for t in tasks if t.status in (TaskStatus.RUNNING, TaskStatus.IN_REVIEW)],
                "proposals": [p.to_dict() for p in sorted(self.store.proposals.values(), key=lambda p: -p.created_at)
                              if p.conversation_id in conv_ids and self.can_review_proposal(p, actor)],
                "invitations": [self.invitation_view(g) for g in self.store.groups.values()
                                if actor is not None and actor in g.invited],
                "approvals": [ap.to_dict() for ap in self.pending_approvals(actor)],
                "authorities": AUTHORITIES,
                "seq": self.bus.last_seq,
            }

    def board(self, actor: str | None = None) -> dict[str, Any]:
        """任务看板（参照 Raft task board）：人交代给 Agent 的任务，按状态分列；加上“待我处理”。
        Agent 之间的委托不单独成卡，只在任务详情里展示。"""
        with self._lock:
            reqs = sorted((r for r in self.store.requests.values() if self.can_see_request(r, actor)),
                          key=lambda r: -r.created_at)
            cards = [self.request_card(r) for r in reqs]
            proposals = [p.to_dict() for p in self.store.proposals.values()
                         if p.status == "pending" and self.can_review_proposal(p, actor)]
            busy = {a.id: self.ledger(a.id) for a in self.store.agents.values()
                    if a.active and self.can_use_agent(a, actor)}
            invitations = [self.invitation_view(g) for g in self.store.groups.values()
                           if actor is not None and actor in g.invited]
            suggestions = [{"group_id": g.id, "group_name": g.name, **asdict(x)} for g in self.store.groups.values()
                           if g.owner_id in (None, actor) or actor is None
                           for x in g.suggestions if x.status == "pending"]
        return {"requests": cards, "approvals": [a.to_dict() for a in self.pending_approvals(actor)],
                "proposals": proposals, "ledger": busy, "invitations": invitations, "suggestions": suggestions}

    def conversation_detail(self, conv_id: str, actor: str | None = None) -> dict[str, Any]:
        """联合群组里按人过滤：委托只给两端所有者；过程、唤醒摘要、失败说明、审批只给 Agent 的所有者。"""
        with self._lock:
            conv = self.visible_conv(conv_id, actor)
            tasks = [t.to_dict() for t in self.store.tasks.values()
                     if t.conversation_id == conv_id and self.can_see_task(t, actor)]
            proposals = [p.to_dict() for p in self.store.proposals.values()
                         if p.conversation_id == conv_id and self.can_review_proposal(p, actor)]
            mine = {a for a in conv.seats if self.store.agents.get(a) and self.can_use_agent(self.store.agents[a], actor)}
            live = {a: list(self._live.get(seat_id(conv_id, a), [])) for a in conv.seats
                    if seat_id(conv_id, a) in self._running and a in mine}
            running = [a for a in conv.seats if seat_id(conv_id, a) in self._running]
            approvals = [{**a.to_dict(), "can_decide": actor is None or a.owner_id in (None, actor)}
                         for a in self.store.approvals.values() if a.conversation_id == conv_id and a.status == "pending"
                         and (actor is None or a.owner_id in (None, actor))]
            d = self.conversation_view(conv, actor)
            g = self.store.groups.get(conv.group_id or "")
            return {**d, "messages": [self.message_view(m, actor) for m in conv.messages
                                      if self.can_see_message(m, actor)],
                    "tasks": sorted(tasks, key=lambda t: t["created_at"]), "proposals": proposals,
                    "requests": [self.request_card(r) for r in self.store.requests.values()
                                 if r.conversation_id == conv_id and self.can_see_request(r, actor)],
                    "approvals": approvals, "members": self._members(conv), "live": live, "running": running,
                    "group_kind": g.kind if g else None, "is_host": g is None or actor is None or g.owner_id in (None, actor),
                    "senders": {u: self._email(u) for u in {m.user_id for m in conv.messages if m.user_id}}}

    def conversation_view(self, c: Conversation, actor: str | None) -> dict[str, Any]:
        """调用方持有锁。列表摘要：联合群组里只带自己 Agent 的席位状态，最后一条消息按可见性取。"""
        d = c.to_dict(full=False)
        if actor is None:
            return d
        mine = {a for a in c.seats if (ag := self.store.agents.get(a)) is not None and self.can_use_agent(ag, actor)}
        d["seats"] = {k: v for k, v in d["seats"].items() if k in mine}
        d["blocked"] = any(s.blocked for a, s in c.seats.items() if a in mine)
        last = next((m for m in reversed(c.messages) if m.role != "event" and self.can_see_message(m, actor)), None)
        d["last_message"] = last.text[:120] if last else ""
        d["message_count"] = sum(1 for m in c.messages if self.can_see_message(m, actor))
        return d

    def message_view(self, m: Message, actor: str | None) -> dict[str, Any]:
        d = _msg(m)
        if actor is not None and m.private_to and actor not in m.private_to:
            d["steps"], d["wake"] = [], []
            d["task_ids"] = [t for t in m.task_ids if t in self.store.tasks and self.can_see_task(self.store.tasks[t], actor)]
        d.pop("audience", None)
        d.pop("private_to", None)
        return d

    def _members(self, conv: Conversation) -> list[str]:
        """调用方持有锁。可以 @ 的成员：群聊是群组成员，私聊是对方。"""
        if conv.kind == "dm":
            return [conv.agent_id] if conv.agent_id else []
        g = self.store.groups.get(conv.group_id or "")
        return [m for m in (g.member_ids if g else []) if m in self.store.agents]

    def task_detail(self, task_id: str, actor: str | None = None) -> dict[str, Any]:
        with self._lock:
            t = self.visible_task(task_id, actor)
            return {**t.to_dict(full=True), "live": list(self._live.get(task_id, []))}

    def knowledge_item(self, key: str, actor: str | None = None) -> KnowledgeItem:
        with self._lock:
            k = self.store.knowledge.get(key)
            self._check(k is not None, f"knowledge {key}")
            self.visible_task(k.owner_task_id, actor)  # 知识随产生它的委托归属
            return k

    # ================= 事件可见性 =================
    def event_visible(self, ev_type: str, data: dict[str, Any], actor: str | None) -> bool:
        return self.event_view(ev_type, data, actor) is not None

    def event_view(self, ev_type: str, data: dict[str, Any], actor: str | None) -> dict[str, Any] | None:
        """SSE 推送前按用户过滤（不变量 13）：返回给这个用户看的事件数据，None 表示不推送。
        联合群组里：委托事件只给两端所有者；运行过程只给 Agent 的所有者；消息去掉不给他看的过程。"""
        if actor is None:
            return data
        with self._lock:
            if ev_type == "group.invited":
                return data if data.get("user_id") == actor else None
            if ev_type == "group.left":  # 退出的人据此从界面上移除群组
                g = self.store.groups.get(data.get("group_id", ""))
                return data if data.get("user_id") == actor or (g and self.can_see_group(g, actor)) else None
            if ev_type == "approval.updated":
                return data if (data.get("approval") or {}).get("owner_id") in (None, actor) else None
            if ev_type == "request.updated":
                r = self.store.requests.get((data.get("request") or {}).get("id", ""))
                return data if r is None or self.can_see_request(r, actor) else None
            if ev_type == "proposal.updated":
                p = self.store.proposals.get((data.get("proposal") or {}).get("id", ""))
                return data if p is None or self.can_review_proposal(p, actor) else None
            task_id = (data.get("task") or {}).get("id") or data.get("task_id")
            ctx_id = data.get("context_id")
            if task_id is None and ctx_id in self.store.tasks:
                task_id = ctx_id
            if task_id is not None and task_id in self.store.tasks:
                t = self.store.tasks[task_id]
                if not self.can_see_task(t, actor):
                    return None
                if ev_type in ("run.progress", "runtime.invoke", "tool.call", "run.retry"):  # 执行过程：执行方所有者
                    a = self.store.agents.get(t.delegatee_id or "")
                    return data if a is None or self.can_use_agent(a, actor) else None
                return data
            if isinstance(ctx_id, str) and "/" in ctx_id:  # 席位的运行过程：只给 Agent 的所有者
                conv_id, _, agent_id = ctx_id.partition("/")
                c = self.store.conversations.get(conv_id)
                a = self.store.agents.get(agent_id)
                if c is not None and not self.can_see_conv(c, actor):
                    return None
                return data if a is None or self.can_use_agent(a, actor) else None
            conv_id = data.get("conversation_id") or (data.get("conversation") or {}).get("id")
            if conv_id is not None:
                c = self.store.conversations.get(conv_id)
                if c is None:
                    return data  # 删除事件里对话已不在：不泄露内容，放行
                if not self.can_see_conv(c, actor):
                    return None
                if ev_type == "message.added":
                    m = next((x for x in reversed(c.messages) if x.id == data["message"]["id"]), None)
                    if m is not None:
                        return {**data, "message": self.message_view(m, actor)} if self.can_see_message(m, actor) \
                            else None
                if ev_type == "conversation.updated":
                    return {**data, "conversation": self.conversation_view(c, actor)}
                return data
            if "group" in data:
                g = self.store.groups.get(data["group"]["id"])
                if g is None:
                    return data
                return {**data, "group": self.group_brief(g, actor)} if self.can_see_group(g, actor) else None
            if ev_type == "group.deleted":
                return data
            aid = (data.get("agent") or {}).get("id") or data.get("agent_id")
            if aid is not None:
                a = self.store.agents.get(aid)
                if a is None:
                    return data
                if ev_type == "agent.updated" and self.can_see_agent(a, actor):
                    return {**data, "agent": self.agent_view(a, actor)}
                return data if self.can_use_agent(a, actor) else None
            return data

    def group_brief(self, g: Group, actor: str | None) -> dict[str, Any]:
        """调用方持有锁。列表与事件里的群组：邀请名单只给主持人。"""
        d = g.to_dict()
        d["host_email"] = self._email(g.owner_id) if g.owner_id else None
        if actor is not None and g.owner_id not in (None, actor):
            d.pop("invited")
            d["suggestions"] = [x for x in d["suggestions"] if x["by_user"] == actor]
        return d


# 网络工具名 → 处理函数。mcp_server.TOOLS（schema）、这里（实现）、protocol（提示词）三处由测试保证一致。
TOOL_HANDLERS = {
    "list_members": Network._tool_list_members,
    "delegate": Network._tool_delegate,
    "share_knowledge": Network._tool_share,
    "list_tasks": Network._tool_list_tasks,
    "accept_task": Network._tool_accept,
    "request_changes": Network._tool_request_changes,
    "retry_task": Network._tool_retry,
    "propose_agent": Network._tool_propose_agent,
    "record_decision": Network._tool_record_decision,
    "list_decisions": Network._tool_list_decisions,
    "publish_file": Network._tool_publish_file,
    "propose_brief": Network._tool_propose_brief,
    "open_request": Network._tool_open_request,
    "deliver_request": Network._tool_deliver_request,
    "accept_request": Network._tool_accept_request,
}


def network_tools_for(kind: str, in_group: bool, can_delegate: bool) -> list[str]:
    """一次执行能用的网络工具：席位（群聊、私聊）有用户任务工具；群组里有档案工具；有 delegate 时再加协作工具。"""
    def ok(n: str) -> bool:
        if n in REQUEST_TOOLS:
            return kind == "seat"
        if n in ARCHIVE_TOOLS:
            return in_group
        return can_delegate
    return [n for n in TOOL_HANDLERS if ok(n)]



UPDATABLE_AGENT_FIELDS = ("name", "description", "capabilities", "authority", "chat_enabled")
APPROVAL_CN = {"allowed": "批准", "rejected": "拒绝", "cancelled": "作废", "pending": "待审批"}


@dataclass
class HomeMount:
    base: dict[str, tuple[int, int]]   # 挂载时拷进工作目录的版本
    home: dict[str, tuple[int, int]]   # 挂载时 home 里的版本（写回时据此判断期间有没有别的运行改过）


def _safe_filename(name: str) -> str:
    """群组档案里的文件名：只取最后一段，去掉路径分隔与隐藏前缀，防止写到档案目录以外。"""
    base = Path(str(name or "").replace("\\", "/")).name.strip().lstrip(".")
    clean = "".join(c if c.isalnum() or c in "-_. " or ord(c) > 127 else "_" for c in base)[:120].strip()
    if not clean:
        raise NetworkError("文件名无效")
    return clean


def _fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def _clean_model(v: dict[str, Any] | None) -> dict[str, str] | None:
    if not v or not str(v.get("provider", "")).strip() or not str(v.get("model", "")).strip():
        return None
    m = {"provider": str(v["provider"]).strip(), "model": str(v["model"]).strip()}
    if str(v.get("reasoning_effort") or "").strip():
        m["reasoning_effort"] = str(v["reasoning_effort"]).strip()
    return m


def _violations(tool_calls: list[dict[str, Any]], authority: list[str]) -> list[str]:
    out = []
    for c in tool_calls:
        tool = str(c.get("tool") or "")
        if tool.startswith(MCP_PREFIX) and "delegate" not in authority \
                and tool.removeprefix(MCP_PREFIX) not in ARCHIVE_TOOLS + REQUEST_TOOLS:
            out.append(f"无 delegate 权限却调用了 {tool}")
        else:
            need = TOOL_AUTHORITY.get(tool)
            if need and need not in authority:
                out.append(f"{tool} 需要 {need}，未授予")
    return out


def _msg(m: Message) -> dict[str, Any]:
    from dataclasses import asdict
    return asdict(m)


def _clean_auth(v: list[str]) -> list[str]:
    bad = [a for a in v if a not in AUTHORITIES]
    if bad:
        raise NetworkError(f"未知权限：{', '.join(bad)}")
    return _dedup(v)


def _clean_caps(v: list[str]) -> list[str]:
    return _dedup([c.strip().lower() for c in v if c.strip()])


def _as_list(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def _as_lines(v: Any) -> list[str]:
    """自由文本条目：schema 里是字符串（每行一条），也接受分号分隔与旧的数组写法。"""
    items = v if isinstance(v, list) else re.split(r"[\n;；]", str(v or ""))
    return [s for x in items if (s := str(x).strip().lstrip("-*•").strip())]


def _dedup(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = re.sub(r"^[#>*\-\s]+", "", line).strip()
        if line:
            return line
    return ""


def _safe(key: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in key)[:80]


MAX_STEPS = 200  # 单轮最多保留的过程条目


def _step(ev: dict[str, Any]) -> dict[str, Any] | None:
    """把运行时事件压成一条可展示的过程条目；思考内容与状态事件不展示。"""
    t = ev.get("type")
    if t == "text" and str(ev.get("text") or "").strip():
        return {"kind": "text", "text": _short(str(ev["text"]).strip(), 2000), "ts": time.time()}
    if t == "tool_call":
        return {"kind": "tool", "call_id": ev.get("callId"), "tool": str(ev.get("tool") or ""),
                "brief": _tool_brief(ev.get("input")), "status": "running", "ts": time.time()}
    if t == "tool_result" and ev.get("callId"):
        return {"kind": "result", "call_id": ev["callId"], "status": ev.get("status") or "completed"}
    return None


def _tool_brief(v: Any) -> str:
    """工具参数的一行摘要：优先取目标、路径、查询这类字段，避免把整段长参数推给前端。"""
    if isinstance(v, dict):
        for k in ("agent", "file_path", "path", "url", "command", "query", "queries", "pattern", "key"):
            if v.get(k):
                x = v[k]
                return _short(" · ".join(map(str, x)) if isinstance(x, list) else str(x), 120)
        return ""
    return _short(str(v or ""), 120)


def _short(v: Any, n: int = 200) -> str:
    s = v if isinstance(v, str) else str(v)
    return s if len(s) <= n else s[:n] + "…"
