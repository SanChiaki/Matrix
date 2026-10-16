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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .events import EventBus
from .models import (AGENT_HOME as HOME_DIR, AUTHORITIES, MCP_PREFIX, TOOL_AUTHORITY, AgentNode, Approval,
                     Conversation, Delegation, Group, KnowledgeItem, Message, Proposal, Seat, Task, TaskStatus,
                     new_id, seat_id)
from .protocol import (build_notice_prompt, build_seat_prompt, build_task_prompt, format_delivery, format_failure,
                       format_failure_notice, format_ledger, format_proposal_notice, format_shared_knowledge,
                       format_task_result, format_transcript, retry_prompt)
from .runtime import AgentRuntime, ApprovalBridge, RunRequest, RunResult, ToolBridge
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
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def can_delegate(self) -> bool:
        return self.group_id is not None and "delegate" in self.authority

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
    # 规则：Agent 所有者可编辑；public 对所有用户可见可用；shared 对 shared_with 里的用户可见可用。
    # 群组、群聊只对所有者可见；私聊属于发起它的用户。委托、提议、看板随对话归属。
    def can_use_agent(self, a: AgentNode, actor: str | None) -> bool:
        return (actor is None or a.owner_id in (None, actor) or a.visibility == "public"
                or (a.visibility == "shared" and actor in a.shared_with))

    def can_edit_agent(self, a: AgentNode, actor: str | None) -> bool:
        return actor is None or a.owner_id in (None, actor)

    def can_see_group(self, g: Group, actor: str | None) -> bool:
        return actor is None or g.owner_id in (None, actor)

    def can_see_conv(self, c: Conversation, actor: str | None) -> bool:
        return actor is None or c.owner_id in (None, actor)

    def _check(self, ok: bool, what: str) -> None:
        if not ok:
            raise NetworkError(f"unknown {what}")  # 对没有权限的人，假装不存在（404），不泄露

    def visible_agent(self, agent_id: str, actor: str | None) -> AgentNode:
        a = self._agent(agent_id)
        self._check(self.can_use_agent(a, actor), f"agent {agent_id}")
        return a

    def owned_agent(self, agent_id: str, actor: str | None) -> AgentNode:
        a = self.visible_agent(agent_id, actor)
        if not self.can_edit_agent(a, actor):
            raise NetworkError(f"只有 {a.name} 的所有者能修改它")
        return a

    def visible_group(self, group_id: str, actor: str | None) -> Group:
        g = self._group(group_id)
        self._check(self.can_see_group(g, actor), f"group {group_id}")
        return g

    def visible_conv(self, conv_id: str, actor: str | None) -> Conversation:
        c = self._conv(conv_id)
        self._check(self.can_see_conv(c, actor), f"conversation {conv_id}")
        return c

    def visible_task(self, task_id: str, actor: str | None) -> Task:
        t = self.store.tasks.get(task_id)
        conv = self.store.conversations.get(t.conversation_id) if t else None
        self._check(t is not None and (actor is None or conv is None or self.can_see_conv(conv, actor)),
                    f"task {task_id}")
        return t

    def _agents_used_by(self, actor: str) -> list[AgentNode]:
        return [a for a in self.store.agents.values() if self.can_use_agent(a, actor)]

    # ================= Agent 管理 =================
    def create_agent(self, name: str, description: str, capabilities: list[str], authority: list[str],
                     chat_enabled: bool = True, model: dict[str, str] | None = None, actor: str | None = None,
                     visibility: str = "private", shared_with: list[str] | None = None) -> AgentNode:
        with self._lock:
            a = self._new_agent(name, description, capabilities, authority, chat_enabled, model, "human", actor,
                                visibility, shared_with)
        self.bus.emit("agent.updated", agent=a.to_dict())
        return a

    def _new_agent(self, name: str, description: str, capabilities: list[str], authority: list[str],
                   chat_enabled: bool, model: dict[str, str] | None, created_by: str, owner_id: str | None = None,
                   visibility: str = "private", shared_with: list[str] | None = None) -> AgentNode:
        """调用方持有锁。"""
        if self.store.agent_by_name(name):
            raise NetworkError(f"Agent 名称已存在：{name}")
        a = AgentNode(id=new_id("agent"), name=name.strip(), description=description.strip(),
                      capabilities=_clean_caps(capabilities), authority=_clean_auth(authority),
                      chat_enabled=chat_enabled, model=_clean_model(model), created_by=created_by,
                      owner_id=owner_id, visibility=_clean_visibility(visibility),
                      shared_with=self._clean_users(shared_with or [], owner_id))
        self.store.agents[a.id] = a
        self.store.save()
        return a

    def _clean_users(self, ids: list[str], owner_id: str | None) -> list[str]:
        """调用方持有锁。"""
        bad = [u for u in ids if u not in self.store.users]
        if bad:
            raise NetworkError(f"未知用户：{', '.join(bad)}")
        return [u for u in _dedup(ids) if u != owner_id]

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
                elif k == "visibility":
                    v = _clean_visibility(v)
                elif k == "shared_with":
                    v = self._clean_users(v, a.owner_id)
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
    # 群组只对所有者可见；所有者只能把自己能用的 Agent（自己的、公开的、分享给自己的）加进来。
    def _check_members(self, member_ids: list[str], actor: str | None) -> list[str]:
        for m in member_ids:
            self.visible_agent(m, actor)
        return list(dict.fromkeys(member_ids))

    def create_group(self, name: str, description: str, member_ids: list[str], actor: str | None = None) -> Group:
        with self._lock:
            g = Group(id=new_id("group"), name=name.strip(), description=description.strip(),
                      member_ids=self._check_members(member_ids, actor), owner_id=actor)
            self.store.groups[g.id] = g
            conv = self._group_chat(g)
            self.store.save()
        self.bus.emit("group.updated", group=g.to_dict())
        self.bus.emit("conversation.updated", conversation=conv.to_dict(full=False))
        return g

    def update_group(self, group_id: str, name: str | None = None, description: str | None = None,
                     member_ids: list[str] | None = None, actor: str | None = None) -> Group:
        with self._lock:
            g = self.visible_group(group_id, actor)
            if member_ids is not None:
                # 已在群里的成员即使后来不再对所有者可见，也允许留着；新加的必须可见
                new = list(dict.fromkeys(member_ids))
                self._check_members([m for m in new if m not in g.member_ids], actor)
                self._member_events(g, [m for m in new if m not in g.member_ids],
                                    [m for m in g.member_ids if m not in new])
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

    def _member_events(self, g: Group, added: list[str], removed: list[str]) -> None:
        """调用方持有锁。成员变动写进群聊：人看得到，之后被 @ 的成员也看得到。
        移出群组不打断它手上已经接下的任务，只是不能再被 @、不能再被委托。"""
        conv = self._group_chat(g)
        for verb, ids in (("加入了群组", added), ("离开了群组", removed)):
            for aid in ids:
                conv.messages.append(Message(id=new_id("msg"), role="event", agent_id=aid,
                                             text=f"{self._name(aid)} {verb}"))
        if added or removed:
            conv.updated_at = time.time()

    def _emit_group_chat(self, g: Group) -> None:
        conv = self._group_chat_of(g.id)
        if conv is not None:
            self.bus.emit("conversation.updated", conversation=conv.to_dict(full=False))

    def delete_group(self, group_id: str, actor: str | None = None) -> None:
        with self._lock:
            self.visible_group(group_id, actor)
            if any(c.group_id == group_id and c.status != "idle" for c in self.store.conversations.values()):
                raise NetworkError("群组内有进行中的对话，不能删除")
            del self.store.groups[group_id]
            for c in self.store.conversations.values():
                if c.group_id == group_id:
                    c.group_id, c.archived = None, True  # 群聊记录保留，只读
            self.store.save()
        self.bus.emit("group.deleted", group_id=group_id)

    # ================= 对话：群聊与私聊 =================
    # 群聊（kind=group）就是群组本身：每个群组一个，没有“主 Agent”，人在消息里 @ 谁，谁就接这个任务。
    # 被 @ 的成员在群聊里有自己的席位（独立 DSH 会话、收件箱、工作目录），自己做，或在群组里找合适的成员委托。
    # 私聊（kind=dm）是一个用户和一个 Agent 的会话：每个（用户, Agent）一个，不用 @，不能委托。
    # 同一个公开 Agent 可以同时和多个用户私聊，各自独立的会话，并行运行。
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
        """清空聊天记录与所有席位（会话从头开始）。委托、知识不受影响。"""
        with self._lock:
            conv = self.visible_conv(conv_id, actor)
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
        """人发消息。群聊里只唤醒被 @ 的成员；没 @ 任何人的消息留在对话里，之后被 @ 的成员能看到。"""
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
            msg = Message(id=new_id("msg"), role="user", text=text.strip(), mentions=targets)
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
            self._append(conv, Message(id=new_id("msg"), role="system", agent_id=agent_id,
                                       text=f"（{self._name(agent_id)}：网络内部错误 {e!r}）"))
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
    def _seat_round(self, conv: Conversation, agent_id: str) -> None:
        agent = self._agent(agent_id)
        seat = conv.seats[agent_id]
        group = self.store.groups.get(conv.group_id) if conv.group_id else None
        items = self._drain(seat)
        sid = seat_id(conv.id, agent_id)
        ctx = ExecContext(kind="seat", id=sid, agent_id=agent.id, conversation_id=conv.id,
                          group_id=group.id if group else None, authority=list(agent.authority),
                          constraints=[], depth=0, chain=[agent.id])
        with self._lock:
            upto = len(conv.messages)
            fresh = conv.messages[seat.seen:upto]  # 上次运行之后对话里的新消息：公开上下文
            shared = [k for k in self.store.knowledge.values()
                      if sid in k.visible_to and k.key not in seat.known and k.owner_task_id not in self._own_tasks(sid)]
        transcript = format_transcript(fresh, agent.id, self._name)
        notices, wake = self._render_inbox(items, ctx)
        if shared:
            notices = "\n\n".join(filter(None, [notices, format_shared_knowledge(shared)]))
        first = seat.dsh_session_id is None
        prompt = build_seat_prompt(agent, conv, group, ctx.can_delegate, transcript, notices, first,
                                   home_writable="file.write" in ctx.authority)
        res = self._run(ctx, agent, prompt, self.store.root / seat.workdir, seat, self.limits.turn_timeout_s)
        steps = self._live.pop(sid, [])
        if res.ok:
            with self._lock:
                seat.seen = upto
                seat.known = _dedup(seat.known + [k.key for k in shared])
            self._auto_accept(ctx)
            self._append(conv, Message(id=new_id("msg"), role="agent", agent_id=agent.id,
                                       text=res.final_text.strip() or "（空回复）", task_ids=ctx.created,
                                       steps=steps, wake=wake))
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
                                   task_ids=ctx.created, steps=steps, wake=wake))

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
        workdir = self.store.task_dir(task.id)
        if task.dsh_session_id is None:  # 第一次运行（或在拿到会话之前就失败了）：发完整委托
            knowledge = self.visible_knowledge(ctx)
            task.prompt = build_task_prompt(task, agent, delegator, g, ctx.can_delegate, knowledge, home_writable)
            workdir = self._write_task_files(task, knowledge)
            prompt = task.prompt + (f"\n\n{inbox}" if inbox else "")
        else:
            prompt = build_notice_prompt(inbox)
        res = self._run(ctx, agent, prompt, workdir, task, self.limits.task_timeout_s)
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
            elif kind == "proposal":
                p = self.store.proposals.get(it["proposal_id"])
                if p is None:
                    continue
                blocks.append(format_proposal_notice(p, self._name(p.agent_id) if p.agent_id else ""))
                wake.append(f"提议 {p.name} 已{'批准' if p.status == 'approved' else '拒绝'}")
        return "\n\n".join(blocks), wake

    # ================= 运行时调用 =================
    def _run(self, ctx: ExecContext, agent: AgentNode, prompt: str, workdir: Path, holder: Seat | Task,
             timeout_s: float) -> RunResult:
        """跑一轮；可恢复的运行时错误（如工具参数 JSON 损坏）用同一个会话自动续接一次。"""
        others = self.ledger(agent.id, exclude=ctx.id)
        if others:  # 工作台账：同时在做的其他工作
            prompt = f"{prompt}\n\n{format_ledger(others)}"
        before = self._mount_home(agent, workdir)
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
            conflicts = self._sync_home(agent, workdir, before, ctx.id)
            if conflicts:
                self.bus.emit("home.conflict", agent_id=agent.id, context_id=ctx.id, files=conflicts)
        return res

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
    # 同一个 Agent 可以同时为多个人、多个任务工作（不限并发）。每次运行前告诉它“你同时还在做什么”，
    # 让它避免改动别的工作的产物、不把别人的结论当成这次的输入。看板与 Agent 详情也展示这份台账。
    def ledger(self, agent_id: str, exclude: str | None = None) -> list[dict[str, Any]]:
        out = []
        with self._lock:
            for ctx in self._contexts.values():
                if ctx.agent_id != agent_id or ctx.id == exclude or any(o["context_id"] == ctx.id for o in out):
                    continue
                conv = self.store.conversations.get(ctx.conversation_id)
                owner = self.store.users.get(conv.owner_id or "") if conv else None
                item = {"context_id": ctx.id, "kind": ctx.kind, "conversation_id": ctx.conversation_id,
                        "user": owner.email if owner else None, "since": ctx.started_at}
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
        if ctx.can_delegate:
            tools = ToolBridge(url=self.tool_url, token=token,
                               handler=lambda name, args: self.handle_tool(token, name, args),
                               timeout_s=self.limits.task_timeout_s)
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
            who = self.store.users.get(ap.owner_id or "")
            self._append(conv, Message(id=new_id("msg"), role="system", agent_id=agent.id,
                                       text=f"{agent.name} 申请在工作目录以外操作，等 {who.email if who else '所有者'} 批准："
                                            f"{_short(ap.reason, 200)}"))
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

    def _require_group(self, ctx: ExecContext) -> Group:
        if not ctx.can_delegate:
            raise NetworkError("当前执行上下文没有委托能力（不在群组中或没有 delegate 权限）")
        g = self.store.groups.get(ctx.group_id or "")
        if g is None:
            raise NetworkError("群组已不存在")
        return g

    def _tool_list_members(self, ctx: ExecContext, args: dict[str, Any]) -> tuple[str, bool]:
        g = self._require_group(ctx)
        lines = [f"群组「{g.name}」成员（你自己除外）："]
        for mid in g.member_ids:
            a = self.store.agents.get(mid)
            if a is None or a.id == ctx.agent_id:
                continue
            note = "；已在本委托链上，不能再委托" if a.id in ctx.chain else ""
            lines.append(f"- {a.name}：{a.description} | 能力：{', '.join(a.capabilities)} | "
                         f"自身权限：{', '.join(a.authority) or '无'} | {a.status}{note}")
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
                    effective_constraints=_dedup(ctx.constraints + delegation.constraints))

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
        # 有效权限 = 执行者自身权限 ∩ 委托授予；委托授予只能来自委托方的有效权限
        offered = [a for a in delegation.authority if a in ctx.authority]
        task.granted_authority = [a for a in target.authority if a in offered]
        task.denied_authority = [a for a in _dedup(delegation.authority) if a not in task.granted_authority]
        task.inbox.append({"kind": "start"})
        with self._lock:
            shared, denied_keys = self._share_into(ctx, task.id, delegation.share)
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
        self.bus.emit("knowledge.shared", task_id=None, from_agent=ctx.agent_id, to_agent=target.id,
                      keys=[key], denied=[])
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
                         max_authority=list(ctx.authority), reason=reason)
            self.store.proposals[p.id] = p
            self.store.save()
        self.bus.emit("proposal.updated", proposal=p.to_dict())
        return (f"已提交提议 {p.id}（{name}），等人审核。批准后它会加入群组「{g.name}」，结果会作为通知送给你；"
                "在那之前不要假设它存在。"), False

    def approve_proposal(self, proposal_id: str, name: str | None = None, description: str | None = None,
                         capabilities: list[str] | None = None, authority: list[str] | None = None,
                         model: dict[str, str] | None = None, note: str = "", actor: str | None = None) -> AgentNode:
        """对话所有者批准提议：可以修改字段，但权限不能超出提议者提议时的有效权限；
        以批准人的身份创建（新 Agent 归批准人、默认私有）并加入群组。"""
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
                                owner_id=actor or g.owner_id)
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
        """提议归它所在对话的所有者审核。"""
        p = self.store.proposals.get(proposal_id)
        conv = self.store.conversations.get(p.conversation_id) if p else None
        self._check(p is not None and (actor is None or conv is None or self.can_see_conv(conv, actor)),
                    f"proposal {proposal_id}")
        return p

    # ================= 人：续接 =================
    def resume_conversation(self, conv_id: str, agent_id: str | None = None, note: str = "",
                            actor: str | None = None) -> list[str]:
        """某个席位这一轮失败后，人点“继续”：同一个会话接着做，已完成的委托与知识都还在。
        不指定 agent_id 时续接这个对话里所有中断的席位。"""
        with self._lock:
            conv = self.visible_conv(conv_id, actor)
            seats = [s for a, s in conv.seats.items() if s.blocked and (agent_id is None or a == agent_id)]
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
        """对话所有者续接一个失败的委托（人的权限在所有 Agent 之上）。"""
        t = self.visible_task(task_id, actor)
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
        """调用方持有锁。用户能看到的委托：发生在自己对话里的。"""
        if actor is None:
            return list(self.store.tasks.values())
        mine = {c.id for c in self.store.conversations.values() if self.can_see_conv(c, actor)}
        return [t for t in self.store.tasks.values() if t.conversation_id in mine]

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
        owner = self.store.users.get(a.owner_id or "")
        d["owner_email"] = owner.email if owner else None
        if not d["mine"]:
            d.pop("shared_with")  # 名单只给所有者看
        return d

    def snapshot(self, actor: str | None = None) -> dict[str, Any]:
        with self._lock:
            agents = [a for a in self.store.agents.values() if self.can_use_agent(a, actor)]
            groups = [g for g in self.store.groups.values() if self.can_see_group(g, actor)]
            convs = [c for c in self.store.conversations.values() if self.can_see_conv(c, actor)]
            tasks = self._tasks_of(actor)
            conv_ids = {c.id for c in convs}
            return {
                "agents": [self.agent_view(a, actor) for a in agents],
                "groups": [g.to_dict() for g in groups],
                "conversations": [c.to_dict(full=False) for c in sorted(convs, key=lambda c: -c.updated_at)],
                "relations": self.relations(actor),
                "active_tasks": [t.to_dict() for t in tasks if t.status in (TaskStatus.RUNNING, TaskStatus.IN_REVIEW)],
                "proposals": [p.to_dict() for p in sorted(self.store.proposals.values(), key=lambda p: -p.created_at)
                              if actor is None or p.conversation_id in conv_ids],
                "approvals": [ap.to_dict() for ap in self.pending_approvals(actor)],
                "authorities": AUTHORITIES,
                "seq": self.bus.last_seq,
            }

    def board(self, actor: str | None = None) -> dict[str, Any]:
        """任务看板（参照 Raft task board）：我对话里的全部委托，按状态分列；加上“待我处理”。"""
        with self._lock:
            tasks = sorted(self._tasks_of(actor), key=lambda t: -t.created_at)
            convs = {c.id: c for c in self.store.conversations.values()}
            cards = []
            for t in tasks:
                d = t.to_dict()
                conv = convs.get(t.conversation_id)
                d["conversation_title"] = conv.title if conv else ""
                d["delegator_name"], d["delegatee_name"] = self._name(t.delegator_id), self._name(t.delegatee_id)
                cards.append(d)
            mine = {c.id for c in convs.values() if self.can_see_conv(c, actor)}
            proposals = [p.to_dict() for p in self.store.proposals.values()
                         if p.status == "pending" and (actor is None or p.conversation_id in mine)]
            busy = {a.id: self.ledger(a.id) for a in self.store.agents.values()
                    if a.active and self.can_use_agent(a, actor)}
        return {"tasks": cards, "approvals": [a.to_dict() for a in self.pending_approvals(actor)],
                "proposals": proposals, "ledger": busy}

    def conversation_detail(self, conv_id: str, actor: str | None = None) -> dict[str, Any]:
        with self._lock:
            conv = self.visible_conv(conv_id, actor)
            tasks = [t.to_dict() for t in self.store.tasks.values() if t.conversation_id == conv_id]
            proposals = [p.to_dict() for p in self.store.proposals.values() if p.conversation_id == conv_id]
            live = {a: list(self._live.get(seat_id(conv_id, a), [])) for a in conv.seats
                    if seat_id(conv_id, a) in self._running}
            approvals = [{**a.to_dict(), "can_decide": actor is None or a.owner_id in (None, actor)}
                         for a in self.store.approvals.values() if a.conversation_id == conv_id and a.status == "pending"]
            return {**conv.to_dict(full=False), "messages": [_msg(m) for m in conv.messages],
                    "tasks": sorted(tasks, key=lambda t: t["created_at"]), "proposals": proposals,
                    "approvals": approvals, "members": self._members(conv), "live": live}

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
        """SSE 推送前按用户过滤：不把别人的对话、委托、审批过程推给你。"""
        if actor is None:
            return True
        with self._lock:
            conv_id = data.get("conversation_id") or (data.get("conversation") or {}).get("id") \
                or (data.get("task") or {}).get("conversation_id") or (data.get("proposal") or {}).get("conversation_id")
            if conv_id is None and data.get("task_id") in self.store.tasks:
                conv_id = self.store.tasks[data["task_id"]].conversation_id
            ctx_id = data.get("context_id")
            if conv_id is None and ctx_id in self.store.tasks:
                conv_id = self.store.tasks[ctx_id].conversation_id
            elif conv_id is None and isinstance(ctx_id, str) and "/" in ctx_id:  # 席位：对话/Agent
                conv_id = ctx_id.split("/", 1)[0]
            if conv_id is not None:
                c = self.store.conversations.get(conv_id)
                return c is None or self.can_see_conv(c, actor)  # 删除事件里对话已不在：不泄露内容，放行
            if ev_type == "approval.updated":
                return (data.get("approval") or {}).get("owner_id") in (None, actor)
            if "group" in data:
                g = self.store.groups.get(data["group"]["id"])
                return g is None or self.can_see_group(g, actor)
            aid = (data.get("agent") or {}).get("id") or data.get("agent_id")
            if aid is not None:
                a = self.store.agents.get(aid)
                return a is None or self.can_use_agent(a, actor)
            return True


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
}



VISIBILITIES = ("private", "public", "shared")
APPROVAL_CN = {"allowed": "批准", "rejected": "拒绝", "cancelled": "作废", "pending": "待审批"}


@dataclass
class HomeMount:
    base: dict[str, tuple[int, int]]   # 挂载时拷进工作目录的版本
    home: dict[str, tuple[int, int]]   # 挂载时 home 里的版本（写回时据此判断期间有没有别的运行改过）


def _clean_visibility(v: str) -> str:
    if v not in VISIBILITIES:
        raise NetworkError(f"未知可见性：{v}（可选 {' / '.join(VISIBILITIES)}）")
    return v


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
        if tool.startswith(MCP_PREFIX) and "delegate" not in authority:
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
