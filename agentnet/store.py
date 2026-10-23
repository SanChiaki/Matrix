"""网络状态持久化：Agent、项目、对话、委托、共享知识都写入 data_dir/state.json。

网络状态属于系统；工作成果（报告、文件）由 Agent 按委托的输出要求写进各自的工作目录。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from dataclasses import asdict, fields

from .models import (BRIEF_LIMIT, AgentNode, Approval, BriefSuggestion, Connection, ConnectorNeed, Conversation,
                     Decision, Delegation, Device,
                     KnowledgeItem, Message, Project, Proposal, ResourceRef, Seat, Task, TaskStatus, User, UserRequest,
                     new_id, seat_id)


def _known(cls: type, d: dict[str, Any]) -> dict[str, Any]:
    """只取 dataclass 认识的字段：旧版 state.json 缺字段用默认值，多余字段（已移除的功能）丢弃。"""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


def _load_conversation(c: dict[str, Any]) -> Conversation:
    msgs = [Message(**_known(Message, m)) for m in c.pop("messages", [])]
    if "kind" in c:
        seats = {a: Seat(**_known(Seat, s)) for a, s in (c.pop("seats", None) or {}).items()}
        return Conversation(**_known(Conversation, c), seats=seats, messages=msgs)
    # 旧版：对话 = 一个“主 Agent”。迁移成只有这一个席位的项目群聊（在项目里）或私聊（不在项目里）；
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
    project = c.get("project_id")
    title = c.get("title", "")
    if project and title.startswith("与 ") and title.endswith(" 的对话"):  # 旧的默认标题是按主 Agent 起的
        title = ""
    return Conversation(id=c["id"], kind="project" if project else "dm", project_id=project,
                        agent_id=None if project else agent_id, title=title, seats={agent_id: seat},
                        messages=msgs, created_at=c.get("created_at", 0), updated_at=c.get("updated_at", 0))


def _move_dir(src: Path, dst: Path) -> None:
    """把一个目录搬到新位置；目标已存在时合并（同名文件以已有的为准）。重复执行无害。"""
    if not src.is_dir():
        return
    if dst.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=lambda a, b: None if Path(b).exists()
                        else shutil.copy2(a, b))
        shutil.rmtree(src)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))


def _merge_groups(raw: dict[str, Any], root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """旧版（群组 + 项目两层）→ 只有项目一层（docs/projects.md 第 5 节）。改写 raw 并搬好文件目录，
    返回新格式的 raw 和要写进项目群聊的说明（项目 id → 文本）。
    - 没挂项目的群组（包括联合群组、别人的群组）原样成为项目；挂在旧项目里的联合群组带上共享给它的项目文件。
    - 旧项目吸收挂进来的、同一所有者的内部群组：成员取并集，纲要、决定、文件以项目为准，群组的带上来源，
      群聊里最近活跃的留作项目群聊（_one_chat_each），其余归档；交接、负责人、方面不再有。"""
    groups = {g["id"]: g for g in raw.pop("groups", [])}
    olds = {p["id"]: p for p in raw.get("projects", [])}
    handoffs = raw.pop("handoffs", [])
    absorbed = {gid: g["project_id"] for gid, g in groups.items()
                if (p := olds.get(g.get("project_id") or "")) is not None
                and g.get("kind", "internal") == "internal" and g.get("owner_id") == p.get("owner_id")}
    mapping = {gid: absorbed.get(gid, gid) for gid in groups}
    projects: list[dict[str, Any]] = []
    for gid, g in groups.items():
        if gid in absorbed:
            continue
        d = {k: v for k, v in g.items() if k not in ("project_id", "workstream_id", "projects")}
        d["refs"] = g.get("refs") or g.get("projects") or []  # 更早的版本叫“项目引用”
        projects.append(d)
        _move_dir(root / "groups" / gid, root / "projects" / gid)
        p = olds.get(g.get("project_id") or "")
        if p is not None and g.get("kind") == "joint":  # 共享给它的项目文件拷进它自己的项目
            dst = root / "projects" / gid / "files"
            for name, to in (p.get("shares") or {}).items():
                src = root / "projects" / p["id"] / "files" / name
                if gid in to and src.is_file() and not (dst / name).exists():
                    dst.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst / name)
    notes: dict[str, str] = {}
    for pid, p in olds.items():
        mine = [g for gid, g in groups.items() if absorbed.get(gid) == pid]
        briefs = [(None, p.get("brief") or "")] + [(g["name"], g.get("brief") or "") for g in mine]
        briefs = [(n, b.strip()) for n, b in briefs if b.strip()]
        brief = briefs[0][1] if len(briefs) == 1 else "\n\n".join(
            b if n is None else f"### 原「{n}」的纲要\n{b}" for n, b in briefs)
        decisions = list(p.get("decisions") or [])
        for g in mine:
            decisions += [{**d, "text": f"（原「{g['name']}」）{d.get('text', '')}"} for d in g.get("decisions") or []]
        refs = list({r["id"]: r for g in mine for r in (g.get("refs") or g.get("projects") or [])}.values())
        members = list(dict.fromkeys(m for g in mine for m in g.get("member_ids") or []))
        projects.append({"id": pid, "name": p.get("name", ""), "description": p.get("description", ""),
                         "member_ids": members, "owner_id": p.get("owner_id"), "kind": "internal",
                         "brief": brief[:BRIEF_LIMIT], "decisions": decisions, "refs": refs,
                         "created_at": p.get("created_at", time.time())})
        dst = root / "projects" / pid / "files"
        log = root / "projects" / pid / "log.jsonl"
        for g in mine:
            src = root / "groups" / g["id"]
            for f in sorted((src / "files").iterdir()) if (src / "files").is_dir() else []:
                name, n = f.name, 1
                while (dst / name).exists():  # 重名的加上来源群组的名字，不覆盖项目里的
                    name = f"{g['name']}-{f.name}" if n == 1 else f"{g['name']}-{n}-{f.name}"
                    n += 1
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst / name)
            if (src / "log.jsonl").is_file():
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open("a", encoding="utf-8") as out:
                    out.write((src / "log.jsonl").read_text(encoding="utf-8"))
            shutil.rmtree(src, ignore_errors=True)
        dropped = sum(1 for h in handoffs if h.get("project_id") == pid and h.get("status") in ("pending", "scheduled", "sent")
                      and not h.get("returned_at"))
        if mine:
            notes[pid] = ("这个项目由原来的群组" + "".join(f"「{g['name']}」" for g in mine) + "合并而成："
                          "成员都在这里，可以直接互相委托；原来的群聊已归档为只读。"
                          + (f"{dropped} 条未完成的跨群组交接已作废，还需要的话直接在这里交代或委托。" if dropped else ""))
    gone = root / "groups"
    for leftover in sorted(gone.iterdir()) if gone.is_dir() else []:  # 已删除群组留下的档案
        _move_dir(leftover, root / "projects" / leftover.name)
    shutil.rmtree(gone, ignore_errors=True)
    ids = {p["id"] for p in projects}
    for c in raw.get("conversations", []):
        if c.get("kind") == "group":
            c["kind"] = "project"
        gid = c.pop("group_id", None)
        c["project_id"] = mapping.get(gid) if gid else None
        for seat in (c.get("seats") or {}).values():
            seat["archive_seen"] = -1  # 档案的样子变了（文件在 project/、纲要合并）：下一轮重新推送
    chat_of = {c["id"]: c["project_id"] for c in raw.get("conversations", []) if c.get("project_id")}
    for key in ("tasks", "knowledge", "proposals"):
        for x in raw.get(key, []):
            gid = x.pop("group_id", "")
            x["project_id"] = mapping.get(gid, gid)
            x.pop("project_seen", None)
            if key == "tasks":
                x["archive_seen"] = -1
    for r in raw.get("requests", []):
        r["project_id"] = chat_of.get(r.get("conversation_id", "")) or (r.get("project_id") if r.get("project_id") in ids else None)
    raw["projects"] = projects
    return raw, notes


SEED_AGENTS: list[dict[str, Any]] = [
    {"name": "Product Strategy Agent",
     "description": "产品机会负责人：理解目标、判断需要哪些能力、在项目内委托成员、整合结论并给出建议。",
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

SEED_PROJECT = {
    "name": "AI 产品机会研究",
    "description": "研究新的 AI 产品机会：市场、技术、竞争。",
    "members": ["Product Strategy Agent", "Market Research Agent", "Technical Analysis Agent",
                "Competitive Intelligence Agent"],
}


class Store:
    def __init__(self, root: Path, seed: bool = True):
        """seed：全新的数据目录是否预置种子 Agent 与研究项目（测试、离线演练用；服务默认不预置，由用户自己创建）。"""
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = root / "state.json"
        self.lock = threading.RLock()
        self.agents: dict[str, AgentNode] = {}
        self.projects: dict[str, Project] = {}
        self.conversations: dict[str, Conversation] = {}
        self.tasks: dict[str, Task] = {}
        self.knowledge: dict[str, KnowledgeItem] = {}
        self.proposals: dict[str, Proposal] = {}
        self.users: dict[str, User] = {}
        self.sessions: dict[str, dict[str, Any]] = {}   # 登录令牌 → {user_id, created_at}
        self.approvals: dict[str, Approval] = {}
        self.requests: dict[str, UserRequest] = {}  # 用户下发的任务（看板）
        self.devices: dict[str, Device] = {}   # 端侧连接器（令牌只存 sha256）
        self.connections: dict[str, Connection] = {}  # 我的连接器（凭证不在这里，见 vault.py）
        if self.path.exists():
            self._load()
        else:
            if seed:
                self._seed()
            self.save()

    # ---------- 目录 ----------
    def seat_dir(self, conv_id: str, agent_id: str | None) -> Path:
        """席位工作目录：项目群聊里每个成员一个子目录，私聊、项目会话直接用对话目录。"""
        d = self.root / "conversations" / conv_id
        if agent_id:
            d = d / agent_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def project_dir(self, project_id: str) -> Path:
        """项目档案的文件区：不属于任何代码仓库。只有网络写入（publish_file、人上传），运行时只读挂载。"""
        d = self.root / "projects" / project_id / "files"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def project_skills_dir(self, project_id: str) -> Path:
        """项目技能：项目档案的一部分，和 files/ 平级。只有网络写入（人的操作、人批准的提议）。"""
        d = self.root / "projects" / project_id / "skills"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def project_plugins_dir(self, project_id: str) -> Path:
        """项目装了的插件包（固定版本的拷贝）：项目档案的一部分，只有主持人安装 / 升级 / 卸载。"""
        d = self.root / "projects" / project_id / "plugins"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def plugin_data_dir(self, project_id: str, plugin: str) -> Path:
        """插件的记录：每张表一个 JSON 文件。卸载不删，重新装回来还在。"""
        d = self.root / "projects" / project_id / "plugin-data" / plugin
        d.mkdir(parents=True, exist_ok=True)
        return d

    def user_skills_dir(self, user_id: str) -> Path:
        """我的技能：人维护的技能库，勾给自己的 Agent。和 Agent 的私有目录分开。"""
        d = self.root / "users" / user_id / "skills"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def connector_skills_dir(self, connector_id: str) -> Path:
        """连接器自带的技能：从连接器目录展开出来的缓存，目录变了就重写。"""
        d = self.root / "cache" / "connector-skills" / connector_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def connection_dir(self, connection_id: str) -> Path:
        """一个连接的调用记录（calls.jsonl）。只给授权人看；不含凭证与调用内容。"""
        d = self.root / "connections" / connection_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def agent_dir(self, agent_id: str) -> Path:
        """Agent 的持久私有目录：跨对话、跨任务保留，内容由 Agent 自己决定，网络不读。"""
        d = self.root / "agents" / agent_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def attachment_dir(self, conv_id: str) -> Path:
        """对话的附件区：按 sha256 存放、只读。不在任何工作目录里（私聊的席位工作目录就是对话目录），
        运行时只拿到拷贝。"""
        d = self.root / "attachments" / conv_id
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
                "projects": [p.to_dict() for p in self.projects.values()],
                "conversations": [c.to_dict() for c in self.conversations.values()],  # 含收件箱
                "tasks": [t.to_dict(full=True) for t in self.tasks.values()],
                "knowledge": [k.to_dict(content=True) for k in self.knowledge.values()],
                "proposals": [p.to_dict() for p in self.proposals.values()],
                "users": [asdict(u) for u in self.users.values()],
                "sessions": self.sessions,
                "approvals": [a.to_dict() for a in self.approvals.values()],
                "requests": [r.to_dict() for r in self.requests.values()],
                "devices": [asdict(d) for d in self.devices.values()],
                "connections": [asdict(c) for c in self.connections.values()],
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)

    def _load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        notes: dict[str, str] = {}
        migrated = "groups" in raw  # 旧版：群组 + 项目两层
        if migrated:
            backup = self.root / "state.v1.json"
            if not backup.exists():
                shutil.copy2(self.path, backup)
            raw, notes = _merge_groups(raw, self.root)
        for a in raw.get("agents", []):
            a.pop("memory", None)  # 旧版本的 Agent Memory，已移除
            if "connections" not in a and a.get("device_runtime"):
                # 有连接器之前，电脑上的 Agent 一直能用那个 AI 工具里自己配的服务；现在要有 connector.use 并勾选，
                # 保留这份能力（勾选在设备上线、报告服务时补上，见 Network.register_runtime）
                a["authority"] = list(dict.fromkeys([*(a.get("authority") or []), "connector.use"]))
            self.agents[a["id"]] = AgentNode(**_known(AgentNode, a))
        for p in raw.get("projects", []):
            p["decisions"] = [Decision(**_known(Decision, d)) for d in p.get("decisions") or []]
            p["refs"] = [ResourceRef(**_known(ResourceRef, r)) for r in p.get("refs") or []]
            p["suggestions"] = [BriefSuggestion(**_known(BriefSuggestion, x)) for x in p.get("suggestions") or []]
            p["connector_needs"] = [ConnectorNeed(**_known(ConnectorNeed, n)) for n in p.get("connector_needs") or []]
            self.projects[p["id"]] = Project(**_known(Project, p))
        for c in raw.get("conversations", []):
            conv = _load_conversation(c)
            if not conv.title:
                p = self.projects.get(conv.project_id or "")
                conv.title = p.name if p else "群聊"
            conv.status = "idle"  # 重启时中断的轮次不再继续；收件箱保留，下一次被 @ 或“继续”时送达
            self.conversations[conv.id] = conv
        self._one_chat_each()
        for pid, text in notes.items():
            chat = next((c for c in self.conversations.values()
                         if c.kind == "project" and c.project_id == pid and not c.archived), None)
            if chat is not None:
                chat.messages.append(Message(id=new_id("msg"), role="event", text=text))
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
            item = KnowledgeItem(**_known(KnowledgeItem, k))
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
        for c in raw.get("connections", []):
            self.connections[c["id"]] = Connection(**_known(Connection, c))
        self._one_trust_domain()
        self.default_assistants()
        if migrated:
            self.save()

    def default_assistants(self) -> None:
        """还没指定助理的用户：取他最早创建、还在的 Agent（没有 Agent 的等创建第一个时再定）。"""
        for u in self.users.values():
            if u.assistant_id in self.agents and self.agents[u.assistant_id].owner_id == u.id:
                continue
            mine = sorted((a for a in self.agents.values() if a.owner_id == u.id), key=lambda a: a.created_at)
            u.assistant_id = mine[0].id if mine else None

    def _one_trust_domain(self) -> None:
        """Agent 不跨信任域（不变量 16）。旧版本的 public / shared Agent 可以被别人私聊、拉进别人的项目：
        别人和它的私聊归档为只读；内部项目里不属于项目所有者的 Agent 移出，群聊里记一条事件。已有委托与记录保留。"""
        for c in self.conversations.values():
            a = self.agents.get(c.agent_id or "")
            if c.kind == "dm" and not c.archived and a is not None and a.owner_id and c.owner_id \
                    and a.owner_id != c.owner_id:
                c.archived = True
        for t in self.tasks.values():
            if not t.owners:  # 旧版委托：归对话所有者
                conv = self.conversations.get(t.conversation_id)
                t.owners = [conv.owner_id] if conv and conv.owner_id else []
        for p in self.projects.values():
            if p.kind != "internal" or not p.owner_id:
                continue
            foreign = [m for m in p.member_ids if (a := self.agents.get(m)) and a.owner_id and a.owner_id != p.owner_id]
            if not foreign:
                continue
            p.member_ids = [m for m in p.member_ids if m not in foreign]
            chat = next((c for c in self.conversations.values()
                         if c.kind == "project" and c.project_id == p.id and not c.archived), None)
            if chat is not None:
                for m in foreign:
                    chat.messages.append(Message(id=new_id("msg"), role="event", agent_id=m,
                                                 text=f"{self.agents[m].name} 因为不属于你而被移出项目"
                                                      "（Agent 只能为它的所有者工作；跨用户协作请用联合项目）"))

    def claim_orphans(self, user_id: str) -> None:
        """第一个注册的用户认领所有还没有所有者的数据（旧版本的单人数据）。调用方持有锁。"""
        for a in self.agents.values():
            a.owner_id = a.owner_id or user_id
        for p in self.projects.values():
            p.owner_id = p.owner_id or user_id
        for c in self.conversations.values():
            c.owner_id = c.owner_id or user_id
        self.default_assistants()

    def _one_chat_each(self) -> None:
        """每个项目一个项目群聊、每个（用户, Agent）一个私聊（参照 Raft 的频道与 DM）。
        旧版本可以开多个对话：最近活跃的那个作为正式的群聊 / 私聊，其余归档为只读，记录不丢。
        没有群聊的项目补建一个。项目会话本来就可以有多个，只在项目不在了时归档。"""
        live: dict[tuple[str, ...], list[Conversation]] = {}
        for c in self.conversations.values():
            if c.archived:
                continue
            if c.kind == "session":
                c.archived = c.project_id not in self.projects
                continue
            key = ("project", c.project_id) if c.kind == "project" else ("dm", c.agent_id, c.owner_id or "")
            if key[1] is None or (key[0] == "project" and key[1] not in self.projects) \
                    or (key[0] == "dm" and key[1] not in self.agents):
                c.archived = True
                continue
            live.setdefault(key, []).append(c)
        for convs in live.values():
            convs.sort(key=lambda c: -c.updated_at)
            for old in convs[1:]:
                old.archived = True
        for p in self.projects.values():
            if ("project", p.id) not in live:
                c = Conversation(id=new_id("conv"), kind="project", project_id=p.id, agent_id=None, title=p.name,
                                 owner_id=p.owner_id)
                self.conversations[c.id] = c
            else:
                live[("project", p.id)][0].title = p.name

    def _seed(self) -> None:
        for spec in SEED_AGENTS:
            a = AgentNode(id=new_id("agent"), **spec)
            self.agents[a.id] = a
        by_name = {a.name: a.id for a in self.agents.values()}
        p = Project(id=new_id("prj"), name=SEED_PROJECT["name"], description=SEED_PROJECT["description"],
                    member_ids=[by_name[n] for n in SEED_PROJECT["members"]])
        self.projects[p.id] = p
        self._one_chat_each()

    # ---------- 查询 ----------
    def agent_by_name(self, name: str) -> AgentNode | None:
        name = name.strip().lower()
        return next((a for a in self.agents.values() if a.name.lower() == name), None)

    def projects_of(self, agent_id: str) -> list[Project]:
        return [p for p in self.projects.values() if agent_id in p.member_ids]
