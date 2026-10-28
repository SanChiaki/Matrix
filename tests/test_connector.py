"""端侧连接器本体（docs/device-runtimes.md 第 2.3 节）：工作目录边界、本机桥、两家驱动的参数与解析。"""
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from matrix.connector.bridge import LocalBridge
from matrix.connector.client import Connector
from matrix.connector.drivers.base import RunSpec
from matrix.connector.drivers.claude import ClaudeDriver
from matrix.connector.drivers.codex import CodexDriver
from matrix.device import RUN_REQUEST as RUN_REQUEST_MSG
from matrix.device import DeviceRuntime, MemoryLink
from matrix.runtime import ADVISORY, ENFORCED, ToolBridge


def fake_bin(tmp_path: Path, name: str, body: str) -> str:
    """写一个假 harness：读 stdin，按 body 往 stdout 写。"""
    p = tmp_path / name
    p.write_text(f"#!{sys.executable}\nimport sys, json\nsys.stdin.read()\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    if os.name == "nt":  # Windows 不认 shebang：用 .cmd 包一层（和 npm 装的 harness 一样）
        shim = tmp_path / f"{name}.cmd"
        shim.write_text(f'@set PYTHONIOENCODING=utf-8\r\n@"{sys.executable}" "{p}" %*\r\n', encoding="utf-8")
        return str(shim)
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
    for bad in ("../etc", "/etc/passwd", "a/../../b", "", "C:/x", "C:x/y", "a/C:"):  # 盘符在 Windows 上会跳出根目录
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
    assert resumed[1:3] == ["exec", "resume"] and resumed[-2:] == ["th-1", "-"]
    # resume 子命令没有 --sandbox / --cd（传了 codex 直接报参数错误）：沙箱经 -c 覆盖
    assert "--sandbox" not in resumed and "--cd" not in resumed and 'sandbox_mode="read-only"' in resumed
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
    from matrix.connector.client import _Run
    run = _Run(c, "run-x")

    def deliver(msg):
        c._deliver({"reply_to": msg["call_id"], "text": "network said hi", "is_error": False})

    c.send = lambda payload: deliver(payload)
    assert run.call_tool("delegate", {}) == ("network said hi", False)
    c.send = lambda payload: c._deliver({"reply_to": payload["call_id"], "outcome": "rejected"})
    assert run.ask_approval("write", "outside") == "rejected"
    c.close()


# ================= 材料下发与回传 =================
def test_materials_are_written_and_group_is_refreshed(tmp_path):
    from matrix.connector.client import Connector
    import base64
    c = Connector(ClaudeDriver(), tmp_path)
    wd = c.workdir_of("a/c")
    (wd / "project").mkdir(parents=True)
    (wd / "project" / "stale.md").write_text("old", encoding="utf-8")
    c._write_materials(wd, [{"path": "project/brief.md", "data": base64.b64encode("新档案".encode()).decode()}])
    assert (wd / "project" / "brief.md").read_text(encoding="utf-8") == "新档案"
    assert not (wd / "project" / "stale.md").exists()  # 上一轮的只读副本不残留
    for bad in ({"path": p, "data": ""} for p in ("/etc/passwd", "../x", r"\x", "C:x", r"C:\x", r"\\srv\share\x",
                                                    r"group\..\..\x")):
        with pytest.raises(Exception):
            c._write_materials(wd, [bad])
    c.close()


def test_safe_path_rejects_escape_and_missing(tmp_path):
    from matrix.connector.client import Connector
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "ok.md").write_text("x", encoding="utf-8")
    assert Connector._safe_path(root, "sub/ok.md").read_text(encoding="utf-8") == "x"
    for bad in ("../root/sub/ok.md", "/etc/passwd", "sub/../../x", "nope.md", r"\sub\ok.md", "C:sub/ok.md"):
        with pytest.raises(Exception):
            Connector._safe_path(root, bad)


