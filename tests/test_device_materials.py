"""端侧材料：服务端每轮只给清单（路径、sha256、大小），连接器按 sha 缓存在本机，缺的再取（material.fetch）。
没变的档案文件、对话附件第二轮起一个字节都不传；旧连接器照旧整份内联。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import matrix.connector.client as client_mod
from matrix.connector.client import Connector
from matrix.connector.drivers.base import RunOutcome
from matrix.device import (FEATURE_MATERIALS_CACHE, HELLO_OK, MATERIAL_CHUNK, MATERIAL_FETCH, PROTOCOL, RUN_REQUEST,
                           DeviceRuntime, MemoryLink, capabilities_from_hello)
from matrix.engine import Network
from matrix.runtime import ENFORCED, RunRequest, ScriptedRuntime, material_bytes, material_file
from tests.test_users import users


class Seen:
    """假 harness：记下这一轮工作目录里看到的材料。"""
    name = "fake"

    def __init__(self):
        self.runs: list[dict[str, bytes]] = []

    def capabilities(self):
        from matrix.runtime import RuntimeCapabilities
        return RuntimeCapabilities.all_enforced("fake", placement="device")

    def run(self, spec, ctx, cancel):
        wd = spec.workdir
        self.runs.append({p.relative_to(wd).as_posix(): p.read_bytes() for p in wd.rglob("*") if p.is_file()})
        return RunOutcome(session_id="s", final_text="ok")


class Wire:
    """服务器的 DeviceRuntime ↔ 真实的 Connector，内存里直连；记下服务器发给连接器的字节数与取了哪些材料。"""

    def __init__(self, rt: DeviceRuntime, c: Connector):
        self.rt, self.c = rt, c
        self.down_bytes = 0
        self.fetched: list[str] = []
        self.tamper = False
        rt.link = MemoryLink(serve=self.down)
        c.send = self.up

    def down(self, msg):  # 服务器 → 连接器
        self.down_bytes += len(json.dumps(msg))
        if msg["type"] == RUN_REQUEST:
            self.c._start(msg)
        return None

    def up(self, msg):  # 连接器 → 服务器（回执交回连接器）
        reply = self.rt.on_message(msg)
        if msg.get("type") == MATERIAL_FETCH:
            self.fetched.append(msg["sha"])
            if self.tamper and reply.get("data"):
                reply["data"] = "dGFtcGVyZWQ="  # “tampered”
            self.down_bytes += len(json.dumps(reply))
        if reply is not None:
            self.c._deliver(reply)


def pair(tmp_path: Path, features=(FEATURE_MATERIALS_CACHE,)):
    info = capabilities_from_hello({"type": HELLO_OK, "protocol": PROTOCOL, "runtime": "fake", "features": list(features),
                                    "capabilities": {"name": "fake", "enforcement": {"file.write": ENFORCED}}})
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), info)
    driver = Seen()
    c = Connector(driver, tmp_path / "device")
    return rt, c, driver, Wire(rt, c)


def run(rt: DeviceRuntime, materials, ctx="conv-1"):
    return rt.run(RunRequest(agent_id="a", context_id=ctx, prompt="p", cwd=Path("."), authority=[],
                             materials=list(materials), timeout_s=10))


def test_second_round_transfers_nothing_unchanged(tmp_path: Path):
    """第一轮按清单取齐；第二轮内容没变，一份都不取，run.request 只带清单（字节数和内容大小无关）。"""
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 3_000_000)  # 比一块（1 MB）大：分块取
    brief = tmp_path / "brief.md"
    brief.write_text("目标：调研", encoding="utf-8")
    mats = lambda: [material_file("project/brief.md", brief), material_file("attachments/abcd1234/big.bin", big)]  # noqa: E731
    rt, c, driver, wire = pair(tmp_path)
    assert run(rt, mats()).ok
    first = wire.down_bytes
    assert driver.runs[0]["project/brief.md"] == "目标：调研".encode() and len(driver.runs[0]["attachments/abcd1234/big.bin"]) == 3_000_000
    assert sorted(set(wire.fetched)) == sorted({m.sha for m in mats()}) and len(wire.fetched) == 1 + 3  # 3 MB 分三块
    assert first > 3_000_000

    wire.down_bytes, wire.fetched = 0, []
    assert run(rt, mats()).ok
    assert wire.fetched == [] and wire.down_bytes < 5_000  # 只剩清单与运行请求本身
    assert driver.runs[1] == driver.runs[0]

    brief.write_text("目标：调研 + 竞品", encoding="utf-8")  # 只改一份：只取这一份
    wire.fetched = []
    run(rt, mats())
    assert wire.fetched == [hashlib.sha256("目标：调研 + 竞品".encode()).hexdigest()]


def test_old_connectors_still_get_materials_inline(tmp_path: Path):
    rt, c, driver, wire = pair(tmp_path, features=())
    assert run(rt, [material_bytes("plugins/wiki/index.md", b"# wiki")]).ok
    assert driver.runs[0]["plugins/wiki/index.md"] == b"# wiki" and wire.fetched == []
    assert "materials" in wire.rt.link.sent[0] and "manifest" not in wire.rt.link.sent[0]


def test_stale_copies_are_cleared_each_round(tmp_path: Path):
    """上一轮的只读副本（含 releases/、plugins/）先清掉：清单里没有的不会留在工作目录里。"""
    rt, c, driver, wire = pair(tmp_path)
    run(rt, [material_bytes("releases/weekly/a.json", b"{}"), material_bytes("plugins/wiki/x.md", b"x")])
    run(rt, [material_bytes("project/b.md", b"b")])
    assert set(driver.runs[1]) == {"project/b.md"}


def test_fetch_is_scoped_to_the_run_and_its_manifest(tmp_path: Path):
    """material.fetch 只认这一轮清单里的 sha，轮次结束就不能再取；越界的路径、坏 sha 在连接器那边就拒绝。"""
    rt, c, driver, wire = pair(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("别的对话的附件", encoding="utf-8")
    m = material_file("attachments/x/secret.txt", secret)
    assert rt.on_message({"type": MATERIAL_FETCH, "run_id": "run-none", "call_id": "c1", "sha": m.sha, "offset": 0}) == \
        {"reply_to": "c1", "error": "这一轮没有这份材料"}
    run(rt, [material_bytes("project/a.md", b"a")])
    assert "error" in rt.on_message({"type": MATERIAL_FETCH, "run_id": "run-1", "call_id": "c2", "sha": m.sha, "offset": 0})
    wd = c.workdir_of("a/conv-1")
    for bad in ({"path": "agent-home/x", "sha": "0" * 64, "size": 1}, {"path": "../x", "sha": "0" * 64, "size": 1},
                {"path": "project/x", "sha": "nothex", "size": 1}, {"path": "project/x", "sha": "0" * 64, "size": -1}):
        with pytest.raises(Exception):
            c._sync_materials(client_mod._Run(c, "r"), wd, [bad])


def test_tampered_content_fails_the_round_and_is_not_cached(tmp_path: Path):
    """取到的内容和清单的 sha 对不上：这一轮按传输错误失败，缓存里不留坏文件。"""
    rt, c, driver, wire = pair(tmp_path)
    wire.tamper = True
    res = run(rt, [material_bytes("project/a.md", b"real content")])
    assert not res.ok and res.error.startswith("TRANSPORT 材料没取到") and driver.runs == []
    assert not any(c.cache_dir.iterdir())


def test_cache_is_pruned_by_last_use(tmp_path: Path, monkeypatch):
    """缓存超过上限：按最近使用时间清最旧的，这一轮刚用的不清。"""
    monkeypatch.setattr(client_mod, "CACHE_LIMIT", 250)
    rt, c, driver, wire = pair(tmp_path)
    a, b, d = (material_bytes(f"project/{n}.md", n.encode() * 100) for n in "abd")
    import os
    run(rt, [a])
    os.utime(c.cache_dir / a.sha, (1000, 1000))  # 明确先后：不靠文件系统的时间精度
    run(rt, [b])
    os.utime(c.cache_dir / b.sha, (2000, 2000))
    run(rt, [d])  # a + b + d = 300 字节 > 250：清最旧的 a
    names = {f.name for f in c.cache_dir.iterdir()}
    assert a.sha not in names and {b.sha, d.sha} <= names


def test_device_seat_fetches_each_attachment_once(tmp_path: Path):
    """经网络的完整路径：端侧席位每轮看到对话里的全部附件，但同一个附件只传一次。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path / "data", seed=False)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("A", "a", [], [], actor=alice)
    rt, c, driver, wire = pair(tmp_path)
    a.device_runtime = "dev"
    net.register_runtime("dev", rt)
    dm = net.direct_chat(a.id, actor=alice)
    att = net.upload_attachment(dm.id, "data.csv", b"x,y\n" * 100_000, actor=alice)
    net.send_message(dm.id, "看看数据", attachments=[att], actor=alice, background=False)
    net.send_message(dm.id, "再看一遍", actor=alice, background=False)
    net.send_message(dm.id, "第三遍", actor=alice, background=False)
    assert wire.fetched.count(att["sha"]) == 1  # 400 KB 的附件只传了一次
    assert all("attachments/" + att["sha"][:8] + "/data.csv" in r for r in driver.runs) and len(driver.runs) == 3
    assert MATERIAL_CHUNK == 1 << 20
