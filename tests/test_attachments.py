"""对话附件：内容寻址、只读保存在对话的附件区；席位运行时拷进工作目录 attachments/，提示词里每个文件一行路径。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from pathlib import Path

import pytest

from matrix.device import RUN_REQUEST, RUN_RESULT, DeviceRuntime, MemoryLink
from matrix.engine import Network, NetworkError
from matrix.models import ATTACHMENT_LIMIT, ATTACHMENTS, attachment_rel
from matrix.runtime import ScriptCall, ScriptedRuntime
from matrix.store import Store
from tests.test_device import connector, hello
from tests.test_joint import is_task, joint
from tests.test_users import client, users


def _capture(seen: list[ScriptCall], reply: str = "收到"):
    def handler(call: ScriptCall) -> str:
        seen.append(call)
        call.files = {p.relative_to(call.cwd).as_posix(): p.read_bytes()
                      for p in (call.cwd / ATTACHMENTS).rglob("*") if p.is_file()}
        call.modes = {p.relative_to(call.cwd).as_posix(): p.stat().st_mode
                      for p in (call.cwd / ATTACHMENTS).rglob("*") if p.is_file()}
        return reply
    return handler


def test_attachment_reaches_the_seat_as_a_path_line(tmp_path: Path):
    """附件在席位运行时以只读副本出现在工作目录里，提示词里只有一行路径；运行后副本丢弃，原件不在任何工作目录里。"""
    seen: list[ScriptCall] = []
    net = Network(ScriptedRuntime(_capture(seen)), tmp_path, seed=False)
    alice = users(net, "alice@x.io")[0]
    a = net.create_agent("A", "a", [], ["file.write"], actor=alice)
    chat = net.direct_chat(a.id, actor=alice)
    meta = net.upload_attachment(chat.id, "../报告 v1.txt", b"hello attachment", "text/plain", actor=alice)
    assert meta == {"sha": hashlib.sha256(b"hello attachment").hexdigest(), "name": "报告 v1.txt",
                    "size": 16, "mime": "text/plain"}
    net.send_message(chat.id, "看看这个文件", attachments=[meta], background=False, actor=alice)

    rel = attachment_rel(meta)
    assert rel == f"{ATTACHMENTS}/{meta['sha'][:8]}/报告 v1.txt"
    call = seen[-1]
    assert f"`{rel}`" in call.prompt and "hello attachment" not in call.prompt
    assert call.files == {rel: b"hello attachment"}
    assert not call.modes[rel] & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)  # 只读副本
    workdir = tmp_path / chat.seats[a.id].workdir
    assert not (workdir / ATTACHMENTS).exists()  # 运行后丢弃，下一轮重新拷入
    stored = net.store.attachment_dir(chat.id) / meta["sha"]
    assert stored.read_bytes() == b"hello attachment" and workdir not in stored.parents
    assert chat.messages[0].attachments == [meta]

    # 下一轮（没有新附件）：之前的附件仍在原路径，提示词里不再重复
    net.send_message(chat.id, "再说一句", background=False, actor=alice)
    assert seen[-1].files == {rel: b"hello attachment"} and rel not in seen[-1].prompt


def test_attachment_only_message_and_limits(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice = users(net, "alice@x.io")[0]
    a = net.create_agent("A", "a", [], [], actor=alice)
    chat = net.direct_chat(a.id, actor=alice)
    meta = net.upload_attachment(chat.id, "a.bin", b"\x00\x01", "", actor=alice)
    assert meta["mime"] == "application/octet-stream"
    again = net.upload_attachment(chat.id, "b.bin", b"\x00\x01", "", actor=alice)
    assert again["sha"] == meta["sha"]  # 内容寻址：同样的内容只存一份
    m = net.send_message(chat.id, "  ", attachments=[meta], actor=alice)  # 只有附件也能发
    assert m.text == "" and m.attachments[0]["name"] == "a.bin"
    with pytest.raises(NetworkError, match="不能为空"):
        net.send_message(chat.id, " ", actor=alice)
    with pytest.raises(NetworkError, match="上限"):
        net.upload_attachment(chat.id, "big.bin", b"x" * (ATTACHMENT_LIMIT + 1), "", actor=alice)
    with pytest.raises(NetworkError, match="附件"):  # 没上传过的内容不能挂到消息上
        net.send_message(chat.id, "x", attachments=[{"sha": "0" * 64, "name": "x"}], actor=alice)
    net.wait_quiet()


def test_attachments_follow_conversation_visibility(tmp_path: Path):
    """不变量 13：看不到对话的人上传、下载附件一律 404；下载以附件形式返回、不在站点内渲染。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    chat = net.direct_chat(a.id, actor=alice)
    ca, cb = client(net, "alice@x.io"), client(net, "bob@x.io")

    r = ca.post(f"/api/conversations/{chat.id}/attachments", params={"name": "<b>x</b>.html"},
                content=b"<script>alert(1)</script>", headers={"Content-Type": "text/html"})
    assert r.status_code == 200, r.text
    meta = r.json()
    assert cb.post(f"/api/conversations/{chat.id}/attachments", params={"name": "y.txt"},
                   content=b"y").status_code == 404
    r = ca.post(f"/api/conversations/{chat.id}/messages", json={"text": "", "attachments": [meta]})
    assert r.status_code == 200, r.text
    assert r.json()["attachments"] == [meta]
    net.wait_quiet()

    url = f"/api/conversations/{chat.id}/attachments/{meta['sha']}"
    r = ca.get(url)
    assert r.status_code == 200 and r.content == b"<script>alert(1)</script>"
    assert r.headers["content-disposition"].startswith("attachment") and r.headers["x-content-type-options"] == "nosniff"
    assert cb.get(url).status_code == 404
    assert ca.get(f"/api/conversations/{chat.id}/attachments/{'0' * 64}").status_code == 404
    assert ca.get(f"/api/conversations/{chat.id}").json()["messages"][0]["attachments"] == [meta]

    big = ca.post(f"/api/conversations/{chat.id}/attachments", params={"name": "big.bin"},
                  content=b"x" * (ATTACHMENT_LIMIT + 1))
    assert big.status_code == 413


