"""网络状态持久化：Agent、群组、对话、委托、共享知识都写入 data_dir/state.json。

网络状态属于系统；工作成果（报告、文件）由 Agent 按委托的输出要求写进各自的工作目录。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from dataclasses import asdict, fields

from .models import (AgentNode, Approval, BriefSuggestion, Conversation, Decision, Delegation, Device, Group, KnowledgeItem,
                     Message, Project, Proposal, ResourceRef, Seat, Task, TaskStatus, User, UserRequest, Workstream, new_id,
                     seat_id)


def _known(cls: type, d: dict[str, Any]) -> dict[str, Any]:
    """只取 dataclass 认识的字段：旧版 state.json 缺字段用默认值，多余字段（已移除的功能）丢弃。"""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


def _load_conversation(c: dict[str, Any]) -> Conversation:
    msgs = [Message(**_known(Message, m)) for m in c.pop("messages", [])]
    if "kind" in c:
        seats = {a: Seat(**_known(Seat, s)) for a, s in (c.pop("seats", None) or {}).items()}
        return Conversation(**_known(Conversation, c), seats=seats, messages=msgs)
    # 旧版：对话 = 一个“主 Agent”。迁移成只有这一个席位的群聊（在群组里）或私聊（不在群组里）；
    # 席位沿用原来的对话目录和 DSH 会话，所以还能续接。旧的用户消息记为 @ 了它。
    agent_id = c["agent_id"]
    seat = Seat(agent_id=agent_id, workdir=f"conversations/{c['id']}", dsh_session_id=c.get("dsh_session_id"),
                inbox=c.get("inbox") or [], seen=len(msgs), delegations=c.get("delegations", 0),
                blocked=c.get("blocked"))
    for m in msgs:
        if m.role == "user" and not m.mentions:
            m.mentions = [agent_id]
        elif m.role in ("agent", "system") and m.agent_id is None:
            m.agent_id = agent_id
    group = c.get("group_id")
    title = c.get("title", "")
    if group and title.startswith("与 ") and title.endswith(" 的对话"):  # 旧的默认标题是按主 Agent 起的
        title = ""
    return Conversation(id=c["id"], kind="group" if group else "dm", group_id=group,
                        agent_id=None if group else agent_id, title=title, seats={agent_id: seat},
                        messages=msgs, created_at=c.get("created_at", 0), updated_at=c.get("updated_at", 0))

SEED_AGENTS: list[dict[str, Any]] = [
    {"name": "Product Strategy Agent",
     "description": "产品机会负责人：理解目标、判断需要哪些能力、在群组内委托成员、整合结论并给出建议。",
     "capabilities": ["goal.planning", "synthesis", "product.strategy"],
     "authority": ["web.research", "file.write", "delegate"]},
    {"name": "Market Research Agent",
     "description": "研究市场规模、用户需求、增长趋势和细分赛道，基于公开信息给出有出处的结论。",
     "capabilities": ["market.research", "trend.analysis"],
     "authority": ["web.research"]},
    {"name": "Technical Analysis Agent",
     "description": "评估技术可行性、技术栈、实现难度、成本与风险。",
     "capabilities": ["technical.analysis", "feasibility"],
     "authority": ["web.research", "file.write"]},
    {"name": "Competitive Intelligence Agent",
     "description": "梳理竞品、竞争格局与差异化空间。",
     "capabilities": ["competitive.intelligence"],
     "authority": ["web.research"]},
    {"name": "Legal Compliance Agent",
     "description": "评估数据合规、隐私与监管风险。",
     "capabilities": ["legal.compliance"],
     "authority": ["web.research"]},
]

SEED_GROUP = {
    "name": "AI 产品机会研究组",
    "description": "研究新的 AI 产品机会：市场、技术、竞争。",
    "members": ["Product Strategy Agent", "Market Research Agent", "Technical Analysis Agent",
                "Competitive Intelligence Agent"],
}


class Store:
    def __init__(self, root: Path, seed: bool = True):
        """seed：全新的数据目录是否预置种子 Agent 与研究组（测试、离线演练用；服务默认不预置，由用户自己创建）。"""
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = root / "state.json"
        self.lock = threading.RLock()
        self.agents: dict[str, AgentNode] = {}
        self.groups: dict[str, Group] = {}
        self.conversations: dict[str, Conversation] = {}
        self.tasks: dict[str, Task] = {}
        self.knowledge: dict[str, KnowledgeItem] = {}
        self.proposals: dict[str, Proposal] = {}
        self.users: dict[str, User] = {}
        self.sessions: dict[str, dict[str, Any]] = {}   # 登录令牌 → {user_id, created_at}
        self.approvals: dict[str, Approval] = {}
        self.requests: dict[str, UserRequest] = {}  # 用户下发的任务（看板）
        self.devices: dict[str, Device] = {}   # 端侧连接器（令牌只存 sha256）
        self.projects: dict[str, Project] = {}  # 项目：目标与汇总层（docs/projects.md）
        if self.path.exists():
            self._load()
        else:
            if seed:
                self._seed()
            self.save()

    # ---------- 目录 ----------
    def seat_dir(self, conv_id: str, agent_id: str | None) -> Path:
        """席位工作目录：群聊里每个成员一个子目录，私聊直接用对话目录。"""
        d = self.root / "conversations" / conv_id
        if agent_id:
            d = d / agent_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def group_dir(self, group_id: str) -> Path:
        """群组档案的文件区：不属于任何项目仓库。只有网络写入（publish_file），运行时只读挂载。"""
        d = self.root / "groups" / group_id / "files"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def project_dir(self, project_id: str) -> Path:
        """项目文件区：只有网络写入（publish_file target=project、人上传），挂载群组的运行时只读挂载。"""
        d = self.root / "projects" / project_id / "files"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def agent_dir(self, agent_id: str) -> Path:
        """Agent 的持久私有目录：跨对话、跨任务保留，内容由 Agent 自己决定，网络不读。"""
        d = self.root / "agents" / agent_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def task_dir(self, task_id: str) -> Path:
        d = self.root / "tasks" / task_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---------- 持久化 ----------
    def save(self) -> None:
        with self.lock:
            data = {
                "agents": [a.persist() for a in self.agents.values()],
                "groups": [g.to_dict() for g in self.groups.values()],
                "conversations": [c.to_dict() for c in self.conversations.values()],  # 含收件箱
                "tasks": [t.to_dict(full=True) for t in self.tasks.values()],
                "knowledge": [k.to_dict(content=True) for k in self.knowledge.values()],
                "proposals": [p.to_dict() for p in self.proposals.values()],
                "users": [asdict(u) for u in self.users.values()],
                "sessions": self.sessions,
                "approvals": [a.to_dict() for a in self.approvals.values()],
                "requests": [r.to_dict() for r in self.requests.values()],
                "devices": [asdict(d) for d in self.devices.values()],
                "projects": [p.to_dict() for p in self.projects.values()],
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)

    def _load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for a in raw.get("agents", []):
            a.pop("memory", None)  # 旧版本的 Agent Memory，已移除
            self.agents[a["id"]] = AgentNode(**_known(AgentNode, a))
        for g in raw.get("groups", []):
            g["decisions"] = [Decision(**_known(Decision, d)) for d in g.get("decisions") or []]
            # 旧版的“项目引用”（projects）改名为资源引用（refs）：“项目”现在是另一个概念
            g["refs"] = [ResourceRef(**_known(ResourceRef, r)) for r in g.get("refs") or g.get("projects") or []]
            g["suggestions"] = [BriefSuggestion(**_known(BriefSuggestion, x)) for x in g.get("suggestions") or []]
            self.groups[g["id"]] = Group(**_known(Group, g))
        for c in raw.get("conversations", []):
            conv = _load_conversation(c)
            if not conv.title:
                g = self.groups.get(conv.group_id or "")
                conv.title = g.name if g else "群聊"
            conv.status = "idle"  # 重启时中断的轮次不再继续；收件箱保留，下一次被 @ 或“继续”时送达
            self.conversations[conv.id] = conv
        self._one_chat_each()
        for t in raw.get("tasks", []):
            t.pop("summary", None)
            t["delegation"] = Delegation(**t["delegation"])
            t["status"] = TaskStatus(t["status"])
            task = Task(**_known(Task, t))
            self.tasks[task.id] = task
        for task in self.tasks.values():
            if task.status != TaskStatus.RUNNING:
                continue
            # 端侧任务：服务器重启不代表执行者失败，回到等待，端侧上线后继续（docs/device-runtimes.md §5）
            delegatee = next((a for a in self.agents.values() if a.id == task.delegatee_id), None)
            if delegatee is not None and delegatee.device_runtime:
                task.status, task.error = TaskStatus.WAITING, None
                continue
            # 委托方会在下一轮看到失败通知，可以 retry_task 续接原会话
            task.status, task.error = TaskStatus.FAILED, "服务重启，任务中断"
            conv = self.conversations.get(task.conversation_id)
            parent = self.tasks.get(task.parent_task_id or "") or (conv.seats.get(task.delegator_id) if conv else None)
            if parent is not None:
                parent.inbox.append({"kind": "failed", "task_id": task.id})
        for k in raw.get("knowledge", []):
            item = KnowledgeItem(**k)
            # 旧版里对话本身是委托方的执行上下文；现在是委托方在对话里的席位（对话/Agent）
            owner = self.tasks.get(item.owner_task_id)
            if owner is not None and owner.conversation_id in item.visible_to:
                item.visible_to = [seat_id(owner.conversation_id, owner.delegator_id) if v == owner.conversation_id
                                   else v for v in item.visible_to]
            self.knowledge[item.key] = item
        for p in raw.get("proposals", []):
            self.proposals[p["id"]] = Proposal(**_known(Proposal, p))
        for u in raw.get("users", []):
            self.users[u["id"]] = User(**_known(User, u))
        self.sessions = {t: s for t, s in (raw.get("sessions") or {}).items() if s.get("user_id") in self.users}
        for a in raw.get("approvals", []):
            ap = Approval(**_known(Approval, a))
            if ap.status == "pending":  # 等审批的那次运行已随重启结束
                ap.status, ap.note = "cancelled", "服务重启，申请作废"
            self.approvals[ap.id] = ap
        # UserRequest 新增审计字段缺失时由 dataclass 默认 None，旧状态无需迁移。
        for d in raw.get("devices", []):
            self.devices[d["id"]] = Device(**_known(Device, d))
        for r in raw.get("requests", []):
            self.requests[r["id"]] = UserRequest(**_known(UserRequest, r))
        for pj in raw.get("projects", []):
            pj["workstreams"] = [Workstream(**_known(Workstream, w)) for w in pj.get("workstreams") or []]
            self.projects[pj["id"]] = Project(**_known(Project, pj))
        self._one_trust_domain()

    def _one_trust_domain(self) -> None:
        """Agent 不跨信任域（不变量 16）。旧版本的 public / shared Agent 可以被别人私聊、拉进别人的群组：
        别人和它的私聊归档为只读；内部群组里不属于群组所有者的 Agent 移出，群聊里记一条事件。已有委托与记录保留。"""
        for c in self.conversations.values():
            a = self.agents.get(c.agent_id or "")
            if c.kind == "dm" and not c.archived and a is not None and a.owner_id and c.owner_id \
                    and a.owner_id != c.owner_id:
                c.archived = True
        for t in self.tasks.values():
            if not t.owners:  # 旧版委托：归对话所有者
                conv = self.conversations.get(t.conversation_id)
                t.owners = [conv.owner_id] if conv and conv.owner_id else []
        for g in self.groups.values():
            if g.kind != "internal" or not g.owner_id:
                continue
            foreign = [m for m in g.member_ids if (a := self.agents.get(m)) and a.owner_id and a.owner_id != g.owner_id]
            if not foreign:
                continue
            g.member_ids = [m for m in g.member_ids if m not in foreign]
            chat = next((c for c in self.conversations.values()
                         if c.kind == "group" and c.group_id == g.id and not c.archived), None)
            if chat is not None:
                for m in foreign:
                    chat.messages.append(Message(id=new_id("msg"), role="event", agent_id=m,
                                                 text=f"{self.agents[m].name} 因为不属于你而被移出群组"
                                                      "（Agent 只能为它的所有者工作；跨用户协作请用联合群组）"))

    def claim_orphans(self, user_id: str) -> None:
        """第一个注册的用户认领所有还没有所有者的数据（旧版本的单人数据）。调用方持有锁。"""
        for a in self.agents.values():
            a.owner_id = a.owner_id or user_id
        for g in self.groups.values():
            g.owner_id = g.owner_id or user_id
        for c in self.conversations.values():
            c.owner_id = c.owner_id or user_id

    def _one_chat_each(self) -> None:
        """每个群组一个群聊、每个（用户, Agent）一个私聊（参照 Raft 的频道与 DM）。
        旧版本可以开多个对话：最近活跃的那个作为正式的群聊 / 私聊，其余归档为只读，记录不丢。
        没有群聊的群组补建一个。"""
        live: dict[tuple[str, ...], list[Conversation]] = {}
        for c in self.conversations.values():
            if c.archived:
                continue
            key = ("group", c.group_id) if c.kind == "group" else ("dm", c.agent_id, c.owner_id or "")
            if key[1] is None or (key[0] == "group" and key[1] not in self.groups) \
                    or (key[0] == "dm" and key[1] not in self.agents):
                c.archived = True
                continue
            live.setdefault(key, []).append(c)
        for convs in live.values():
            convs.sort(key=lambda c: -c.updated_at)
            for old in convs[1:]:
                old.archived = True
        for g in self.groups.values():
            if ("group", g.id) not in live:
                c = Conversation(id=new_id("conv"), kind="group", group_id=g.id, agent_id=None, title=g.name,
                                 owner_id=g.owner_id)
                self.conversations[c.id] = c
            else:
                live[("group", g.id)][0].title = g.name

    def _seed(self) -> None:
        for spec in SEED_AGENTS:
            a = AgentNode(id=new_id("agent"), **spec)
            self.agents[a.id] = a
        by_name = {a.name: a.id for a in self.agents.values()}
        g = Group(id=new_id("group"), name=SEED_GROUP["name"], description=SEED_GROUP["description"],
                  member_ids=[by_name[n] for n in SEED_GROUP["members"]])
        self.groups[g.id] = g
        self._one_chat_each()

    # ---------- 查询 ----------
    def agent_by_name(self, name: str) -> AgentNode | None:
        name = name.strip().lower()
        return next((a for a in self.agents.values() if a.name.lower() == name), None)

    def groups_of(self, agent_id: str) -> list[Group]:
        return [g for g in self.groups.values() if agent_id in g.member_ids]
