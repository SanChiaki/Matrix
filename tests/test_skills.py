"""技能（docs/connectors-and-skills.md）：项目技能、我的技能、Agent 自己攒的；挂载顺序与不读部署机器上的技能。"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from matrix import skills as skillfs
from matrix.engine import Network, NetworkError
from matrix.models import SKILL_LIMIT, SKILLS
from matrix.runtime import ScriptCall, ScriptedRuntime, RuntimeCapabilities, build_patch_yaml
from tests.test_users import client, joint, users


def recorder():
    seen: list[dict] = []

    def handler(call: ScriptCall) -> str:
        d = call.cwd / SKILLS
        seen.append({"skills": call.skills, "prompt": call.prompt,
                     "bodies": {n: (d / n / "SKILL.md").read_text(encoding="utf-8") for n in call.skills},
                     "tools": list(call.tool_names)})
        return "ok"
    return ScriptedRuntime(handler), seen


# ================= 第 0 阶段：不读部署机器上的技能 =================
def test_dsh_overlay_never_reads_default_skill_roots(tmp_path: Path):
    """不变量 8 / 16 的延伸：覆盖层总是关掉 dsh 的默认技能根（~/.dsh/skills、~/.agents/skills、
    数据目录所在仓库的 .agents/skills），只读网络放进工作目录的两处，网络合并的在前。"""
    y = build_patch_yaml([])
    assert "- id: skill-filesystem" in y and "includeDefaultRoots: false" in y and "customSkillDirs: []" in y
    y = build_patch_yaml([], skill_dirs=["/w/skills", "/w/agent-home/skills"])
    assert 'customSkillDirs: ["/w/skills", "/w/agent-home/skills"]' in y

    from matrix.runtime import DshRuntime, RunRequest
    from tests.test_connector import fake_bin
    import json
    fake = fake_bin(tmp_path, "fake-dsh",   # 只记录覆盖层，不真的跑模型
                    "import pathlib\np=sys.argv[sys.argv.index('--patch')+1]\n"
                    "pathlib.Path(p).with_name('seen.yml').write_text(pathlib.Path(p).read_text(encoding='utf-8'), encoding='utf-8')\n"
                    "print(json.dumps({'type':'final','text':'ok'}))\n")
    cwd = tmp_path / "w"
    res = DshRuntime(fake).run(RunRequest(agent_id="a", context_id="c", prompt="hi", cwd=cwd, authority=[]))
    assert res.ok, res.error
    seen = (cwd / "seen.yml").read_text(encoding="utf-8")
    assert "includeDefaultRoots: false" in seen
    assert json.dumps([str((cwd / "skills").resolve()), str((cwd / "agent-home" / "skills").resolve())]) in seen


# ================= 文件格式 =================
def test_skill_files_roundtrip_and_validation(tmp_path: Path):
    root = tmp_path / "lib"
    skillfs.write(root, "weekly-report", "写周报：本周进展、风险、下周计划", "1. 先列事实\n2. 再写判断")
    name, desc, body = skillfs.read(root, "weekly-report")
    assert (name, desc) == ("weekly-report", "写周报：本周进展、风险、下周计划") and body.startswith("1. 先列事实")
    text = (root / "weekly-report" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith('---\nname: "weekly-report"\ndescription: "写周报')  # dsh 认的 frontmatter
    for bad in ("Weekly", "周报", "a_b", "-x", "x-", "a" * 65, ""):
        with pytest.raises(skillfs.SkillError):
            skillfs.check_name(bad)
    with pytest.raises(skillfs.SkillError):
        skillfs.write(root, "no-desc", "  ", "x")
    skillfs.add_file(root, "weekly-report", "templates/report.md", b"# {{title}}")
    assert "templates/report.md" in skillfs.info(root / "weekly-report").files
    for bad in ("../x", "/etc/passwd", "SKILL.md", ".hidden"):
        with pytest.raises(skillfs.SkillError):
            skillfs.add_file(root, "weekly-report", bad, b"x")
    with pytest.raises(skillfs.SkillError):
        skillfs.write(root, "huge", "大", "x" * (SKILL_LIMIT + 1))


def test_frontmatter_in_the_shapes_real_skills_use():
    """真实技能包（如官方 anthropics/skills）的 frontmatter：折叠 / 字面块、缩进续行、嵌套的 metadata、
    Windows 的 BOM 与 CRLF。只取顶层的 name 与 description。"""
    folded = "---\nname: guide\ndescription: >\n  Stop and check\n  this skill.\n\n  Second para.\nlicense: MIT\n---\n\nbody"
    assert skillfs.parse(folded) == ("guide", "Stop and check this skill.\nSecond para.", "body")
    literal = "---\nname: x\ndescription: |-\n  line one\n  line two\n---\nbody"
    assert skillfs.parse(literal)[1] == "line one\nline two"
    plain = "---\nname: x\ndescription: Use when\n  the user asks.\nmetadata:\n  name: nested\n  description: no\n---\nb"
    assert skillfs.parse(plain)[:2] == ("x", "Use when the user asks.")
    quoted = '---\nname: "x"\ndescription: \'it\'\'s fine\'  \n---\nb'
    assert skillfs.parse(quoted)[:2] == ("x", "it's fine")
    crlf = "\ufeff---\r\nname: win\r\ndescription: 在 Windows 上写的 # 注释\r\n---\r\nbody\r\n"
    assert skillfs.parse(crlf) == ("win", "在 Windows 上写的", "body")


def test_resolve_puts_earlier_layers_first(tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"
    skillfs.write(a, "shared", "项目的写法", "项目")
    skillfs.write(b, "shared", "我的写法", "我的")
    skillfs.write(b, "only-mine", "只有我有", "x")
    skillfs.write(b, "not-picked", "没勾", "x")
    chosen, shadowed = skillfs.resolve([("项目技能", a, None), ("我的技能", b, ["shared", "only-mine"])])
    assert set(chosen) == {"shared", "only-mine"} and chosen["shared"][0] == "项目技能"
    assert shadowed == [("shared", "我的技能")]


# ================= 一轮里的技能 =================
def test_project_skill_beats_my_skill_and_is_read_only(tmp_path: Path):
    """项目技能 > 我的技能；工作目录里的 skills/ 是只读副本，运行后丢弃（不变量 17：档案只走网络）。"""
    def handler(call: ScriptCall) -> str:
        out = " / ".join(f"{n}={(call.cwd / SKILLS / n / 'SKILL.md').read_text(encoding='utf-8')[-6:].strip()}"
                         for n in call.skills)
        (call.cwd / SKILLS / "weekly-report" / "SKILL.md").write_text("被 Agent 改了", encoding="utf-8")
        return out
    rt = ScriptedRuntime(handler)
    net = Network(rt, tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("Writer", "写东西", [], ["file.write"], actor=alice)
    net.save_my_skill("weekly-report", "我的周报写法", "我的写法", actor=alice)
    net.save_my_skill("meeting-notes", "会议纪要", "纪要写法", actor=alice)
    net.update_agent(a.id, actor=alice, skills=["weekly-report", "meeting-notes"])
    g = net.create_project("P", "", [a.id], actor=alice)
    net.save_project_skill(g.id, "weekly-report", "项目约定的周报", "项目写法", actor=alice)
    chat = net.project_chat(g.id, actor=alice)
    net.send_message(chat.id, "@Writer 写周报", actor=alice, background=False)
    reply = chat.messages[-1].text
    assert "weekly-report=项目写法" in reply and "meeting-notes=纪要写法" in reply
    assert not (tmp_path / "conversations" / chat.id / a.id / SKILLS).exists()  # 运行后丢弃
    assert skillfs.read(net.store.project_skills_dir(g.id), "weekly-report")[2] == "项目写法"  # 档案没被改
    # 私聊里没有项目层：用的是我的技能
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "写周报", actor=alice, background=False)
    assert "weekly-report=我的写法" in dm.messages[-1].text


def test_shadowed_skills_are_reported_to_the_owner_only(tmp_path: Path):
    rt, seen = recorder()
    net = Network(rt, tmp_path, seed=False)
    host, guest = users(net, "host@x.io", "guest@x.io")
    ha = net.create_agent("HostAgent", "主持人的", [], [], actor=host)
    ga = net.create_agent("GuestAgent", "参与者的", [], [], actor=guest)
    net.save_my_skill("weekly-report", "我自己的周报写法", "x", actor=guest)
    net.update_agent(ga.id, actor=guest, skills=["weekly-report"])
    g, chat = joint(net, host, "guest@x.io", guest, [ha.id])  # GuestAgent 是参与者的助理，接受邀请时自动加入
    net.save_project_skill(g.id, "weekly-report", "项目约定的周报", "y", actor=host)
    net.send_message(chat.id, "@GuestAgent 写周报", actor=host, background=False)
    shadow = [e for e in net.bus.since(0) if e.type == "skills.shadowed"]
    assert shadow and shadow[0].data["skills"] == [{"name": "weekly-report", "from": "我的技能", "by": "项目技能"}]
    assert net.event_view("skills.shadowed", shadow[0].data, guest) is not None
    assert net.event_view("skills.shadowed", shadow[0].data, host) is None  # 主持人看不到别人的技能库


def test_personal_skills_go_along_into_joint_projects_but_stay_private(tmp_path: Path):
    """技能是能力，跟着 Agent 走；但技能库的正文只有本人看得到（不变量 13）。"""
    rt, seen = recorder()
    net = Network(rt, tmp_path, seed=False)
    host, guest = users(net, "host@x.io", "guest@x.io")
    ga = net.create_agent("GuestAgent", "参与者的", [], [], actor=guest)
    net.save_my_skill("secret-sauce", "我的独门做法", "配方", actor=guest)
    net.update_agent(ga.id, actor=guest, skills=["secret-sauce"])
    g, chat = joint(net, host, "guest@x.io", guest)
    net.send_message(chat.id, "@GuestAgent 来", actor=host, background=False)
    assert seen[-1]["skills"] == ["secret-sauce"]
    assert net.my_skills(host) == []
    with pytest.raises(NetworkError):
        net.my_skill("secret-sauce", host)
    c = client(net, "host@x.io")
    assert c.get("/api/me/skills/secret-sauce").status_code == 404


def test_agent_can_only_pick_its_owners_skills(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda c: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    net.save_my_skill("bobs-skill", "bob 的", "x", actor=bob)
    with pytest.raises(NetworkError, match="技能库里没有"):
        net.update_agent(a.id, actor=alice, skills=["bobs-skill"])
    net.save_my_skill("mine", "我的", "x", actor=alice)
    net.update_agent(a.id, actor=alice, skills=["mine"])
    net.delete_my_skill("mine", alice)
    assert net.store.agents[a.id].skills == []  # 删掉技能，勾选随之去掉


# ================= 项目技能：档案的一部分 =================
def test_project_skills_are_archive_writes_logged_and_public(tmp_path: Path):
    """不变量 17：项目技能只经人的操作写入，记 log.jsonl、群聊里有公开事件；只有主持人能写，参与者能读全文。"""
    import json
    net = Network(ScriptedRuntime(lambda c: "ok"), tmp_path, seed=False)
    host, guest, other = users(net, "host@x.io", "guest@x.io", "other@x.io")
    g, chat = joint(net, host, "guest@x.io", guest)
    net.save_project_skill(g.id, "triage", "整理议题", "按影响排序", actor=host)
    log = [json.loads(x) for x in (tmp_path / "projects" / g.id / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log[-1]["action"] == "skill.create" and log[-1]["skill"] == "triage" and log[-1]["by_user"] == host
    assert any("新增了项目技能「triage」" in m.text for m in net.store.conversations[chat.id].messages)
    assert net.project_skill(g.id, "triage", actor=guest)["body"] == "按影响排序"
    with pytest.raises(NetworkError):
        net.save_project_skill(g.id, "triage", "改", "改", actor=guest)  # 只有主持人能写
    with pytest.raises(NetworkError, match="unknown"):
        net.project_skill(g.id, "triage", actor=other)  # 看不到项目 = 不存在
    detail = net.project_detail(g.id, actor=guest)
    assert [s["name"] for s in detail["skill_list"]] == ["triage"] and detail["skill_list"][0]["author"] == "host@x.io"
    rev = net.store.projects[g.id].archive_rev
    net.delete_project_skill(g.id, "triage", actor=host)
    assert net.store.projects[g.id].archive_rev == rev + 1 and net.project_detail(g.id, actor=host)["skill_list"] == []


def test_agent_proposes_a_skill_and_only_approval_writes_it(tmp_path: Path):
    """不变量 9：Agent 沉淀做法只能提议，主持人批准后才进档案。"""
    def handler(call: ScriptCall) -> str:
        if "沉淀" in call.prompt and call.turn == 1:
            return call.tool("propose_skill", name="bug-triage", description="排查线上问题的顺序",
                             body="1. 看日志\n2. 复现", reason="这次摸索出来的，以后还会用")
        return "ok"
    rt = ScriptedRuntime(handler)
    net = Network(rt, tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("Ops", "运维", [], [], actor=alice)
    g = net.create_project("P", "", [a.id], actor=alice)
    chat = net.project_chat(g.id, actor=alice)
    net.send_message(chat.id, "@Ops 把做法沉淀下来", actor=alice, background=False)
    assert "主持人批准后" in chat.messages[-1].text
    assert skillfs.listing(net.store.project_skills_dir(g.id)) == []  # 批准前不生效
    sug = net.store.projects[g.id].suggestions[-1]
    assert sug.kind == "skill" and sug.skill_name == "bug-triage" and sug.by_agent == a.id
    net.decide_suggestion(g.id, sug.id, True, actor=alice)
    sk = net.project_skill(g.id, "bug-triage", actor=alice)
    assert sk["body"].startswith("1. 看日志") and sk["by_agent"] == a.id and sk["approved_by"] == alice
    # 第二轮：Agent 在工作目录里看到它，档案推送里也列出它
    rt2, seen = recorder()
    net.runtime = rt2
    net.send_message(chat.id, "@Ops 再来", actor=alice, background=False)
    assert seen[-1]["skills"] == ["bug-triage"] and "### 项目技能" in seen[-1]["prompt"]


def test_bad_skill_proposals_never_reach_the_archive(tmp_path: Path):
    def handler(call: ScriptCall) -> str:
        return call.tool("propose_skill", name="不合法的名字", description="x", body="x", reason="x")
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("Ops", "运维", [], [], actor=alice)
    g = net.create_project("P", "", [a.id], actor=alice)
    chat = net.project_chat(g.id, actor=alice)
    net.send_message(chat.id, "@Ops 来", actor=alice, background=False)
    assert "技能名无效" in chat.messages[-1].text and not net.store.projects[g.id].suggestions


# ================= 端侧：技能随材料下发 =================
def test_device_runs_receive_skills_as_materials(tmp_path: Path):
    from matrix import auth
    from matrix.device import RUN_REQUEST, RUN_RESULT, DeviceRuntime, MemoryLink, capabilities_from_hello, HELLO_OK, PROTOCOL
    from tests.test_device import connector
    net = Network(ScriptedRuntime(lambda c: "cloud"), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    dev, _ = auth.issue_device_token(net.store, alice, "laptop")
    info = capabilities_from_hello({"type": HELLO_OK, "protocol": PROTOCOL, "runtime": "codex",
                                    "runtimes": [{"runtime": "codex", "capabilities": {"enforcement": {}, "skills": "prompt"}}]})
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), info)
    connector(rt, script=lambda msg, deliver: deliver({"type": RUN_RESULT, "run_id": msg["run_id"], "final_text": "done"}))
    net.register_runtime(dev.id, rt)
    a = net.create_agent("Local", "端侧", [], [], actor=alice, device_runtime=dev.id)
    net.save_my_skill("weekly-report", "写周报", "正文", actor=alice)
    skillfs.add_file(net.store.user_skills_dir(alice), "weekly-report", "tpl/a.md", b"tpl")
    net.update_agent(a.id, actor=alice, skills=["weekly-report"])
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "写", actor=alice, background=False)
    req = next(m for m in rt.link.sent if m["type"] == RUN_REQUEST)
    files = {m["path"]: base64.b64decode(m["data"]) for m in req["materials"]}
    assert files["skills/weekly-report/tpl/a.md"] == b"tpl" and b"name: \"weekly-report\"" in files["skills/weekly-report/SKILL.md"]
    # 这个 harness 不会自己找技能：提示词里列出名字与路径
    assert "weekly-report：写周报" in req["prompt"] and f"./{SKILLS}/<名字>/SKILL.md" in req["prompt"]
    # 第二轮没有变化，不再重复列出
    net.send_message(dm.id, "再写", actor=alice, background=False)
    req2 = [m for m in rt.link.sent if m["type"] == RUN_REQUEST][-1]
    assert "weekly-report：写周报" not in req2["prompt"]


def test_skill_routes(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda c: "ok"), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    c = client(net, "alice@x.io")
    r = c.put("/api/me/skills", json={"name": "weekly-report", "description": "写周报", "body": "x"})
    assert r.status_code == 200 and r.json()["name"] == "weekly-report"
    assert c.put("/api/me/skills", json={"name": "Bad Name", "description": "x"}).status_code == 409
    assert [s["name"] for s in c.get("/api/me/skills").json()] == ["weekly-report"]
    assert c.post("/api/me/skills/weekly-report/files", json={"path": "../x", "content": "x"}).status_code == 409
    g = net.create_project("P", "", [], actor=alice)
    r = c.put(f"/api/projects/{g.id}/skills", json={"name": "triage", "description": "整理", "body": "b"})
    assert r.status_code == 200 and r.json()["skill_list"][0]["name"] == "triage"
    assert c.get(f"/api/projects/{g.id}/skills/triage").json()["body"] == "b"
    assert c.delete(f"/api/projects/{g.id}/skills/triage").status_code == 200
    assert c.delete("/api/me/skills/weekly-report").json() == {"ok": True}


def _zip(files: dict[str, bytes]) -> bytes:
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p, b in files.items():
            z.writestr(p, b)
    return buf.getvalue()


SKILL_MD = b'---\nname: pdf-tools\ndescription: "\xe5\xa4\x84\xe7\x90\x86 PDF"\n---\n\n# steps\n'


def test_skill_package_upload(tmp_path: Path):
    """标准技能包：zip 或文件夹，可多一层顶层目录、可带二进制文件；名字取 frontmatter；坏包拒绝。"""
    net = Network(ScriptedRuntime(lambda c: "ok"), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    c = client(net, "alice@x.io")
    z = _zip({"pdf/SKILL.md": SKILL_MD, "pdf/scripts/run.py": b"print(1)", "pdf/assets/logo.png": b"\x89PNG\x00\xff",
              "__MACOSX/pdf/._SKILL.md": b"junk"})
    r = c.post("/api/me/skills/upload", content=z, headers={"Content-Type": "application/zip"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "pdf-tools" and r.json()["description"] == "处理 PDF"
    assert sorted(r.json()["files"]) == ["assets/logo.png", "scripts/run.py"]
    folder = {"files": [{"path": "x/SKILL.md", "data": base64.b64encode(SKILL_MD.replace(b"pdf-tools", b"other")).decode()}]}
    assert c.post("/api/me/skills/upload", json=folder).json()["name"] == "other"
    assert {s["name"] for s in c.get("/api/me/skills").json()} == {"pdf-tools", "other"}
    # 隐藏文件（.git/、.gitignore）不属于技能，跳过；1 MB 上下的技能（官方 docx / pptx / xlsx）装得下
    big = _zip({"office/SKILL.md": SKILL_MD.replace(b"pdf-tools", b"office"), "office/.gitignore": b"*.pyc",
                "office/.git/HEAD": b"ref", "office/schemas/ooxml.xsd": b"x" * 1_200_000})
    r = c.post("/api/me/skills/upload", content=big, headers={"Content-Type": "application/zip"})
    assert r.status_code == 200, r.text
    assert r.json()["files"] == ["schemas/ooxml.xsd"]
    bad = [_zip({"a.txt": b"x"}), _zip({"SKILL.md": b"---\nname: Bad\ndescription: x\n---\n"}),
           _zip({"SKILL.md": b"---\nname: ok\n---\n"}), _zip({"SKILL.md": SKILL_MD, "../evil": b"x"}),
           _zip({"a/SKILL.md": SKILL_MD, "b/x": b"x"}), b"not a zip"]
    for b in bad:
        assert c.post("/api/me/skills/upload", content=b, headers={"Content-Type": "application/zip"}).status_code in (400, 409)
    assert not (tmp_path / "evil").exists()
    g = net.create_project("P", "", [], actor=alice)
    r = c.post(f"/api/projects/{g.id}/skills/upload", content=z, headers={"Content-Type": "application/zip"})
    assert r.status_code == 200 and net.store.projects[g.id].skills["pdf-tools"]["by_user"] == alice
    bob, = users(net, "bob@x.io")
    assert client(net, "bob@x.io").post(f"/api/projects/{g.id}/skills/upload", content=z,
                                        headers={"Content-Type": "application/zip"}).status_code == 404
