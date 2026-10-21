"""委托产物：执行者工作目录里写出来的文件随交付拷进委托方工作目录；人能看自己 Agent 的工作目录。"""
import io
import re
import zipfile
from pathlib import Path

from agentnet.engine import Network
from agentnet.runtime import ScriptCall, ScriptedRuntime
from tests.test_users import client, users


def build(tmp_path: Path, handler):
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    lead = net.create_agent("Lead", "lead", [], ["delegate", "file.write"], actor=alice)
    eng = net.create_agent("Eng", "eng", [], ["file.write", "delegate"], actor=alice)
    helper = net.create_agent("Helper", "helper", [], ["file.write"], actor=alice)
    other = net.create_agent("Other", "other", [], [], actor=alice)
    g = net.create_group("开发组", "", [lead.id, eng.id, helper.id, other.id], actor=alice)
    return net, alice, bob, lead, eng, helper, other, g


def test_delivery_hands_files_to_direct_delegator(tmp_path: Path):
    """不变量 7、9：执行者的产物只拷给直接委托方（席位或上一级任务），网络与运行时的文件、临时文件不随交付。"""
    prompts: dict[str, list[str]] = {}

    def handler(call: ScriptCall) -> str:
        prompts.setdefault(call.agent_id, []).append(call.prompt)
        cwd = Path(call.cwd)
        if call.agent_id == lead.id:
            if call.turn == 1:
                call.tool("delegate", agent="Eng", mission="实现 pkg", expected_outcome="代码")
                return "已委托"
            return "收到"
        if call.agent_id == eng.id:
            if call.turn == 1:
                (cwd / "pkg").mkdir(exist_ok=True)
                (cwd / "pkg" / "a.py").write_text("A = 1", encoding="utf-8")
                (cwd / "pkg" / "__pycache__").mkdir(exist_ok=True)
                (cwd / "pkg" / "__pycache__" / "a.pyc").write_bytes(b"x")
                (cwd / "tmp").mkdir(exist_ok=True)
                (cwd / "tmp" / "scratch.txt").write_text("draft", encoding="utf-8")
                call.tool("delegate", agent="Helper", mission="写文档", expected_outcome="doc")
                return "等 Helper"
            # Helper 的产物在本任务目录的 deliveries/ 下：集成进自己的产物
            sub = next((cwd / "deliveries").iterdir())
            (cwd / "docs.md").write_text((sub / "doc.md").read_text(encoding="utf-8"), encoding="utf-8")
            return "实现完成"
        if call.agent_id == helper.id:
            (cwd / "doc.md").write_text("# doc", encoding="utf-8")
            return "文档完成"
        return "ok"
    net, alice, bob, lead, eng, helper, other, g = build(tmp_path, handler)
    chat = net.group_chat(g.id, actor=alice)
    net.send_message(chat.id, "@Lead 实现 pkg", actor=alice, background=False)
    net.wait_quiet()
    t_eng = next(t for t in net.store.tasks.values() if t.delegatee_id == eng.id)
    t_help = next(t for t in net.store.tasks.values() if t.delegatee_id == helper.id)

    # 子任务的产物进上一级任务的工作目录
    assert (tmp_path / "tasks" / t_eng.id / "deliveries" / t_help.id / "doc.md").read_text(encoding="utf-8") == "# doc"
    # 交到席位的产物：只有执行者自己写的，不含运行时文件、临时文件、缓存、别人交来的产物
    seat_dir = tmp_path / chat.seats[lead.id].workdir
    got = seat_dir / "deliveries" / t_eng.id
    assert sorted(p.relative_to(got).as_posix() for p in got.rglob("*") if p.is_file()) == ["docs.md", "pkg/a.py"]
    assert t_eng.result["files"] == ["docs.md", "pkg/a.py"]
    assert any(f"deliveries/{t_eng.id}/" in p and "pkg/a.py" in p for p in prompts[lead.id][1:])
    # 只给直接委托方：同组其他成员、群聊里都看不到
    assert not (seat_dir / "deliveries" / t_help.id).exists()  # 孙任务的产物不越级
    assert other.id not in chat.seats
    assert not any("pkg/a.py" in m.text for m in chat.messages if m.role != "agent")


def test_workdir_browsing_and_directory_publish(tmp_path: Path):
    """不变量 13：工作目录只给 Agent 的所有者（任务给两端的所有者）；越界路径与运行时文件按不存在处理。目录发布打成 zip。"""
    out: list[str] = []

    def handler(call: ScriptCall) -> str:
        cwd = Path(call.cwd)
        (cwd / "site").mkdir(exist_ok=True)
        (cwd / "site" / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
        (cwd / "site" / "node_modules").mkdir(exist_ok=True)
        (cwd / "site" / "node_modules" / "big.js").write_text("x", encoding="utf-8")
        out.append(call.tool("publish_file", path="site"))
        return "ok"
    net, alice, bob, lead, eng, helper, other, g = build(tmp_path, handler)
    chat = net.group_chat(g.id, actor=alice)
    net.send_message(chat.id, "@Lead 做个网站", actor=alice, background=False)
    assert "site.zip" in out[0]
    z = zipfile.ZipFile(io.BytesIO(net.group_file(g.id, "site.zip").read_bytes()))
    assert z.namelist() == ["site/index.html"]

    ca, cb = client(net, "alice@x.io"), client(net, "bob@x.io")
    base = f"/api/conversations/{chat.id}/seats/{lead.id}/files"
    files = [f["path"] for f in ca.get(base).json()["files"]]
    assert "site/index.html" in files and ".dsh-authority.yml" not in files and not any("node_modules" in f for f in files)
    r = ca.get(f"{base}/site/index.html")
    assert r.text == "<h1>hi</h1>" and "attachment" in r.headers["content-disposition"]
    assert ca.get(f"{base}/../../state.json").status_code == 404
    assert ca.get(f"{base}/site/node_modules/big.js").status_code == 404
    assert cb.get(base).status_code == 404
    assert ca.get(f"/api/conversations/{chat.id}/seats/{other.id}/files").status_code == 404  # 没有席位


def test_redelivery_replaces_files(tmp_path: Path):
    """退回修改后重新交付：委托方拿到的是最新一版，旧文件不残留。"""
    def handler(call: ScriptCall) -> str:
        cwd = Path(call.cwd)
        if call.agent_id == lead.id:
            if call.turn == 1:
                call.tool("delegate", agent="Eng", mission="写", expected_outcome="文件")
                return "已委托"
            if call.turn == 2:
                tid = re.search(r"task-[0-9a-f]{8}", call.prompt).group()
                call.tool("request_changes", task_id=tid, feedback="改名为 v2")
                return "退回"
            return "收到"
        if call.turn == 1:
            (cwd / "v1.txt").write_text("1", encoding="utf-8")
        else:
            (cwd / "v1.txt").unlink()
            (cwd / "v2.txt").write_text("2", encoding="utf-8")
        return "交付"
    net, alice, bob, lead, eng, helper, other, g = build(tmp_path, handler)
    chat = net.group_chat(g.id, actor=alice)
    net.send_message(chat.id, "@Lead 写", actor=alice, background=False)
    net.wait_quiet()
    t = next(iter(net.store.tasks.values()))
    assert t.revisions == 1
    got = tmp_path / chat.seats[lead.id].workdir / "deliveries" / t.id
    assert sorted(p.name for p in got.iterdir()) == ["v2.txt"]
    ca = client(net, "alice@x.io")
    assert [f["path"] for f in ca.get(f"/api/tasks/{t.id}/files").json()["files"]] == ["v2.txt"]
