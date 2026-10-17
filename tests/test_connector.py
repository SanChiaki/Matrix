"""端侧连接器本体（docs/device-runtimes.md 第 2.3 节）：工作目录边界、本机桥、两家驱动的参数与解析。"""
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from agentnet.connector.bridge import LocalBridge
from agentnet.connector.client import Connector
from agentnet.connector.drivers.base import RunSpec
from agentnet.connector.drivers.claude import ClaudeDriver
from agentnet.connector.drivers.codex import CodexDriver
from agentnet.runtime import ADVISORY, ENFORCED, ToolBridge


def fake_bin(tmp_path: Path, name: str, body: str) -> str:
    """写一个假 harness：读 stdin，按 body 往 stdout 写。"""
    p = tmp_path / name
    p.write_text(f"#!{sys.executable}\nimport sys, json\nsys.stdin.read()\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


def spec(tmp_path, **kw):
    base = dict(run_id="run-1", agent_id="a", context_id="c", prompt="hi", workdir=tmp_path / "w",
                authority=["web.research"], tool_names=[])
    base.update(kw)
    return RunSpec(**base)


class Ctx:
    def __init__(self):
        self.events = []

    def emit(self, ev):
        self.events.append(ev)

    def call_tool(self, name, args):
        return "ok", False

    def ask_approval(self, tool, reason):
        return "rejected"


# ================= 工作目录边界 =================
def test_workdir_ref_cannot_escape_the_root(tmp_path):
    c = Connector(ClaudeDriver(), tmp_path)
    assert c.workdir_of("agent-1/conv-1") == tmp_path / "agent-1" / "conv-1"
    for bad in ("../etc", "/etc/passwd", "a/../../b", ""):
        with pytest.raises(Exception):
            c.workdir_of(bad)
    c.close()


# ================= 本机桥 =================
def test_bridge_scopes_tokens_and_routes(tmp_path):
    import urllib.error
    import urllib.request

    b = LocalBridge()
    seen = []

    def tools(name, args):
        seen.append((name, args))
        return "已受理", False

    b.register("run-1", tools, lambda tool, reason: "allowed-once")

    def post(path, body):
        req = urllib.request.Request(b.url + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:  # 4xx 也会带 JSON（403 = 令牌无效）
            return json.loads(e.read())

    assert post("/internal/tools", {"token": "run-1", "tool": "delegate", "args": {"x": 1}}) == {
        "text": "已受理", "is_error": False}
    assert seen == [("delegate", {"x": 1})]
    assert post("/internal/approval", {"token": "run-1", "tool": "write", "reason": "r"}) == {
        "outcome": "allowed-once"}
    assert post("/internal/tools", {"token": "run-2", "tool": "delegate"})["is_error"] is True
    b.unregister("run-1")
    assert post("/internal/tools", {"token": "run-1", "tool": "delegate"})["is_error"] is True
    b.close()


# ================= Claude Code =================
def test_claude_command_maps_authority_to_tool_whitelist(tmp_path):
    d = ClaudeDriver("claude", "http://127.0.0.1:1")
    cmd = d.command(spec(tmp_path, authority=["web.research"], model={"model": "sonnet"}, session_id="s1",
                         tool_names=["delegate"]))
    assert cmd[1] == "-p" and "stream-json" in cmd and "--resume" in cmd and "s1" in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "WebSearch" in allowed and "Bash" not in allowed and "Write" not in allowed
    assert "--mcp-config" in cmd

    caps = d.capabilities()
    assert caps.enforces("web.research") and caps.enforces("delegate")
    assert caps.approval == "none"  # 没有审批上行：如实声明


def test_claude_runs_a_fake_harness(tmp_path):
    body = """
print(json.dumps({"type":"system","subtype":"init","session_id":"sess-9"}))
print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"thinking"}]}}))
print(json.dumps({"type":"result","session_id":"sess-9","result":"done"}))
"""
    d = ClaudeDriver(fake_bin(tmp_path, "claude", body))
    ctx = Ctx()
    out = d.run(spec(tmp_path), ctx, threading.Event())
    assert out.ok and out.final_text == "done" and out.session_id == "sess-9"
    assert ctx.events == [{"type": "text", "text": "thinking"}]


def test_claude_nonzero_exit_is_a_server_error(tmp_path):
    body = "print('boom', file=sys.stderr)\nsys.exit(3)\n"
    d = ClaudeDriver(fake_bin(tmp_path, "claude", body))
    out = d.run(spec(tmp_path), Ctx(), threading.Event())
    assert not out.ok and "SERVER" in out.error and "boom" in out.error


# ================= Codex =================
def test_codex_command_sandbox_and_resume(tmp_path):
    d = CodexDriver("codex")
    ro = d.command(spec(tmp_path, authority=["shell.exec"]))
    rw = d.command(spec(tmp_path, authority=["shell.exec", "file.write"]))
    assert "read-only" in ro and "workspace-write" in rw
    resumed = d.command(spec(tmp_path, session_id="th-1"))
    assert resumed[1:3] == ["exec", "resume"] and "th-1" in resumed
    assert d.capabilities().level("web.research") == ADVISORY  # 不在 exec 参数里：不入有效权限


def test_codex_runs_a_fake_harness(tmp_path):
    body = """
print(json.dumps({"type":"thread.started","thread_id":"th-7"}))
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"hello"}}))
print(json.dumps({"type":"turn.completed","last_agent_message":"hello"}))
"""
    d = CodexDriver(fake_bin(tmp_path, "codex", body))
    ctx = Ctx()
    out = d.run(spec(tmp_path), ctx, threading.Event())
    assert out.ok and out.final_text == "hello" and out.session_id == "th-7"
    assert ctx.events == [{"type": "text", "text": "hello"}]


# ================= 运行上下文 =================
def test_run_ctx_relays_tools_and_approvals():
    c = Connector(ClaudeDriver(), Path("/tmp"))
    from agentnet.connector.client import _Run
    run = _Run(c, "run-x")

    def deliver(msg):
        c._deliver({"reply_to": msg["call_id"], "text": "network said hi", "is_error": False})

    c.send = lambda payload: deliver(payload)
    assert run.call_tool("delegate", {}) == ("network said hi", False)
    c.send = lambda payload: c._deliver({"reply_to": payload["call_id"], "outcome": "rejected"})
    assert run.ask_approval("write", "outside") == "rejected"
    c.close()
