"""连接器的写操作逐次确认（不变量 19）：部署方目录里的 MCP 服务按标准的工具标注（annotations）声明哪些工具
会对外产生影响；这类工具每次调用前，网络先给连接的授权人一张确认卡，照原样列出要发出的参数，同意才发、只发这一次。"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from matrix.connector_catalog import ConnectorCatalog
from matrix.engine import Limits, Network, NetworkError, _writes_outward
from matrix.runtime import ScriptCall, ScriptedRuntime
from tests.test_users import client, users

TOOLS = [
    {"name": "get_board", "description": "读看板", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}},
    {"name": "create_board", "description": "建草稿", "annotations": {"readOnlyHint": False, "destructiveHint": False,
                                                                     "openWorldHint": False},
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}}},
    {"name": "publish_board", "description": "发布看板", "annotations": {"title": "发布看板", "readOnlyHint": False,
                                                                       "destructiveHint": False, "openWorldHint": True},
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}},
    {"name": "ping", "description": "没有标注", "inputSchema": {"type": "object", "properties": {}}},
]


class Boards:
    """一个不要授权的公开 MCP 服务，记下真正被调用的工具。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def do_POST(self):  # noqa: N802
                msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "id" not in msg:
                    self.send_response(202)
                    self.end_headers()
                    return
                m = msg["method"]
                if m == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "b"}}
                elif m == "tools/list":
                    result = {"tools": TOOLS}
                else:
                    outer.calls.append((msg["params"]["name"], msg["params"]["arguments"]))
                    result = {"content": [{"type": "text", "text": f"ok {msg['params']['name']}"}]}
                body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"


@pytest.fixture
def boards():
    b = Boards()
    yield b
    b.server.shutdown()


def setup(tmp_path: Path, boards: Boards, handler, timeout: float = 30):
    (tmp_path / "connectors.json").write_text(json.dumps({"connectors": [
        {"id": "boards", "name": "看板系统", "url": boards.url}]}), encoding="utf-8")
    net = Network(ScriptedRuntime(handler), tmp_path / "data", seed=False,
                  connectors=ConnectorCatalog(tmp_path / "connectors.json"),
                  limits=Limits(connector_ask_timeout_s=timeout))
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    a = net.create_agent("Dash", "做看板", [], ["connector.use"], actor=alice)
    net.authorize_connector("boards", {}, actor=alice, agent_ids=[a.id])  # 公开服务：不填任何东西
    return net, alice, bob, a


def run_in_background(net: Network, conv_id: str, text: str, actor: str) -> threading.Thread:
    t = threading.Thread(target=net.send_message, args=(conv_id, text), kwargs={"actor": actor, "background": False})
    t.start()
    return t


def wait_pending(net: Network, actor: str, timeout: float = 10):
    end = time.time() + timeout
    while time.time() < end:
        ap = [x for x in net.pending_approvals(actor) if x.kind == "connector_write"]
        if ap:
            return ap[0]
        time.sleep(0.02)
    raise AssertionError("没有等到确认卡")


def test_annotation_rule():
    """不变量 19：只认明确的标注——不是只读，并且标了 destructiveHint 或 openWorldHint。"""
    assert [t["name"] for t in TOOLS if _writes_outward(t)] == ["publish_board"]
    assert _writes_outward({"annotations": {"destructiveHint": True}})
    assert not _writes_outward({"annotations": {"readOnlyHint": True, "destructiveHint": True}})


def test_write_call_waits_for_owner_and_sends_exactly_once(tmp_path: Path, boards: Boards):
    """不变量 19：发布这类工具每次调用前先停下，确认卡照原样列出参数；同意后只发这一次，下一次还要问；
    只读、建草稿、没有标注的工具直接调用。"""
    replies: list[str] = []

    def handler(call: ScriptCall) -> str:
        replies.append(call.connector("boards__create_board", title="周报"))
        replies.append(call.connector("boards__publish_board", id="b-1"))
        replies.append(call.connector("boards__publish_board", id="b-2"))
        return "ok"

    net, alice, bob, a = setup(tmp_path, boards, handler)
    dm = net.direct_chat(a.id, actor=alice)
    t = run_in_background(net, dm.id, "做个看板并发布", alice)
    ap = wait_pending(net, alice)
    assert ap.tool == "publish_board" and ap.args == {"id": "b-1"} and "openWorld" not in ap.reason
    assert "要用你的「看板系统」执行「发布看板」：会影响服务以外的人或系统" in ap.reason
    assert [c[0] for c in boards.calls] == ["create_board"]  # 等确认时还没有发出
    with pytest.raises(NetworkError, match="unknown approval"):  # 只有授权人能决定
        net.decide_approval(ap.id, True, actor=bob)
    net.decide_approval(ap.id, True, actor=alice)
    ap2 = wait_pending(net, alice)  # 第二次调用再问一次
    assert ap2.id != ap.id and ap2.args == {"id": "b-2"}
    net.decide_approval(ap2.id, False, actor=alice)
    t.join(10)
    assert boards.calls == [("create_board", {"title": "周报"}), ("publish_board", {"id": "b-1"})]
    assert replies[1] == "ok publish_board" and "没有同意" in replies[2]
    assert not any(e.type == "authority.violation" for e in net.bus.since(0))  # 收窄，不是越权


def test_unanswered_confirmation_expires(tmp_path: Path, boards: Boards):
    """不变量 19：没人确认就作废，不发出。"""
    replies: list[str] = []

    def handler(call: ScriptCall) -> str:
        replies.append(call.connector("boards__publish_board", id="b-1"))
        return "ok"

    net, alice, _, a = setup(tmp_path, boards, handler, timeout=0.3)
    net.send_message(net.direct_chat(a.id, actor=alice).id, "发布", actor=alice, background=False)
    assert "没有同意" in replies[0] and boards.calls == []
    assert [x.status for x in net.store.approvals.values() if x.kind == "connector_write"] == ["cancelled"]


def test_tool_list_marks_confirmation(tmp_path: Path, boards: Boards):
    """Agent 拿到的工具说明里写明哪些要先确认。"""
    seen: list[dict] = []

    def handler(call: ScriptCall) -> str:
        seen.extend(call._req.connectors.list_tools())
        return "ok"

    net, alice, _, a = setup(tmp_path, boards, handler)
    net.send_message(net.direct_chat(a.id, actor=alice).id, "看看", actor=alice, background=False)
    desc = {t["name"]: t["description"] for t in seen}
    assert "每次调用都要你的所有者先确认" in desc["boards__publish_board"]
    assert "确认" not in desc["boards__create_board"] and "确认" not in desc["boards__ping"]


def test_confirmation_card_over_http(tmp_path: Path, boards: Boards):
    """不变量 19：确认卡在看板“待我处理”里，带参数；只有授权人看得到。"""
    def handler(call: ScriptCall) -> str:
        return call.connector("boards__publish_board", id="b-9")

    net, alice, bob, a = setup(tmp_path, boards, handler)
    t = run_in_background(net, net.direct_chat(a.id, actor=alice).id, "发布", alice)
    ap = wait_pending(net, alice)
    al, bo = client(net, "alice@x.io"), client(net, "bob@x.io")
    card = next(x for x in al.get("/api/board").json()["approvals"] if x["id"] == ap.id)
    assert card["kind"] == "connector_write" and card["args"] == {"id": "b-9"}
    assert not any(x["id"] == ap.id for x in bo.get("/api/board").json()["approvals"])
    assert al.post(f"/api/approvals/{ap.id}", json={"allow": True}).status_code == 200
    t.join(10)
    assert boards.calls == [("publish_board", {"id": "b-9"})]
