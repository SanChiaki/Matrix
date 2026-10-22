"""旧版 state.json（群组 + 项目两层）迁移为只有项目一层（docs/projects.md 第 5 节）。"""

from __future__ import annotations

import json
from pathlib import Path

from agentnet.store import Store


def _agent(aid: str, owner: str = "u1", created: float = 1.0) -> dict:
    return {"id": aid, "name": aid.upper(), "description": "", "capabilities": [], "authority": [],
            "owner_id": owner, "created_at": created}


def _group(gid: str, members: list[str], owner: str = "u1", **kw) -> dict:
    return {"id": gid, "name": kw.pop("name", gid), "description": "", "member_ids": members, "owner_id": owner,
            "kind": kw.pop("kind", "internal"), **kw}


def _chat(cid: str, gid: str, owner: str = "u1", updated: float = 1.0, seats: dict | None = None) -> dict:
    return {"id": cid, "kind": "group", "group_id": gid, "agent_id": None, "title": gid, "owner_id": owner,
            "seats": seats or {}, "messages": [], "updated_at": updated}


def _task(tid: str, conv: str, gid: str) -> dict:
    return {"id": tid, "conversation_id": conv, "group_id": gid, "delegator_id": "a1", "delegatee_id": "a2",
            "parent_task_id": None, "depth": 1, "status": "completed", "project_seen": "x:1:",
            "delegation": {"mission": "m", "authority": [], "constraints": [], "expected_outcome": "o"}}


