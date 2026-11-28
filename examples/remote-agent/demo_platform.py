"""演示平台：一个虚构的“公司 AI 平台”，照 Matrix 的标准 HTTP 形状提供云端 Agent（docs/remote-agents.md）。

用来本地验证接入，也是接真实平台时对照的样板：

- `GET  /agents/{agent}`：名称与描述；
- `POST /agents/{agent}/chat`：建会话与发第一条消息是同一次请求（session_id 为空），后续轮次带 session_id 续接；
  返回 SSE，`session` 事件最先；
- `POST /sessions/{id}/interrupt`：会话中断，会话保留；
- `PUT  /agents/{agent}/mcp`：扮演平台的“Agent 级 MCP 配置”页面（真实平台是在网页上填）：{url, headers}；
  之后这个 Agent 每次做事都按这份配置回调 MCP，请求里带 `X-Session-Id`（平台填的，模型改不了）。
- 平台自己的文件（docs/files-across-platforms.md）：`POST /files?name=` 上传（请求体就是内容）→ {file_id}；
  `GET /files/{file_id}/content` 取（要 API Key）。对话请求里的 `files`（Matrix 按 files.in 附上的短期链接或 file_id）都收下。

演示 Agent 的“脑子”是确定性的：
- 消息里有“成员”：经 MCP 调 list_members，把结果说出来；
- 消息里有“委托”：经 MCP 调 delegate（委托给消息里「」括起来的成员）；
- 消息里有“慢慢来”：一直不答，直到被中断；
- 消息里有“抢跑”：先经 MCP 调 list_members、过一会儿才在流里报出会话 id（建会话与第一条消息同一次请求时的时序）；
- 消息里有“发文件”：在平台上写一份演示报告，POST 到 Matrix 的 `<MCP 地址>/files`（同一份请求头 + `X-Session-Id`）拿 file_id，
  再经 MCP 调 send_file(file_id=…) 附在回复上（委托任务里就是随交付交回）；“转发「stg_…」”是拿一个给定的 file_id 调 send_file；
- 消息里有“委托「成员」带文件：<要它做的事>”：先暂存一份报告，再 delegate，attachments 写 file_id，mission 是冒号后面那句；
- 消息里有“报文件”：把报告存进平台自己的文件库（同一个 Agent 一直是同一个 file_id，像平台每轮都报一遍会话里的文件），
  在流里报 `file {ref, name}`（Matrix 按 files.sources 取回）；“报链接「<链接>」”：报一个放在第三方暂存的文件；
- 消息里有“拉文件「来源」”：把报告存进平台自己的文件库，经 MCP 调 fetch_file(source=来源, ref=file_id)，再 send_file；
- 消息里有“导出”：对这一轮列出的第一个文件调 export_file，按返回的短期链接取下来（或拿到平台的 file_id）；
- Matrix 在消息末尾列了这一轮拿得到的文件（`GET /mcp/remote/files/<sha256>`）：平台都取下来，回复里说收到了哪些、多大；
- 只照“对话里的新消息”（委托任务里是目标）动手：提示词里的工具说明、网络通知里引用的任务说明不算（`asked`）；
- 最后一句回复里写明收没收到调用方的授权（`X-User-Token`），只说有没有，不回显内容。

运行：`uv run python examples/remote-agent/demo_platform.py --port 8801`（只听 127.0.0.1）。
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

API_KEY = "demo-platform-key"
AGENTS = {"expense": {"name": "报销助手（演示平台）", "description": "查报销进度、提交报销单"},
          "travel": {"name": "差旅助手（演示平台）", "description": "订票、整理差旅单据"}}

McpPost = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]
FilePost = Callable[[str, dict[str, str], bytes], tuple[int, dict[str, Any]]]
FileGet = Callable[[str, dict[str, str]], tuple[int, bytes]]


def report(agent: str) -> bytes:
    """演示 Agent 在平台上产出的文件（每个 Agent 的内容不同）。"""
    return f"# {AGENTS[agent]['name']}的汇总\n\n- 已处理 3 笔，共 1,280 元\n- 待审批 1 笔\n".encode("utf-8")


def http_mcp_post(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=120) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def http_file_post(url: str, headers: dict[str, str], data: bytes) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def asked(prompt: str) -> str:
    """Matrix 发来的是整份提示词（权限、工具说明、档案……），演示“脑子”只照人或委托方要它做的事动手：
    席位看“对话里的新消息”里 @ 它的那条，委托任务看目标；只有网络通知（交付回报等，里面引用了原来的任务说明）的一轮只收文件。"""
    m = re.search(r"## 对话里的新消息\n(.*?)(?:\n## |\Z)", prompt, re.S)
    if m:  # 群聊里只照 @ 它的那条（别人的消息、别的成员的回复只是上下文）；私聊没有 @，就是全部新消息
        mine = re.findall(r"\*\*用户[^*]*\*\*（@ 你）：(.*)", m.group(1))
        return "\n".join(mine) if mine else ("" if "（@ " in m.group(1) else m.group(1))
    m = re.search(r"- 目标（Mission）：(.*)", prompt)
    if m:
        return m.group(1)
    return "" if "网络通知" in prompt else prompt


def http_file_get(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=120) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class DemoPlatform:
    def __init__(self, mcp_post: McpPost = http_mcp_post, port: int = 0, file_post: FilePost = http_file_post,
                 file_get: FileGet = http_file_get):
        self.mcp_post = mcp_post
        self.file_post = file_post
        self.file_get = file_get
        self.uploads: list[tuple[int, dict[str, Any]]] = []  # 暂存文件的（状态码, 返回）
        self.downloads: list[tuple[str, int, bytes]] = []    # 取文件的（agent, 状态码, 内容）
        self.mcp: dict[str, dict[str, Any]] = {}        # agent → {url, headers}（Agent 级 MCP 配置）
        self.sessions: dict[str, list[str]] = {}        # 会话 → 收到过的消息
        self.interrupted: set[str] = set()
        self.seen_headers: list[dict[str, str]] = []
        self.mcp_results: list[dict[str, Any]] = []
        self.files: dict[str, tuple[str, bytes]] = {}   # 平台自己的文件库：file_id → (文件名, 内容)
        self.received: list[dict[str, Any]] = []        # 对话请求里收到的 files（每项另记取到的内容 data）
        self.received_last: list[dict[str, Any]] = []   # 上一轮收到的（回复之后挪到这里）
        self.file_gets: list[dict[str, str]] = []       # 取平台文件时带的请求头
        self.reported: dict[str, str] = {}              # agent → “报文件”报过的那个 file_id
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # noqa: D401
                pass

            def _json(self, code: int, body: Any) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {API_KEY}":
                    self._json(401, {"error": "bad api key"})
                    return False
                return True

            def _body(self) -> dict[str, Any]:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self) -> None:  # noqa: N802
                f = re.match(r"^/files/([\w-]+)/content$", self.path)
                if f:
                    outer.file_gets.append(dict(self.headers))
                    if not self._authed():
                        return
                    if f.group(1) not in outer.files:
                        self._json(404, {"error": "no such file"})
                        return
                    name, data = outer.files[f.group(1)]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/markdown")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                m = re.match(r"^/agents/([\w-]+)$", self.path)
                if not self._authed():
                    return
                if m and m.group(1) in AGENTS:
                    self._json(200, AGENTS[m.group(1)])
                else:
                    self._json(404, {"error": "no such agent"})

            def do_PUT(self) -> None:  # noqa: N802
                m = re.match(r"^/agents/([\w-]+)/mcp$", self.path)
                if not self._authed() or not m:
                    return
                outer.mcp[m.group(1)] = self._body()
                self._json(200, {"ok": True})

            def do_POST(self) -> None:  # noqa: N802
                if not self._authed():
                    return
                if self.path.startswith("/files?"):
                    name = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("name", ["file"])[0]
                    n = int(self.headers.get("Content-Length") or 0)
                    fid = outer.store(name, self.rfile.read(n))
                    self._json(200, {"file_id": fid})
                    return
                m = re.match(r"^/sessions/([\w-]+)/interrupt$", self.path)
                if m:
                    outer.interrupted.add(m.group(1))
                    self._json(200, {"ok": True})
                    return
                m = re.match(r"^/agents/([\w-]+)/chat$", self.path)
                if not m or m.group(1) not in AGENTS:
                    self._json(404, {"error": "no such agent"})
                    return
                outer.seen_headers.append(dict(self.headers))
                body = self._body()
                sid = body.get("session_id") or f"s-{uuid.uuid4().hex[:10]}"
                outer.sessions.setdefault(sid, []).append(str(body.get("message", "")))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                outer.take_files(body.get("files") or [])
                outer.chat(m.group(1), sid, str(body.get("message", "")), dict(self.headers), self._send)

            def _send(self, event: str, data: dict[str, Any]) -> bool:
                try:
                    self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    return True
                except OSError:
                    return False

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()

    def store(self, name: str, data: bytes) -> str:
        fid = f"pf-{uuid.uuid4().hex[:10]}"
        self.files[fid] = (name, data)
        return fid

    def take_files(self, files: list[dict[str, Any]]) -> None:
        """Matrix 随对话请求附上的文件：短期链接直接取（不带任何请求头），file_id 是先传进平台文件库的。"""
        for f in files:
            if f.get("url"):
                code, data = self.file_get(str(f["url"]), {})
            else:
                code, data = (200, self.files[f["file_id"]][1]) if f.get("file_id") in self.files else (404, b"")
            self.received.append({**f, "code": code, "data": data})

    # ---- 演示 Agent 的“脑子” ----
    def _call(self, agent: str, sid: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg = self.mcp.get(agent)
        if not cfg:
            return {"error": {"message": "没有配置 MCP"}}
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex[:6], "method": method, "params": params or {}}
        out = self.mcp_post(cfg["url"], {**cfg.get("headers", {}), "X-Session-Id": sid}, body)
        self.mcp_results.append({"method": method, "params": params, "out": out})
        return out

    def _stage(self, agent: str, sid: str) -> tuple[int, dict[str, Any]]:
        """平台把这个 Agent 产出的文件暂存到 Matrix：同一份 MCP 请求头 + 会话 id，请求体就是文件内容。"""
        cfg = self.mcp[agent]
        url = cfg["url"] + "/files?" + urllib.parse.urlencode({"name": f"{AGENTS[agent]['name'][:4]}汇总.md"})
        code, out = self.file_post(url, {**cfg.get("headers", {}), "X-Session-Id": sid,
                                         "Content-Type": "text/markdown"}, report(agent))
        self.uploads.append((code, out))
        return code, out

    def _fetch_listed(self, agent: str, sid: str, message: str) -> list[str]:
        """Matrix 在消息末尾列了这一轮拿得到的文件：平台都取下来（同一份请求头 + 会话 id）。"""
        cfg = self.mcp[agent]
        base = cfg["url"].removesuffix("/mcp/remote")
        got = []
        for name, path in re.findall(r"  - (.+?)（[^\n]*?`GET (/mcp/remote/files/[0-9a-f]{64})`", message):
            code, data = self.file_get(base + path, {**cfg.get("headers", {}), "X-Session-Id": sid})
            self.downloads.append((agent, code, data))
            got.append(f"{name}（{len(data)} 字节）" if code == 200 else f"{name}（取不到：{code}）")
        return got

    def _tool(self, agent: str, sid: str, name: str, args: dict[str, Any]) -> str:
        out = self._call(agent, sid, "tools/call", {"name": name, "arguments": args})
        if "error" in out:
            return f"（MCP 拒绝：{out['error'].get('message')}）"
        return "".join(c.get("text", "") for c in out.get("result", {}).get("content", []))

    def chat(self, agent: str, sid: str, message: str, headers: dict[str, str],
             send: Callable[[str, dict[str, Any]], bool]) -> None:
        listing, message = message, asked(message)
        early: list[str] = []
        if "抢跑" in message and agent in self.mcp:
            th = threading.Thread(target=lambda: early.append(self._tool(agent, sid, "list_members", {})))
            th.start()
            time.sleep(0.3)  # 会话 id 晚一点才出现在流里
            send("session", {"session_id": sid})
            th.join(10)
        else:
            send("session", {"session_id": sid})
        if "慢慢来" in message:
            while sid not in self.interrupted:
                if not send("delta", {"text": "."}):
                    return
                time.sleep(0.05)
            return
        parts = []
        if early:
            parts.append(f"抢跑查到：{early[0]}")
        if agent in self.mcp:
            self._call(agent, sid, "initialize")
            if "成员" in message:
                send("tool", {"name": "list_members", "input": {}})
                parts.append("成员：" + self._tool(agent, sid, "list_members", {}))
            got = self._fetch_listed(agent, sid, listing)
            if got:
                parts.append("收到文件：" + "、".join(got))
            m = re.search(r"委托.*?「(.+?)」(带文件)?(?:：(.+))?", message)
            if m:
                args = {"agent": m.group(1), "mission": (m.group(3) or "演示委托").strip(), "expected_outcome": "一句话结论"}
                if m.group(2):  # 先暂存，再把 file_id 写进 attachments 转交
                    code, out = self._stage(agent, sid)
                    if code == 200:
                        args["attachments"] = out["file_id"]
                send("tool", {"name": "delegate", "input": {"agent": m.group(1)}})
                parts.append(self._tool(agent, sid, "delegate", args))
            if "发文件" in message:  # 平台上产出的文件：先暂存到 Matrix 拿 file_id，再 send_file
                code, out = self._stage(agent, sid)
                if code == 200:
                    send("tool", {"name": "send_file", "input": {"file_id": out["file_id"]}})
                    parts.append(self._tool(agent, sid, "send_file", {"file_id": out["file_id"]}))
                else:
                    parts.append(f"暂存失败（{code}）：{out.get('detail')}")
            m = re.search(r"转发「(stg_[\w-]+)", message)  # 后面可能跟着 Matrix 的标注（已拿到的文件 id）
            if m:
                parts.append(self._tool(agent, sid, "send_file", {"file_id": m.group(1)}))
            m = re.search(r"拉文件「([\w-]+)」", message)
            if m:  # 文件在平台自己的文件库里：让 Matrix 按登记的来源拉过去，再发
                fid = self.store(f"{AGENTS[agent]['name'][:4]}底稿.md", report(agent))
                out = self._tool(agent, sid, "fetch_file", {"source": m.group(1), "ref": fid})
                parts.append(out)
                staged = re.search(r"`(stg_[\w-]+)`", out)
                if staged:
                    parts.append(self._tool(agent, sid, "send_file", {"file_id": staged.group(1)}))
            if "导出" in message:
                sha = re.search(r"`GET /mcp/remote/files/([0-9a-f]{64})`", listing)
                out = self._tool(agent, sid, "export_file", {"file": sha.group(1) if sha else ""})
                link = re.search(r"(https?://\S+/mcp/remote/links/[\w-]+)", out)
                if link:
                    code, data = self.file_get(link.group(1), {})
                    self.downloads.append((agent, code, data))
                    out += f"（取到 {len(data)} 字节）" if code == 200 else f"（取不到：{code}）"
                parts.append(out)
        if "报文件" in message:  # 平台产出的文件：放进平台自己的文件库，在流里报出来，Matrix 取回附在回复上
            name = f"{AGENTS[agent]['name'][:4]}报表.md"
            fid = self.reported.get(agent) or self.store(name, report(agent))
            self.reported[agent] = fid
            send("file", {"ref": fid, "name": name})
            parts.append(f"报表的文件 id 是 {fid}")
        for link in re.findall(r"报链接「(\S+?)」", message):
            send("file", {"ref": link})
        if self.received:
            parts.append("随消息收到：" + "、".join(f"{f['name']}（{len(f['data'])} 字节）" for f in self.received
                                                if f.get("code") == 200))
            self.received_last, self.received = self.received, []
        token = next((v for k, v in headers.items() if k.lower() == "x-user-token"), "")
        parts.append("收到了调用方的授权" if token else "没有调用方的授权")
        send("delta", {"text": "处理中"})
        send("done", {"text": "；".join(parts)})


def main() -> None:
    p = argparse.ArgumentParser(description="演示平台（虚构的公司 AI 平台）")
    p.add_argument("--port", type=int, default=8801)
    args = p.parse_args()
    d = DemoPlatform(port=args.port)
    print(f"演示平台 → {d.url}  API Key：{API_KEY}  Agent：{', '.join(AGENTS)}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        d.close()


if __name__ == "__main__":
    main()
