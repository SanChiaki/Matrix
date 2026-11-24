"""模拟的业务系统「报销系统」：演示业务系统对接 Matrix 能用的全部能力（docs/open-api-integration.md）。只用 Python 标准库。

    uv run python examples/business-system/biz_system.py --config .matrix-biz-demo/biz-system.json

打开 http://127.0.0.1:8800 ：

- 「报销单」：员工提交报销单，报销系统调用 Matrix 上的服务审核，流式显示过程（入口说的话、调的工具、交给了谁、
  被委托方在做什么、心跳），能追问（续接同一个服务会话）、能中断。
- 「对接能力」：把 /open/v1 与 /svc 的每个接口做成一步，点一下就真的调一次，下面列出请求与响应。
  顺序就是一个业务系统接入的过程：查 → 组 → 发 → 调 → 看 → 错误处理 → 清理。

令牌只在这个服务端（读 --config 里的文件），浏览器拿不到。真实系统里放进密钥管理，不要写进代码仓库与前端。
"""

from __future__ import annotations

import argparse
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MATERIALS = ROOT / "materials"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}

PROJECT = "报销审核"
TEAM = ("财务助手", "票据核验", "合同审核", "预算管家", "法务顾问")   # 项目成员：多 Agent 协同
ROLES = (("intake", "接待", "财务助手"), ("invoice", "票据", "票据核验"), ("contract", "合同", "合同审核"),
         ("budget", "预算", "预算管家"), ("legal", "法务", "法务顾问"))
SERVICE = "baoxiao"
CLAIMS = [  # 报销系统自己的数据（演示）
    {"id": "BX-1001", "who": "李雷", "title": "打车", "amount": 280,
     "message": "请审核报销单 BX-1001：李雷，打车 280 元，事由：客户拜访，票据齐全。"},
    {"id": "BX-1002", "who": "韩梅梅", "title": "技术咨询费（附合同，多 Agent 协同）", "amount": 12000,
     "message": "请审核报销单 BX-1002：韩梅梅，技术咨询费 12,000 元，附合同《技术咨询服务合同》，发票一张。"},
    {"id": "BX-1003", "who": "王芳", "title": "批量报销 8 张发票", "amount": 3560,
     "message": "请批量核对报销单 BX-1003：王芳，8 张发票，合计 3,560 元。"},
]


class MatrixError(Exception):
    def __init__(self, status: int, body: Any):
        super().__init__(f"HTTP {status}")
        self.status, self.body = status, body


class Matrix:
    """Matrix 的接口：每次请求带 `Authorization: Bearer mat_…`。"""

    def __init__(self, base: str, token: str):
        self.base, self.token = base.rstrip("/"), token

    def _req(self, method: str, path: str, body: Any = None, raw: bytes | None = None,
             token: str | None = None) -> urllib.request.Request:
        data = raw if raw is not None else (json.dumps(body, ensure_ascii=False).encode() if body is not None else None)
        headers = {"Authorization": f"Bearer {self.token if token is None else token}"}
        if raw is None and body is not None:
            headers["Content-Type"] = "application/json"
        return urllib.request.Request(self.base + urllib.parse.quote(path, safe="/?=&"), data=data, method=method,
                                      headers=headers)

    def call(self, method: str, path: str, body: Any = None, raw: bytes | None = None,
             token: str | None = None) -> tuple[int, Any]:
        try:
            with urllib.request.urlopen(self._req(method, path, body, raw, token), timeout=60) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except ValueError:
                return e.code, {"detail": e.reason}

    def stream(self, path: str, body: Any) -> Iterator[bytes]:
        """/svc/…/chat：原样读出 SSE 的每一行（服务等到这一串做完，最多 15 分钟：读超时要更长）。"""
        try:
            with urllib.request.urlopen(self._req("POST", path, body), timeout=960) as r:
                for line in r:
                    yield line
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            yield f"event: error\ndata: {json.dumps({'message': f'HTTP {e.code} {detail}'}, ensure_ascii=False)}\n\n".encode()


def parse_sse(lines: Iterator[bytes]) -> Iterator[tuple[str, dict[str, Any]]]:
    event, data = "message", []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif not line and data:
            yield event, json.loads("\n".join(data))
            event, data = "message", []


