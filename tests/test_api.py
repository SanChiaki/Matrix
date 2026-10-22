"""HTTP API 冒烟测试（ScriptedRuntime）+ MCP 服务经真实 HTTP 回调网络。"""

from __future__ import annotations

import json
import os
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

    from agentnet import auth
    _, token, _ = auth.register(net.store, "owner@example.com", "password-123")
    opened = threading.Event()

    def read() -> None:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/events",
                                         headers={"Cookie": f"{auth.COOKIE}={token}"})
            with urllib.request.urlopen(req, timeout=20) as r:
                opened.set()
                for _ in r:
                    pass
        except Exception:  # noqa: BLE001 —— 断开方式不重要，重要的是断开
            pass
        ended["t"] = time.time()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    assert opened.wait(5), "事件流没有建立（登录态没生效？）"
    time.sleep(0.3)
    t0 = time.time()
    server.handle_exit(signal.SIGTERM, None)
    th.join(10)
    reader.join(10)
    assert not th.is_alive(), "服务没有退出"
    assert "t" in ended and ended["t"] - t0 < 3, "事件流没有随服务关闭而断开"


def login(c: TestClient, email: str = "owner@example.com", password: str = "password-123") -> dict:
    """注册（已注册则登录）；TestClient 自动带上 cookie。"""
    r = c.post("/api/auth/register", json={"email": email, "password": password})
    if r.status_code == 400 and "注册过" in r.text:
        r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def test_api_flow(tmp_path: Path):
    net = Network(ScriptedRuntime(scenario_handler), tmp_path)
    c = TestClient(create_app(net))

    assert c.get("/").status_code == 200
    assert c.get("/static/app.js").status_code == 200
    assert "Matrix" in c.get("/about").text  # 简介页不需要登录
    assert c.get("/api/info").json()["runtime"] == "scripted"
    assert c.get("/api/network").status_code == 401  # 没登录
    me = login(c)
    assert me["claimed_existing"] is True  # 第一个用户认领种子数据
    snap = c.get("/api/network").json()
    assert len(snap["agents"]) == 5 and len(snap["projects"]) == 1
    names = {a["name"]: a["id"] for a in snap["agents"]}
    group = snap["projects"][0]

    # Agent 管理
    new = c.post("/api/agents", json={"name": "Finance Agent", "description": "财务测算",
                                      "capabilities": ["finance"], "authority": ["web.research"]}).json()
    assert c.post("/api/agents", json={"name": "Finance Agent", "description": "dup"}).status_code == 409
    assert c.post("/api/agents", json={"name": "Bad Agent", "description": "xx", "authority": ["root"]}).status_code == 409
    assert c.post("/api/agents", json={"name": "Long Agent", "description": "长" * 2001}).status_code == 422
    assert c.patch(f"/api/agents/{new['id']}", json={"description": "长" * 2000}).status_code == 200
    assert c.patch(f"/api/agents/{new['id']}", json={"authority": ["web.research", "file.write"]}).json()["authority"] \
        == ["web.research", "file.write"]
    m = {"provider": "hi-code-claude", "model": "claude-opus-5-5"}
    assert c.patch(f"/api/agents/{new['id']}", json={"model": m}).json()["model"] == m
    assert c.patch(f"/api/agents/{new['id']}", json={"description": "财务测算与预算"}).json()["model"] == m
    assert c.patch(f"/api/agents/{new['id']}", json={"model": None}).json()["model"] is None

    # 项目管理
    g2 = c.post("/api/projects", json={"name": "财务组", "member_ids": [names["Product Strategy Agent"], new["id"]]}).json()
    assert c.patch(f"/api/projects/{g2['id']}", json={"member_ids": [new["id"]]}).status_code == 409  # 助理不能移出
    assert c.patch(f"/api/projects/{g2['id']}", json={"member_ids": [names["Product Strategy Agent"]]}).json()["member_ids"] \
        == [names["Product Strategy Agent"]]
    assert c.post("/api/projects", json={"name": "x", "member_ids": ["agent-nope"]}).status_code == 404

    # 群聊：@ 谁谁接
    conv = c.post("/api/conversations", json={"project_id": group["id"]}).json()
    assert conv["kind"] == "project" and conv["seats"] == {}
    lead = names["Product Strategy Agent"]
    r = c.post(f"/api/conversations/{conv['id']}/messages", json={"text": "研究 AI 产品机会", "mentions": [lead]})
    assert r.status_code == 200 and r.json()["mentions"] == [lead]
    net.wait_idle(conv["id"], timeout=10)
    d = c.get(f"/api/conversations/{conv['id']}").json()
    msgs = [m for m in d["messages"] if m["role"] != "event"]  # 任务登记 / 交付是 event
    roles = [m["role"] for m in msgs]
    assert roles[0] == "user" and set(roles[1:]) == {"agent"} and len(roles) >= 3  # 委托回报唤醒后续回合
    assert {m["agent_id"] for m in msgs[1:]} == {lead}
    assert [r["status"] for r in c.get("/api/board").json()["requests"]] == ["in_review"]
    assert "机会成立" in d["messages"][-1]["text"] and d["status"] == "idle"
    assert set(d["members"]) == set(group["member_ids"]) and list(d["seats"]) == [lead]
    assert "inbox" not in d["seats"][lead] and d["seats"][lead]["pending"] == 0
    assert len(d["tasks"]) == 5
    t = next(t for t in d["tasks"] if t["status"] == "completed")
    full = c.get(f"/api/tasks/{t['id']}").json()
    assert full["result"]["report"] and c.get(f"/api/knowledge/{full['result']['knowledge_key']}").json()["content"]
    assert c.get("/api/network").json()["relations"]
    # 没 @ 任何人：发出但不唤醒
    before = len(d["messages"])
    c.post(f"/api/conversations/{conv['id']}/messages", json={"text": "收到，谢谢"})
    net.wait_idle(conv["id"], timeout=10)
    assert len(c.get(f"/api/conversations/{conv['id']}").json()["messages"]) == before + 1
    # 只能 @ 项目成员
    assert c.post(f"/api/conversations/{conv['id']}/messages",
                  json={"text": "x", "mentions": [new["id"]]}).status_code == 409
    # 私聊
    dm = c.post("/api/conversations", json={"agent_id": new["id"]}).json()
    assert dm["kind"] == "dm" and dm["agent_id"] == new["id"]
    assert c.post("/api/conversations", json={}).status_code == 409
    assert c.get("/api/tasks/nope").status_code == 404
    assert c.post(f"/api/conversations/{conv['id']}/messages", json={"text": ""}).status_code == 422
    assert c.post("/internal/tools", json={"token": "x", "tool": "list_members"}).json()["is_error"] is True
    assert c.post(f"/api/conversations/{conv['id']}/resume", json={}).status_code == 409  # 没有中断
    assert c.post(f"/api/tasks/{t['id']}/retry", json={}).status_code == 409             # 不是失败任务
    assert c.delete(f"/api/agents/{new['id']}").json()["ok"]
    assert new["id"] not in c.get("/api/network").json()["projects"][1]["member_ids"]


