"""插件服务：网络托管的 stdio MCP 进程（docs/plugin-hooks.md 第 4 节）。

只有部署方目录里的插件能带服务（不变量 24）。一个安装（项目 × 插件）一个进程，按需启动、空闲停掉；
工作目录是这个安装的数据目录，环境里只有运行一个程序必需的变量，不带任何凭证与网络的密钥。
网络是 MCP 客户端：列工具、调工具。工具调用的身份、权限、留痕都在 engine，服务看到的只是参数。
只依赖标准库。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROTO = "2025-06-18"
CALL_TIMEOUT = 60.0       # 一次工具调用最多等多久（秒）
START_TIMEOUT = 20.0      # 启动 + initialize 最多等多久
IDLE_STOP = 15 * 60       # 空闲多久停掉进程
START_FAILURES = 3        # 连续启动失败几次后暂停，等主持人重新启用
RESULT_LIMIT = 60_000     # 工具结果最多回给 Agent 多少字符
# 子进程只继承这些变量：找得到程序、能在 Windows 上起进程、文本按 UTF-8。名字带 KEY / TOKEN / SECRET 的一律不给
_KEEP_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL")


class ServiceError(RuntimeError):
    pass


def service_env(data: Path) -> dict[str, str]:
    """插件服务进程的环境：最小集合，HOME 指到它自己的数据目录。"""
    env = {k: v for k, v in os.environ.items() if k in _KEEP_ENV}
    env.update(HOME=str(data), USERPROFILE=str(data), PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               PYTHONDONTWRITEBYTECODE="1")
    return env


def resolve_command(command: str, args: tuple[str, ...], pkg: Path, data: Path) -> list[str]:
    """清单里的 command / args → 命令行。`python` 是网络自己的解释器；`{plugin}` 是装进项目的包目录，
    `{data}` 是数据目录。不经 shell。"""
    def fill(s: str) -> str:
        return s.replace("{plugin}", str(pkg)).replace("{data}", str(data))
    exe = sys.executable if command == "python" else fill(command)
    return [exe, *(fill(a) for a in args)]


@dataclass
class StdioClient:
    """一个插件服务进程：按行收发 JSON-RPC。调用串行（同一时刻只有一个请求在途）。"""

    argv: list[str]
    cwd: Path
    proc: subprocess.Popen | None = None
    _lines: queue.Queue = field(default_factory=queue.Queue)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _seq: int = 0
    last_used: float = field(default_factory=time.time)
    tools: list[dict[str, Any]] = field(default_factory=list)
    stderr_tail: list[str] = field(default_factory=list)

    def start(self) -> None:
        self.cwd.mkdir(parents=True, exist_ok=True)
        try:
            self.proc = subprocess.Popen(self.argv, cwd=self.cwd, env=service_env(self.cwd), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                                         errors="replace", bufsize=1,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            raise ServiceError(f"插件服务起不来：{e}") from e
        threading.Thread(target=self._pump, args=(self.proc.stdout, self._lines), daemon=True).start()
        threading.Thread(target=self._drain_err, daemon=True).start()
        self._request("initialize", {"protocolVersion": PROTO, "capabilities": {},
                                     "clientInfo": {"name": "matrix", "version": "0.2.0"}}, START_TIMEOUT)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.tools = list((self._request("tools/list", {}, START_TIMEOUT) or {}).get("tools") or [])

    @staticmethod
    def _pump(stream: Any, out: queue.Queue) -> None:
        for line in stream:
            out.put(line)
        out.put(None)

    def _drain_err(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for line in self.proc.stderr:  # 只给部署方排错用，不进事件与 API
            self.stderr_tail = (self.stderr_tail + [line.rstrip()])[-20:]

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _send(self, obj: dict[str, Any]) -> None:
        if not self.alive():
            raise ServiceError("插件服务已经退出")
        assert self.proc is not None and self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except OSError as e:
            raise ServiceError(f"插件服务断开了：{e}") from e

    def _request(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        self._seq += 1
        mid = self._seq
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                self.stop()
                raise ServiceError(f"插件服务 {timeout:.0f} 秒没有回应（{method}）")
            try:
                line = self._lines.get(timeout=left)
            except queue.Empty:
                continue
            if line is None:
                raise ServiceError("插件服务已经退出" + (f"：{self.stderr_tail[-1]}" if self.stderr_tail else ""))
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # 服务往 stdout 打了别的东西：跳过
            if not isinstance(msg, dict) or msg.get("id") != mid:
                continue  # 通知或别的回应
            if "error" in msg:
                err = msg["error"] or {}
                raise ServiceError(f"插件服务报错：{err.get('message', err)}")
            return msg.get("result")

    def call(self, name: str, args: dict[str, Any], timeout: float = CALL_TIMEOUT) -> tuple[str, bool]:
        with self._lock:
            self.last_used = time.time()
            res = self._request("tools/call", {"name": name, "arguments": args}, timeout) or {}
        parts = [c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
        text = "\n".join(parts)
        if len(text) > RESULT_LIMIT:
            text = text[:RESULT_LIMIT] + f"\n…（结果太长，只回了前 {RESULT_LIMIT} 个字符）"
        return text, bool(res.get("isError"))

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
            p.terminate()
            p.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            p.kill()


@dataclass
class ServiceHost:
    """所有插件服务进程。key = (项目 id, 插件名)；版本换了（升级）就重启。"""

    _procs: dict[tuple[str, str], tuple[str, StdioClient]] = field(default_factory=dict)
    _failures: dict[tuple[str, str], int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def client(self, project_id: str, plugin: str, version: str, argv: list[str], cwd: Path) -> StdioClient:
        key = (project_id, plugin)
        with self._lock:
            hit = self._procs.get(key)
            if hit is not None and hit[0] == version and hit[1].alive():
                return hit[1]
            if hit is not None:
                hit[1].stop()
                self._procs.pop(key, None)
            if self._failures.get(key, 0) >= START_FAILURES:
                raise ServiceError(f"插件服务连续 {START_FAILURES} 次没起来，已暂停：请主持人停用再启用插件")
            c = StdioClient(argv, cwd)
            try:
                c.start()
            except ServiceError:
                c.stop()
                self._failures[key] = self._failures.get(key, 0) + 1
                raise
            self._failures.pop(key, None)
            self._procs[key] = (version, c)
            return c

    def running(self, project_id: str, plugin: str) -> bool:
        with self._lock:
            hit = self._procs.get((project_id, plugin))
            return hit is not None and hit[1].alive()

    def stop(self, project_id: str, plugin: str) -> None:
        """停用、卸载、升级时调。也清掉失败计数：主持人重新启用就是“再试一次”。"""
        with self._lock:
            hit = self._procs.pop((project_id, plugin), None)
            self._failures.pop((project_id, plugin), None)
        if hit is not None:
            hit[1].stop()

    def reap(self, now: float | None = None) -> int:
        """停掉空闲太久的进程。定时线程每分钟调一次。"""
        now = time.time() if now is None else now
        with self._lock:
            idle = [k for k, (_, c) in self._procs.items() if now - c.last_used > IDLE_STOP or not c.alive()]
            gone = [self._procs.pop(k)[1] for k in idle]
        for c in gone:
            c.stop()
        return len(gone)

    def stop_all(self) -> None:
        with self._lock:
            gone = [c for _, c in self._procs.values()]
            self._procs.clear()
        for c in gone:
            c.stop()
