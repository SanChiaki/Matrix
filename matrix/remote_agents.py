"""外部平台 Agent（docs/remote-agents.md，不变量 28）：公司内部别的平台上建的云端 Agent，以 HTTP 会话接进 Matrix 当成员。

部署方在平台目录里登记平台（默认 `<data>/agent-platforms.json`，`--platforms` / `MATRIX_PLATFORMS`，改了就生效）::

    {"platforms": [
      {"id": "corp-ai", "name": "公司 AI 平台", "url": "http://ai.corp.example/api", "driver": "standard",
       "auth": {"header": "Authorization", "template": "Bearer {value}", "label": "平台 API Key"},
       "mcp_session_header": "X-Session-Id",
       "caller": {"header": "X-User-Token", "connector": "erp", "field": "token", "template": "{value}"},
       "bind_wait_s": 30, "timeout_s": 900, "proxy": "direct"}
    ]}

- `url` 只来自这里（部署方负责，可以是内网、可以用 http）；用户与 Agent 都改不了（网络会被当跳板访问内网）。
- `driver`：怎么和这个平台说话。内置 `standard`（下面的“标准 HTTP 形状”）；别的经 entry point `matrix.agent_platforms` 装上，
  照 examples/remote-agent 写。
- `auth`：发起会话用的平台凭证，添加 Agent 的人填自己的 API Key，只进保管库。
- `mcp_session_header`：平台回调 Matrix 的 MCP（Agent 级配置）时，请求里带的平台会话 id 在哪个请求头里。
- `caller`（可省略）：调用方的授权怎么透传：取这一轮调用方自己在 Matrix 里授权的那个目录连接器的某个字段，放进这个请求头。
  调用方没授权时这一轮不发起会话。从不放维护人 / 添加的人的。
- `bind_wait_s`：建会话与第一条消息是同一次请求时，MCP 调用可能比流里的会话 id 先到：带着陌生会话 id 的调用最多等这么久。
- `files`（可省略，docs/files-across-platforms.md）：这个平台怎么收发文件——
  `in`：这一轮拿得到的文件怎么交给平台。`pull`（默认）在消息末尾列出，平台带 MCP 密钥来取；
  `url` 随对话请求附上 Matrix 签的短期链接（这一轮有效，平台不用带请求头）；`upload` 先传到平台自己的文件接口，附上平台的 file_id。
  `sources`：平台在流里报 `file` 事件时，ref 按 file-sources.json 里的哪些来源取（按 ref 正则选，恰好匹配一个才取；
  写一个字符串 `source` 也认）。`repeat`：平台又报了这个对话里它已经发过的文件时，`skip`（默认，不再附）或 `attach`。
  `upload_ttl_s`：`upload` 传过的平台 file_id 用多久（默认 0 = 一直用）。`resend`：`all`（默认，每轮交这一轮拿得到的全部）
  或 `new`（同一个平台会话里只交上一轮之后新出现的）。
  `export`：export_file 工具把 Matrix 的文件交给平台的方式（`url` / `upload`；不写时有 matrix_url 就是 `url`）。
  `matrix_url`：平台访问 Matrix 用的地址（签短期链接用），如 https://matrix.corp.example。

标准 HTTP 形状（平台能改的话照这个提供，就不用写驱动）::

    GET  {url}/agents/{agent}                 -> {"name": ..., "description": ...}
    POST {url}/agents/{agent}/chat            {"message": ..., "session_id": null | "..."}
         {"message": ..., "session_id": ..., "files": [...]}  files 只在 files.in 是 url / upload 时有：
            每项 {name, mime, size, sha256, url | file_id}
         -> text/event-stream：event: session {"session_id"} 必须最先；delta {"text"}；tool {"name", "input"}；
            done {"text"}（最终回复）；error {"message"}；可选 file {"ref", "name"?}（平台产出的文件：Matrix 按 files.sources
            里 ref 格式对得上的那个来源取回、附在这一轮的回复上）；可选 progress {"kind", ...}（被委托方的进展与心跳；
            作为调用方，这里不认识的事件一律跳过）
    POST {url}/sessions/{session_id}/interrupt
    POST {url}/files?name=<文件名>           请求体就是文件内容 -> {"file_id": ...}   （只在 files.in / export 是 upload 时用）
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol
from urllib.parse import quote

from .models import AUTHORITIES
from .runtime import (ADVISORY, ENFORCED, STOPPED_ERROR, RunRequest, RunResult, RuntimeCapabilities, ToolBridge)

PLATFORM_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
HEADER = re.compile(r"^[A-Za-z0-9-]{1,64}$")
ENTRY_POINT = "matrix.agent_platforms"
# 远程 Agent 经网络工具做到的事由网络强制；它在自己平台上做什么（执行代码、联网、它自己的连接器）Matrix 管不到
NETWORK_ENFORCED = ("delegate",)


FILES_IN = ("pull", "url", "upload")


class PlatformError(RuntimeError):
    pass


@dataclass(frozen=True)
class CallerHeader:
    header: str
    connector: str
    field: str = "token"
    template: str = "{value}"


@dataclass(frozen=True)
class PlatformSpec:
    id: str
    name: str
    url: str
    driver: str = "standard"
    auth_header: str = "Authorization"
    auth_template: str = "Bearer {value}"
    auth_label: str = "平台 API Key"
    mcp_session_header: str = "X-Session-Id"
    caller: CallerHeader | None = None
    bind_wait_s: float = 30
    timeout_s: float = 900
    proxy: str = "direct"
    files_in: str = "pull"             # pull | url | upload
    files_sources: tuple[str, ...] = ()  # 流里 file 事件的 ref 按哪些文件来源取（按 ref 正则选）
    files_repeat: str = "skip"          # 平台又报了已经发过的文件：skip | attach
    files_upload_ttl_s: float = 0       # upload 传过的平台 file_id 用多久；0 = 一直用
    files_resend: str = "all"           # all | new（同一个平台会话里只交新出现的）
    files_export: str | None = None     # export_file 的方式：url | upload；None = 没有这个工具
    matrix_url: str = ""                # 平台访问 Matrix 的地址（签短期链接用）

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "auth_label": self.auth_label,
                "caller_connector": self.caller.connector if self.caller else None}


def parse_platforms(raw: Any) -> list[PlatformSpec]:
    if not isinstance(raw, dict) or not isinstance(raw.get("platforms", []), list):
        raise PlatformError("平台目录要是 {\"platforms\": [...]}")
    out: list[PlatformSpec] = []
    for p in raw.get("platforms", []):
        if not isinstance(p, dict):
            raise PlatformError("每个平台要是一个对象")
        pid, url = str(p.get("id", "")), str(p.get("url", "")).rstrip("/")
        if not PLATFORM_ID.match(pid):
            raise PlatformError(f"平台 id 不合法：{pid!r}")
        if any(x.id == pid for x in out):
            raise PlatformError(f"平台 id 重复：{pid}")
        if not re.match(r"^https?://", url):
            raise PlatformError(f"{pid} 的 url 要是 http(s) 地址")
        auth = p.get("auth") or {}
        caller = p.get("caller")
        headers = [str(auth.get("header", "Authorization")), str(p.get("mcp_session_header", "X-Session-Id"))]
        cal = None
        if caller:
            cal = CallerHeader(header=str(caller.get("header", "")), connector=str(caller.get("connector", "")),
                               field=str(caller.get("field", "token")), template=str(caller.get("template", "{value}")))
            headers.append(cal.header)
            if not cal.connector:
                raise PlatformError(f"{pid} 的 caller 要写 connector（调用方授权的哪个目录连接器）")
        bad = [h for h in headers if not HEADER.match(h)]
        if bad:
            raise PlatformError(f"{pid} 的请求头名不合法：{bad}")
        proxy = str(p.get("proxy", "direct"))
        if proxy not in ("direct", "env"):
            raise PlatformError(f"{pid} 的 proxy 只能是 direct 或 env")
        files = p.get("files") or {}
        if not isinstance(files, dict):
            raise PlatformError(f"{pid} 的 files 要是一个对象")
        f_in, matrix_url = str(files.get("in") or "pull"), str(files.get("matrix_url") or "").rstrip("/")
        if f_in not in FILES_IN:
            raise PlatformError(f"{pid} 的 files.in 只能是 {' / '.join(FILES_IN)}")
        if matrix_url and not re.match(r"^https?://[^/\s]+(/\S*)?$", matrix_url):
            raise PlatformError(f"{pid} 的 files.matrix_url 要是 http(s) 地址")
        f_export = files.get("export", "url" if matrix_url else None)
        f_export = None if f_export in (None, "", "none") else str(f_export)
        if f_export not in (None, "url", "upload"):
            raise PlatformError(f"{pid} 的 files.export 只能是 url / upload / none")
        if "url" in (f_in, f_export) and not matrix_url:
            raise PlatformError(f"{pid} 用短期链接交文件（files.in / export 是 url）：要写 files.matrix_url")
        f_sources = files.get("sources", [files["source"]] if files.get("source") else [])
        if isinstance(f_sources, str):
            f_sources = [f_sources]
        if not isinstance(f_sources, list) or not all(isinstance(x, str) and x for x in f_sources):
            raise PlatformError(f"{pid} 的 files.sources 要是来源 id 的列表")
        f_repeat, f_resend = str(files.get("repeat") or "skip"), str(files.get("resend") or "all")
        if f_repeat not in ("skip", "attach"):
            raise PlatformError(f"{pid} 的 files.repeat 只能是 skip / attach")
        if f_resend not in ("all", "new"):
            raise PlatformError(f"{pid} 的 files.resend 只能是 all / new")
        try:
            f_ttl = float(files.get("upload_ttl_s", 0) or 0)
        except (TypeError, ValueError) as e:
            raise PlatformError(f"{pid} 的 files.upload_ttl_s 要是秒数") from e
        out.append(PlatformSpec(
            id=pid, name=str(p.get("name") or pid), url=url, driver=str(p.get("driver") or "standard"),
            auth_header=headers[0], auth_template=str(auth.get("template", "Bearer {value}")),
            auth_label=str(auth.get("label", "平台 API Key")), mcp_session_header=headers[1], caller=cal,
            bind_wait_s=float(p.get("bind_wait_s", 30)), timeout_s=float(p.get("timeout_s", 900)), proxy=proxy,
            files_in=f_in, files_sources=tuple(dict.fromkeys(f_sources)), files_repeat=f_repeat,
            files_upload_ttl_s=max(0.0, f_ttl), files_resend=f_resend, files_export=f_export, matrix_url=matrix_url))
    return out


class PlatformCatalog:
    """部署方的平台目录：按修改时间热加载；写坏了保留上一份能用的。"""

    def __init__(self, path: Path | None):
        self.path = path
        self._mtime: float | None = None
        self._items: list[PlatformSpec] = []
        self._lock = threading.Lock()

    def items(self) -> list[PlatformSpec]:
        with self._lock:
            if self.path is None or not self.path.is_file():
                self._items, self._mtime = [], None
                return []
            m = self.path.stat().st_mtime
            if m != self._mtime:
                try:
                    self._items = parse_platforms(json.loads(self.path.read_text(encoding="utf-8")))
                except (ValueError, PlatformError) as e:
                    print(f"[matrix] 平台目录 {self.path} 读不了，沿用上一份：{e}", file=sys.stderr)
                self._mtime = m
            return list(self._items)

    def get(self, pid: str | None) -> PlatformSpec | None:
        return next((p for p in self.items() if p.id == pid), None)


# ================= 驱动 =================
class Driver(Protocol):
    """和一个平台说话的方式。events() 产出 {"type": "session" | "delta" | "tool" | "file" | "done" | "error", ...}；
    session 必须最先（拿到会话 id 网络才能把 MCP 回调落到这一轮）。

    文件（可选，docs/files-across-platforms.md）：驱动在 `files_in` 里声明支持哪些交文件的方式（"url"、"upload"），
    声明了才会收到 `chat(..., files=[...])`；支持 "upload" 的还要有 `upload()`。没声明的照旧只收文本，网络按 pull 交文件。
    平台产出的文件用 {"type": "file", "ref": ..., "name": ...} 报出来，网络按平台目录的 files.source 取回。"""

    files_in: tuple[str, ...]

    def describe(self, spec: PlatformSpec, agent: str, headers: dict[str, str]) -> dict[str, str]: ...

    def chat(self, spec: PlatformSpec, agent: str, message: str, session_id: str | None,
             headers: dict[str, str], files: list[dict[str, Any]] | None = None) -> Iterator[dict[str, Any]]: ...

    def interrupt(self, spec: PlatformSpec, session_id: str, headers: dict[str, str]) -> None: ...

    def upload(self, spec: PlatformSpec, name: str, data: bytes, mime: str, headers: dict[str, str]) -> str: ...


def files_supported(driver: Any, mode: str) -> bool:
    """驱动支不支持这种交文件的方式（pull 谁都支持：只是提示词里的一节）。"""
    if mode == "pull":
        return True
    ok = mode in tuple(getattr(driver, "files_in", ()) or ())
    return ok and (mode != "upload" or callable(getattr(driver, "upload", None)))


def _opener(spec: PlatformSpec) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener() if spec.proxy == "env" else \
        urllib.request.build_opener(urllib.request.ProxyHandler({}))


class StandardDriver:
    """标准 HTTP 形状（见文件开头）。"""

    files_in = ("url", "upload")

    def _req(self, spec: PlatformSpec, path: str, headers: dict[str, str], body: Any = None,
             timeout: float = 30) -> Any:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(spec.url + path, data=data, method="POST" if data is not None else "GET",
                                     headers={"Content-Type": "application/json", "Accept": "text/event-stream, application/json",
                                              **headers})
        try:
            return _opener(spec).open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise PlatformError(f"平台返回 {e.code}：{detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise PlatformError(f"连不上平台：{e}") from e

    def describe(self, spec: PlatformSpec, agent: str, headers: dict[str, str]) -> dict[str, str]:
        with self._req(spec, f"/agents/{quote(agent, safe='')}", headers) as r:
            d = json.loads(r.read().decode("utf-8"))
        return {"name": str(d.get("name") or agent), "description": str(d.get("description") or "")}

    def chat(self, spec: PlatformSpec, agent: str, message: str, session_id: str | None,
             headers: dict[str, str], files: list[dict[str, Any]] | None = None) -> Iterator[dict[str, Any]]:
        body: dict[str, Any] = {"message": message, "session_id": session_id}
        if files:
            body["files"] = files
        r = self._req(spec, f"/agents/{quote(agent, safe='')}/chat", headers, body, timeout=spec.timeout_s)
        with r:
            event, data = "message", []
            for raw in r:
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
                elif not line and data:
                    try:
                        body = json.loads("\n".join(data))
                    except ValueError:
                        body = {}
                    data = []
                    yield _standard_event(event, body)
                    event = "message"

    def interrupt(self, spec: PlatformSpec, session_id: str, headers: dict[str, str]) -> None:
        with self._req(spec, f"/sessions/{quote(session_id, safe='')}/interrupt", headers, {}):
            pass

    def upload(self, spec: PlatformSpec, name: str, data: bytes, mime: str, headers: dict[str, str]) -> str:
        req = urllib.request.Request(f"{spec.url}/files?name={quote(name, safe='')}", data=data, method="POST",
                                     headers={**headers, "Content-Type": mime or "application/octet-stream"})
        try:
            with _opener(spec).open(req, timeout=120) as r:
                fid = str(json.loads(r.read().decode("utf-8") or "{}").get("file_id") or "")
        except urllib.error.HTTPError as e:
            raise PlatformError(f"平台的文件接口返回 {e.code}：{e.read().decode('utf-8', 'replace')[:300]}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise PlatformError(f"传不到平台的文件接口：{e}") from e
        if not fid:
            raise PlatformError("平台的文件接口没有返回 file_id")
        return fid


def _standard_event(event: str, body: dict[str, Any]) -> dict[str, Any]:
    if event == "session":
        return {"type": "session", "id": str(body.get("session_id", ""))}
    if event == "delta":
        return {"type": "delta", "text": str(body.get("text", ""))}
    if event == "tool":
        return {"type": "tool", "name": str(body.get("name", "")), "input": body.get("input") or {}}
    if event == "done":
        return {"type": "done", "text": str(body.get("text", ""))}
    if event == "error":
        return {"type": "error", "message": str(body.get("message", "平台报错"))}
    if event == "file":
        return {"type": "file", "ref": str(body.get("ref") or body.get("url") or body.get("file_id") or ""),
                "name": str(body.get("name") or "")}
    return {"type": "other"}


BUILTIN: dict[str, Callable[[], Driver]] = {"standard": StandardDriver}


def load_driver(name: str) -> Driver:
    if name in BUILTIN:
        return BUILTIN[name]()
    for ep in entry_points(group=ENTRY_POINT):
        if ep.name == name:
            return ep.load()()
    raise PlatformError(f"没有平台驱动 {name}（内置 standard；别的经 entry point {ENTRY_POINT} 装上）")


# ================= 会话绑定（MCP 回调落到哪一轮） =================
@dataclass
class Binding:
    agent_id: str
    tools: ToolBridge | None
    bound_at: float = field(default_factory=time.time)


class Bindings:
    """（Matrix Agent, 平台会话 id）→ 这一轮的网络工具。流里报出会话 id 时绑定，这一轮结束解除；
    带着还没绑定的会话 id 的回调最多等 bind_wait_s，等不到就拒绝（在平台上直接聊天的会话永远等不到）。"""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._map: dict[tuple[str, str], Binding] = {}

    def bind(self, agent_id: str, session_id: str, tools: ToolBridge | None) -> None:
        with self._cv:
            self._map[(agent_id, session_id)] = Binding(agent_id, tools)
            self._cv.notify_all()

    def unbind(self, agent_id: str, session_id: str) -> None:
        with self._cv:
            self._map.pop((agent_id, session_id), None)

    def wait(self, agent_id: str, session_id: str, timeout: float) -> Binding | None:
        deadline = time.time() + max(0.0, timeout)
        with self._cv:
            while (agent_id, session_id) not in self._map:
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._cv.wait(left)
            return self._map[(agent_id, session_id)]


# ================= 运行时 =================
@dataclass
class RemoteCall:
    """网络在这一轮交给远程运行时的东西：平台、平台上的 Agent、发起会话要带的请求头（含凭证，不打印）。"""

    platform: PlatformSpec
    agent: str
    headers: dict[str, str] = field(repr=False)
    error: str | None = None   # 网络在发起之前就判定这一轮不能跑（例如调用方还没授权），直接按失败返回
    # 随对话请求交给平台的文件（files.in 是 url / upload 时；pull 时为空）：每项 {name, mime, size, sha256, url | file_id}
    files: list[dict[str, Any]] = field(default_factory=list, repr=False)
    # 平台在流里报出文件时：网络按 files.source 取回、附在这一轮的回复上，返回（给这一轮记下的说明, 是否失败）
    on_file: Callable[[str, str], tuple[str, bool]] | None = field(default=None, repr=False)


def capabilities(platform: str, mcp: bool) -> RuntimeCapabilities:
    """接了 Matrix 的 MCP（完整接入）时委托等网络工具由网络强制；其余权限 Matrix 管不到，一律按约定收窄。"""
    return RuntimeCapabilities(
        name=f"remote:{platform}", placement="remote",
        enforcement={a: ENFORCED if mcp and a in NETWORK_ENFORCED else ADVISORY for a in AUTHORITIES},
        session_resume=True, approval="none", sandbox="none", skills="prompt", questions=False, goals=False)


class RemoteRuntime:
    """把一轮交给外部平台：续接或新建平台会话、读流、把过程转成网络的事件、到点或被终止时调中断接口。"""

    name = "remote"

    def __init__(self, bindings: Bindings, driver: Callable[[str], Driver] = load_driver):
        self.bindings = bindings
        self._driver = driver

    def capabilities(self) -> RuntimeCapabilities:
        return capabilities("?", False)

    def run(self, req: RunRequest) -> RunResult:
        call: RemoteCall | None = getattr(req, "remote", None)
        if call is None:
            return RunResult(req.session_id or "", "", ok=False, error="runtime error: 没有远程平台的配置")
        if call.error:
            return RunResult(req.session_id or "", "", ok=False, error=call.error)
        try:
            driver = self._driver(call.platform.driver)
        except PlatformError as e:
            return RunResult(req.session_id or "", "", ok=False, error=f"TRANSPORT {e}")
        state: dict[str, Any] = {"sid": req.session_id, "text": [], "final": None, "error": None, "tools": []}
        done = threading.Event()

        def emit(ev: dict[str, Any]) -> None:
            if req.on_event:
                req.on_event(ev)

        def pump() -> None:
            try:
                kw = {"files": call.files} if call.files else {}
                for ev in driver.chat(call.platform, call.agent, req.prompt, req.session_id, call.headers, **kw):
                    t = ev.get("type")
                    if t == "session" and ev.get("id"):
                        if state["sid"] and state["sid"] != ev["id"]:
                            self.bindings.unbind(req.agent_id, state["sid"])
                        state["sid"] = ev["id"]
                        self.bindings.bind(req.agent_id, ev["id"], req.tools)
                        emit({"type": "session", "sessionId": ev["id"]})
                    elif t == "delta":
                        state["text"].append(ev.get("text", ""))
                        emit({"type": "text", "text": ev.get("text", "")})
                    elif t == "tool":
                        cid = f"remote-{len(state['tools'])}"
                        state["tools"].append({"tool": ev.get("name", ""), "input": ev.get("input") or {},
                                               "status": "completed"})
                        emit({"type": "tool_call", "callId": cid, "tool": ev.get("name", ""), "input": ev.get("input") or {}})
                        emit({"type": "tool_result", "callId": cid, "status": "completed", "result": ""})
                    elif t == "file":
                        cid = f"remote-{len(state['tools'])}"
                        args = {"ref": ev.get("ref", ""), "name": ev.get("name", "")}
                        emit({"type": "tool_call", "callId": cid, "tool": "file", "input": args})
                        text, bad = (call.on_file(args["ref"], args["name"]) if call.on_file is not None
                                     else ("这个平台没有配置 files.sources：平台报的文件取不回来", True))
                        status = "failed" if bad else "completed"
                        state["tools"].append({"tool": "file", "input": args, "status": status, "result": text})
                        emit({"type": "tool_result", "callId": cid, "status": status, "result": text})
                    elif t == "done":
                        state["final"] = ev.get("text", "")
                    elif t == "error":
                        state["error"] = ev.get("message") or "平台报错"
                    if done.is_set():
                        return
            except PlatformError as e:
                state["error"] = f"TRANSPORT {e}"
            except Exception as e:  # noqa: BLE001 —— 驱动里的意外一律当成这一轮失败，不让线程悄悄死掉
                state["error"] = f"runtime error: {e!r}"
            finally:
                done.set()

        th = threading.Thread(target=pump, daemon=True, name="matrix-remote")
        th.start()
        limit = req.timeout_s or call.platform.timeout_s
        deadline = time.time() + limit
        stopped = timed_out = False
        while not done.wait(0.05):
            if req.cancel is not None and req.cancel.is_set():
                stopped = True
                break
            if time.time() > deadline:
                timed_out = True
                break
        sid = state["sid"] or ""
        if stopped or timed_out:
            done.set()
            if sid:
                try:
                    driver.interrupt(call.platform, sid, call.headers)
                except PlatformError:
                    pass
        if sid:  # 这一轮结束：晚到的回调一律拒绝（不让停了以后还在改项目档案）
            self.bindings.unbind(req.agent_id, sid)
        if stopped:
            return RunResult(sid, "", ok=False, error=STOPPED_ERROR, tool_calls=state["tools"])
        if timed_out:
            return RunResult(sid, "", ok=False, error=f"TIMEOUT 平台 {int(limit)} 秒内没有答完", tool_calls=state["tools"])
        if state["error"]:
            err = state["error"] if re.match(r"^[A-Z_]+ |^runtime error", state["error"]) else f"SERVER {state['error']}"
            return RunResult(sid, "", ok=False, error=err, tool_calls=state["tools"])
        final = state["final"] if state["final"] is not None else "".join(state["text"])
        return RunResult(sid, final, ok=True, tool_calls=state["tools"])
