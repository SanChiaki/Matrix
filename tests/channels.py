"""两种通道跑同一组测试（docs/cloud-runtime.md 第 8 节“引擎层的测试”）：classic = 云侧 Agent 是服务的子进程（默认）；
sandbox = 云侧执行面的沙箱方式（内存里的假后端 + 真实的沙箱客户端代码，ScriptedRuntime 在“沙箱”里扮演 harness）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from matrix.engine import Network
from matrix.sandboxes import MemorySandboxes

CHANNELS = ["classic", "sandbox"]


def make_net(runtime: Any, root: Path, channel: str, **kw: Any) -> Network:
    if channel == "sandbox":
        kw.setdefault("sandbox", MemorySandboxes())
    return Network(runtime, root, **kw)
