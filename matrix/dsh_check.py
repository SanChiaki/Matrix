"""matrix-dsh-check：对真实的 dsh 跑一遍冒烟检查。升级云端 dsh 之前（本机）和之后（容器里）各跑一次（deploy/README.md「升级 dsh」）。

`uv run pytest` 用的是脚本运行时，不会真的跑 dsh；Matrix 依赖 dsh 的这几处接口，换版本可能悄悄变：

- 静态（不调模型）：版本；headless 的组合配置读得出来；覆盖层引用的插件都还在（dsh 对找不到的插件只在 stderr 警告、
  照常运行，被禁用的插件改了名就会在没有权限时重新可用）；有没有新的工具插件要按权限管；带全部桥的覆盖层能加载；
  配了抓取白名单时，自带的 web_fetch 关得掉、web_search 还在、白名单抓取服务装得上。
- 平台配的模型供应商（不调真模型：本机起一个假的 OpenAI 兼容服务）：覆盖层里的供应商生效、密钥经环境变量带上，
  而 Agent 的命令看不到这个密钥（printenv、各进程的 /proc/*/environ、ps eww）。research: deepseek-harness#model-providers
- 实际跑（`--live`，要调模型，花一点 token）：回复与网络工具、续接会话、技能、委托、连接器代调用、沙箱升权审批。

    uv run matrix-dsh-check [--dsh-bin dsh] [--live] [--model provider/model] [--only resume,approval] [--keep]
    docker compose exec matrix matrix-dsh-check --live

数据放在临时目录，跑完删掉（`--keep` 或有失败时保留，便于看 `dsh-events.jsonl`）。不碰正式数据目录。
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .model_catalog import ModelPreset, Provider, llm_config
from .runtime import (ALWAYS_DISABLED, AUTHORITY_PLUGINS, ApprovalBridge, ConnectorBridge, DshRuntime, RunRequest,
                      ToolBridge, argv, build_patch_yaml)
from .web_fetch import FetchPolicy

# 覆盖层按 id 配置的插件（不是 insert 进去的）：必须在 dsh 的组合配置里
CONFIGURED = ["skill-filesystem", "agent-default-model"]
# 已经看过、知道该怎么管的工具插件：要么按权限禁用（AUTHORITY_PLUGINS / ALWAYS_DISABLED），要么只读或受沙箱约束。
# 新版本多出来的 tool-* 不在这里时给提醒：确认它要不要按权限禁用，再加进来
KNOWN_TOOLS = {
    "tool-bash", "tool-fs", "tool-fs-search", "tool-goal", "tool-jobs", "tool-plugin-manager", "tool-pwsh", "tool-ralph",
    "tool-result-pruner", "tool-skill", "tool-subagent", "tool-subagent-control", "tool-subagent-fork",
    "tool-subagent-list-agents", "tool-todo", "tool-web", "tool-workflow", "tool-working-directory",
}
OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "✓", WARN: "!", FAIL: "✗"}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str) -> None:
        self.rows.append((status, name, detail))
        print(f"{MARK[status]} {name:<12} {detail}", flush=True)

    @property
    def failed(self) -> bool:
        return any(s == FAIL for s, _, _ in self.rows)


def _run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(argv(cmd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, **kw)


def plugin_ids(config: str) -> set[str]:
    return set(re.findall(r"^- id: (\S+)", config, re.M))


def block_of(config: str, pid: str) -> str:
    block = re.search(rf"^- id: {re.escape(pid)}\n((?:[ ].*\n?)*)", config, re.M)
    return block.group(1) if block else ""


def disabled_in(config: str, pid: str) -> bool:
    return bool(re.search(r"^  disabled: true", block_of(config, pid), re.M))


# ================= 静态检查（不调模型） =================
def static_checks(rt: DshRuntime, report: Report, workdir: Path) -> bool:
    try:
        out = _run([rt.dsh_bin, "--version"])
    except (OSError, subprocess.SubprocessError) as e:
        report.add(FAIL, "version", f"运行不了 {rt.dsh_bin}：{e}")
        return False
    report.add(OK if out.returncode == 0 else FAIL, "version", (out.stdout or out.stderr).strip().splitlines()[0][:80]
               if (out.stdout or out.stderr).strip() else f"退出码 {out.returncode}")

    base = _run([rt.dsh_bin, rt.profile, "--dump-config"])
    if base.returncode != 0 or not plugin_ids(base.stdout):
        report.add(FAIL, "config", f"`dsh {rt.profile} --dump-config` 失败：{base.stderr.strip()[-300:]}")
        return False
    ids = plugin_ids(base.stdout)
    model = rt.default_model()
    report.add(OK, "config", f"{len(ids)} 个插件；默认模型 "
               + (f"{model.get('provider')}/{model.get('model')}" if model else "读不出来（Agent 不设模型时由 dsh 自己决定）"))

    # 按权限禁用的、要配置的插件找不到是失败（改了名就会在没有权限时也能用）；始终禁用的那些可能是旧版本还没有，只提醒
    required = list(dict.fromkeys([*(p for ps in AUTHORITY_PLUGINS.values() for p in ps), *CONFIGURED]))
    missing = [p for p in required if p not in ids]
    extra = [p for p in ALWAYS_DISABLED if p not in ids]
    report.add(FAIL if missing else OK, "plugins",
               f"覆盖层引用的插件找不到：{'、'.join(missing)}（改了名的话没有权限时也能用，先改 runtime.AUTHORITY_PLUGINS）"
               if missing else f"按权限禁用、要配置的 {len(required)} 个插件都在")
    if extra:
        report.add(WARN, "disabled", f"始终禁用的插件这个版本没有：{'、'.join(extra)}（旧版本没有就没事；改了名要改 runtime.ALWAYS_DISABLED）")
    new_tools = sorted(p for p in ids if p.startswith("tool-") and p not in KNOWN_TOOLS)
    if new_tools:
        report.add(WARN, "new-tools", f"新出现的工具插件：{'、'.join(new_tools)}。确认要不要按权限禁用，再加进 dsh_check.KNOWN_TOOLS")

    noop = lambda *a: ("", False)  # noqa: E731
    patch = workdir / "overlay.yml"
    patch.write_text(build_patch_yaml(
        [], ToolBridge("http://127.0.0.1:9/internal/tools", "t", noop, 60, ["record_decision"]), model,
        ApprovalBridge("http://127.0.0.1:9/internal/approval", "t", lambda *a: "cancelled", 60),
        skill_dirs=[str(workdir / "skills")],
        connectors=ConnectorBridge("http://127.0.0.1:9/internal/connectors", "t", list, noop)), encoding="utf-8")
    full = _run([rt.dsh_bin, rt.profile, "--patch", str(patch), "--dump-config"])
    problems = []
    if full.returncode != 0:
        problems.append(f"退出码 {full.returncode}：{full.stderr.strip()[-300:]}")
    problems += [line.strip() for line in full.stderr.splitlines()
                 if "not found" in line and not any(f'"{p}"' in line for p in extra)]
    inserted = ["mcp-matrix_net", "mcp-connectors", "matrix-approval"]
    problems += [f"没装上 {p}" for p in inserted if p not in plugin_ids(full.stdout)]
    problems += [f"{p} 没被禁用" for p in [*ALWAYS_DISABLED, *(p for ps in AUTHORITY_PLUGINS.values() for p in ps)]
                 if p in ids and not disabled_in(full.stdout, p)]
    if "includeDefaultRoots: false" not in full.stdout:
        problems.append("skill-filesystem 的 includeDefaultRoots: false 没生效（会读部署机器上的技能目录）")
    report.add(FAIL if problems else OK, "overlay", "；".join(problems)[:400] if problems
               else "没有权限的覆盖层：网络工具、连接器桥、审批应答者都装上，按权限禁用的插件都关了")
    fetch_check(rt, report, workdir)
    return not missing


def fetch_check(rt: DshRuntime, report: Report, workdir: Path) -> None:
    """配了抓取白名单（fetch.json）的覆盖层：自带的 web_fetch 关掉、web_search 还在，web_fetch.py 装上。
    dsh 改了 tool-web 的配置字段（fetch / search）时这里变红：自带的 web_fetch 关不掉，模型会挑它、在内网地址上碰壁。"""
    patch = workdir / "overlay-fetch.yml"
    patch.write_text(build_patch_yaml(["web.research"], fetch=FetchPolicy(domains=("intranet.example",))),
                     encoding="utf-8")
    full = _run([rt.dsh_bin, rt.profile, "--patch", str(patch), "--dump-config"])
    tool_web = block_of(full.stdout, "tool-web")
    problems = [] if full.returncode == 0 else [f"退出码 {full.returncode}：{full.stderr.strip()[-300:]}"]
    if "mcp-matrix_web" not in plugin_ids(full.stdout):
        problems.append("没装上 mcp-matrix_web")
    if not re.search(r"^\s+fetch: false", tool_web, re.M):
        problems.append("tool-web 的 fetch: false 没生效（自带的 web_fetch 还在）")
    if disabled_in(full.stdout, "tool-web") or re.search(r"^\s+search: false", tool_web, re.M):
        problems.append("tool-web 被整个关掉了（web_search 也没了）")
    report.add(FAIL if problems else OK, "web-fetch", "；".join(problems)[:400] if problems
               else "抓取白名单：自带的 web_fetch 关掉、web_search 保留，白名单抓取服务装上")


# ================= 平台配的模型供应商（假服务，不调真模型） =================
PROVIDER_DONE = "MATRIX-PROVIDER-OK"


class FakeProvider:
    """OpenAI chat completions 的最小假服务（SSE）。带工具的第一次请求让 Agent 用 bash 找密钥，之后收尾；
    记下每次请求的认证头、模型、推理强度与工具结果。"""

    def __init__(self, probe: str) -> None:
        self.requests: list[dict[str, Any]] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                tools = {t.get("function", {}).get("name") for t in body.get("tools") or []}
                results = [str(m.get("content")) for m in body.get("messages") or [] if m.get("role") == "tool"]
                fake.requests.append({"auth": self.headers.get("Authorization") or self.headers.get("x-api-key") or "",
                                      "model": body.get("model"), "effort": body.get("reasoning_effort"),
                                      "bash": "bash" in tools, "results": results})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def chunk(delta: dict[str, Any], finish: str | None = None) -> None:
                    c = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": body.get("model"),
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())

                if "bash" in tools and not results:
                    args = json.dumps({"description": "check", "command": probe})
                    chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                                "function": {"name": "bash", "arguments": args}}]})
                    chunk({}, "tool_calls")
                else:
                    chunk({"role": "assistant", "content": PROVIDER_DONE})
                    chunk({}, "stop")
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def provider_checks(rt: DshRuntime, report: Report, workdir: Path, timeout_s: float = 120) -> None:
    secret = "sk-matrix-check-" + secrets.token_hex(8)
    # 命令里不能出现密钥本身（ps 会看到这条命令）：按变量名找，[Y] 让 grep 不匹配到自己
    prov0 = Provider(id="matrix-check", name="Matrix check", api="openai-completions", base_url="http://127.0.0.1:9")
    pattern = prov0.key_env[:-1] + "[" + prov0.key_env[-1] + "]="
    probe = (f"printenv {prov0.key_env} >/dev/null; echo PRINTENV=$?; "
             f"echo PROC=$(cat /proc/[0-9]*/environ 2>/dev/null | tr '\\0' '\\n' | grep -c '{pattern}'); "
             f"echo PS=$(ps eww 2>/dev/null | grep -c '{pattern}')")
    fake = FakeProvider(probe)
    try:
        prov = Provider(id=prov0.id, name=prov0.name, api=prov0.api, base_url=fake.url)
        preset = ModelPreset(id="check", label="Fake", provider=prov.id, model="fake-1", reasoning_effort="high")
        res = rt.run(RunRequest(agent_id="check", context_id="provider", prompt="跑一下", cwd=workdir / "provider",
                                authority=list(AUTHORITY_PLUGINS), model=preset.run_model(), timeout_s=timeout_s,
                                llm_provider={"id": prov.id, "config": llm_config(prov, preset)},
                                env={prov.key_env: secret}))
    finally:
        fake.server.shutdown()
    reqs = fake.requests
    if not reqs:
        report.add(FAIL, "provider", f"覆盖层里的供应商没生效：dsh 没请求假服务（{res.error or '没有报错'}）"[:400])
        return
    bad_auth = [r for r in reqs if secret not in r["auth"]]
    report.add(FAIL if bad_auth else OK, "provider",
               f"{len(bad_auth)}/{len(reqs)} 次请求没带上环境变量里的密钥" if bad_auth
               else f"覆盖层里的供应商生效，{len(reqs)} 次请求都带着环境变量里的密钥，模型 {reqs[-1]['model']}")
    efforts = {r["effort"] for r in reqs if r["bash"]}
    if efforts != {"high"}:
        report.add(WARN, "effort", f"推理强度 high 到供应商那里是 {sorted(map(str, efforts))}（model_catalog.llm_config 的映射可能要改）")
    results = " ".join(x for r in reqs for x in r["results"])
    leaked = [k for k, ok in (("printenv", "PRINTENV=0" not in results), ("/proc", re.search(r"PROC=[1-9]", results) is None),
                              ("ps", re.search(r"PS=[1-9]", results) is None)) if not ok]
    if "PRINTENV=" not in results:
        report.add(FAIL, "key-hidden", f"Agent 的 bash 没跑起来，没法确认看不到密钥：{results[:200] or res.error or res.final_text}"[:400])
    else:
        report.add(FAIL if leaked else OK, "key-hidden",
                   f"Agent 的命令看得到供应商密钥（{'、'.join(leaked)}）：不能把密钥交给 dsh 进程的环境" if leaked
                   else "Agent 的命令看不到供应商密钥（printenv、/proc/*/environ、ps eww）")
    if PROVIDER_DONE not in (res.final_text or ""):
        report.add(FAIL, "provider-end", f"这一轮没正常收尾：{res.error or res.final_text}"[:300])


# ================= 实际跑（--live） =================
class EchoMCP:
    """本机的一个远程 MCP 服务（Streamable HTTP，不要授权）：只有 echo 工具，记下收到的调用。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "id" not in msg:
                    self.send_response(202)
                    self.end_headers()
                    return
                m = msg["method"]
                if m == "initialize":
                    result: Any = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                   "serverInfo": {"name": "smoke"}}
                elif m == "tools/list":
                    result = {"tools": [{"name": "echo", "description": "原样返回 ECHO-<text>",
                                         "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                                                         "required": ["text"]}}]}
                else:
                    args = msg["params"].get("arguments") or {}
                    outer.calls.append(args)
                    result = {"content": [{"type": "text", "text": f"ECHO-{args.get('text', '')}"}]}
                body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Live:
    """一个临时的网络：真实 dsh 运行时 + 只监听本机的回调服务 + 本机 echo 连接器。"""

    def __init__(self, rt: DshRuntime, root: Path, timeout_s: float, model: dict[str, str] | None = None):
        import uvicorn

        from . import auth
        from .connector_catalog import ConnectorCatalog
        from .engine import Network
        from .server import create_app

        self.timeout_s = timeout_s
        self.mcp = EchoMCP()
        catalog = root / "connectors.json"
        catalog.write_text(json.dumps({"connectors": [{"id": "smoke", "name": "冒烟", "url": self.mcp.url}]}), encoding="utf-8")
        port = _free_port()
        (root / "data").mkdir()
        self.net = Network(rt, root / "data", tool_url=f"http://127.0.0.1:{port}/internal/tools", seed=False,
                           connectors=ConnectorCatalog(catalog))
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.net), host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=self.server.run, daemon=True).start()
        while not self.server.started:
            time.sleep(0.05)
        self.uid = auth.register(self.net.store, "smoke@matrix.local", secrets.token_urlsafe(18))[0].id
        mk = self.net.create_agent
        self.lead = mk("Lead", "冒烟检查：主执行者", [], ["delegate", "file.write", "shell.exec", "connector.use"],
                       model=model, actor=self.uid)
        self.helper = mk("Helper", "冒烟检查：被委托的成员", [], [], model=model, actor=self.uid)
        self.project = self.net.create_project("dsh 冒烟检查", "", [self.lead.id, self.helper.id], actor=self.uid)
        self.chat = self.net.project_chat(self.project.id, actor=self.uid)
        self.outside = root / "outside"
        self.outside.mkdir()

    def ask(self, text: str, approve: bool = False) -> tuple[str, list[Any]]:
        """@Lead 说一句，等到对话安静下来（委托也都有了结果）。返回 Lead 最后一条回复与这期间出现的审批申请。"""
        n = len(self.chat.messages)
        self.net.send_message(self.chat.id, f"@Lead {text}", mentions=[self.lead.id], actor=self.uid)
        seen: dict[str, Any] = {}
        deadline, quiet = time.time() + self.timeout_s, 0
        time.sleep(0.5)
        while time.time() < deadline:
            for ap in self.net.pending_approvals(None):
                if ap.id not in seen:
                    seen[ap.id] = ap
                    self.net.decide_approval(ap.id, approve, note="冒烟检查")
            with self.net.store.lock:
                idle = not self.net._running and self.chat.status == "idle"
            quiet = quiet + 1 if idle else 0
            if quiet >= 3:
                break
            time.sleep(0.5)
        else:
            raise TimeoutError(f"{int(self.timeout_s)} 秒内没结束")
        replies = [m for m in self.chat.messages[n:] if m.role == "agent" and m.agent_id == self.lead.id]
        failure = [m.text for m in self.chat.messages[n:] if m.role == "system"]
        if not replies:
            raise RuntimeError("Lead 没有回复" + (f"：{failure[-1][:200]}" if failure else ""))
        return replies[-1].text, list(seen.values())


def _scenarios(live: Live) -> list[tuple[str, Callable[[], tuple[str, str]]]]:
    nonce = lambda: secrets.token_hex(3).upper()  # noqa: E731
    first = nonce()

    def reply() -> tuple[str, str]:
        text, _ = live.ask(f"冒烟检查：先调用网络工具 record_decision，记下决定「冒烟检查 {first}」；然后只回复 OK-{first}，不要别的内容。")
        decided = any(first in d.text for d in live.project.decisions)
        if f"OK-{first}" not in text:
            return FAIL, f"回复里没有 OK-{first}：{text[:120]!r}"
        return (OK, "headless 的事件流、网络工具（record_decision）都通") if decided \
            else (FAIL, "回复了，但决定没记上：网络工具没调通")

    def resume() -> tuple[str, str]:
        seat = live.chat.seats[live.lead.id]
        before = seat.dsh_session_id
        text, _ = live.ask("我上一条消息里的编号（OK- 后面那串）是多少？只回复编号本身，不要调用任何工具。")
        if not before or seat.dsh_session_id != before:
            return FAIL, f"没有续接同一个会话（之前 {before}，之后 {seat.dsh_session_id}）"
        return (OK, f"续接会话 {before[:8]}… 记得上一轮的编号") if first in text \
            else (FAIL, f"续接了会话但不记得编号：{text[:120]!r}")

    def skill() -> tuple[str, str]:
        code = nonce()
        live.net.save_project_skill(live.project.id, "smoke-skill", "冒烟检查时按这个回复",
                                    f"有人要你按 smoke-skill 回复时，只回复 SKILL-{code}。", actor=live.uid)
        text, _ = live.ask("读一下项目技能 smoke-skill（在 ./skills/ 下），照它说的回复。")
        return (OK, "技能目录挂载与读取正常") if f"SKILL-{code}" in text else (FAIL, f"没照技能回复：{text[:120]!r}")

    def delegate() -> tuple[str, str]:
        code = nonce()
        text, _ = live.ask(f"用网络工具 delegate 把一个子任务委托给 Helper：让它只回复 PONG-{code}。"
                           "等它交付后验收，然后只回复它交付的原文。")
        tasks = [t for t in live.net.store.tasks.values() if t.delegatee_id == live.helper.id]
        if not tasks:
            return FAIL, f"没有发起委托：{text[:120]!r}"
        delivered = any(code in json.dumps(t.result or {}, ensure_ascii=False) for t in tasks)
        if f"PONG-{code}" in text or delivered:
            return OK, f"委托、收件箱唤醒、交付都通（任务 {tasks[-1].status.value}）"
        return FAIL, f"委托了（{tasks[-1].status.value}），但结果没回到 Lead：{text[:120]!r}"

    def connector() -> tuple[str, str]:
        code = nonce()
        live.net.authorize_connector("smoke", {}, live.uid, agent_ids=[live.lead.id])
        text, _ = live.ask(f"调用连接器工具 smoke__echo，参数 text 填 {code}；然后只回复工具返回的原文。")
        if not any(c.get("text") == code for c in live.mcp.calls):
            return FAIL, f"连接器没被调用：{text[:120]!r}"
        return (OK, "连接器桥、网络代调用正常") if f"ECHO-{code}" in text else (WARN, f"调用了，但回复没照抄结果：{text[:120]!r}")

    def approval() -> tuple[str, str]:
        target = live.outside / f"smoke-{nonce()}.txt"
        text, asked = live.ask(f"用 bash 执行 `echo hi > {target}`。这个路径在工作目录以外，沙箱会拒绝；"
                               "被拒绝时按提示带上理由申请升权，重试同一条命令一次。完成后只回复 DONE。", approve=True)
        if not asked:
            return (FAIL, "没申请升权就写到了工作目录以外：沙箱没起作用") if target.exists() \
                else (FAIL, f"没有出现升权申请（模型没申请，或审批应答者没装上）：{text[:120]!r}")
        return (OK, f"沙箱拦住了工作目录以外的写，升权申请转到网络，批准后写成（{asked[0].tool}）") if target.exists() \
            else (FAIL, "申请批准了，但文件没写出来")

    return [("reply", reply), ("resume", resume), ("skill", skill), ("delegate", delegate),
            ("connector", connector), ("approval", approval)]


def live_checks(rt: DshRuntime, report: Report, root: Path, timeout_s: float, only: set[str],
                model: dict[str, str] | None = None) -> None:
    live = Live(rt, root, timeout_s, model)
    try:
        for name, fn in _scenarios(live):
            if only and name not in only:
                continue
            started = time.time()
            try:
                status, detail = fn()
            except Exception as e:  # noqa: BLE001 —— 一项出错不影响后面的
                status, detail = FAIL, f"{type(e).__name__}: {e}"[:300]
            report.add(status, name, f"{detail}（{time.time() - started:.0f} 秒）")
            if name == "reply" and status == FAIL:  # 最基本的一轮都不通，后面几项只会重复同一个错
                if "MISSING_CREDENTIAL" in detail or "SIGN_IN_REQUIRED" in detail:
                    print("  dsh 读不到模型凭据：容器里按 deploy/README.md「配置模型凭据」配好；本机单独装的 dsh 可能解析不了"
                          "桌面 App 存的凭据引用；用账号登录的换 --model deepseek-account/deepseek-flash。")
                print("  其余实际检查跳过。")
                break
    finally:
        live.server.should_exit = True
        live.mcp.server.shutdown()


def main() -> None:
    from . import utf8_stdio
    utf8_stdio()
    p = argparse.ArgumentParser(prog="matrix-dsh-check", description="对真实的 dsh 跑一遍冒烟检查（升级 dsh 前后用）")
    p.add_argument("--dsh-bin", default="dsh")
    p.add_argument("--profile", default="headless")
    p.add_argument("--live", action="store_true", help="实际跑几轮（要调模型）")
    p.add_argument("--only", default="", help="只跑这些实际检查，逗号分隔：reply,resume,skill,delegate,connector,approval")
    p.add_argument("--model", default="", help="实际检查的 Agent 用这个模型，provider/model（默认用 dsh 的默认模型），"
                   "如本机用账号登录时 deepseek-account/deepseek-flash")
    p.add_argument("--timeout", type=float, default=300, help="每项实际检查最多等多少秒")
    p.add_argument("--keep", action="store_true", help="保留临时数据目录")
    args = p.parse_args()

    if shutil.which(args.dsh_bin) is None and not Path(args.dsh_bin).is_file():
        raise SystemExit(f"找不到 dsh：{args.dsh_bin}")
    rt = DshRuntime(args.dsh_bin, args.profile)
    root = Path(tempfile.mkdtemp(prefix="matrix-dsh-check-"))
    report = Report()
    ok = static_checks(rt, report, root)
    if ok:
        provider_checks(rt, report, root)
    if args.live and ok:
        provider, _, name = args.model.partition("/")
        live_checks(rt, report, root, args.timeout, {x.strip() for x in args.only.split(",") if x.strip()},
                    {"provider": provider, "model": name} if name else None)
    elif args.live:
        print("静态检查没过，不跑实际检查。")
    if report.failed or args.keep:
        print(f"\n临时数据：{root}（各轮的 dsh 事件在 data/conversations/*/ 下的 dsh-events.jsonl）")
    else:
        shutil.rmtree(root, ignore_errors=True)
    print(f"\n{'有检查没过' if report.failed else '全部通过'}：" + "  ".join(f"{MARK[s]}{n}" for s, n, _ in report.rows))
    sys.exit(1 if report.failed else 0)


if __name__ == "__main__":
    main()