def test_fetch_file_round_trip():
    """publish_file 的回读：服务器发 file.fetch，连接器校验路径后回 base64。"""
    import base64
    from tests.test_device import hello as device_hello
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), device_hello())
    sent = []

    def serve(msg):
        sent.append(msg)
        if msg["type"] == RUN_REQUEST_MSG:
            # 模拟连接器的运行登记与文件存在
            return None
        return None

    link = MemoryLink(serve=serve)
    rt.link = link
    rt._by_context["a/c"] = "run-1"
    import threading

    def responder():
        for _ in range(100):
            msg = next((m for m in sent if m["type"] == "file.fetch"), None)
            if msg:
                rt.on_message({"reply_to": msg["call_id"], "run_id": "",
                               "data": base64.b64encode(b"report").decode()})
                return
            import time
            time.sleep(0.01)

    threading.Thread(target=responder, daemon=True).start()
    assert rt.fetch_file("a/c", "out/report.md", timeout_s=5) == b"report"
    assert rt.fetch_file("unknown", "x") is None


def test_run_request_carries_materials_to_the_connector(tmp_path):
    """服务器下发的材料随 run.request 到达连接器，并写进这一轮的工作目录。"""
    from matrix.device import RUN_REQUEST as RUN_REQ
    from matrix.device import DeviceRuntime, MemoryLink
    from tests.test_device import hello as device_hello

    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), device_hello())
    c = Connector(ClaudeDriver(), tmp_path)
    got = []

    def serve(msg):
        got.append(msg)
        if msg["type"] == RUN_REQ:
            c._start(msg)
        return None

    c._ws = None
    c.send = lambda payload: got.append(payload)
    link = MemoryLink(serve=serve)
    rt.link = link
    with c._send_lock:
        pass
    import base64
    materials = [{"path": "project/brief.md", "data": base64.b64encode("目标：调研".encode()).decode()}]
    # 直接走连接器入口，避免真实 WebSocket
    c.send = lambda payload: None
    c._start({"run_id": "run-m", "agent_id": "a", "context_id": "conv", "prompt": "p",
              "workdir_ref": "a/conv", "authority": [], "tool_names": [], "materials": materials})
    wd = c.workdir_of("a/conv")
    import time
    for _ in range(100):
        if (wd / "project" / "brief.md").exists():
            break
        time.sleep(0.01)
    assert (wd / "project" / "brief.md").read_text(encoding="utf-8") == "目标：调研"
    c.close()


def test_claude_reports_the_harness_reason_not_empty_stderr(tmp_path):
    """harness 用 result 事件说明失败原因（如未登录）时，退出码 0 也要按失败处理并把原因带出来。"""
    body = """
print(json.dumps({"type":"system","subtype":"init","session_id":"s"}))
print(json.dumps({"type":"result","session_id":"s","is_error":True,"result":"Not logged in"}))
"""
    d = ClaudeDriver(fake_bin(tmp_path, "claude", body))
    out = d.run(spec(tmp_path), Ctx(), threading.Event())
    assert not out.ok and "Not logged in" in out.error


def test_claude_env_drops_host_credentials():
    """宿主进程注入的凭据 / 网关变量不能带进用户的 claude，否则会变成 "Not logged in"。"""
    from matrix.connector.drivers.claude import clean_env

    env = clean_env({"PATH": "/bin", "CLAUDECODE": "1", "ANTHROPIC_BASE_URL": "http://host",
                     "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1", "HOME": "/Users/x"})
    assert env == {"PATH": "/bin", "HOME": "/Users/x"}


# ================= Windows =================
NPM_SHIM = ('@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\nSETLOCAL\r\nCALL :find_dp0\r\n'
            'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
            r'"%dp0%\node_modules\@deepseek-ai\dsh\lib\bin.js" %*' '\r\n')
NPM_SCRIPT = r"node_modules\@deepseek-ai\dsh\lib\bin.js"


def test_npm_cmd_shim_runs_through_node_not_cmd_exe(tmp_path):
    """Windows 上 npm 装的 harness 是 .cmd 包装：直接用 node 跑它指向的脚本，参数（JSON、路径）不经 cmd.exe 改写。"""
    from matrix.runtime import argv
    shim = tmp_path / "dsh.cmd"
    shim.write_text(NPM_SHIM, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "node.exe").write_text("", encoding="utf-8")
    got = argv([str(shim), "headless", '{"a": "b c"}'], windows=True)
    assert got == [str(tmp_path / "node.exe"), str(tmp_path / NPM_SCRIPT), "headless", '{"a": "b c"}']
    assert argv(["dsh", "x"], windows=False) == ["dsh", "x"]  # 其他系统原样
    assert argv(["no-such-harness-xyz"], windows=True) == ["no-such-harness-xyz"]


