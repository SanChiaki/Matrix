"""代码从 agentnet 改名为 matrix 之后的兼容（matrix/compat.py）：老部署的环境变量、老数据目录、
旧 state.json 与还没升级的端侧连接器里的网络工具名。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from matrix import compat
from matrix.device import RUN_REQUEST, RUN_RESULT, DeviceRuntime, MemoryLink
from matrix.models import MCP_PREFIX
from matrix.runtime import RunRequest
from matrix.store import Store

ROOT = Path(__file__).resolve().parent.parent
LEGACY_NAMES = ("DATA", "HOST", "MODELS", "CONNECTORS", "PLUGINS", "VAULT_KEY", "MCP_CMD")


@pytest.fixture
def clean_env(monkeypatch):
    for n in LEGACY_NAMES:
        monkeypatch.delenv(f"MATRIX_{n}", raising=False)
        monkeypatch.delenv(f"AGENTNET_{n}", raising=False)
    return monkeypatch


def test_env_prefers_new_name_and_falls_back_to_old(clean_env):
    """部署方设的变量：新名字优先，没设（或为空）时沿用 AGENTNET_*，都没有用默认值。"""
    assert compat.env("MODELS") is None and compat.env("HOST", "127.0.0.1") == "127.0.0.1"
    clean_env.setenv("AGENTNET_MODELS", "/old/models.json")
    assert compat.env("MODELS") == "/old/models.json"
    clean_env.setenv("MATRIX_MODELS", "")
    assert compat.env("MODELS") == "/old/models.json"
    clean_env.setenv("MATRIX_MODELS", "/new/models.json")
    assert compat.env("MODELS") == "/new/models.json"


def test_vault_reads_legacy_key(clean_env, tmp_path: Path):
    from cryptography.fernet import Fernet

    from matrix.vault import Vault
    key = Fernet.generate_key().decode()
    clean_env.setenv("AGENTNET_VAULT_KEY", key)
    v = Vault(tmp_path)
    v.put("c1", {"token": "x"})
    clean_env.delenv("AGENTNET_VAULT_KEY")
    clean_env.setenv("MATRIX_VAULT_KEY", key)
    assert Vault(tmp_path).get("c1") == {"token": "x"}  # 同一把密钥，换了变量名照样解得开
    assert not (tmp_path / "vault.key").exists()


def test_default_data_dir_falls_back_to_legacy(clean_env, tmp_path: Path):
    """没指定数据目录：默认 .matrix；只有改名前的 .agentnet 时沿用它（不改名、提示一行）。"""
    notes: list[str] = []
    assert compat.data_dir(tmp_path, notes.append) == ".matrix" and notes == []
    (tmp_path / ".agentnet").mkdir()
    assert compat.data_dir(tmp_path, notes.append) == ".agentnet"
    assert len(notes) == 1 and ".agentnet" in notes[0] and ".matrix" in notes[0]
    assert (tmp_path / ".agentnet").is_dir() and not (tmp_path / ".matrix").exists()  # 没有动旧目录
    (tmp_path / ".matrix").mkdir()
    assert compat.data_dir(tmp_path, notes.append) == ".matrix" and len(notes) == 1  # 两个都在：用新的


def test_data_dir_env_wins_over_directories(clean_env, tmp_path: Path):
    (tmp_path / ".agentnet").mkdir()
    clean_env.setenv("AGENTNET_DATA", "/srv/old")
    assert compat.data_dir(tmp_path, lambda _: None) == "/srv/old"
    clean_env.setenv("MATRIX_DATA", "/srv/new")
    assert compat.data_dir(tmp_path, lambda _: None) == "/srv/new"


def test_old_tool_names_in_state_are_renamed(tmp_path: Path):
    """旧 state.json 里委托证据记的是 mcp__agentnet__*：读进来换成现在的前缀，其余工具原样。"""
    state = {"agents": [], "users": [], "projects": [], "conversations": [], "tasks": [{
        "id": "task-1", "conversation_id": "conv-1", "project_id": None, "delegator_id": "a1", "delegatee_id": "a2",
        "parent_task_id": None, "depth": 1, "status": "completed",
        "delegation": {"mission": "m", "authority": [], "constraints": [], "expected_outcome": "o"},
        "evidence": [{"tool": "mcp__agentnet__delegate", "input": "{}", "status": "ok"},
                     {"tool": "web_search", "input": "q", "status": "ok"}]}]}
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    store = Store(tmp_path)
    assert [e["tool"] for e in store.tasks["task-1"].evidence] == [MCP_PREFIX + "delegate", "web_search"]


def test_old_connector_tool_calls_are_renamed():
    """还没升级的端侧连接器报回 mcp__agentnet__*：网络这边换成现在的名字，违规检测照常认得。"""
    def serve(msg):
        if msg["type"] == RUN_REQUEST:
            rt.on_message({"type": RUN_RESULT, "run_id": msg["run_id"], "final_text": "ok",
                           "tool_calls": [{"tool": "mcp__agentnet__delegate", "status": "ok"}, {"tool": "write"}]})

    from matrix.device import capabilities_from_hello
    rt = DeviceRuntime(MemoryLink(serve=serve), capabilities_from_hello({"type": "hello.ok", "runtime": "x"}))
    rt.link.on_incoming = lambda m: rt.on_message(m)
    res = rt.run(RunRequest(agent_id="a", context_id="c", prompt="hi", cwd=None, authority=[]))
    assert [c["tool"] for c in res.tool_calls] == [MCP_PREFIX + "delegate", "write"]


def test_server_cli_entry_point():
    """入口改名为 matrix：`python -m matrix.server --help` 能跑，帮助里说明数据目录的沿用。
    输出到管道时一律 UTF-8：这里把管道编码设成 cp1252（英文 Windows 上 Python 3.14 的默认），在哪个系统上都能复现。"""
    out = subprocess.run([sys.executable, "-m", "matrix.server", "--help"], capture_output=True, text=True,
                         encoding="utf-8", cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "cp1252"}, timeout=60)
    assert "数据目录" in out.stdout
    assert out.returncode == 0 and "--data" in out.stdout and ".agentnet" in out.stdout
