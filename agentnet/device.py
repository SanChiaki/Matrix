"""端侧运行时：用户电脑上的连接器（见 docs/device-runtimes.md 第 2 节）。

分两层：

- **协议**：服务器与连接器之间的一组消息（本文件顶部）。传输无关，测试用内存链路，线上用出站 WebSocket。
- **DeviceRuntime**：实现 `AgentRuntime`，把 `RunRequest` 变成 `run.request` 发出去，等待 `run.result`；
  期间把 `run.event` 转成进度、把 `tool.call` / `approval.request` 交回网络。

连接器那一侧（各 harness 的驱动）不在这个进程里；它只需要实现同一组消息。

不变量：
- 身份由网络认定（不变量 10）：连接器不提供身份，`tool.call` 交给执行上下文令牌对应的 handler。
- 权限只收窄（不变量 1）：`run.request.authority` 是网络算好的有效权限，连接器不得放大。
- 外溢审批由 Agent 的所有者决定（不变量 14）：连接器只转发，不自行批准。
"""

from __future__ import annotations

import asyncio
import base64
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .models import AUTHORITIES
from .runtime import (ENFORCED, RunRequest, RunResult, RuntimeCapabilities)

# ---- 消息（服务器 → 连接器）----
HELLO = "hello"                    # {protocol, token, runtime}
RUN_REQUEST = "run.request"        # {run_id, agent_id, context_id, conversation_id, session_id, workdir_ref,
                                   #  prompt, authority, tool_names, model, materials}
                                   #  materials: [{path, data(base64)}] —— 群组文件与委托材料的只读副本
CANCEL = "cancel"                  # {run_id}
SHUTDOWN = "shutdown"

# ---- 消息（连接器 → 服务器）----
HELLO_OK = "hello.ok"              # {runtime, capabilities}
RUN_EVENT = "run.event"            # {run_id, event}  —— 与 RunRequest.on_event 的事件同形
RUN_RESULT = "run.result"          # {run_id, session_id, final_text, tool_calls, usage, ok, error}
TOOL_CALL = "tool.call"            # {run_id, call_id, name, args}
APPROVAL_REQUEST = "approval.request"  # {run_id, call_id, tool, reason}
FILE_FETCH = "file.fetch"          # {run_id, call_id, path} —— 服务器要读端侧工作目录里的文件（publish_file）

PROTOCOL = 1


class DeviceError(RuntimeError):
    """连接器不可达 / 拒绝 / 协议错误。"""


class DeviceLink(Protocol):
    """一条与连接器的连接。真实实现是 WebSocket；测试用内存链路。

    - `request(payload, timeout)`：发一条需要回执的消息，阻塞到收到 `{reply_to: ...}` 或超时；
    - `send(payload)`：不需要回执（进度、取消）；
    - `close()`。
    实现方负责重连；本层只关心"这一轮能不能拿到答复"。
    """

    def send(self, payload: dict[str, Any]) -> None: ...
    def request(self, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]: ...
    def close(self) -> None: ...


@dataclass
class DeviceInfo:
    """连接器握手时报告的能力（第 10 节要逐条验证的那几项）。"""
    runtime: str
    capabilities: RuntimeCapabilities
    protocol: int = PROTOCOL


