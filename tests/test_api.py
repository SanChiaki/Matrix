"""HTTP API 冒烟测试（ScriptedRuntime）+ MCP 服务经真实 HTTP 回调网络。"""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi.testclient import TestClient

from agentnet.engine import ExecContext, Network
from agentnet.runtime import MCP_SERVER_SCRIPT, ScriptedRuntime
from agentnet.scripted import scenario_handler
from agentnet.server import AgentNetServer, create_app


def test_event_stream_resume_after_restart():
    """事件序号不持久化：服务重启后旧页面带着更大的 since 重连，必须从头补发，而不是一直沉默。"""
    from agentnet.server import resume_seq
    assert resume_seq(since=4, last_event_id=None, last_seq=10) == 4
    assert resume_seq(since=4, last_event_id="7", last_seq=10) == 7   # 浏览器自动重连带 Last-Event-ID
    assert resume_seq(since=300, last_event_id=None, last_seq=2) == 0  # 服务已重启
    assert resume_seq(since=4, last_event_id="300", last_seq=2) == 0
    assert resume_seq(since=0, last_event_id="x", last_seq=2) == 0


def test_event_stream_closes_on_shutdown(tmp_path: Path):
    """停服务时事件流必须立刻断开：否则进程挂着不退出，浏览器也一直以为连接正常，不会重连。"""
    import urllib.request

    net = Network(ScriptedRuntime(scenario_handler), tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # 优雅关闭超时设得很长：事件流必须因退出信号立即断开，而不是等超时强切
    # 优雅关闭超时设得很长：事件流必须因退出信号立即断开，而不是等超时强切
    server = AgentNetServer(uvicorn.Config(create_app(net), host="127.0.0.1", port=port, log_level="warning",
                                           timeout_graceful_shutdown=30), net)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.05)

    ended: dict[str, float] = {}

    def read() -> None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/events", timeout=20) as r:
                for _ in r:
                    pass
        except Exception:  # noqa: BLE001 —— 断开方式不重要，重要的是断开
            pass
        ended["t"] = time.time()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    time.sleep(0.5)
    t0 = time.time()
    server.handle_exit(signal.SIGTERM, None)
    th.join(10)
    reader.join(10)
    assert not th.is_alive(), "服务没有退出"
    assert "t" in ended and ended["t"] - t0 < 3, "事件流没有随服务关闭而断开"


def test_api_flow(tmp_path: Path):
    net = Network(ScriptedRuntime(scenario_handler), tmp_path)
    c = TestClient(create_app(net))

    assert c.get("/").status_code == 200
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/api/info").json()["runtime"] == "scripted"
    snap = c.get("/api/network").json()
    assert len(snap["agents"]) == 5 and len(snap["groups"]) == 1
    names = {a["name"]: a["id"] for a in snap["agents"]}
    group = snap["groups"][0]

    # Agent 管理
    new = c.post("/api/agents", json={"name": "Finance Agent", "description": "财务测算",
                                      "capabilities": ["finance"], "authority": ["web.research"]}).json()
    assert c.post("/api/agents", json={"name": "Finance Agent", "description": "dup"}).status_code == 409
    assert c.post("/api/agents", json={"name": "Bad Agent", "description": "xx", "authority": ["root"]}).status_code == 409
    assert c.patch(f"/api/agents/{new['id']}", json={"authority": ["web.research", "file.write"]}).json()["authority"] \
        == ["web.research", "file.write"]

    # 群组管理
    g2 = c.post("/api/groups", json={"name": "财务组", "member_ids": [names["Product Strategy Agent"], new["id"]]}).json()
    assert c.patch(f"/api/groups/{g2['id']}", json={"member_ids": [new["id"]]}).json()["member_ids"] == [new["id"]]
    assert c.post("/api/groups", json={"name": "x", "member_ids": ["agent-nope"]}).status_code == 404

    # 对话
    conv = c.post("/api/conversations", json={"agent_id": names["Product Strategy Agent"],
                                              "group_id": group["id"]}).json()
    assert c.post(f"/api/conversations/{conv['id']}/messages", json={"text": "研究 AI 产品机会"}).status_code == 200
    net.wait_idle(conv["id"], timeout=10)
    d = c.get(f"/api/conversations/{conv['id']}").json()
    assert [m["role"] for m in d["messages"]] == ["user", "agent"]
    assert len(d["tasks"]) == 5
    t = next(t for t in d["tasks"] if t["status"] == "completed")
    full = c.get(f"/api/tasks/{t['id']}").json()
    assert full["result"]["report"] and c.get(f"/api/knowledge/{full['result']['knowledge_key']}").json()["content"]
    assert c.get("/api/network").json()["relations"]

    assert c.post("/api/conversations", json={"agent_id": new["id"], "group_id": group["id"]}).status_code == 409
    assert c.get("/api/tasks/nope").status_code == 404
    assert c.post(f"/api/conversations/{conv['id']}/messages", json={"text": ""}).status_code == 422
    assert c.post("/internal/tools", json={"token": "x", "tool": "list_members"}).json()["is_error"] is True
    assert c.delete(f"/api/agents/{new['id']}").json()["ok"]
    assert new["id"] not in c.get("/api/network").json()["groups"][1]["member_ids"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_mcp_server_calls_back_into_network(tmp_path: Path):
    port = _free_port()
    net = Network(ScriptedRuntime(scenario_handler), tmp_path, tool_url=f"http://127.0.0.1:{port}/internal/tools")
    server = uvicorn.Server(uvicorn.Config(create_app(net), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    try:
        lead = net.store.agent_by_name("Product Strategy Agent")
        group = next(iter(net.store.groups.values()))
        conv = net.create_conversation(lead.id, group.id)
        net._contexts["tok"] = ExecContext(kind="conversation", id=conv.id, agent_id=lead.id,
                                           conversation_id=conv.id, group_id=group.id, authority=lead.authority,
                                           constraints=[], depth=0, chain=[lead.id])
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_members", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "delegate", "arguments": {
                "agent": "Market Research Agent", "mission": "看市场", "expected_outcome": "摘要"}}},
        ]
        out = subprocess.run([sys.executable, str(MCP_SERVER_SCRIPT)], input="\n".join(json.dumps(m) for m in msgs),
                             capture_output=True, text=True, timeout=30,
                             env={"AGENTNET_TOOL_URL": net.tool_url, "AGENTNET_TOKEN": "tok"}).stdout
        res = {r["id"]: r["result"] for r in map(json.loads, out.splitlines())}
        assert "Market Research Agent" in res[2]["content"][0]["text"] and not res[2]["isError"]
        assert "completed" in res[3]["content"][0]["text"] and not res[3]["isError"]
        assert len(net.store.tasks) == 1
    finally:
        server.should_exit = True