def test_api_proposal_review(tmp_path: Path):
    """人在 API 上审核 Agent 的提议：批准时可改字段，权限不能超出提议者。"""
    from agentnet.runtime import ScriptCall

    def handler(call: ScriptCall) -> str:
        if call.can_delegate and call.turn == 1:
            call.tool("propose_agent", name="Legal Agent", description="合规", reason="缺人", authority=["web.research"])
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path)
    c = TestClient(create_app(net))
    login(c)
    lead = net.store.agent_by_name("Product Strategy Agent")
    group = next(iter(net.store.projects.values()))
    conv = net.create_conversation(group.id)
    net.send_message(conv.id, f"@{lead.name} 需要合规", background=False)
    props = c.get("/api/network").json()["proposals"]
    assert len(props) == 1 and props[0]["status"] == "pending"
    pid = props[0]["id"]
    assert c.post(f"/api/proposals/{pid}/approve", json={"authority": ["shell.exec"]}).status_code == 409
    a = c.post(f"/api/proposals/{pid}/approve", json={"description": "合规与隐私", "note": "ok"}).json()
    assert a["description"] == "合规与隐私" and a["authority"] == ["web.research"]
    assert a["id"] in c.get("/api/network").json()["projects"][0]["member_ids"]
    assert c.post(f"/api/proposals/{pid}/reject", json={}).status_code == 409
    assert c.post("/api/proposals/nope/approve", json={}).status_code == 404


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_mcp_server_calls_back_into_network(tmp_path: Path):
    """不变量 10。"""
    port = _free_port()
    net = Network(ScriptedRuntime(scenario_handler), tmp_path, tool_url=f"http://127.0.0.1:{port}/internal/tools")
    server = uvicorn.Server(uvicorn.Config(create_app(net), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    try:
        lead = net.store.agent_by_name("Product Strategy Agent")
        group = next(iter(net.store.projects.values()))
        conv = net.create_conversation(group.id)
        with net.store.lock:
            net._seat(conv, lead.id)  # 委托方的席位：交付通知会送到这里
        net._contexts["tok"] = ExecContext(kind="seat", id=f"{conv.id}/{lead.id}", agent_id=lead.id,
                                           conversation_id=conv.id, project_id=group.id, authority=lead.authority,
                                           constraints=[], depth=0, chain=[lead.id])
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_members", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "delegate", "arguments": {
                "agent": "Market Research Agent", "mission": "看市场", "expected_outcome": "摘要"}}},
        ]
        out = subprocess.run([sys.executable, str(MCP_SERVER_SCRIPT)], input="\n".join(json.dumps(m) for m in msgs),
                             capture_output=True, text=True, timeout=30,
                             env={**os.environ, "AGENTNET_TOOL_URL": net.tool_url, "AGENTNET_TOKEN": "tok"}, encoding="utf-8").stdout
        res = {r["id"]: r["result"] for r in map(json.loads, out.splitlines())}
        assert "Market Research Agent" in res[2]["content"][0]["text"] and not res[2]["isError"]
        assert "已受理" in res[3]["content"][0]["text"] and not res[3]["isError"]  # 异步：立即返回
        net.wait_quiet()
        # 交付唤醒对话 Agent 的下一轮，由它验收
        assert len(net.store.tasks) == 1 and next(iter(net.store.tasks.values())).status.value == "completed"
    finally:
        server.should_exit = True
