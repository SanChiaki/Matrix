"""Network：Agent 网络的运行核心。

- 对话：人与有权限（chat_enabled）的 Agent 持续对话，每个对话续接同一个 DSH 会话。
- 群组：只是成员集合。在群组内对话时，Agent 通过网络工具（MCP）只能委托群组成员；网络不会创建 Agent。
- 权限：执行者有效权限 = 执行者自身权限 ∩ 委托授予；委托授予只能是委托方有效权限的子集；约束沿链累加。
- 上下文：默认隔离。每个委托任务独立会话与工作目录；知识只对产生者与直接委托方可见，其余需显式共享。
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .events import EventBus
from .models import (AUTHORITIES, FORBIDDEN_TOOLS, MCP_PREFIX, TOOL_AUTHORITY, AgentNode, Conversation,
                     Delegation, Group, KnowledgeItem, Message, Task, TaskStatus, new_id)
from .protocol import build_conversation_prompt, build_task_prompt, format_task_result
from .runtime import AgentRuntime, RunRequest, RunResult, ToolBridge
from .store import Store


@dataclass
class Limits:
    max_depth: int = 3              # 对话直接发起的委托 depth=1；depth >= max_depth 的任务不能再委托
    max_delegations: int = 8        # 单个执行上下文最多发起几次委托
    turn_timeout_s: float = 1800
    task_timeout_s: float = 900


@dataclass
class ExecContext:
    """一次运行时调用的执行上下文；令牌只在调用期间有效。"""

    kind: str                       # conversation | task
    id: str                         # 对话 id 或任务 id
    agent_id: str
    conversation_id: str
    group_id: str | None
    authority: list[str]            # 有效权限
    constraints: list[str]
    depth: int                      # 对话为 0
    chain: list[str]                # 从对话 Agent 到当前 Agent 的链（防环）
    delegations: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def can_delegate(self) -> bool:
        return self.group_id is not None and "delegate" in self.authority

    @property
    def share_tag(self) -> str:
        return f"{self.conversation_id}/{self.agent_id}"


class NetworkError(ValueError):
    pass


class Network:
    def __init__(self, runtime: AgentRuntime, data_dir: Path, limits: Limits | None = None,
                 tool_url: str = "http://127.0.0.1:8765/internal/tools"):
        self.runtime = runtime
        self.limits = limits or Limits()
        self.tool_url = tool_url
        self.store = Store(data_dir)
        self.bus = EventBus()
        self._contexts: dict[str, ExecContext] = {}
        self._live: dict[str, list[dict[str, Any]]] = {}  # 运行中的执行上下文 id → 过程（不持久化）
        self._lock = self.store.lock
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="agentnet-turn")

    # ================= Agent 管理 =================
    def create_agent(self, name: str, description: str, capabilities: list[str], authority: list[str],
                     chat_enabled: bool = True) -> AgentNode:
        with self._lock:
            if self.store.agent_by_name(name):
                raise NetworkError(f"Agent 名称已存在：{name}")
            a = AgentNode(id=new_id("agent"), name=name.strip(), description=description.strip(),
                          capabilities=_clean_caps(capabilities), authority=_clean_auth(authority),
                          chat_enabled=chat_enabled, created_by="human")
            self.store.agents[a.id] = a
            self.store.save()
        self.bus.emit("agent.updated", agent=a.to_dict())
        return a

    def update_agent(self, agent_id: str, **fields: Any) -> AgentNode:
        with self._lock:
            a = self._agent(agent_id)
            if "name" in fields and fields["name"] != a.name and self.store.agent_by_name(fields["name"]):
                raise NetworkError(f"Agent 名称已存在：{fields['name']}")
            for k, v in fields.items():
                if v is None:
                    continue
                if k == "authority":
                    v = _clean_auth(v)
                elif k == "capabilities":
                    v = _clean_caps(v)
                setattr(a, k, v)
            self.store.save()
        self.bus.emit("agent.updated", agent=a.to_dict())
        return a

    def delete_agent(self, agent_id: str) -> None:
        with self._lock:
            a = self._agent(agent_id)
            if a.active:
                raise NetworkError("Agent 正在运行，不能删除")
            del self.store.agents[agent_id]
            for g in self.store.groups.values():
                if agent_id in g.member_ids:
                    g.member_ids.remove(agent_id)
            self.store.save()
        self.bus.emit("agent.deleted", agent_id=agent_id)

    # ================= 群组管理 =================
    def create_group(self, name: str, description: str, member_ids: list[str]) -> Group:
        with self._lock:
            for m in member_ids:
                self._agent(m)
            g = Group(id=new_id("group"), name=name.strip(), description=description.strip(),
                      member_ids=list(dict.fromkeys(member_ids)))
            self.store.groups[g.id] = g
            self.store.save()
        self.bus.emit("group.updated", group=g.to_dict())
        return g

    def update_group(self, group_id: str, name: str | None = None, description: str | None = None,
                     member_ids: list[str] | None = None) -> Group:
        with self._lock:
            g = self._group(group_id)
            if member_ids is not None:
                for m in member_ids:
                    self._agent(m)
                g.member_ids = list(dict.fromkeys(member_ids))
            if name is not None:
                g.name = name.strip()
            if description is not None:
                g.description = description.strip()
            self.store.save()
        self.bus.emit("group.updated", group=g.to_dict())
        return g

    def delete_group(self, group_id: str) -> None:
        with self._lock:
            self._group(group_id)
            if any(c.group_id == group_id and c.status == "thinking" for c in self.store.conversations.values()):
                raise NetworkError("群组内有进行中的对话，不能删除")
            del self.store.groups[group_id]
            for c in self.store.conversations.values():
                if c.group_id == group_id:
                    c.group_id = None  # 对话保留，但不再有委托能力
            self.store.save()
        self.bus.emit("group.deleted", group_id=group_id)

    # ================= 对话管理 =================
    def create_conversation(self, agent_id: str, group_id: str | None = None, title: str = "") -> Conversation:
        with self._lock:
            a = self._agent(agent_id)
            if not a.chat_enabled:
                raise NetworkError(f"{a.name} 未开放直接对话")
            if group_id is not None:
                g = self._group(group_id)
                if agent_id not in g.member_ids:
                    raise NetworkError(f"{a.name} 不是群组「{g.name}」的成员")
            conv = Conversation(id=new_id("conv"), agent_id=agent_id, group_id=group_id,
                                title=title.strip() or f"与 {a.name} 的对话")
            self.store.conversations[conv.id] = conv
            self.store.save()
        self.bus.emit("conversation.updated", conversation=conv.to_dict(full=False))
        return conv

    def delete_conversation(self, conv_id: str) -> None:
        with self._lock:
            conv = self._conv(conv_id)
            if conv.status == "thinking":
                raise NetworkError("对话进行中，不能删除")
            del self.store.conversations[conv_id]
            self.store.save()
        self.bus.emit("conversation.deleted", conversation_id=conv_id)

    def send_message(self, conv_id: str, text: str, background: bool = True) -> Message:
        with self._lock:
            conv = self._conv(conv_id)
            agent = self._agent(conv.agent_id)
            if not agent.chat_enabled:
                raise NetworkError(f"{agent.name} 未开放直接对话")
            if conv.status == "thinking":
                raise NetworkError("上一条消息还在处理中")
            msg = Message(id=new_id("msg"), role="user", text=text.strip())
            conv.messages.append(msg)
            conv.status = "thinking"
            conv.updated_at = time.time()
            self.store.save()
        self.bus.emit("message.added", conversation_id=conv.id, message=_msg(msg))
        self.bus.emit("conversation.updated", conversation=conv.to_dict(full=False))
        if background:
            self._pool.submit(self._turn_safe, conv, agent, msg)
        else:
            self._turn_safe(conv, agent, msg)
        return msg

    def wait_idle(self, conv_id: str, timeout: float = 30) -> Conversation:
        deadline = time.time() + timeout
        while time.time() < deadline:
            conv = self._conv(conv_id)
            if conv.status != "thinking":
                return conv
            time.sleep(0.05)
        raise TimeoutError(conv_id)

    def _turn_safe(self, conv: Conversation, agent: AgentNode, msg: Message) -> None:
        try:
            self._turn(conv, agent, msg)
        except Exception as e:  # noqa: BLE001 —— 引擎缺陷也要落到对话里
            self._agent_reply(conv, f"（网络内部错误：{e!r}）", [], role="system")

    def _turn(self, conv: Conversation, agent: AgentNode, msg: Message) -> None:
        group = self.store.groups.get(conv.group_id) if conv.group_id else None
        ctx = ExecContext(kind="conversation", id=conv.id, agent_id=agent.id, conversation_id=conv.id,
                          group_id=group.id if group else None, authority=list(agent.authority),
                          constraints=[], depth=0, chain=[agent.id])
        first = conv.dsh_session_id is None
        prompt = build_conversation_prompt(agent, conv, group, ctx.can_delegate, [], msg.text, first)
        workdir = self.store.conversation_dir(conv.id)
        started = len(self.store.tasks)
        res = self._invoke(ctx, agent, prompt, workdir, conv.dsh_session_id, self.limits.turn_timeout_s,
                           on_session=lambda sid: setattr(conv, "dsh_session_id", sid))
        conv.dsh_session_id = conv.dsh_session_id or res.session_id or None
        task_ids = [t.id for t in list(self.store.tasks.values())[started:]
                    if t.conversation_id == conv.id and t.parent_task_id is None]
        steps = self._live.pop(conv.id, [])
        if res.ok:
            self._agent_reply(conv, res.final_text.strip() or "（空回复）", task_ids, steps=steps)
        else:
            self._agent_reply(conv, f"（运行失败：{res.error}）", task_ids, role="system", steps=steps)

    def _agent_reply(self, conv: Conversation, text: str, task_ids: list[str], role: str = "agent",
                     steps: list[dict[str, Any]] | None = None) -> None:
        reply = Message(id=new_id("msg"), role=role, text=text, task_ids=task_ids, steps=steps or [])
        with self._lock:
            conv.messages.append(reply)
            conv.status = "idle"
            conv.updated_at = time.time()
            self.store.save()
        self.bus.emit("message.added", conversation_id=conv.id, message=_msg(reply))
        self.bus.emit("conversation.updated", conversation=conv.to_dict(full=False))

    # ================= 运行时调用 =================
    def _invoke(self, ctx: ExecContext, agent: AgentNode, prompt: str, workdir: Path, session_id: str | None,
                timeout_s: float, on_session: Any = None) -> RunResult:
        token = secrets.token_urlsafe(18)
        tools = None
        if ctx.can_delegate:
            tools = ToolBridge(url=self.tool_url, token=token,
                               handler=lambda name, args: self.handle_tool(token, name, args),
                               timeout_s=self.limits.task_timeout_s)
        with self._lock:
            self._contexts[token] = ctx
            self._live[ctx.id] = []
            agent.active += 1
        self.bus.emit("agent.updated", agent=agent.to_dict())
        self.bus.emit("runtime.invoke", context=ctx.kind, context_id=ctx.id, agent_id=agent.id,
                      resumed=bool(session_id), authority=ctx.authority, network_tools=tools is not None)

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
                                              on_event=on_event, timeout_s=timeout_s))
        except Exception as e:  # noqa: BLE001
            res = RunResult(session_id or "", "", ok=False, error=f"runtime error: {e!r}")
        finally:
            with self._lock:
                self._contexts.pop(token, None)
                if ctx.kind == "task":
                    self._live.pop(ctx.id, None)
                agent.active = max(0, agent.active - 1)
            self.bus.emit("agent.updated", agent=agent.to_dict())
        return res

    # ================= 网络工具（MCP） =================
    def handle_tool(self, token: str, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        ctx = self._contexts.get(token)
        if ctx is None:
            return "执行上下文无效或已结束", True
        try:
            if name == "list_members":
                return self._tool_list_members(ctx), False
            if name == "delegate":
                return self._tool_delegate(ctx, args)
            if name == "share_knowledge":
                return self._tool_share(ctx, str(args.get("key", "")), str(args.get("agent", "")))
            return f"未知工具 {name}", True
        except NetworkError as e:
            return str(e), True

    def _require_group(self, ctx: ExecContext) -> Group:
        if not ctx.can_delegate:
            raise NetworkError("当前执行上下文没有委托能力（不在群组中或没有 delegate 权限）")
        g = self.store.groups.get(ctx.group_id or "")
        if g is None:
            raise NetworkError("群组已不存在")
        return g

    def _tool_list_members(self, ctx: ExecContext) -> str:
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
        return "\n".join(lines)

    def _resolve_member(self, g: Group, name: str) -> AgentNode | None:
        a = self.store.agent_by_name(name) or self.store.agents.get(name.strip())
        return a if a and a.id in g.member_ids else None

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
        parent_task_id = ctx.id if ctx.kind == "task" else None
        task = Task(id=new_id("task"), conversation_id=ctx.conversation_id, group_id=g.id,
                    delegator_id=delegator.id, delegatee_id=target.id if target else None,
                    parent_task_id=parent_task_id, depth=ctx.depth + 1, delegation=delegation,
                    effective_constraints=_dedup(ctx.constraints + delegation.constraints))

        reason = None
        if target is None:
            reason = f"「{args.get('agent', '')}」不是群组「{g.name}」的成员；只能委托 list_members 返回的成员"
        elif target.id in ctx.chain:
            reason = f"{target.name} 已在本委托链上（{' → '.join(self.store.agents[i].name for i in ctx.chain if i in self.store.agents)}），不能成环"
        elif not delegation.mission or not delegation.expected_outcome:
            reason = "mission 与 expected_outcome 不能为空"
        elif ctx.depth + 1 > self.limits.max_depth:
            reason = f"委托深度超过上限 {self.limits.max_depth}"
        else:
            with ctx.lock:
                if ctx.delegations >= self.limits.max_delegations:
                    reason = f"本次执行的委托次数已达上限 {self.limits.max_delegations}"
                else:
                    ctx.delegations += 1
        if reason:
            task.status, task.error, task.finished_at = TaskStatus.REJECTED, reason, time.time()
            self._save_task(task, created=True)
            return format_task_result(task, target.name if target else str(args.get("agent", ""))), True

        assert target is not None
        # 有效权限 = 执行者自身权限 ∩ 委托授予；委托授予只能来自委托方的有效权限
        offered = [a for a in delegation.authority if a in ctx.authority]
        task.granted_authority = [a for a in target.authority if a in offered]
        task.denied_authority = [a for a in _dedup(delegation.authority) if a not in task.granted_authority]
        with self._lock:
            shared, denied_keys = self._share_into(ctx, task.id, delegation.share)
        self._save_task(task, created=True)
        if shared or denied_keys:
            self.bus.emit("knowledge.shared", task_id=task.id, from_agent=delegator.id, to_agent=target.id,
                          keys=[k.key for k in shared], denied=denied_keys)
        self._run_task(task, target, delegator, g)
        return format_task_result(task, target.name), task.status != TaskStatus.COMPLETED

    def _run_task(self, task: Task, agent: AgentNode, delegator: AgentNode, g: Group) -> None:
        ctx = ExecContext(kind="task", id=task.id, agent_id=agent.id, conversation_id=task.conversation_id,
                          group_id=g.id, authority=list(task.granted_authority),
                          constraints=list(task.effective_constraints), depth=task.depth,
                          chain=self._chain(task) + [agent.id])
        if ctx.depth >= self.limits.max_depth and "delegate" in ctx.authority:
            ctx.authority.remove("delegate")  # 到达深度上限：不再挂载网络工具
        knowledge = self.visible_knowledge(ctx)
        task.prompt = build_task_prompt(task, agent, delegator, g, ctx.can_delegate, knowledge)
        workdir = self._write_task_files(task, knowledge)
        res = self._invoke(ctx, agent, task.prompt, workdir, None, self.limits.task_timeout_s,
                           on_session=lambda sid: setattr(task, "dsh_session_id", sid))
        task.dsh_session_id = res.session_id or task.dsh_session_id
        task.violations = _violations(res.tool_calls, ctx.authority)
        for v in task.violations:
            self.bus.emit("authority.violation", task_id=task.id, agent_id=agent.id, detail=v)
        task.evidence = [{"tool": c["tool"], "input": _short(c["input"], 300), "status": c["status"]}
                         for c in res.tool_calls]
        text = res.final_text.strip()
        if res.ok and text:
            key = f"{task.id}:result"
            item = KnowledgeItem(key=key, title=f"{agent.name}：{_first_line(text)[:60]}", content=text,
                                 group_id=g.id, owner_task_id=task.id,
                                 visible_to=[task.id, task.parent_task_id or task.conversation_id])
            with self._lock:
                self.store.knowledge[key] = item
            task.result = {"summary": _first_line(text)[:200], "report": text, "knowledge_key": key}
            task.status = TaskStatus.COMPLETED
        else:
            task.status = TaskStatus.FAILED
            task.error = res.error or "执行者没有交回任何内容"
        task.finished_at = time.time()
        self._save_task(task)

    def _tool_share(self, ctx: ExecContext, key: str, agent_name: str) -> tuple[str, bool]:
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

    def relations(self) -> list[dict[str, Any]]:
        """长期关系：按 (委托方, 执行方) 聚合所有委托。"""
        agg: dict[tuple[str, str], dict[str, Any]] = {}
        for t in self.store.tasks.values():
            if not t.delegatee_id:
                continue
            r = agg.setdefault((t.delegator_id, t.delegatee_id), {
                "from": t.delegator_id, "to": t.delegatee_id, "count": 0, "completed": 0, "failed": 0,
                "running": 0, "last_at": 0.0, "groups": []})
            r["count"] += 1
            r[{"completed": "completed", "running": "running"}.get(t.status.value, "failed")] += 1
            r["last_at"] = max(r["last_at"], t.created_at)
            if t.group_id not in r["groups"]:
                r["groups"].append(t.group_id)
        return sorted(agg.values(), key=lambda r: -r["count"])

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "agents": [a.to_dict() for a in self.store.agents.values()],
                "groups": [g.to_dict() for g in self.store.groups.values()],
                "conversations": [c.to_dict(full=False) for c in
                                  sorted(self.store.conversations.values(), key=lambda c: -c.updated_at)],
                "relations": self.relations(),
                "active_tasks": [t.to_dict() for t in self.store.tasks.values() if t.status == TaskStatus.RUNNING],
                "authorities": AUTHORITIES,
                "seq": self.bus.last_seq,
            }

    def conversation_detail(self, conv_id: str) -> dict[str, Any]:
        with self._lock:
            conv = self._conv(conv_id)
            tasks = [t.to_dict() for t in self.store.tasks.values() if t.conversation_id == conv_id]
            return {**conv.to_dict(), "tasks": sorted(tasks, key=lambda t: t["created_at"]),
                    "live": list(self._live.get(conv_id, []))}

    def task_detail(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            t = self.store.tasks.get(task_id)
            if t is None:
                raise NetworkError(f"unknown task {task_id}")
            return {**t.to_dict(full=True), "live": list(self._live.get(task_id, []))}


def _violations(tool_calls: list[dict[str, Any]], authority: list[str]) -> list[str]:
    out = []
    for c in tool_calls:
        tool = str(c.get("tool") or "")
        if tool in FORBIDDEN_TOOLS:
            out.append(f"调用了被禁止的工具 {tool}")
        elif tool.startswith(MCP_PREFIX) and "delegate" not in authority:
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
