"""网络状态持久化：Agent、项目、对话、委托、共享知识都写入 data_dir/state.json。

网络状态属于系统；工作成果（报告、文件）由 Agent 按委托的输出要求写进各自的工作目录。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from dataclasses import asdict, fields

from .compat import current_call
from .models import (BRIEF_LIMIT, AgentNode, Approval, BriefSuggestion, Connection, ConnectorNeed, Conversation,
                     Decision, Delegation, Device, Draft, Held, ImBinding, ImChannel,
                     KnowledgeItem, Message, Project, Proposal, ProjectService, ExternalApp, AppGrant, Question, Release, ResourceRef, SandboxRecord, Schedule, Seat, Task, TaskStatus, User,
                     UserRequest,
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


SLOW_SAVE_S = 1.0   # 一次保存超过这么久就在标准错误里提醒（M2：先看得见，再换存储）


class Timings:
    """一类耗时的统计：次数、总和、最大、最近一次，以及最近 1000 次用来算分位数。只在内存里，重启清零。"""

    def __init__(self, keep: int = 1000) -> None:
        self.count = 0
        self.total = 0.0
        self.max = 0.0
        self.last = 0.0
        self._recent: deque[float] = deque(maxlen=keep)
        self._mu = threading.Lock()

    def add(self, seconds: float) -> None:
        with self._mu:
            self.count += 1
            self.total += seconds
            self.last = seconds
            self.max = max(self.max, seconds)
            self._recent.append(seconds)

    def snapshot(self) -> dict[str, float]:
        with self._mu:
            recent = sorted(self._recent)
            count, total, mx, last = self.count, self.total, self.max, self.last

        def q(p: float) -> float:
            return recent[min(len(recent) - 1, int(p * len(recent)))] if recent else 0.0

        return {"count": count, "total_s": round(total, 6), "max_s": round(mx, 6), "last_s": round(last, 6),
                "p50_s": round(q(0.5), 6), "p99_s": round(q(0.99), 6)}


class TimedRLock:
    """可重入锁，另记“等了多久才拿到”（只在拿不到、真的要等时计时，没有争用时只多一次非阻塞尝试）。
    引擎与存储共用这一把（engine.py 的 self._lock），所以它的等待时间就是“全网停住”的时间（docs/control-plane.md）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.waits = Timings()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.acquire(False):
            return True
        if not blocking:
            return False
        t0 = time.perf_counter()
        got = self._lock.acquire(True, timeout)
        self.waits.add(time.perf_counter() - t0)
        return got

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


