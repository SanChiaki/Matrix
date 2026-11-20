"""项目资料的目录层级，资料与项目技能的可见范围（docs/reference/archive.md，不变量 26）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from matrix.engine import ExecContext, Network, NetworkError
from matrix.models import PROJECT_FILES, SKILLS
from matrix.runtime import ScriptCall, ScriptedRuntime
from tests.test_users import client, users


def recorder():
    """每一轮记下：工作目录 project/ 下的文件、skills/ 下的技能、提示词。"""
    seen: dict[str, list[dict]] = {}

    def handler(call: ScriptCall) -> str:
        root = call.cwd / PROJECT_FILES
        files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) if root.is_dir() else []
        seen.setdefault(call.agent_id, []).append({"files": files, "skills": sorted(call.skills), "prompt": call.prompt})
        return "ok"
    return ScriptedRuntime(handler), seen


def setup(tmp_path: Path):
    rt, seen = recorder()
    net = Network(rt, tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("Lead", "负责人", [], ["file.write"], actor=alice)
    b = net.create_agent("Helper", "帮手", [], ["file.write"], actor=alice)
    g = net.create_project("P", "", [a.id, b.id], actor=alice)
    net.publish_file(g.id, "公开.md", "大家都看".encode(), actor=alice)
    net.publish_file(g.id, "设计/方案.md", "方案".encode(), actor=alice)
    net.publish_file(g.id, "设计/内部/薪资.md", "薪资".encode(), actor=alice)
    net.save_project_skill(g.id, "triage", "整理议题", "按影响排序", actor=alice)
    net.save_project_skill(g.id, "pay-review", "薪资评审", "保密流程", actor=alice)
    net.set_project_roles(g.id, {"lead": {"name": "负责人", "members": [a.id]}}, actor=alice)
    return net, seen, alice, a, b, g


def ask(net: Network, g, agent, alice) -> None:
    chat = net.project_chat(g.id, actor=alice)
    net.send_message(chat.id, f"@{agent.name} 看看资料", actor=alice, background=False)


# ================= 目录层级 =================
def test_files_can_live_in_folders(tmp_path: Path):
    """资料可以有多级目录：发布时名字带目录，目录按需建；路径净化，不会写出档案目录。"""
    net, seen, alice, a, b, g = setup(tmp_path)
    root = net.store.project_dir(g.id)
    assert (root / "设计" / "内部" / "薪资.md").read_text(encoding="utf-8") == "薪资"
    assert net.publish_file(g.id, "../../x/./.hidden/../y.md", b"1", actor=alice) == "x/hidden/y.md"
    with pytest.raises(NetworkError):
        net.publish_file(g.id, "../..", b"x", actor=alice)
    with pytest.raises(NetworkError, match="是一个目录"):
        net.publish_file(g.id, "设计", b"x", actor=alice)
    with pytest.raises(NetworkError, match="是一个文件"):
        net.publish_file(g.id, "公开.md/子.md", b"x", actor=alice)
    detail = net.project_detail(g.id, actor=alice)
    assert {f["name"] for f in detail["files"]} == {"公开.md", "设计/方案.md", "设计/内部/薪资.md", "x/hidden/y.md"}
    assert {f["name"] for f in detail["folders"]} >= {"设计", "设计/内部", "x", "x/hidden"}
    # 空目录、移动（改名）、删整个目录
    net.create_folder(g.id, "归档/2026", actor=alice)
    assert "归档/2026" in [f["name"] for f in net.project_detail(g.id, actor=alice)["folders"]]
    net.move_file(g.id, "设计/方案.md", "归档/2026/方案.md", actor=alice)
    assert (root / "归档" / "2026" / "方案.md").is_file() and not (root / "设计" / "方案.md").exists()
    with pytest.raises(NetworkError, match="自己里面"):
        net.move_file(g.id, "归档", "归档/2026/归档", actor=alice)
    net.delete_file(g.id, "x", actor=alice)
    assert not (root / "x").exists()
    # 更新人跟着移动走
    f = next(x for x in net.project_detail(g.id, actor=alice)["files"] if x["name"] == "归档/2026/方案.md")
    assert f["by_user"] == alice
    # Agent 拿到的 project/ 保留目录层级
    ask(net, g, a, alice)
    assert set(seen[a.id][-1]["files"]) == {"公开.md", "归档/2026/方案.md", "设计/内部/薪资.md"}
    log = [json.loads(x)["action"] for x in (root.parent / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {"folder.create", "file.move", "folder.delete"} <= set(log)


def test_agent_publishes_into_a_folder(tmp_path: Path):
    """publish_file 的 name 可以带目录。"""
    def handler(call: ScriptCall) -> str:
        (call.cwd / "out.md").write_text("报告", encoding="utf-8")
        return call.tool("publish_file", path="out.md", name="周报/第一周.md")[0]
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("W", "w", [], ["file.write"], actor=alice)
    g = net.create_project("P", "", [a.id], actor=alice)
    net.send_message(net.project_chat(g.id, actor=alice).id, "@W 发", actor=alice, background=False)
    assert (net.store.project_dir(g.id) / "周报" / "第一周.md").read_text(encoding="utf-8") == "报告"


# ================= 可见范围 =================
def test_restricted_files_and_skills_reach_only_allowed_agents(tmp_path: Path):
    """不变量 26：可见范围收窄到角色 / 成员后，范围外的 Agent 在 project/、skills/、档案推送里都拿不到；范围内的照常。"""
    net, seen, alice, a, b, g = setup(tmp_path)
    net.set_file_access(g.id, "设计/内部", {"roles": ["lead"]}, actor=alice)
    net.set_skill_access(g.id, "pay-review", {"agents": ["Lead"]}, actor=alice)  # 也认名字
    assert net.store.projects[g.id].skill_access["pay-review"] == {"roles": [], "agents": [a.id]}
    ask(net, g, a, alice)
    ask(net, g, b, alice)
    sa, sb = seen[a.id][-1], seen[b.id][-1]
    assert set(sa["files"]) == {"公开.md", "设计/方案.md", "设计/内部/薪资.md"} and sa["skills"] == ["pay-review", "triage"]
    assert sb["files"] == ["公开.md", "设计/方案.md"] and sb["skills"] == ["triage"]
    assert "薪资.md" in sa["prompt"] and "pay-review" in sa["prompt"]
    assert "薪资" not in sb["prompt"] and "pay-review" not in sb["prompt"]
    # 人照常都看得到，带着范围
    d = net.project_detail(g.id, actor=alice)
    assert next(f for f in d["folders"] if f["name"] == "设计/内部")["access"] == {"roles": ["lead"], "agents": []}
    assert next(s for s in d["skill_list"] if s["name"] == "pay-review")["access"]["agents"] == [a.id]
    assert any("仅 角色「负责人」 可见" in m.text for m in net.project_chat(g.id, actor=alice).messages)
    # 端侧材料同样过滤
    ctx = ExecContext(kind="seat", id="x", agent_id=b.id, conversation_id=net.project_chat(g.id).id, project_id=g.id,
                      authority=[], constraints=[], depth=0, chain=[b.id])
    with net._lock:
        rels = [m.path for m in net._materials(ctx)]
    assert f"{PROJECT_FILES}/设计/内部/薪资.md" not in rels and f"{PROJECT_FILES}/公开.md" in rels
    # 改回公开：下一轮 B 也拿到
    net.set_file_access(g.id, "设计/内部", {}, actor=alice)
    net.set_skill_access(g.id, "pay-review", None, actor=alice)
    ask(net, g, b, alice)
    assert "设计/内部/薪资.md" in seen[b.id][-1]["files"] and "pay-review" in seen[b.id][-1]["skills"]
    assert "薪资.md" in seen[b.id][-1]["prompt"]  # 档案版本变了，重新推送


def test_folder_rules_only_narrow(tmp_path: Path):
    """不变量 26：目录的范围管到里面的每个文件，层层只收窄，文件上的设置放不大目录的。"""
    net, seen, alice, a, b, g = setup(tmp_path)
    net.set_file_access(g.id, "设计", {"agents": [a.id]}, actor=alice)
    net.set_file_access(g.id, "设计/内部/薪资.md", {"agents": [b.id]}, actor=alice)
    ask(net, g, a, alice)
    ask(net, g, b, alice)
    assert seen[a.id][-1]["files"] == ["公开.md", "设计/方案.md"]  # 文件只给 B：A 也看不到
    assert seen[b.id][-1]["files"] == ["公开.md"]                  # 目录只给 A：B 看不到
    # 角色成员变了，范围跟着变
    net.set_file_access(g.id, "设计/内部/薪资.md", {"roles": ["lead"]}, actor=alice)
    net.set_file_access(g.id, "设计", {"roles": ["lead"]}, actor=alice)
    net.set_project_roles(g.id, {"lead": {"name": "负责人", "members": [b.id]}}, actor=alice)
    ask(net, g, b, alice)
    assert set(seen[b.id][-1]["files"]) == {"公开.md", "设计/方案.md", "设计/内部/薪资.md"}


def test_access_follows_moves_and_guards_roles(tmp_path: Path):
    """移动目录时范围跟着走；还在用的角色不能删；只有主持人能设；没有的角色、不是成员的 Agent 不收。"""
    net, seen, alice, a, b, g = setup(tmp_path)
    net.set_file_access(g.id, "设计/内部", {"roles": ["lead"]}, actor=alice)
    net.move_file(g.id, "设计", "归档/设计", actor=alice)
    assert net.store.projects[g.id].file_access == {"归档/设计/内部": {"roles": ["lead"], "agents": []}}
    ask(net, g, b, alice)
    assert "归档/设计/内部/薪资.md" not in seen[b.id][-1]["files"]
    with pytest.raises(NetworkError, match="可见范围还在用角色 lead"):
        net.set_project_roles(g.id, {}, actor=alice)
    with pytest.raises(NetworkError, match="没有角色"):
        net.set_file_access(g.id, "公开.md", {"roles": ["nope"]}, actor=alice)
    with pytest.raises(NetworkError, match="不是项目成员"):
        net.set_skill_access(g.id, "triage", {"agents": ["陌生人"]}, actor=alice)
    bob, = users(net, "bob@x.io")
    with pytest.raises(NetworkError):
        net.set_file_access(g.id, "公开.md", {"roles": ["lead"]}, actor=bob)
    net.delete_file(g.id, "归档", actor=alice)
    assert net.store.projects[g.id].file_access == {}
    net.set_skill_access(g.id, "pay-review", {"roles": ["lead"]}, actor=alice)
    net.delete_project_skill(g.id, "pay-review", actor=alice)
    assert net.store.projects[g.id].skill_access == {}


def test_agent_cannot_publish_where_it_cannot_see(tmp_path: Path):
    """不变量 26：Agent 不能往看不到的位置发布（也就覆盖不了看不到的文件）。"""
    net, seen, alice, a, b, g = setup(tmp_path)
    net.set_file_access(g.id, "设计/内部", {"roles": ["lead"]}, actor=alice)
    with pytest.raises(NetworkError, match="看不到"):
        net.publish_file(g.id, "设计/内部/薪资.md", "覆盖".encode(), agent_id=b.id)
    with pytest.raises(NetworkError, match="看不到"):
        net.publish_file(g.id, "设计/内部/新.md", b"x", agent_id=b.id)
    assert net.publish_file(g.id, "设计/内部/新.md", b"x", agent_id=a.id) == "设计/内部/新.md"
    assert (net.store.project_dir(g.id) / "设计" / "内部" / "薪资.md").read_text(encoding="utf-8") == "薪资"


def test_folders_and_access_over_http(tmp_path: Path):
    net, seen, alice, a, b, g = setup(tmp_path)
    c = client(net, "alice@x.io")
    base = f"/api/projects/{g.id}"
    assert c.post(f"{base}/folders", json={"path": "会议/周会"}).status_code == 200
    r = c.post(f"{base}/files", json={"name": "会议/周会/第1次.md", "content": "纪要"})
    assert r.status_code == 200 and "会议/周会/第1次.md" in [f["name"] for f in r.json()["files"]]
    got = c.get(f"{base}/files/会议/周会/第1次.md")
    assert got.status_code == 200 and got.text == "纪要"
    assert c.get(f"{base}/files/../../state.json").status_code == 404
    r = c.put(f"{base}/file-access", json={"path": "会议", "roles": ["lead"]})
    assert r.status_code == 200 and next(f for f in r.json()["folders"] if f["name"] == "会议")["access"]["roles"] == ["lead"]
    r = c.put(f"{base}/skills/triage/access", json={"agents": [b.id]})
    assert next(s for s in r.json()["skill_list"] if s["name"] == "triage")["access"]["agents"] == [b.id]
    r = c.post(f"{base}/moves", json={"src": "会议", "dst": "归档/会议"})
    assert r.status_code == 200 and "归档/会议/周会/第1次.md" in [f["name"] for f in r.json()["files"]]
    r = c.delete(f"{base}/files/归档/会议")
    assert r.status_code == 200 and not any(f["name"].startswith("归档/会议") for f in r.json()["files"])


def test_upload_any_file_type_over_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """人上传资料不限类型：请求体就是文件内容，字节原样存下（docx 这类二进制以前会被当文本读坏）；超过 FILE_LIMIT 413。"""
    net, seen, alice, a, b, g = setup(tmp_path)
    c = client(net, "alice@x.io")
    base = f"/api/projects/{g.id}"
    docx = b"PK\x03\x04\x00\x00\xff\xfe word/document.xml \x00\x01"
    r = c.post(f"{base}/files", params={"name": "设计/方案.docx"}, content=docx,
               headers={"Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"})
    assert r.status_code == 200 and "设计/方案.docx" in [f["name"] for f in r.json()["files"]]
    assert (net.store.project_dir(g.id) / "设计" / "方案.docx").read_bytes() == docx
    assert c.get(f"{base}/files/设计/方案.docx").content == docx
    import matrix.engine as engine_mod
    import matrix.server as server_mod
    monkeypatch.setattr(server_mod, "FILE_LIMIT", 10)
    monkeypatch.setattr(engine_mod, "FILE_LIMIT", 10)
    r = c.post(f"{base}/files", params={"name": "大.bin"}, content=b"x" * 11)
    assert r.status_code == 413
    assert c.post(f"{base}/files", content=b"not json").status_code == 422  # 没给名字也不是 JSON
    with pytest.raises(NetworkError, match="太大"):
        net.publish_file(g.id, "大.bin", b"x" * 11, agent_id=a.id)


def test_nested_files_in_releases(tmp_path: Path):
    """交付件可以带目录里的文件，快照里保留路径。"""
    net, seen, alice, a, b, g = setup(tmp_path)
    r = net.create_release(g.id, "design", "fact", "方案", files=["设计/方案.md"], actor=alice)
    assert [f["name"] for f in net.release_view(r, alice)["files"]] == ["设计/方案.md"]
    assert net.release_file(r.id, "设计/方案.md", actor=alice).read_text(encoding="utf-8") == "方案"