def _write(root: Path, state: dict, files: dict[str, str] | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    for rel, text in (files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _users(*ids: str) -> list[dict]:
    return [{"id": u, "email": f"{u}@x.io", "password_hash": "", "salt": ""} for u in ids]


def test_standalone_group_becomes_project(tmp_path: Path):
    """没挂项目的群组原样成为项目：成员、纲要、决定、资源引用、文件、群聊、委托、知识、提议都跟过去，旧数据另存一份。"""
    state = {
        "agents": [_agent("a1"), _agent("a2")],
        "users": _users("u1"),
        "groups": [_group("group-1", ["a1", "a2"], name="研究组", brief="目标",
                          decisions=[{"id": "dec-1", "text": "用中文"}],
                          refs=[{"id": "ref-1", "name": "repo", "location": "git@x"}])],
        "conversations": [_chat("conv-1", "group-1", seats={"a1": {"agent_id": "a1", "workdir": "conversations/conv-1/a1",
                                                                      "archive_seen": 3, "project_seen": "p:1:"}})],
        "tasks": [_task("task-1", "conv-1", "group-1")],
        "knowledge": [{"key": "task-1:result", "title": "t", "content": "c", "group_id": "group-1",
                       "owner_task_id": "task-1", "visible_to": ["task-1"]}],
        "proposals": [{"id": "prop-1", "proposer_id": "a1", "context_kind": "seat", "context_id": "conv-1/a1",
                       "conversation_id": "conv-1", "group_id": "group-1", "name": "N", "description": "d",
                       "capabilities": [], "authority": [], "max_authority": []}],
        "requests": [{"id": "req-1", "conversation_id": "conv-1", "agent_id": "a1", "requester_id": "u1",
                      "message_id": "m1", "title": "t", "expected_outcome": "o", "workstream_id": "ws-x"}],
        "projects": [], "handoffs": [],
    }
    _write(tmp_path, state, {"groups/group-1/files/a.md": "A", "groups/group-1/log.jsonl": "{}\n"})
    s = Store(tmp_path)
    p = s.projects["group-1"]
    assert (p.name, p.member_ids, p.brief, p.owner_id, p.kind) == ("研究组", ["a1", "a2"], "目标", "u1", "internal")
    assert [d.text for d in p.decisions] == ["用中文"] and [r.name for r in p.refs] == ["repo"]
    assert (s.project_dir(p.id) / "a.md").read_text(encoding="utf-8") == "A"
    assert (s.project_dir(p.id).parent / "log.jsonl").is_file()
    assert not (tmp_path / "groups").exists()
    c = s.conversations["conv-1"]
    assert (c.kind, c.project_id, c.archived) == ("project", "group-1", False)
    assert c.seats["a1"].archive_seen == -1  # 档案的样子变了：下一轮重新推送
    assert s.tasks["task-1"].project_id == "group-1"
    assert s.knowledge["task-1:result"].project_id == "group-1"
    assert s.proposals["prop-1"].project_id == "group-1"
    assert s.requests["req-1"].project_id == "group-1"
    assert (tmp_path / "state.v1.json").is_file()  # 迁移前的原样备份
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "groups" not in saved and "handoffs" not in saved
    assert saved["projects"][0]["member_ids"] == ["a1", "a2"]


def test_project_with_internal_groups_merges(tmp_path: Path):
    """挂了多个内部群组的旧项目合成一个：成员取并集，纲要、决定、文件以项目为准、各群组的带上来源，
    最近活跃的群聊留作项目群聊、其余归档；未完成的交接作废并在群聊里记一条事件。"""
    state = {
        "agents": [_agent("a1"), _agent("a2"), _agent("a3")],
        "users": _users("u1"),
        "projects": [{"id": "prj-1", "name": "上线", "owner_id": "u1", "description": "大事", "brief": "项目目标",
                      "decisions": [{"id": "dec-p", "text": "按 PRD 做"}],
                      "workstreams": [{"id": "ws-1", "name": "开发"}], "lead_agent_id": "a1", "autonomy": "lead"}],
        "groups": [_group("group-1", ["a1", "a2"], name="开发组", brief="开发目标", project_id="prj-1",
                          workstream_id="ws-1", decisions=[{"id": "dec-g", "text": "用 Python"}]),
                   _group("group-2", ["a2", "a3"], name="测试组", project_id="prj-1")],
        "conversations": [_chat("conv-1", "group-1", updated=10), _chat("conv-2", "group-2", updated=20)],
        "tasks": [_task("task-1", "conv-1", "group-1")],
        "requests": [{"id": "req-1", "conversation_id": "conv-1", "agent_id": "a1", "requester_id": "u1",
                      "message_id": "m1", "title": "t", "expected_outcome": "o", "project_id": "prj-1",
                      "workstream_id": "ws-1"}],
        "handoffs": [{"id": "ho-1", "project_id": "prj-1", "owner_id": "u1", "from_group_id": "group-1",
                      "from_agent_id": "a1", "context_kind": "seat", "context_id": "conv-1/a1",
                      "conversation_id": "conv-1", "to_group_id": "group-2", "to_agent_id": "a3", "text": "测一下",
                      "status": "pending"}],
    }
    _write(tmp_path, state, {"projects/prj-1/files/prd.md": "PRD", "projects/prj-1/files/dup.md": "项目的",
                             "groups/group-1/files/dup.md": "开发组的", "groups/group-1/files/impl.md": "I",
                             "groups/group-2/files/plan.md": "T"})
    s = Store(tmp_path)
    assert set(s.projects) == {"prj-1"}
    p = s.projects["prj-1"]
    assert (p.name, p.description, p.owner_id, p.kind) == ("上线", "大事", "u1", "internal")
    assert p.member_ids == ["a1", "a2", "a3"]
    assert p.brief.startswith("项目目标") and "开发组" in p.brief and "开发目标" in p.brief
    texts = [d.text for d in p.decisions]
    assert texts[0] == "按 PRD 做" and any("用 Python" in t and "开发组" in t for t in texts)
    files = {f.name: f.read_text(encoding="utf-8") for f in s.project_dir(p.id).iterdir()}
    assert files["prd.md"] == "PRD" and files["dup.md"] == "项目的" and files["impl.md"] == "I" and files["plan.md"] == "T"
    assert "开发组的" in [v for k, v in files.items() if k != "dup.md"]  # 重名的加了前缀，没有覆盖
    live, old = s.conversations["conv-2"], s.conversations["conv-1"]
    assert (live.kind, live.project_id, live.archived, live.title) == ("project", "prj-1", False, "上线")
    assert (old.project_id, old.archived) == ("prj-1", True)
    assert any(m.role == "event" and "交接" in m.text for m in live.messages)
    assert s.tasks["task-1"].project_id == "prj-1"
    assert s.requests["req-1"].project_id == "prj-1"
    assert not (tmp_path / "groups").exists()


def test_joint_group_in_project_stays_its_own_project(tmp_path: Path):
    """挂在旧项目里的联合群组不和内部群组合并（不同的信任域）：单独成为联合项目，带上共享给它的项目文件。"""
    state = {
        "agents": [_agent("a1"), _agent("b1", owner="u2")],
        "users": _users("u1", "u2"),
        "projects": [{"id": "prj-1", "name": "上线", "owner_id": "u1", "shares": {"spec.md": ["group-j"]}}],
        "groups": [_group("group-1", ["a1"], project_id="prj-1"),
                   _group("group-j", ["a1", "b1"], name="合作", kind="joint", participants=["u2"], project_id="prj-1",
                          home_enabled=["a1"])],
        "conversations": [_chat("conv-1", "group-1"), _chat("conv-j", "group-j")],
        "requests": [{"id": "req-j", "conversation_id": "conv-j", "agent_id": "b1", "requester_id": "u1",
                      "message_id": "m1", "title": "t", "expected_outcome": "o", "project_id": "prj-1"}],
    }
    _write(tmp_path, state, {"projects/prj-1/files/spec.md": "S", "projects/prj-1/files/internal.md": "内部"})
    s = Store(tmp_path)
    assert set(s.projects) == {"prj-1", "group-j"}
    j = s.projects["group-j"]
    assert (j.kind, j.participants, j.member_ids, j.home_enabled) == ("joint", ["u2"], ["a1", "b1"], ["a1"])
    assert [f.name for f in s.project_dir(j.id).iterdir()] == ["spec.md"]  # 只有共享给它的
    assert s.projects["prj-1"].member_ids == ["a1"]
    assert s.requests["req-j"].project_id == "group-j"
    assert s.conversations["conv-j"].project_id == "group-j"


def test_empty_old_project_and_foreign_group(tmp_path: Path):
    """没有群组的旧项目也成为项目（补建群聊）；别人的群组即使残留了项目归属也不并进来。"""
    state = {
        "agents": [_agent("a1"), _agent("b1", owner="u2")],
        "users": _users("u1", "u2"),
        "projects": [{"id": "prj-1", "name": "空项目", "owner_id": "u1", "brief": "b"}],
        "groups": [_group("group-x", ["b1"], owner="u2", project_id="prj-1")],
        "conversations": [],
    }
    _write(tmp_path, state)
    s = Store(tmp_path)
    assert s.projects["prj-1"].member_ids == [] and s.projects["prj-1"].brief == "b"
    assert s.projects["group-x"].owner_id == "u2"
    chats = [c for c in s.conversations.values() if c.kind == "project" and not c.archived]
    assert sorted(c.project_id for c in chats) == ["group-x", "prj-1"]


def test_assistant_defaults_to_earliest_agent(tmp_path: Path):
    """已有用户的助理取他最早创建的 Agent；还没有 Agent 的用户暂时没有助理。"""
    state = {"agents": [_agent("a2", created=5), _agent("a1", created=1), _agent("b1", owner="u2", created=0)],
             "users": _users("u1", "u3"), "groups": [], "conversations": []}
    _write(tmp_path, state)
    s = Store(tmp_path)
    assert s.users["u1"].assistant_id == "a1"
    assert s.users["u3"].assistant_id is None


def test_migrated_state_reloads_unchanged(tmp_path: Path):
    """迁移后的状态再加载一次不再迁移，内容不变。"""
    state = {"agents": [_agent("a1")], "users": _users("u1"),
             "groups": [_group("group-1", ["a1"], brief="x")], "conversations": [_chat("conv-1", "group-1")]}
    _write(tmp_path, state, {"groups/group-1/files/a.md": "A"})
    Store(tmp_path)
    first = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    s = Store(tmp_path)
    s.save()
    again = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert again["projects"] == first["projects"] and again["conversations"] == first["conversations"]
    assert (s.project_dir("group-1") / "a.md").is_file()
