"""云侧读隔离（需求 #34、#59 阶段 0，docs/cloud-runtime.md 第 8 节）：真的起一次外层 bwrap，里面的命令只读得到
这个执行单元的工作目录与它自己的 dsh 状态目录。数据目录的其余部分（别人的私有目录、别的对话与委托、项目档案、
外部平台暂存的文件、共用的 ~/.dsh、`vault.key`）在外层里不存在。

命令拼装的单元测试在 test_cloud_isolation.py；这里是能变红的端到端检查，机器上没有能用的嵌套 bwrap 时跳过
（真实 dsh 上的同一组检查是 `matrix-dsh-check` 的 read-isolation）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from matrix import sandbox
from matrix.engine import Network
from matrix.runtime import ScriptedRuntime, outer_argv
from matrix.sandboxes import MemorySandboxes
from tests.test_users import users


def _nested_ok() -> bool:
    return sys.platform.startswith("linux") and sandbox.available("nested")[0]


needs_bwrap = pytest.mark.skipif(not _nested_ok(), reason="这台机器上没有能用的嵌套 bwrap")

SECRET = "matrix-read-isolation-marker"


def _data_dir(root: Path) -> dict[str, Path]:
    """一个数据目录：这一轮的工作目录与状态目录，以及外层里不该出现的那些东西（每个都写进标记）。"""
    data = root / "data"
    own_work = data / "conversations" / "c1" / "a1"
    own_state = data / "dsh-state" / "seats" / "c1" / "a1"
    others = {
        "vault_key": data / "vault.key",
        "state_json": data / "state.json",
        "other_home": data / "agents" / "b1" / "notes.md",                 # 别人的私有目录
        "other_seat": data / "conversations" / "c2" / "b1" / "out.md",     # 别的对话里的席位
        "other_task": data / "tasks" / "t9" / "report.md",                 # 别的委托
        "other_state": data / "dsh-state" / "seats" / "c2" / "b1" / "session.jsonl",
        "archive": data / "projects" / "p1" / "files" / "secret.md",       # 项目档案（服务只给圈定的范围）
        "staging": data / "staging" / "abc" / "upload.bin",                # 外部平台 Agent 暂存的文件
        "legacy_dsh": root / "home" / ".dsh" / "sessions" / "x.jsonl",     # 升级前共用的 ~/.dsh
    }
    for p in others.values():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SECRET, encoding="utf-8")
    own_work.mkdir(parents=True)
    own_state.mkdir(parents=True)
    (own_work / "mine.md").write_text("mine", encoding="utf-8")
    return {"work": own_work, "state": own_state, **others}


def _run_inside(paths: dict[str, Path], script: str) -> subprocess.CompletedProcess[str]:
    cmd = outer_argv(["/bin/sh", "-c", script], cwd=paths["work"], state=paths["state"], ro=[])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


@needs_bwrap
def test_outer_sandbox_reads_only_its_own_unit(tmp_path: Path):
    paths = _data_dir(tmp_path)
    probes = {k: v for k, v in paths.items() if k not in ("work", "state")}
    script = "; ".join(f"cat {p} 2>/dev/null && echo LEAK:{k}" for k, p in probes.items())
    script += f"; cat {paths['work'] / 'mine.md'} && echo; echo hi > {paths['state'] / 'w'} && echo STATE_OK"
    top = Path(*tmp_path.parts[:2])   # 数据目录所在的顶层目录（/tmp、/home …）：外层里不该搜得到任何标记
    script += f"; grep -rl {SECRET} {top} /home /root 2>/dev/null | head -5 | sed 's/^/FOUND:/'"
    p = _run_inside(paths, script)
    assert "LEAK:" not in p.stdout, p.stdout
    assert "FOUND:" not in p.stdout, p.stdout
    assert "mine" in p.stdout and "STATE_OK" in p.stdout, (p.stdout, p.stderr)


@needs_bwrap
def test_outer_sandbox_cannot_list_the_data_dir_or_other_processes(tmp_path: Path):
    paths = _data_dir(tmp_path)
    data = tmp_path / "data"
    keeper = subprocess.Popen(["sleep", "30"])   # 沙箱外的进程
    try:
        p = _run_inside(paths, f"ls {data}; ls {data / 'agents'} 2>&1; ls /proc | grep -x {keeper.pid} && echo SEE_PID")
    finally:
        keeper.kill()
    listed = p.stdout.split()
    # 数据目录里只剩通向这一轮两个目录的路径
    assert set(listed) & {"vault.key", "state.json", "agents", "tasks", "projects", "staging"} == set(), p.stdout
    assert "SEE_PID" not in p.stdout


def test_remote_agents_never_get_a_cloud_sandbox_or_state_dir(tmp_path: Path):
    """外部平台 Agent 在平台上跑，不在 Matrix 服务器上执行：沙箱方式打开时也不给它占沙箱、不建 dsh 状态目录。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path / "data", seed=False, sandbox=MemorySandboxes())
    alice = users(net, "alice@x.io")[0]
    a = net.create_agent("A", "a", [], [], actor=alice)
    assert net._sandboxed(a)
    a.remote = {"platform": "demo", "agent": "x"}
    assert not net._sandboxed(a)
    a.remote = None
    a.device_runtime = "dev-1"
    assert not net._sandboxed(a)