def test_joint_participants_see_attachments_outsiders_do_not(tmp_path: Path):
    """不变量 13：联合项目群聊里的附件对所有参与者可见，项目外的人 404。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice, bob, carol = users(net, "alice@x.io", "bob@x.io", "carol@x.io")
    for u in (alice, bob, carol):
        net.create_agent(f"助理-{u}", "a", [], [], actor=u)
    g, chat = joint(net, alice, "bob@x.io", bob)
    meta = net.upload_attachment(chat.id, "brief.md", b"# brief", "text/markdown", actor=bob)
    net.send_message(chat.id, "资料", attachments=[meta], actor=bob)
    path, info = net.attachment_file(chat.id, meta["sha"], actor=alice)
    assert path.read_bytes() == b"# brief" and info["name"] == "brief.md"
    with pytest.raises(NetworkError, match="unknown"):
        net.attachment_file(chat.id, meta["sha"], actor=carol)
    with pytest.raises(NetworkError, match="unknown"):
        net.upload_attachment(chat.id, "x.txt", b"x", "", actor=carol)


def _pass_on(seen: list[ScriptCall]):
    def handler(call: ScriptCall) -> str:
        seen.append(call)
        if is_task(call):
            call.files = {p.relative_to(call.cwd).as_posix(): p.read_bytes()
                          for p in (call.cwd / ATTACHMENTS).rglob("*") if p.is_file()}
            return "B 看完了"
        if call.turn == 1:
            rel = re.search(r"`(attachments/[0-9a-f]{8}/[^`]+)`", call.prompt).group(1)
            call.bad = call.tool("delegate", agent="B", mission="读附件", expected_outcome="摘要",
                                 attachments="attachments/deadbeef/nope.txt")
            call.tool("delegate", agent="B", mission="读附件", expected_outcome="摘要", attachments=f"./{rel}")
            return "已委托 B"
        return "汇总"
    return handler


def test_delegator_passes_an_attachment_on_explicitly(tmp_path: Path):
    """不变量 7、9：附件只经委托方显式转交（delegate 的 attachments）进任务；只能转交自己看得到的。"""
    seen: list[ScriptCall] = []
    net = Network(ScriptedRuntime(_pass_on(seen)), tmp_path, seed=False)
    alice = users(net, "alice@x.io")[0]
    a = net.create_agent("A", "a", [], ["delegate"], actor=alice)
    b = net.create_agent("B", "b", [], [], actor=alice)
    g = net.create_project("P", "p", [a.id, b.id], actor=alice)
    chat = net.project_chat(g.id, actor=alice)
    meta = net.upload_attachment(chat.id, "data.csv", b"a,b\n1,2\n", "text/csv", actor=alice)
    net.send_message(chat.id, "@A 分析一下", attachments=[meta], background=False, actor=alice)

    first = next(c for c in seen if not is_task(c))
    assert "附件" in first.bad and "nope.txt" in first.bad
    tasks = list(net.store.tasks.values())
    assert len(tasks) == 1 and tasks[0].attachments == [meta]
    run = next(c for c in seen if is_task(c))
    rel = attachment_rel(meta)
    assert f"`{rel}`" in run.prompt and run.files == {rel: b"a,b\n1,2\n"}
    # 没 @ 到的成员不会拿到附件：B 在群聊里没有席位
    assert b.id not in chat.seats


def test_device_seat_gets_attachments_as_materials(tmp_path: Path):
    """端侧席位：附件随 run.request 的 materials 下发（base64），服务器不在本机挂载。"""
    net = Network(ScriptedRuntime(lambda call: "cloud"), tmp_path, seed=False)
    alice = users(net, "alice@x.io")[0]
    local = net.create_agent("Local", "端侧", [], [], actor=alice)
    local.device_runtime = "laptop-1"
    sent: list[dict] = []
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), hello("fake-harness"))

    def script(msg, deliver):
        sent.append(msg)
        deliver({"type": RUN_RESULT, "run_id": msg["run_id"], "session_id": "dev-1", "final_text": "ok"})
    connector(rt, script=script)
    net.register_runtime("laptop-1", rt)
    chat = net.direct_chat(local.id, actor=alice)
    meta = net.upload_attachment(chat.id, "notes.txt", b"device data", "text/plain", actor=alice)
    net.send_message(chat.id, "看附件", attachments=[meta], actor=alice, background=False)

    msg = next(m for m in sent if m["type"] == RUN_REQUEST)
    json.dumps(msg)  # 能经 WebSocket 发出去
    mats = {m["path"]: base64.b64decode(m["data"]) for m in msg["materials"]}
    assert mats == {attachment_rel(meta): b"device data"}
    assert f"`{attachment_rel(meta)}`" in msg["prompt"]


def test_clear_drops_unreferenced_attachments(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice = users(net, "alice@x.io")[0]
    a = net.create_agent("A", "a", [], [], actor=alice)
    chat = net.direct_chat(a.id, actor=alice)
    meta = net.upload_attachment(chat.id, "x.txt", b"x", "", actor=alice)
    net.send_message(chat.id, "x", attachments=[meta], background=False, actor=alice)
    net.clear_conversation(chat.id, actor=alice)
    assert not (net.store.attachment_dir(chat.id) / meta["sha"]).exists()


def test_old_state_without_attachments_loads(tmp_path: Path):
    """旧版 state.json 里的消息、任务没有 attachments 字段：照常读出，取默认值。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    alice = users(net, "alice@x.io")[0]
    a = net.create_agent("A", "a", [], [], actor=alice)
    chat = net.direct_chat(a.id, actor=alice)
    net.send_message(chat.id, "hi", background=False, actor=alice)
    raw = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    for c in raw["conversations"]:
        for m in c["messages"]:
            m.pop("attachments", None)
    (tmp_path / "state.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    store = Store(tmp_path, seed=False)
    assert all(m.attachments == [] for m in store.conversations[chat.id].messages)
    assert os.path.exists(tmp_path / "state.json")