def test_npm_cmd_shim_to_a_native_exe_runs_it_directly(tmp_path):
    """新版 claude-code 的 npm 包带原生 claude.exe，.cmd 包装直接调用它：直接跑 .exe，不交给 node（node 不认 .exe）。"""
    from matrix.runtime import argv
    shim = tmp_path / "claude.cmd"
    shim.write_text('@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\nSETLOCAL\r\n'
                    'CALL :find_dp0\r\n' r'"%dp0%\node_modules\@anthropic-ai\claude-code\bin\claude.exe"   %*' '\r\n',
                    encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "node.exe").write_text("", encoding="utf-8")
    exe = tmp_path / r"node_modules\@anthropic-ai\claude-code\bin\claude.exe"
    assert argv([str(shim), "-p", '{"a": "b c"}'], windows=True) == [str(exe), "-p", '{"a": "b c"}']


def test_codex_resume_trusts_windows_paths(tmp_path):
    r"""codex 的 -c 值是 TOML：Windows 路径里的反斜杠不能写进基本字符串（\U 是非法转义）。"""
    import tomllib
    from matrix.connector.drivers.codex import toml_key
    for p in (r"C:\Users\me\.matrix\a\c", "/home/me/a", "it's"):
        assert tomllib.loads(f'projects.{toml_key(p)}.trust_level="trusted"')["projects"][p]["trust_level"] == "trusted"


def test_harness_output_in_locale_encoding_does_not_fail_the_run(tmp_path):
    """harness 调的子进程（如 Windows 的 taskkill）按区域编码（GBK）输出：解不了的字节替换掉，这一轮照常完成。"""
    body = """
sys.stderr.buffer.write("成功: 已终止 PID 1 的进程。\\r\\n".encode("gbk")); sys.stderr.flush()
print(json.dumps({"type":"thread.started","thread_id":"th-8"}))
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"ok"}}))
"""
    out = CodexDriver(fake_bin(tmp_path, "codex", body)).run(spec(tmp_path), Ctx(), threading.Event())
    assert out.ok and out.final_text == "ok" and out.session_id == "th-8"


def test_driver_guard_enforces_timeout_and_cancel():
    """端侧驱动按这一轮的超时（Agent 设置）或网络取消结束 harness 进程；不设超时就一直跑。"""
    import subprocess
    import time

    from matrix.runtime import watch_process as guard

    sleep = [sys.executable, "-c", "import time; time.sleep(30)"]
    p = subprocess.Popen(sleep)
    timed_out = guard(p, 0.3, threading.Event())
    p.wait(10)
    assert timed_out.is_set()

    cancel = threading.Event()
    p = subprocess.Popen(sleep)
    timed_out = guard(p, None, cancel)
    time.sleep(0.6)
    assert p.poll() is None  # 不限制：还在跑
    cancel.set()
    p.wait(10)
    assert not timed_out.is_set()


def test_dsh_runtime_stops_on_cancel(tmp_path):
    """云侧 dsh：人终止时结束进程，这一轮以 STOPPED 结束，拿到的会话 id 保留。"""
    import time

    from matrix.runtime import DshRuntime, RunRequest

    fake = fake_bin(tmp_path, "fake-dsh", "import time\nprint(json.dumps({'type': 'session', 'sessionId': 's-1'}), flush=True)\n"
                    "time.sleep(30)\n")  # Windows 上是 .cmd 包装：停止要连同真正的 harness 一起结束
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    t0 = time.monotonic()
    res = DshRuntime(dsh_bin=fake).run(RunRequest(agent_id="a", context_id="c", prompt="hi",
                                                      cwd=tmp_path / "w", authority=[], cancel=stop))
    assert time.monotonic() - t0 < 10
    assert not res.ok and "STOPPED" in res.error and res.session_id == "s-1"
