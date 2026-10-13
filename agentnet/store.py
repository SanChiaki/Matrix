"""网络状态持久化：Agent、群组、对话、委托、共享知识都写入 data_dir/state.json。

网络状态属于系统；工作成果（报告、文件）由 Agent 按委托的输出要求写进各自的工作目录。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .models import (AgentNode, Conversation, Delegation, Group, KnowledgeItem, Message, Task, TaskStatus,
                     new_id)

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
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = root / "state.json"
        self.lock = threading.RLock()
        self.agents: dict[str, AgentNode] = {}
        self.groups: dict[str, Group] = {}
        self.conversations: dict[str, Conversation] = {}
        self.tasks: dict[str, Task] = {}
        self.knowledge: dict[str, KnowledgeItem] = {}
        if self.path.exists():
            self._load()
        else:
            self._seed()
            self.save()

    # ---------- 目录 ----------
    def conversation_dir(self, conv_id: str) -> Path:
        d = self.root / "conversations" / conv_id
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
                "conversations": [c.to_dict() for c in self.conversations.values()],
                "tasks": [t.to_dict(full=True) for t in self.tasks.values()],
                "knowledge": [k.to_dict(content=True) for k in self.knowledge.values()],
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)

    def _load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for a in raw.get("agents", []):
            a.pop("memory", None)  # 旧版本的 Agent Memory，已移除
            self.agents[a["id"]] = AgentNode(**a)
        for g in raw.get("groups", []):
            self.groups[g["id"]] = Group(**g)
        for c in raw.get("conversations", []):
            msgs = [Message(**m) for m in c.pop("messages", [])]
            conv = Conversation(**c, messages=msgs)
            conv.status = "idle"  # 重启时中断的轮次不再继续
            self.conversations[conv.id] = conv
        for t in raw.get("tasks", []):
            t.pop("summary", None)
            t["delegation"] = Delegation(**t["delegation"])
            t["status"] = TaskStatus(t["status"])
            task = Task(**t)
            if task.status == TaskStatus.RUNNING:
                task.status, task.error = TaskStatus.FAILED, "服务重启，任务中断"
            self.tasks[task.id] = task
        for k in raw.get("knowledge", []):
            self.knowledge[k["key"]] = KnowledgeItem(**k)

    def _seed(self) -> None:
        for spec in SEED_AGENTS:
            a = AgentNode(id=new_id("agent"), **spec)
            self.agents[a.id] = a
        by_name = {a.name: a.id for a in self.agents.values()}
        g = Group(id=new_id("group"), name=SEED_GROUP["name"], description=SEED_GROUP["description"],
                  member_ids=[by_name[n] for n in SEED_GROUP["members"]])
        self.groups[g.id] = g

    # ---------- 查询 ----------
    def agent_by_name(self, name: str) -> AgentNode | None:
        name = name.strip().lower()
        return next((a for a in self.agents.values() if a.name.lower() == name), None)

    def groups_of(self, agent_id: str) -> list[Group]:
        return [g for g in self.groups.values() if agent_id in g.member_ids]
