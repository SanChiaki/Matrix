"""send_file：Agent 把工作目录里的文件附在这一轮的回复上发给对话里的人（存进对话的附件区，人直接下载）。"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Callable

import pytest

import matrix.engine as engine
from matrix.engine import Network, NetworkError
from matrix.models import MCP_PREFIX
from matrix.runtime import ScriptCall, ScriptedRuntime
from tests.test_flow import setup, writes_note
from tests.test_users import client, joint, users


def dm(tmp_path: Path, handler: Callable[[ScriptCall], str]):
    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], ["file.write"], actor=alice)
    return net, alice, bob, a, net.direct_chat(a.id, actor=alice)


def test_file_is_attached_to_the_reply_and_downloadable(tmp_path: Path):
    """发出的文件附在这一轮的回复上；看得到这条回复的人能下载，别人 404。"""
    out: list[str] = []

    def handler(call: ScriptCall) -> str:
        (call.cwd / "report.md").write_text("# 季度报告\n收入增长 12%", encoding="utf-8")
        out.append(call.tool("send_file", path="report.md", name="季度报告.md"))
        return "报告写好了，见附件"

    net, alice, bob, a, conv = dm(tmp_path, handler)
    net.send_message(conv.id, "写个报告", background=False, actor=alice)
    reply = conv.messages[-1]
    assert reply.role == "agent" and reply.text == "报告写好了，见附件"
    assert [x["name"] for x in reply.attachments] == ["季度报告.md"] and "已附上 季度报告.md" in out[0]
    f, meta = net.attachment_file(conv.id, reply.attachments[0]["sha"], actor=alice)
    assert f.read_text(encoding="utf-8").startswith("# 季度报告") and meta["mime"].startswith("text/")
    with pytest.raises(NetworkError):
        net.attachment_file(conv.id, reply.attachments[0]["sha"], actor=bob)
    r = client(net, "alice@x.io").get(f"/api/conversations/{conv.id}/attachments/{reply.attachments[0]['sha']}")
    assert r.status_code == 200 and "收入增长" in r.text


def test_directory_is_zipped_and_paths_are_checked(tmp_path: Path):
    """目录打成 zip；只读挂载、工作目录以外、不存在的都拒绝；一轮最多附 ATTACHMENTS_PER_MESSAGE 个。"""
    out: dict[str, str] = {}

    def handler(call: ScriptCall) -> str:
        (call.cwd / "charts").mkdir(exist_ok=True)
        (call.cwd / "charts" / "a.csv").write_bytes(b"x,y\n1,2")  # 按字节写：Windows 上 write_text 会改换行符
        out["dir"] = call.tool("send_file", path="charts")
        for bad in ("agent-home/notes.md", "project/brief.md", "skills/x/SKILL.md", "../outside.txt", "nope.txt"):
            out[bad] = call.tool("send_file", path=bad)
        return "好了"

    net, alice, bob, a, conv = dm(tmp_path, handler)
    net.send_message(conv.id, "画图", background=False, actor=alice)
    att = conv.messages[-1].attachments
    assert [x["name"] for x in att] == ["charts.zip"] and "已附上 charts.zip" in out["dir"]
    f, _ = net.attachment_file(conv.id, att[0]["sha"], actor=alice)
    assert zipfile.ZipFile(io.BytesIO(f.read_bytes())).read("charts/a.csv") == b"x,y\n1,2"  # 同 publish_file：顶层是目录名
    assert all(out[k].startswith("找不到") for k in out if k != "dir")


def test_limits(tmp_path: Path, monkeypatch):
    out: list[str] = []

    def handler(call: ScriptCall) -> str:
        (call.cwd / "big.bin").write_bytes(b"x" * 2000)
        out.append(call.tool("send_file", path="big.bin"))
        for i in range(engine.ATTACHMENTS_PER_MESSAGE + 1):
            (call.cwd / f"f{i}.txt").write_text(str(i), encoding="utf-8")
            out.append(call.tool("send_file", path=f"f{i}.txt"))
        return "ok"

    monkeypatch.setattr(engine, "ATTACHMENT_LIMIT", 1000)
    net, alice, bob, a, conv = dm(tmp_path, handler)
    net.send_message(conv.id, "go", background=False, actor=alice)
    assert "超过上限" in out[0]
    assert "最多附" in out[-1] and len(conv.messages[-1].attachments) == engine.ATTACHMENTS_PER_MESSAGE


def test_others_in_the_group_get_the_file_and_later_rounds_see_it(tmp_path: Path):
    """共享项目里回复对所有参与者可见，文件也一样；之后被 @ 的成员在 attachments/ 里读得到。"""
    seen: list[str] = []

    def handler(call: ScriptCall) -> str:
        if "整理" in call.prompt and not (call.cwd / "attachments").exists():
            (call.cwd / "list.txt").write_text("清单", encoding="utf-8")
            call.tool("send_file", path="list.txt")
            return "清单在附件里"
        seen.extend(str(p.relative_to(call.cwd)) for p in (call.cwd / "attachments").rglob("*") if p.is_file())
        return "看到了"

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], ["file.write"], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=bob)
    g, chat = joint(net, alice, "bob@x.io", bob, [a.id])
    net.send_message(chat.id, "@A 整理一下", background=False, actor=bob)
    sha = chat.messages[-1].attachments[0]["sha"]
    assert net.attachment_file(chat.id, sha, actor=bob)[0].read_text(encoding="utf-8") == "清单"
    net.send_message(chat.id, "@B 看看清单", background=False, actor=bob)
    assert any(p.endswith("list.txt") for p in seen)


def test_files_survive_a_failed_round_and_a_quiet_reply(tmp_path: Path):
    """这一轮中断：已经发出的文件单独一条带上；定时轮次回“没有新情况”但发了文件，也要发出来。"""
    def handler(call: ScriptCall) -> str:
        (call.cwd / "x.txt").write_text("x", encoding="utf-8")
        call.tool("send_file", path="x.txt")
        call.fail("SERVER boom")
        return ""

    net, alice, bob, a, conv = dm(tmp_path, handler)
    net.send_message(conv.id, "go", background=False, actor=alice)
    last = conv.messages[-1]
    assert last.role == "agent" and "中断前发出的文件" in last.text and last.attachments[0]["name"] == "x.txt"


def test_not_available_in_delegated_tasks(tmp_path: Path):
    """委托任务里没有 send_file：产物在交付时整体拷给委托方。"""
    names: dict[str, list[str]] = {}

    def handler(call: ScriptCall) -> str:
        names[call.context_id] = list(call.tool_names)
        if call.context_id.startswith("task-"):
            return "做完了"
        if call.turn == 1:
            call.tool("delegate", agent="W", mission="m", expected_outcome="o")
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    lead = net.create_agent("L", "l", [], ["delegate"], actor=alice)
    w = net.create_agent("W", "w", [], [], actor=alice)
    g = net.create_project("G", "", [lead.id, w.id], actor=alice)
    net.send_message(net.project_chat(g.id, actor=alice).id, "@L 开始", background=False, actor=alice)
    task_tools = next(v for k, v in names.items() if k.startswith("task-"))
    seat_tools = next(v for k, v in names.items() if not k.startswith("task-"))
    assert MCP_PREFIX + "send_file" in seat_tools and MCP_PREFIX + "send_file" not in task_tools


def test_flow_files_are_checked_when_sent_and_held_with_the_reply(tmp_path: Path):
    """不变量 22：读过越界来源之后 send_file 不放行；之前发的文件跟着被拦下的回复，所有者确认后一起发出。"""
    out: list[str] = []

    def handler(call: ScriptCall) -> str:
        if (r := writes_note(call)) is not None:
            return r
        if "能便宜吗" in call.prompt:
            (call.cwd / "quote.md").write_text("标准报价", encoding="utf-8")
            out.append(call.tool("send_file", path="quote.md"))
            call.tool("open_note", path="q.md")
            out.append(call.tool("send_file", path="quote.md", name="again.md"))
            return "底线 50 万"
        return "ok"

    net, alice, bob, a, q, g, chat = setup(tmp_path, handler, authority=("file.write",))
    net.send_message(chat.id, "@A 能便宜吗", actor=bob, background=False)
    assert "已附上" in out[0] and "网络不放行" in out[1]
    held = net.board(alice)["held"]
    assert held[0]["file_names"] == ["quote.md"]
    assert not any(m["attachments"] for m in net.conversation_detail(chat.id, actor=bob)["messages"])
    net.decide_held(held[0]["id"], "send", actor=alice)
    sent = net.conversation_detail(chat.id, actor=bob)["messages"][-1]
    assert sent["text"] == "底线 50 万" and [x["name"] for x in sent["attachments"]] == ["quote.md"]