class BizSystem:
    """报销系统接 Matrix 的那一层：每一步调哪个接口、记下拿到的 id。状态存在 config 旁边的 biz-state.json。"""

    def __init__(self, config: Path):
        self.cfg = json.loads(config.read_text(encoding="utf-8"))
        self.matrix = Matrix(self.cfg["matrix_url"], self.cfg["token"])
        self.state_file = config.with_name("biz-state.json")
        self.state: dict[str, Any] = {}
        if self.state_file.exists():
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.lock = threading.Lock()
        self.steps: list[dict[str, Any]] = []
        self._define_steps()

    def save(self) -> None:
        self.state_file.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 一步 = 一次或几次真实的接口调用 ----
    def step(self, sid: str, group: str, title: str, desc: str, api: str):
        def deco(fn: Callable[[list[dict[str, Any]]], None]):
            self.steps.append({"id": sid, "group": group, "title": title, "desc": desc, "api": api, "fn": fn})
            return fn
        return deco

    def _call(self, log: list[dict[str, Any]], method: str, path: str, body: Any = None, raw: bytes | None = None,
              token: str | None = None, note: str = "") -> tuple[int, Any]:
        status, resp = self.matrix.call(method, path, body, raw, token)
        shown = body if raw is None else f"（{len(raw)} 字节的文件）"
        log.append({"method": method, "path": path, "body": shown, "status": status, "response": resp, "note": note,
                    "token": "代表 " + self.cfg["personal_owner"] if token == self.cfg.get("personal_token")
                    else "无效令牌" if token is not None else "系统账号"})
        return status, resp

    def _confirmed(self, log: list[dict[str, Any]], method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        """要确认的操作（用模板、装插件）：第一次 409 + 要确认的事，业务系统看过（这里直接同意）后带 digest 再来。"""
        status, resp = self._call(log, method, path, body, note="先不带 digest 试一次")
        if status == 409 and isinstance(resp, dict) and resp.get("digest"):
            log[-1]["note"] = "409：返回要确认的事（confirm）与 digest，业务系统给人看过再继续"
            return self._call(log, method, path, {**body, "digest": resp["digest"]}, note="带上 digest 再来一次才生效")
        if status < 300:
            log[-1]["note"] = "没有要确认的事：直接生效"
        return status, resp

    def need(self, *keys: str) -> None:
        missing = [k for k in keys if not self.state.get(k)]
        if missing:
            names = {"agents": "「查：能用的 Agent」", "project": "「组：建项目」", "service": "「发：发布服务」",
                     "blueprint": "「查：能用的模板」", "session": "「调：调用自己的服务」"}
            raise StepError("先做 " + "、".join(names.get(k, k) for k in missing))

    def _define_steps(self) -> None:
        S, step = self.state, self.step

        # ---------------- 查 ----------------
        @step("agents", "查", "能用的 Agent", "公共 Agent、自己的、所有者开放给这个应用的；只有名片", "GET /open/v1/agents")
        def _(log):
            status, resp = self._call(log, "GET", "/open/v1/agents")
            if status == 200:
                S["agents"] = {a["name"]: a["id"] for a in resp}

        @step("blueprints", "查", "能用的模板", "部署方目录里的模板，用来建项目", "GET /open/v1/blueprints")
        def _(log):
            status, resp = self._call(log, "GET", "/open/v1/blueprints")
            if status == 200 and resp:
                S["blueprint"] = next((b["id"] for b in resp if b.get("title") == "报销审核小组"), resp[0]["id"])

        @step("agents_personal", "查", "代表个人：查到的不一样", f"同一个应用，令牌代表 {self.cfg.get('personal_owner')}：查到的是她自己的与公共的",
              "GET /open/v1/agents（个人令牌）")
        def _(log):
            self._call(log, "GET", "/open/v1/agents", token=self.cfg["personal_token"])

        # ---------------- 组 ----------------
        @step("project", "组", "建项目", "五个 Agent 组成一个小组；拉进开放的「合同审核」「预算管家」时，它们的所有者自动成为参与者",
              "POST /open/v1/projects")
        def _(log):
            self.need("agents")
            ag = S["agents"]
            status, resp = self._call(log, "POST", "/open/v1/projects", {
                "name": PROJECT, "description": "报销系统提交的报销单在这里审核",
                "brief": "审核报销单：5000 元以下财务助手直接给结论；5000 元以上的分头交给票据核验、预算管家，附合同的再交给合同审核，"
                         "合同里有风险条款的由合同审核转给法务顾问；财务助手验收各方结果后给结论。",
                "members": [ag[n] for n in TEAM]})
            if status == 200:
                S["project"] = resp["id"]

        @step("project_patch", "组", "改描述与角色", "纲要、描述、角色（角色 → 成员）", "PATCH /open/v1/projects/{id}")
        def _(log):
            self.need("project", "agents")
            ag = S["agents"]
            self._call(log, "PATCH", f"/open/v1/projects/{S['project']}", {
                "description": "报销系统提交的报销单在这里审核（经 Matrix 服务，五个 Agent 协同）",
                "roles": {key: {"name": label, "members": [ag[name]]} for key, label, name in ROLES}})

        @step("members", "组", "增减成员", "移出、再拉回一个成员", "DELETE / PUT /open/v1/projects/{id}/members/{agent}")
        def _(log):
            self.need("project", "agents")
            aid = S["agents"]["合同审核"]
            self._call(log, "DELETE", f"/open/v1/projects/{S['project']}/members/{aid}")
            self._call(log, "PUT", f"/open/v1/projects/{S['project']}/members/{aid}")

        @step("files", "组", "上传资料", "请求体就是文件（任何类型，≤100 MB）；路径里可以带目录", "PUT /open/v1/projects/{id}/files/{path}")
        def _(log):
            self.need("project")
            for rel, src in (("公开/报销制度.md", "报销制度.md"), ("合同/合同模板.md", "合同模板.md")):
                self._call(log, "PUT", f"/open/v1/projects/{S['project']}/files/{rel}", raw=(MATERIALS / src).read_bytes())

        @step("access", "组", "资料的可见范围", "合同目录只给角色“合同”“法务”看（要先有角色）", "PATCH /open/v1/projects/{id}/access")
        def _(log):
            self.need("project")
            self._call(log, "PATCH", f"/open/v1/projects/{S['project']}/access", {"path": "合同", "rule": {"roles": ["contract", "legal"]}})

        @step("skill", "组", "项目技能", "把审核步骤写成项目技能，成员都能用", "POST /open/v1/projects/{id}/skills")
        def _(log):
            self.need("project")
            self._call(log, "POST", f"/open/v1/projects/{S['project']}/skills", {
                "name": "expense-review", "description": "审核报销单的步骤：先金额与票据，再对照制度，大额或附合同的交给合同角色",
                "body": "1. 读 ./project/公开/报销制度.md\n2. 核对金额、票据、事由\n3. 5000 元以上或附合同：委托合同角色\n4. 第一句写结论"})

        @step("plugin", "组", "装插件（先确认）", "只从部署方目录装；第一次 409 给出要确认的事，带 digest 再来才装",
              "POST /open/v1/projects/{id}/plugins")
        def _(log):
            self.need("project")
            self._confirmed(log, "POST", f"/open/v1/projects/{S['project']}/plugins", {"name": "sales-pipeline"})

        @step("from_blueprint", "组", "用模板建项目", "模板里的每个角色都要指给已有的成员（应用不新建 Agent）；先确认再装",
              "POST /open/v1/projects（带 blueprint）")
        def _(log):
            self.need("agents", "blueprint")
            ag = S["agents"]
            status, resp = self._confirmed(log, "POST", "/open/v1/projects", {
                "name": "报销审核（模板）", "members": [ag["财务助手"], ag["合同审核"]], "blueprint": S["blueprint"],
                "params": {"company": "演示公司"}, "roles": {"intake": ag["财务助手"], "contract": ag["合同审核"]}})
            if status == 200:
                S["project_bp"] = resp["id"]

        @step("projects", "组", "它能管的项目", "只有配置，不给内容（群聊、会话、委托过程）", "GET /open/v1/projects")
        def _(log):
            self._call(log, "GET", "/open/v1/projects")

        # ---------------- 发 ----------------
        @step("service", "发", "发布服务", "入口财务助手，岗位是另外四个（能被委托的）；能读的资料、权限、给谁用；发布即生效", "POST /open/v1/projects/{id}/services")
        def _(log):
            self.need("project", "agents")
            ag = S["agents"]
            status, resp = self._call(log, "POST", f"/open/v1/projects/{S['project']}/services", {
                "name": SERVICE, "title": "报销审核", "description": "审核报销单：金额、票据、预算、制度；附合同的看合同条款与风险",
                "entry": ag["财务助手"], "staff": [ag[n] for n in TEAM if n != "财务助手"], "scope": ["公开", "合同"],
                "authority": ["delegate"], "readers": ["maya@demo.local"]})
            if status == 200:
                S["service"] = resp["id"]

        @step("service_patch", "发", "改服务", "改配置切新版本（version +1）", "PATCH /open/v1/projects/{id}/services/{sid}")
        def _(log):
            self.need("project", "service")
            self._call(log, "PATCH", f"/open/v1/projects/{S['project']}/services/{S['service']}",
                       {"description": "审核报销单：金额、票据、制度；附合同的看合同条款；批量发票逐张核对"})

        # ---------------- 调 ----------------
        @step("svc_card", "调", "服务名片", "只认令牌代表的主体看得到、在发布中的服务", "GET /svc/agents/{service}")
        def _(log):
            self.need("service")
            self._call(log, "GET", f"/svc/agents/{S['service']}")

        @step("svc_chat", "调", "调用自己的服务", "流式返回：session → delta / tool / progress → done", "POST /svc/agents/{service}/chat")
        def _(log):
            self.need("service")
            evs = self._collect(log, S["service"], {"message": "BX-1001 打车 280 元，请审核"})
            S["session"] = next((d["session_id"] for e, d in evs if e == "session"), None)

        @step("svc_resume", "调", "续接同一个会话", "带上 session_id：入口记得之前说过什么", "POST /svc/agents/{service}/chat（带 session_id）")
        def _(log):
            self.need("service", "session")
            self._collect(log, S["service"], {"message": "依据是什么？", "session_id": S["session"]})

        @step("svc_other", "调", "调用别人的服务", "别人发布、点名给这个应用的服务：服务 id 由发布方告诉你",
              "POST /svc/agents/{service}/chat")
        def _(log):
            sid = (self.cfg.get("services") or {}).get("差旅咨询")
            if not sid:
                raise StepError("配置里没有别人的服务 id")
            self._call(log, "GET", f"/svc/agents/{sid}")
            self._collect(log, sid, {"message": "去上海出差住宿标准是多少？"})

        @step("usage", "调", "看服务用量", "多少会话、轮次、失败、token、调用方；不给内容", "GET /open/v1/projects/{id}/services/{sid}/usage")
        def _(log):
            self.need("project", "service")
            self._call(log, "GET", f"/open/v1/projects/{S['project']}/services/{S['service']}/usage")

        # ---------------- 错误处理 ----------------
        @step("err_scope", "错误处理", "没有那个 scope → 403", "个人令牌只授权了“查”，拿它管项目", "GET /open/v1/projects（个人令牌）")
        def _(log):
            self._call(log, "GET", "/open/v1/projects", token=self.cfg["personal_token"])

        @step("err_token", "错误处理", "认不出的令牌 → 401", "撤销、停用、写错都是 401：告警，找管理员重签", "GET /open/v1/agents（无效令牌）")
        def _(log):
            self._call(log, "GET", "/open/v1/agents", token="mat_not-a-real-token")

        @step("err_missing", "错误处理", "看不到的 → 404", "别人的项目、不存在的服务一律 404，不泄露", "GET /svc/agents/svc-00000000")
        def _(log):
            self._call(log, "GET", "/svc/agents/svc-00000000")

        # ---------------- 清理 ----------------
        @step("withdraw", "清理", "撤回服务", "下线；已有的服务会话变成只读", "DELETE /open/v1/projects/{id}/services/{sid}")
        def _(log):
            self.need("project", "service")
            status, _ = self._call(log, "DELETE", f"/open/v1/projects/{S['project']}/services/{S['service']}")
            if status == 200:
                S.pop("service", None)
                S.pop("session", None)

        @step("delete", "清理", "删项目", "只能删经这个应用建的项目", "DELETE /open/v1/projects/{id}")
        def _(log):
            for k in ("project", "project_bp"):
                if S.get(k):
                    status, _ = self._call(log, "DELETE", f"/open/v1/projects/{S[k]}")
                    if status in (200, 404):
                        S.pop(k)
            S.pop("service", None)
            S.pop("session", None)

    def _collect(self, log: list[dict[str, Any]], service: str, body: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        path = f"/svc/agents/{service}/chat"
        evs = list(parse_sse(self.matrix.stream(path, body)))
        log.append({"method": "POST", "path": path, "body": body, "status": 200, "response": [
            {"event": e, "data": d} for e, d in evs], "note": "text/event-stream，按事件列出", "token": "系统账号"})
        return evs

    def run(self, sid: str) -> dict[str, Any]:
        st = next((s for s in self.steps if s["id"] == sid), None)
        if st is None:
            raise StepError(f"没有这一步：{sid}")
        log: list[dict[str, Any]] = []
        with self.lock:
            try:
                st["fn"](log)
                error = None
            except StepError as e:
                error = str(e)
            self.save()
        return {"step": sid, "calls": log, "error": error, "state": self.public_state()}

    PREPARE = ("agents", "blueprints", "project", "project_patch", "files", "access", "skill", "service")

    def prepare(self) -> dict[str, Any]:
        """一键准备：报销单审核要用的项目与服务（查 → 组 → 发 的最少几步）。"""
        out = []
        for sid in self.PREPARE:
            if sid == "project" and self.state.get("project"):
                continue
            if sid == "service" and self.state.get("service"):
                continue
            r = self.run(sid)
            out.append(r)
            if r["error"] or any(c["status"] >= 400 for c in r["calls"]):
                break
        return {"runs": out, "state": self.public_state()}

    def public_state(self) -> dict[str, Any]:
        """给浏览器看的：拿到的 id 与 Matrix 地址，不含令牌。"""
        return {"matrix_url": self.cfg["matrix_url"], "app": self.cfg.get("app"),
                "system_login": self.cfg.get("system_login"),
                **{k: v for k, v in self.state.items() if k in ("agents", "blueprint", "project", "project_bp", "service", "session")}}

    def targets(self) -> list[dict[str, Any]]:
        """对话页能找谁说话：自己发布的服务（发布了才有），和别人点名给这个应用的服务。"""
        out = [{"key": "own", "title": "报销审核", "owner": "报销系统自己发布的", "service": self.state.get("service"),
                "hint": "发报销单号与金额，例如“BX-2001 技术咨询费 9,800 元，附合同”；也可以问“报销制度是什么”"}]
        for title, sid in (self.cfg.get("services") or {}).items():
            out.append({"key": title, "title": title, "owner": "别人发布、点名给报销系统用的", "service": sid,
                        "hint": "问差旅标准，例如“去上海出差住宿标准是多少？”"})
        return out

    def target_service(self, key: str) -> str:
        t = next((t for t in self.targets() if t["key"] == (key or "own")), None)
        if t is None:
            raise StepError(f"没有这个服务：{key}")
        if not t["service"]:
            raise StepError("还没有报销审核服务：先点“一键准备”，或在“对接能力”里建项目、发布服务")
        return t["service"]

    def steps_view(self) -> list[dict[str, str]]:
        return [{k: s[k] for k in ("id", "group", "title", "desc", "api")} for s in self.steps]


class StepError(Exception):
    pass


def make_handler(biz: BizSystem):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_: Any) -> None:
            pass

        def _json(self, data: Any, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/state":
                return self._json({"state": biz.public_state(), "steps": biz.steps_view(), "claims": CLAIMS,
                                   "targets": biz.targets()})
            f = STATIC / ("index.html" if path == "/" else path.lstrip("/"))
            if f.resolve().parent != STATIC.resolve() or not f.is_file():
                return self._json({"detail": "not found"}, 404)
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", TYPES.get(f.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            try:
                if path.startswith("/api/steps/"):
                    return self._json(biz.run(path.rsplit("/", 1)[1]))
                if path == "/api/prepare":
                    return self._json(biz.prepare())
                if path == "/api/reset":
                    with biz.lock:
                        biz.state.clear()
                        biz.save()
                    return self._json({"state": biz.public_state()})
                if path in ("/api/chat", "/api/review"):
                    return self._chat(self._body())
                if path == "/api/interrupt":
                    sid = str(self._body().get("session_id") or "")
                    status, resp = biz.matrix.call("POST", f"/svc/sessions/{sid}/interrupt")
                    return self._json({"status": status, "response": resp}, 200 if status < 400 else status)
            except StepError as e:
                return self._json({"detail": str(e)}, 409)
            return self._json({"detail": "not found"}, 404)

        def _chat(self, body: dict[str, Any]) -> None:
            """对话 / 报销单送审：调 service（own = 自己发布的报销审核，或别人的服务），把 Matrix 的 SSE 原样转给浏览器
            （令牌留在这里）。带 session_id 就是续接同一个服务会话。"""
            service = biz.target_service(str(body.get("service") or "own"))
            req = {"message": str(body.get("message") or "")[:20000]}
            if body.get("session_id"):
                req["session_id"] = str(body["session_id"])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                for line in biz.matrix.stream(f"/svc/agents/{service}/chat", req):
                    self.wfile.write(line)
                    if not line.strip():
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # 浏览器关掉了：服务端照常把这一轮做完，带 session_id 再问能接着拿

    return Handler


def serve(config: Path, port: int = 8800) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(BizSystem(config)))
    server.daemon_threads = True
    return server


def main() -> None:
    ap = argparse.ArgumentParser(description="模拟的业务系统「报销系统」：演示对接 Matrix 的全部能力")
    ap.add_argument("--config", default=".matrix-biz-demo/biz-system.json")
    ap.add_argument("--port", type=int, default=8800)
    args = ap.parse_args()
    server = serve(Path(args.config), args.port)
    print(f"报销系统：http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
