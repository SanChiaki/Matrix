"""改名兼容：代码从 agentnet 改名为 matrix 之后，老部署、老数据与还没升级的端侧连接器照常能用。

- 环境变量：部署方设置的 `MATRIX_*` 没设时，沿用改名前的 `AGENTNET_*`（`env`）。网络自己传给子进程的
  （执行上下文令牌、工具回调地址等）两边一起改了名，不走这里。
- 数据目录：没指定 `--data` / `MATRIX_DATA` 时默认 `.matrix`；它不存在而改名前的 `.agentnet` 在，就继续用
  `.agentnet` 并提示一行（`data_dir`）。不自动改名：目录可能正被另一个（旧版本的）进程使用，Agent 写下的
  文本里也有指向它的绝对路径；沿用不丢任何东西，人想换成新名字时停掉服务 `mv .agentnet .matrix` 即可。
- 网络工具名：MCP 服务名从 agentnet 改为 matrix_net，工具前缀随之从 `mcp__agentnet__` 变为 `mcp__matrix_net__`。
  旧 state.json 里的委托证据、还没升级的端侧连接器报回的调用经 `current_tool` 换成新名字，违规检测照常认得。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from .models import MCP_PREFIX

LEGACY_ENV_PREFIX = "AGENTNET_"
DATA_DIR = ".matrix"
LEGACY_DATA_DIR = ".agentnet"
LEGACY_MCP_PREFIX = "mcp__agentnet__"


def env(name: str, default: str | None = None) -> str | None:
    """部署方配置 `MATRIX_<name>`；没设（或为空）时读改名前的 `AGENTNET_<name>`，都没有用 default。"""
    return os.environ.get(f"MATRIX_{name}") or os.environ.get(LEGACY_ENV_PREFIX + name) or default


def _say(text: str) -> None:
    print(text, flush=True)


def data_dir(cwd: Path | None = None, notice: Callable[[str], None] = _say) -> str:
    """`--data` 没给时的数据目录：环境变量优先；否则 `.matrix`，只有旧的 `.agentnet` 时沿用它。"""
    configured = env("DATA")
    if configured:
        return configured
    base = Path.cwd() if cwd is None else cwd
    if not (base / DATA_DIR).exists() and (base / LEGACY_DATA_DIR).is_dir():
        notice(f"[matrix] 沿用改名前的数据目录 {LEGACY_DATA_DIR}（新默认是 {DATA_DIR}；"
               f"停掉服务后 mv {LEGACY_DATA_DIR} {DATA_DIR} 即可改用新名字）")
        return LEGACY_DATA_DIR
    return DATA_DIR


def current_tool(name: str) -> str:
    """改名前的网络工具名（`mcp__agentnet__x`）换成现在的（`mcp__matrix_net__x`）；其余原样返回。"""
    return MCP_PREFIX + name.removeprefix(LEGACY_MCP_PREFIX) if name.startswith(LEGACY_MCP_PREFIX) else name


def current_call(c: Any) -> Any:
    """一条工具调用记录（委托证据、运行结果里的 `{"tool": ...}`）里的网络工具名换成现在的。"""
    if isinstance(c, dict) and isinstance(c.get("tool"), str):
        return {**c, "tool": current_tool(c["tool"])}
    return c