class DeviceRuntime:
    """一个已连接的端侧运行时。一个连接器可以承载多个 Agent；这里按 Agent 绑定。"""

    def __init__(self, link: DeviceLink, info: DeviceInfo, *, now: Callable[[], float] = time.time):
        self.name = info.runtime
        self.link = link
        self.info = info
        self._now = now
        self._runs: dict[str, threading.Event] = {}
        self._results: dict[str, dict[str, Any]] = {}
        self._by_context: dict[str, str] = {}          # 执行上下文 id -> 正在跑的 run_id
        self._fetches: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._seq = 0

    # ---- AgentRuntime ----
    def capabilities(self) -> RuntimeCapabilities:
        return self.info.capabilities

    def run(self, req: RunRequest) -> RunResult:
        run_id = self._next_run_id()
        done = threading.Event()
        with self._lock:
            self._runs[run_id] = done
            self._by_context[req.context_id] = run_id
        payload = {
            "type": RUN_REQUEST, "run_id": run_id,
            "agent_id": req.agent_id, "context_id": req.context_id,
            "session_id": req.session_id, "workdir_ref": self.workdir_ref(req),
            "prompt": req.prompt, "authority": list(req.authority),
            "tool_names": list(req.tools.names or []) if req.tools else [],
            "model": req.model, "materials": list(getattr(req, "materials", None) or []),
        }
        try:
            self.link.send(payload)
            replied = done.wait(req.timeout_s)
        except DeviceError as e:
            return RunResult("", "", ok=False, error=f"TRANSPORT device unreachable: {e}")
        finally:
            with self._lock:
                self._runs.pop(run_id, None)
                if self._by_context.get(req.context_id) == run_id:
                    self._by_context.pop(req.context_id, None)
        if not replied:
            self.cancel(run_id)
            return RunResult("", "", ok=False, error=f"TIMEOUT device did not answer in {req.timeout_s:.0f}s")
        with self._lock:
            data = self._results.pop(run_id, {})
        return RunResult(
            session_id=data.get("session_id") or req.session_id or "",
            final_text=data.get("final_text", ""),
            tool_calls=list(data.get("tool_calls") or []),
            usage=dict(data.get("usage") or {}),
            ok=bool(data.get("ok", True)),
            error=data.get("error"),
        )

    def cancel(self, run_id: str) -> None:
        try:
            self.link.send({"type": CANCEL, "run_id": run_id})
        except DeviceError:
            pass

    # ---- 连接器发来的消息（由传输层投递）----
    def on_message(self, msg: dict[str, Any], *, on_tool: Callable[[str, dict[str, Any]], tuple[str, bool]] | None = None,
                   on_approval: Callable[[str, str], str] | None = None,
                   on_event: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any] | None:
        """处理一条来自连接器的消息；需要回执时返回回执。"""
        kind = msg.get("type")
        run_id = str(msg.get("run_id", ""))
        if kind == RUN_EVENT:
            if on_event is not None:
                on_event(msg.get("event") or {})
        elif kind == TOOL_CALL:
            text, is_error = ("没有可用的网络工具", True) if on_tool is None else on_tool(
                str(msg.get("name", "")), dict(msg.get("args") or {}))
            return {"reply_to": msg.get("call_id"), "text": text, "is_error": is_error}
        elif kind == APPROVAL_REQUEST:
            outcome = "unavailable" if on_approval is None else on_approval(
                str(msg.get("tool", "")), str(msg.get("reason", "")))
            return {"reply_to": msg.get("call_id"), "outcome": outcome}
        elif kind == RUN_RESULT:
            with self._lock:
                self._results[run_id] = msg
                done = self._runs.get(run_id)
            if done is not None:
                done.set()
        elif "reply_to" in msg and run_id == "":  # file.fetch 的回执
            cid = str(msg.get("reply_to", ""))
            with self._lock:
                pending = self._fetches.pop(cid, None)
            if pending is not None:
                pending[1].update(msg)
                pending[0].set()
        return None

    # ---- 读端侧工作目录里的文件（publish_file 用） ----
    def fetch_file(self, context_id: str, path: str, timeout_s: float = 60) -> bytes | None:
        """向连接器要一个文件的字节；不在线 / 越界 / 不存在返回 None。"""
        with self._lock:
            run_id = self._by_context.get(context_id)
        if run_id is None:
            return None
        cid = f"f{uuid.uuid4().hex[:12]}"
        done, box = threading.Event(), {}
        with self._lock:
            self._fetches[cid] = (done, box)
        try:
            self.link.send({"type": FILE_FETCH, "run_id": run_id, "call_id": cid, "path": path})
            if not done.wait(timeout_s):
                return None
        except DeviceError:
            return None
        finally:
            with self._lock:
                self._fetches.pop(cid, None)
        if box.get("error") or not box.get("data"):
            return None
        try:
            return base64.b64decode(box["data"])
        except Exception:  # noqa: BLE001
            return None

    def workdir_ref(self, req: RunRequest) -> str:
        """端侧工作目录的引用：网络只保存不透明引用，不拼路径（第 4 节）。
        同一个席位 / 任务续接时必须一致。"""
        return f"{req.agent_id}/{req.context_id}"

    def _next_run_id(self) -> str:
        with self._lock:
            self._seq += 1
            return f"run-{self._seq}-{int(self._now())}"


class WebSocketLink:
    """线上传输：一个已接受的 WebSocket。服务器的运行线程通过 `run_coroutine_threadsafe` 发消息。"""

    def __init__(self, ws: Any, loop: Any = None):
        self._ws = ws
        self._loop = loop
        self.on_message: Callable[[dict[str, Any]], dict[str, Any] | None] = lambda m: None

    def _call(self, coro: Any, timeout_s: float | None = None) -> Any:
        loop = self._loop or asyncio.get_event_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout_s)

    def send(self, payload: dict[str, Any]) -> None:
        try:
            self._call(self._ws.send_json(payload), 30)
        except Exception as e:  # noqa: BLE001 —— 连接已断
            raise DeviceError(str(e)) from e

    def request(self, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        self.send(payload)
        return {}

    def close(self) -> None:
        try:
            self._call(self._ws.close(), 5)
        except Exception:  # noqa: BLE001
            pass


def capabilities_from_hello(msg: dict[str, Any]) -> DeviceInfo:
    """校验握手回执并转成 DeviceInfo。连接器多报的权限会被裁掉：只认已知权限。"""
    if msg.get("type") != HELLO_OK:
        raise DeviceError(f"expected {HELLO_OK}, got {msg.get('type')!r}")
    proto = msg.get("protocol", PROTOCOL)
    if proto != PROTOCOL:
        raise DeviceError(f"protocol mismatch: connector {proto}, server {PROTOCOL}")
    raw = msg.get("capabilities") or {}
    enforcement = {a: lvl for a, lvl in (raw.get("enforcement") or {}).items()
                   if a in AUTHORITIES and lvl in (ENFORCED, "advisory", "unavailable")}
    caps = RuntimeCapabilities(name=str(msg.get("runtime") or raw.get("name") or "device"),
                               placement="device",
                               enforcement=enforcement,
                               session_resume=bool(raw.get("session_resume", False)),
                               steer=bool(raw.get("steer", False)),
                               approval=str(raw.get("approval") or "none"),
                               sandbox=str(raw.get("sandbox") or "none"))
    return DeviceInfo(runtime=caps.name, capabilities=caps, protocol=proto)


@dataclass
class MemoryLink:
    """测试用内存链路：把服务器发出的消息交给一个 `serve(msg) -> reply | None` 函数。
    需要回执的消息（tool.call / approval.request）由 serve 同步返回。"""

    serve: Callable[[dict[str, Any]], dict[str, Any] | None]
    sent: list[dict[str, Any]] = field(default_factory=list)

    def send(self, payload: dict[str, Any]) -> None:
        self.sent.append(dict(payload))
        reply = self.serve(dict(payload))
        if reply is not None:  # 连接器主动发来的回执（比如 run.result）交回运行时
            self.on_incoming(reply)

    def request(self, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        self.send(payload)
        return {}

    def close(self) -> None: ...

    on_incoming: Callable[[dict[str, Any]], None] = lambda self, msg: None