def _fsync_dir(d: Path) -> None:
    """让目录项（改名）也落盘。Windows 打不开目录，跳过（NTFS 的改名本身有日志）。"""
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_durable(path: Path, text: str, backup: Path | None = None) -> None:
    """写一个状态文件，断电、被杀都不会留下空文件或半个文件（M2）：写临时文件 → fsync → 把旧的留成 backup →
    改名替换 → fsync 目录。backup 用硬链接留（任何时刻 path 都在）；文件系统不支持硬链接时退成改名
    （中间有一瞬只有 backup，读的一方会退到它，见 Store.__init__）。"""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if backup is not None and path.exists():
        link = backup.with_name(backup.name + ".tmp")
        try:
            link.unlink(missing_ok=True)
            os.link(path, link)
            os.replace(link, backup)
        except OSError:
            link.unlink(missing_ok=True)
            os.replace(path, backup)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


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
        self.backup = root / "state.json.bak"   # 上一次保存的样子（M2）：state.json 读不了时从它启动
        self.lock = TimedRLock()
        self.save_timings = Timings()           # 每次保存在锁内花的时间（管理后台“存储”与 /metrics）
        self.loaded_from = ""                   # 启动时从哪里读的状态：state.json / state.json.bak / ""（全新）
        self.agents: dict[str, AgentNode] = {}
        self.projects: dict[str, Project] = {}
        self.conversations: dict[str, Conversation] = {}
        self.tasks: dict[str, Task] = {}
        self.knowledge: dict[str, KnowledgeItem] = {}
        self.proposals: dict[str, Proposal] = {}
        self.users: dict[str, User] = {}
        self.sessions: dict[str, dict[str, Any]] = {}   # 登录令牌 → {user_id, created_at}
        self.resets: dict[str, dict[str, Any]] = {}     # 重置密码令牌的 sha256 → {user_id, by, created_at, expires_at}
        self.approvals: dict[str, Approval] = {}
        self.questions: dict[str, Question] = {}  # Agent 在运行中提的问题（等人或委托方回答）
        self.requests: dict[str, UserRequest] = {}  # 用户下发的任务（看板）
        self.devices: dict[str, Device] = {}   # 端侧连接器（令牌只存 sha256）
        self.connections: dict[str, Connection] = {}  # 我的连接器（凭证不在这里，见 vault.py）
        self.drafts: dict[str, Draft] = {}     # 助理在助理对话里起草、等主人确认的消息
        self.held: dict[str, Held] = {}        # 用到别处资料、等所有者确认的输出（不变量 22）
        self.schedules: dict[str, Schedule] = {}  # 主人的定时（到点唤醒自己 Agent 的私聊）
        self.releases: dict[str, Release] = {}    # 项目发出去的发布物（不变量 23）
        self.project_services: dict[str, ProjectService] = {}  # 项目服务（不变量 29）
        self.apps: dict[str, ExternalApp] = {}        # 外部应用（不变量 30）
        self.app_grants: dict[str, AppGrant] = {}     # 应用令牌（只存 sha256）
        self.im_channels: dict[str, ImChannel] = {}  # IM 机器人（个人的与部署方的）
        self.im_bindings: dict[str, ImBinding] = {}  # 人在 IM 机器人上的身份
        self.sandboxes: dict[str, SandboxRecord] = {}  # 云侧执行单元的沙箱台账（只在沙箱方式下有）
        # 对账时发现、控制面没有记录的沙箱（只剩卷）：执行单元 → 第一次发现的时间。过保留期才删，计时不随重启重来
        self.sandbox_orphans: dict[str, float] = {}
        found = self._read_source()
        if found is not None:
            self._load(*found)
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

    def release_dir(self, release_id: str) -> Path:
        """发布物的快照：每个版本一个 v<N>/（skills/、files/、page/、manifest.json），固定不变，只有网络写入。"""
        d = self.root / "releases" / release_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def task_dir(self, task_id: str) -> Path:
        d = self.root / "tasks" / task_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def task_path(self, task_id: str) -> Path:
        """委托的工作目录路径（不建目录）：沙箱方式下这是它的逻辑路径，内容在执行面目录树里。"""
        return self.root / "tasks" / task_id

    def unit_state_dir(self, kind: str, unit_id: str) -> Path:
        """云侧一个执行单元（席位 `<对话>/<Agent>` 或委托）自己的 dsh 状态目录：那一轮 dsh 的 HOME（会话、缓存）。
        不在任何工作目录里，外层沙箱里只有它和工作目录可写（docs/cloud-runtime.md 第 8 节阶段 0）。不在这里建：
        第一次在新方式下运行时才建，引擎据此判断要不要从共用的 ~/.dsh 搬旧会话。"""
        return self.root / "dsh-state" / ("tasks" if kind == "task" else "seats") / Path(*unit_id.split("/"))

    # ---------- 持久化 ----------
    def _read_source(self) -> tuple[dict[str, Any], Path] | None:
        """启动时读哪份文件（M2）。state.json 读得了就用它；读不了（空、半截、不是 JSON）或者不在而 .bak 在，
        就用 .bak 并在标准错误里报警，坏掉的那份改名留下（state.json.corrupt-<时间>）给人看，不覆盖。都没有就是全新的。"""
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8")), self.path
            except (OSError, ValueError) as e:
                if not self.backup.exists():
                    raise RuntimeError(f"{self.path} 读不了（{e}），也没有 {self.backup.name}：从备份恢复后再启动") from e
                bad = self.path.with_name(f"state.json.corrupt-{int(time.time())}")
                os.replace(self.path, bad)
                print(f"[matrix] 警告：{self.path} 读不了（{e}），已改名为 {bad.name}；"
                      f"改从 {self.backup.name}（上一次保存的状态）启动，最后一次保存的改动可能丢了", file=sys.stderr)
        elif self.backup.exists():
            print(f"[matrix] 警告：没有 {self.path.name}，只有 {self.backup.name}（上次保存到一半被打断）：从它启动",
                  file=sys.stderr)
        else:
            return None
        return json.loads(self.backup.read_text(encoding="utf-8")), self.backup

    def save(self) -> None:
        t0 = time.perf_counter()
        with self.lock:
            t1 = time.perf_counter()
            data = {
                "agents": [a.persist() for a in self.agents.values()],
                "projects": [p.to_dict() for p in self.projects.values()],
                "conversations": [c.to_dict() for c in self.conversations.values()],  # 含收件箱
                "tasks": [t.to_dict(full=True) for t in self.tasks.values()],
                "knowledge": [k.to_dict(content=True) for k in self.knowledge.values()],
                "proposals": [p.to_dict() for p in self.proposals.values()],
                "users": [asdict(u) for u in self.users.values()],
                "sessions": self.sessions,
                "password_resets": self.resets,
                "approvals": [a.to_dict() for a in self.approvals.values()],
                "questions": [q.to_dict() for q in self.questions.values()],
                "requests": [r.to_dict() for r in self.requests.values()],
                "devices": [asdict(d) for d in self.devices.values()],
                "connections": [asdict(c) for c in self.connections.values()],
                "drafts": [d.to_dict() for d in self.drafts.values()],
                "held": [h.to_dict() for h in self.held.values()],
                "schedules": [x.to_dict() for x in self.schedules.values()],
                "releases": [r.to_dict() for r in self.releases.values()],
                "project_services": [x.to_dict() for x in self.project_services.values()],
                "apps": [x.to_dict() for x in self.apps.values()],
                "app_grants": [asdict(x) for x in self.app_grants.values()],
                "im_channels": [x.to_dict() for x in self.im_channels.values()],
                "im_bindings": [x.to_dict() for x in self.im_bindings.values()],
                **({"sandboxes": [x.to_dict() for x in self.sandboxes.values()]} if self.sandboxes else {}),
                **({"sandbox_orphans": dict(self.sandbox_orphans)} if self.sandbox_orphans else {}),
            }
            write_durable(self.path, json.dumps(data, ensure_ascii=False, indent=1), backup=self.backup)
            spent = time.perf_counter() - t1
        self.save_timings.add(spent)
        if spent > SLOW_SAVE_S:
            print(f"[matrix] 保存状态用了 {spent:.2f} 秒（锁内，期间所有请求都在等）；"
                  f"等锁 {t1 - t0:.2f} 秒。状态大了就该换数据库存储（docs/control-plane.md）", file=sys.stderr)

    def metrics(self) -> dict[str, Any]:
        """存储的两个指标（M2）：每次保存在锁内的耗时，以及任何人等这把锁的时间。不含任何内容。"""
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"backend": "json", "path": self.path.name, "bytes": size, "loaded_from": self.loaded_from,
                "save": self.save_timings.snapshot(), "lock_wait": self.lock.waits.snapshot()}

    def _load(self, raw: dict[str, Any], source: Path) -> None:
        self.loaded_from = source.name
        notes: dict[str, str] = {}
        migrated = "groups" in raw  # 旧版：群组 + 项目两层
        if migrated:
            backup = self.root / "state.v1.json"
            if not backup.exists():
                shutil.copy2(source, backup)
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
            _connector_panel(p)
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
            t["evidence"] = [current_call(e) for e in t.get("evidence") or []]  # 改名前的网络工具名
            task = Task(**_known(Task, t))
            self.tasks[task.id] = task
        held_tasks = {h.get("context_id") for h in raw.get("held", []) if h.get("status") == "pending"}
        for task in self.tasks.values():
            if task.status != TaskStatus.RUNNING or task.id in held_tasks:
                continue  # 交付等所有者确认的任务没有在跑：照旧等确认（不变量 22）
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
        now = time.time()
        self.resets = {h: r for h, r in (raw.get("password_resets") or {}).items()
                       if r.get("user_id") in self.users and float(r.get("expires_at") or 0) > now}
        for a in raw.get("approvals", []):
            ap = Approval(**_known(Approval, a))
            if ap.status == "pending":  # 等审批的那次运行已随重启结束
                ap.status, ap.note = "cancelled", "服务重启，申请作废"
            self.approvals[ap.id] = ap
        for x in raw.get("questions", []):  # 旧数据没有：缺省为空
            q = Question(**_known(Question, x))
            if q.status == "pending":  # 等回答的那次运行已随重启结束
                q.status, q.note = "cancelled", "服务重启，问题作废"
            self.questions[q.id] = q
        # UserRequest 新增审计字段缺失时由 dataclass 默认 None，旧状态无需迁移。
        for d in raw.get("devices", []):
            self.devices[d["id"]] = Device(**_known(Device, d))
        for r in raw.get("requests", []):
            self.requests[r["id"]] = UserRequest(**_known(UserRequest, r))
        for c in raw.get("connections", []):
            self.connections[c["id"]] = Connection(**_known(Connection, c))
        for d in raw.get("drafts", []):  # 旧数据没有这两项：缺省为空
            self.drafts[d["id"]] = Draft(**_known(Draft, d))
        for h in raw.get("held", []):
            self.held[h["id"]] = Held(**_known(Held, h))
        for x in raw.get("schedules", []):
            self.schedules[x["id"]] = Schedule(**_known(Schedule, x))
        for r in raw.get("releases", []):  # 旧数据没有：缺省为空
            self.releases[r["id"]] = Release(**_known(Release, r))
        for x in raw.get("project_services", []):  # 旧数据没有：缺省为空
            self.project_services[x["id"]] = ProjectService(**_known(ProjectService, x))
        for x in raw.get("apps", []):
            self.apps[x["id"]] = ExternalApp(**_known(ExternalApp, x))
        for x in raw.get("app_grants", []):
            self.app_grants[x["id"]] = AppGrant(**_known(AppGrant, x))
        for x in raw.get("im_channels", []):  # 旧数据没有：缺省为空
            self.im_channels[x["id"]] = ImChannel(**_known(ImChannel, x))
        for x in raw.get("im_bindings", []):
            self.im_bindings[x["id"]] = ImBinding(**_known(ImBinding, x))
        for x in raw.get("sandboxes", []):  # 旧数据没有：缺省为空
            self.sandboxes[x["unit"]] = SandboxRecord(**_known(SandboxRecord, x))
        self.sandbox_orphans = {str(k): float(v) for k, v in (raw.get("sandbox_orphans") or {}).items()
                                if isinstance(v, (int, float))}
        self._one_trust_domain()
        self.default_assistants()
        if migrated:
            self.save()

    def default_assistants(self) -> None:
        """还没指定助理的用户：取他最早创建、还在的 Agent（没有 Agent 的等创建第一个时再定）。"""
        for u in self.users.values():
            if u.assistant_id in self.agents and self.agents[u.assistant_id].owner_id == u.id:
                continue
            mine = sorted((a for a in self.agents.values() if a.owner_id == u.id and not a.public_status),
                          key=lambda a: a.created_at)  # 公共 Agent 不当助理（不变量 27）
            u.assistant_id = mine[0].id if mine else None

    def _one_trust_domain(self) -> None:
        """Agent 不跨信任域（不变量 16）。旧版本的 public / shared Agent 可以被别人私聊、拉进别人的项目：
        别人和它的私聊归档为只读；内部项目里不属于项目所有者的 Agent 移出，群聊里记一条事件。已有委托与记录保留。
        维护人算自己人；管理员批准过的公共 Agent 本来就给所有人用（不变量 27），都不动。"""
        for c in self.conversations.values():
            a = self.agents.get(c.agent_id or "")
            if c.kind == "dm" and not c.archived and a is not None and a.owner_id and c.owner_id \
                    and not a.maintained_by(c.owner_id) and not a.is_public:
                c.archived = True
        for t in self.tasks.values():
            if not t.owners:  # 旧版委托：归对话所有者
                conv = self.conversations.get(t.conversation_id)
                t.owners = [conv.owner_id] if conv and conv.owner_id else []
        for p in self.projects.values():
            if p.shared or not p.owner_id:
                continue
            foreign = [m for m in p.member_ids if (a := self.agents.get(m)) and a.owner_id
                       and not a.maintained_by(p.owner_id) and not a.is_public]
            if not foreign:
                continue
            p.member_ids = [m for m in p.member_ids if m not in foreign]
            chat = next((c for c in self.conversations.values()
                         if c.kind == "project" and c.project_id == p.id and not c.archived), None)
            if chat is not None:
                for m in foreign:
                    chat.messages.append(Message(id=new_id("msg"), role="event", agent_id=m,
                                                 text=f"{self.agents[m].name} 因为不属于你而被移出项目"
                                                      "（Agent 只能为它的所有者工作；跨用户协作请邀请对方加入项目）"))

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


def _connector_panel(p: dict[str, Any]) -> None:
    """连接器面板之前的项目：所有者逐个打开的连接器（connections_enabled）改成“这个项目里都可以”；
    关联了资源引用的“需要的连接器”并进那条链接（ResourceRef.connector_id），用途补进链接说明。只做一次。"""
    old = p.pop("connections_enabled", None)
    if isinstance(old, dict) and "connector_grants" not in p:
        p["connector_grants"] = {a: {cid: "always" for cid in ids} for a, ids in old.items() if ids}
    refs = {r.id: r for r in p["refs"]}
    keep = []
    for n in p["connector_needs"]:
        ref = refs.get(n.ref_id or "")
        if ref is None:
            n.ref_id = None
            keep.append(n)
            continue
        ref.connector_id = ref.connector_id or n.connector_id
        if n.purpose and n.purpose not in ref.note:
            ref.note = f"{ref.note}；{n.purpose}" if ref.note else n.purpose
    p["connector_needs"] = keep

