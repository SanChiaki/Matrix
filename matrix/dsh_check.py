"""matrix-dsh-check：对真实的 dsh 跑一遍冒烟检查。升级云端 dsh 之前（本机）和之后（容器里）各跑一次（deploy/README.md「升级 dsh」）。

`uv run pytest` 用的是脚本运行时，不会真的跑 dsh；Matrix 依赖 dsh 的这几处接口，换版本可能悄悄变：

- 静态（不调模型）：版本；headless 的组合配置读得出来；覆盖层引用的插件都还在（dsh 对找不到的插件只在 stderr 警告、
  照常运行，被禁用的插件改了名就会在没有权限时重新可用）；有没有新的工具插件要按权限管；带全部桥的覆盖层能加载；
  配了抓取白名单时，自带的 web_fetch 关得掉、web_search 还在、白名单抓取服务装得上。
- 平台配的模型供应商（不调真模型：本机起一个假的 OpenAI 兼容服务）：覆盖层里的供应商生效、密钥经环境变量带上，
  而 Agent 的命令看不到这个密钥（printenv、各进程的 /proc/*/environ、ps eww）。research: deepseek-harness#model-providers
- 云侧隔离（同样由假服务驱动 Agent 的 bash，docs/cloud-runtime.md 第 8 节）：`read-isolation` 读数据目录里的标记、
  另一个执行单元的工作目录与 dsh 会话、`vault.key`、服务进程主目录里的东西，读得到就失败；`proc-isolation` 看沙箱之外的进程
  与它们的环境变量。外层沙箱不可用、又不是 required 时给“未隔离”的提醒，不算通过。
- 实际跑（`--live`，要调模型，花一点 token）：回复与网络工具、续接会话、技能、委托、连接器代调用、
  工作目录以外写不进且不产生审批（云侧不审批，不变量 14）、提问。

    uv run matrix-dsh-check [--dsh-bin dsh] [--live] [--model provider/model] [--only resume,approval] [--keep]
                            [--isolation required|preferred|off]
    docker compose exec matrix matrix-dsh-check --live

数据放在临时目录，跑完删掉（`--keep` 或有失败时保留，便于看 `dsh-events.jsonl`）。不碰正式数据目录。
"""

from __future__ import annotations

import argparse
import json
import os
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
from .runtime import (ALWAYS_DISABLED, AUTHORITY_PLUGINS, ApprovalBridge, ConnectorBridge, DshRuntime, QuestionBridge,
                      RunRequest, ToolBridge, argv, build_patch_yaml)
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
        connectors=ConnectorBridge("http://127.0.0.1:9/internal/connectors", "t", list, noop),
        questions=QuestionBridge("http://127.0.0.1:9/internal/question", "t", lambda q: {}, 60)), encoding="utf-8")
    full = _run([rt.dsh_bin, rt.profile, "--patch", str(patch), "--dump-config"])
    problems = []
    if full.returncode != 0:
        problems.append(f"退出码 {full.returncode}：{full.stderr.strip()[-300:]}")
    problems += [line.strip() for line in full.stderr.splitlines()
                 if "not found" in line and not any(f'"{p}"' in line for p in extra)]
    # tool-ask-user：dsh 自带的提问工具（headless 默认不加载）；改了包名或去掉了，覆盖层会在这里报 not found
    inserted = ["mcp-matrix_net", "mcp-connectors", "matrix-approval", "tool-ask-user", "matrix-questions", "matrix-jobs"]
    problems += [f"没装上 {p}" for p in inserted if p not in plugin_ids(full.stdout)]
    problems += [f"{p} 没被禁用" for p in [*ALWAYS_DISABLED, *(p for ps in AUTHORITY_PLUGINS.values() for p in ps)]
                 if p in ids and not disabled_in(full.stdout, p)]
    if "includeDefaultRoots: false" not in full.stdout:
        problems.append("skill-filesystem 的 includeDefaultRoots: false 没生效（会读部署机器上的技能目录）")
    report.add(FAIL if problems else OK, "overlay", "；".join(problems)[:400] if problems
               else "没有权限的覆盖层：网络工具、连接器桥、审批（端侧驱动用）与提问应答者、后台任务守护都装上，按权限禁用的插件都关了")
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
    记下每次请求的认证头、模型、推理强度、工具结果，以及受 compat 影响的系统提示角色和输出上限字段。"""

    def __init__(self, probe: str, host: str = "127.0.0.1") -> None:
        """host：监听在哪（docker 后端的沙箱在自己的网络里，要监听在它连得到的网关上）。probe 可以随时换。"""
        self.requests: list[dict[str, Any]] = []
        self.probe = probe
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                tools = {t.get("function", {}).get("name") for t in body.get("tools") or []}
                results = [str(m.get("content")) for m in body.get("messages") or [] if m.get("role") == "tool"]
                msgs = body.get("messages") or [{}]
                fake.requests.append({"auth": self.headers.get("Authorization") or self.headers.get("x-api-key") or "",
                                      "model": body.get("model"), "effort": body.get("reasoning_effort"),
                                      "bash": "bash" in tools, "results": results,
                                      "role": msgs[0].get("role"), "max_tokens": "max_tokens" in body})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def chunk(delta: dict[str, Any], finish: str | None = None) -> None:
                    c = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": body.get("model"),
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())

                if "bash" in tools and not results:
                    args = json.dumps({"description": "check", "command": fake.probe})
                    chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                                "function": {"name": "bash", "arguments": args}}]})
                    chunk({}, "tool_calls")
                else:
                    chunk({"role": "assistant", "content": PROVIDER_DONE})
                    chunk({}, "stop")
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer((host, 0), H)
        self.url = f"http://{host}:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def provider_checks(rt: DshRuntime, report: Report, workdir: Path, timeout_s: float = 120) -> None:
    secret = "sk-matrix-check-" + secrets.token_hex(8)
    # 命令里不能出现密钥本身（ps 会看到这条命令）：按变量名找，[Y] 让 grep 不匹配到自己
    prov0 = Provider(id="matrix-check", name="Matrix check", api="openai-completions", base_url="http://127.0.0.1:9")
    pattern = prov0.key_env[:-1] + "[" + prov0.key_env[-1] + "]="
    probe = (f"printenv {prov0.key_env} >/dev/null; echo PRINTENV=$?; "
             f"echo PROC=$(cat /proc/[0-9]*/environ 2>/dev/null | tr '\\0' '\\n' | grep -c '{pattern}'); "
             f"echo PS=$(ps eww 2>/dev/null | grep -c '{pattern}'); "
             "echo PID1=$(tr '\\0' ' ' < /proc/1/cmdline 2>/dev/null | cut -c1-40)")
    fake = FakeProvider(probe)
    try:
        # compat：不配时 dsh 发 developer 角色与 max_completion_tokens，glm 一类端点据此回 400
        prov = Provider(id=prov0.id, name=prov0.name, api=prov0.api, base_url=fake.url,
                        compat={"supportsDeveloperRole": False, "maxTokensField": "max_tokens"})
        preset = ModelPreset(id="check", label="Fake", provider=prov.id, model="fake-1", reasoning_effort="high",
                             max_tokens=4096)
        res = rt.run(RunRequest(agent_id="check", context_id="provider", prompt="跑一下", cwd=workdir / "provider",
                                authority=list(AUTHORITY_PLUGINS), model=preset.run_model(), timeout_s=timeout_s,
                                llm_provider={"id": prov.id, "config": llm_config(prov, preset)},
                                env={prov.key_env: secret}, state_dir=workdir / "provider-state"))
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
    ignored = sorted({f"角色 {r['role']}" for r in reqs if r["role"] != "system"}
                     | ({"没用 max_tokens 字段"} if not all(r["max_tokens"] for r in reqs) else set()))
    report.add(FAIL if ignored else OK, "compat",
               f"供应商的 compat 没生效（{'、'.join(ignored)}）：非标准的兼容端点会 400，model_catalog.llm_config 要改" if ignored
               else "供应商的 compat 生效（系统提示用 system 角色、输出上限用 max_tokens）")
    results = " ".join(x for r in reqs for x in r["results"])
    leaked = [k for k, ok in (("printenv", "PRINTENV=0" not in results), ("/proc", re.search(r"PROC=[1-9]", results) is None),
                              ("ps", re.search(r"PS=[1-9]", results) is None)) if not ok]
    if "PRINTENV=" not in results:
        report.add(FAIL, "key-hidden", f"Agent 的 bash 没跑起来，没法确认看不到密钥：{results[:200] or res.error or res.final_text}"[:400])
    else:
        report.add(FAIL if leaked else OK, "key-hidden",
                   f"Agent 的命令看得到供应商密钥（{'、'.join(leaked)}）：不能把密钥交给 dsh 进程的环境" if leaked
                   else "Agent 的命令看不到供应商密钥（printenv、/proc/*/environ、ps eww）")
    if sys.platform.startswith("linux") and "PRINTENV=" in results:
        # bwrap 起不来时 dsh 不拒绝执行，退到更弱的方式（工作目录以外仍有能写的地方，docs/cloud-runtime.md 第 11 节）：
        # Agent 的命令在 bwrap 里时 1 号进程就是 bwrap
        m = re.search(r"PID1=([^\n]*)", results)
        pid1 = m.group(1).strip() if m else ""
        report.add(OK if pid1.startswith("bwrap") else FAIL, "sandbox",
                   "Agent 的命令在 bwrap 沙箱里" if pid1.startswith("bwrap") else
                   f"Agent 的命令不在 bwrap 里（1 号进程：{pid1 or '看不到'}）：dsh 退到了更弱的沙箱，工作目录以外仍有能写的地方。"
                   "容器里要放开 seccomp 与 /proc 遮挡（deploy/compose.yml），或改用 gVisor")
    if PROVIDER_DONE not in (res.final_text or ""):
        report.add(FAIL, "provider-end", f"这一轮没正常收尾：{res.error or res.final_text}"[:300])


# ================= 云侧隔离（假服务驱动 Agent 的 bash，不调真模型） =================
def _fake_bash(rt: DshRuntime, cwd: Path, state: Path, probe: str, authority: list[str],
               timeout_s: float = 120) -> tuple[str, Any]:
    """让 Agent 的 bash 执行 probe（由假的模型服务下指令），返回（工具结果的文本, 运行结果）。"""
    fake = FakeProvider(probe)
    try:
        prov = Provider(id="matrix-check", name="Matrix check", api="openai-completions", base_url=fake.url)
        preset = ModelPreset(id="check", label="Fake", provider=prov.id, model="fake-1")
        res = rt.run(RunRequest(agent_id="check", context_id="isolation", prompt="跑一下", cwd=cwd, authority=authority,
                                model=preset.run_model(), timeout_s=timeout_s, state_dir=state,
                                llm_provider={"id": prov.id, "config": llm_config(prov, preset)},
                                env={prov.key_env: "sk-matrix-check-" + secrets.token_hex(8)}))
    finally:
        fake.server.shutdown()
    return " ".join(x for r in fake.requests for x in r["results"]), res


def isolation_checks(rt: DshRuntime, report: Report, workdir: Path) -> None:
    """read-isolation / proc-isolation：在一个假的数据目录里摆好别人的东西，让这个执行单元里 Agent 的 bash 去读。
    读得到、看得到就是失败（外层沙箱可用时）；外层不可用、又不是 required 时是“未隔离”的提醒。"""
    iso = rt.isolation()
    report.add(OK if iso["active"] else FAIL if iso["mode"] == "required" else WARN, "isolation",
               f"{iso['mode']}：{iso['note']}"[:400])
    # 不放在 /tmp 下：dsh 的内层沙箱把 /tmp 换成空的 tmpfs，那里的东西没有外层也读不到，检查就失真了。
    # 放在服务进程的主目录下（和真实部署的 /data 一样，内层沙箱里整个可见、只读）
    tag = secrets.token_hex(4)
    base = (Path.home() / ".cache" / f"matrix-dsh-check-{tag}").resolve()
    data = base / "data"
    cwd, state = data / "conversations" / "c1" / "a1", data / "dsh-state" / "seats" / "c1" / "a1"
    marks = {  # 名字 → 路径：都是这个执行单元不该读到的
        "DATA": data / "state.json",                                          # 数据目录里的标记（相当于 state.json）
        "VAULT": data / "vault.key",
        "OTHER_UNIT": data / "tasks" / "task-other" / "report.md",           # 另一个执行单元的工作目录
        "OTHER_SESSION": data / "dsh-state" / "tasks" / "task-other" / ".dsh" / "sessions" / "--x--" / "s-1" / "index.json",
        "OTHER_HOME": data / "agents" / "agent-other" / "notes.md",          # 别人的私有目录
        "SERVER_HOME": base / "marker.txt",                                   # 服务进程的主目录（数据目录以外）
    }
    for name, p in marks.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"MARK-{name}-{tag}\n", encoding="utf-8")
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / "mine.txt").write_text("mine\n", encoding="utf-8")
    shared = Path.home() / ".dsh"   # 共用的 ~/.dsh（旧会话与 dsh 自己的凭据）：在就看能不能列出来
    lines = [f"cat {json.dumps(str(p))} >/dev/null 2>&1 && echo {n}=READ || echo {n}=blocked" for n, p in marks.items()]
    lines.append("cat mine.txt >/dev/null 2>&1 && echo MINE=READ || echo MINE=blocked")
    if shared.is_dir():
        lines.append(f"ls -A {json.dumps(str(shared))} >/dev/null 2>&1 && echo SHARED_DSH=READ || echo SHARED_DSH=blocked")
    # 沙箱之外的进程：一个带标记参数与标记环境变量的进程（[x] 让 grep 不匹配到这条命令自己）
    arg, secret = f"matrix-check-proc-{tag}", f"matrix-check-env-{tag}"
    lines += [f"echo PROC_SEEN=$(grep -l '{arg[:-1]}[{arg[-1]}]' /proc/[0-9]*/cmdline 2>/dev/null | wc -l)",
              f"echo ENV_SEEN=$(cat /proc/[0-9]*/environ 2>/dev/null | tr '\\0' '\\n' | grep -c '{secret[:-1]}[{secret[-1]}]')",
              "echo PROCS=$(ls /proc | grep -c '^[0-9]')"]
    outside = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", arg],
                               env={**os.environ, "MATRIX_CHECK_PROC_ENV": secret})
    try:
        results, res = _fake_bash(rt, cwd, state, "; ".join(lines), ["shell.exec", "file.write"])
    finally:
        outside.kill()
        shutil.rmtree(base, ignore_errors=True)
    if "MINE=" not in results:
        why = res.error or res.final_text or "没有输出"
        status = FAIL if iso["active"] or iso["mode"] == "required" else WARN
        report.add(status, "read-isolation", f"Agent 的 bash 没跑起来，没法检查：{why}"[:400])
        report.add(status, "proc-isolation", "同上，没法检查")
        return
    seen = dict(re.findall(r"(\w+)=(READ|blocked|\d+)", results))
    readable = [n for n in [*marks, "SHARED_DSH"] if seen.get(n) == "READ"]
    if seen.get("MINE") != "READ":
        report.add(FAIL, "read-isolation", "Agent 的命令连自己工作目录里的文件都读不到：探针本身不对")
    elif not iso["active"]:
        report.add(FAIL if iso["mode"] == "required" else WARN, "read-isolation",
                   f"未隔离：Agent 的命令读得到 {'、'.join(readable) or '（这次没读到，但没有外层沙箱）'}（{iso['note']}）"[:400])
    else:
        report.add(FAIL if readable else OK, "read-isolation",
                   f"外层沙箱里读得到不该读的：{'、'.join(readable)}" if readable else
                   "数据目录的标记、vault.key、别的执行单元的工作目录与 dsh 会话、别人的私有目录、服务进程的主目录"
                   + ("、共用的 ~/.dsh" if shared.is_dir() else "") + "都读不到；自己的工作目录照常可读")
    procs = [k for k in ("PROC_SEEN", "ENV_SEEN") if seen.get(k, "0") != "0"]
    detail = (f"看得到沙箱之外的进程（{'、'.join(procs)}，共 {seen.get('PROCS', '?')} 个进程）" if procs else
              f"看不到沙箱之外的进程与它们的环境变量（Agent 的命令只看得到 {seen.get('PROCS', '?')} 个进程）")
    report.add((FAIL if iso["active"] else WARN) if procs else OK, "proc-isolation", detail)


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

    def __init__(self, rt: DshRuntime, root: Path, timeout_s: float, model: dict[str, str] | None = None,
                 sandbox: str = "off", listen: str = "127.0.0.1", port: int = 0):
        import uvicorn

        from . import auth
        from .connector_catalog import ConnectorCatalog
        from .engine import Network
        from .server import create_app

        self.timeout_s = timeout_s
        self.mcp = EchoMCP()
        catalog = root / "connectors.json"
        catalog.write_text(json.dumps({"connectors": [{"id": "smoke", "name": "冒烟", "url": self.mcp.url}]}), encoding="utf-8")
        port = port or _free_port()
        (root / "data").mkdir()
        from .model_catalog import ModelCatalog
        from .sandboxes import provider_named
        # 沙箱方式下 isolation、resource-limit 由假模型驱动（命令一定被执行，不看真模型愿不愿意）：平台供应商指向假服务，
        # 只有探针用的 Agent 选它；Lead / Helper 照旧用真模型（给了 --model 时是目录里的 live 一项）
        self.fake: FakeProvider | None = None
        models = None
        if sandbox != "off":
            self.fake = FakeProvider("true", host=listen)
            presets = [{"id": "probe", "label": "Fake", "provider": "matrix-check", "model": "fake-1"}]
            if model:
                presets.append({"id": "live", "label": "Live", "provider": model["provider"], "model": model["model"]})
            (root / "models.json").write_text(json.dumps({"models": presets, "providers": [
                {"id": "matrix-check", "name": "Matrix check", "api": "openai-completions", "base_url": self.fake.url}]}),
                encoding="utf-8")
            models = ModelCatalog(root / "models.json")
        # docker 后端：沙箱在自己的网络里，连回来的地址是管理器配置里的 connect_url，服务要监听在那个网络能到的地址上
        self.net = Network(rt, root / "data", tool_url=f"http://{listen}:{port}/internal/tools", seed=False,
                           connectors=ConnectorCatalog(catalog), sandbox=provider_named(sandbox), models=models,
                           sandbox_url=f"ws://{listen}:{port}/api/sandbox/connect")
        self.sandbox = sandbox
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.net), host=listen, port=port, log_level="warning"))
        threading.Thread(target=self.server.run, daemon=True).start()
        while not self.server.started:
            time.sleep(0.05)
        self.uid = auth.register(self.net.store, "smoke@matrix.local", secrets.token_urlsafe(18))[0].id
        mk = self.net.create_agent
        pick = {"model_preset": "live"} if models is not None and model else {"model": model}
        self.lead = mk("Lead", "冒烟检查：主执行者", [], ["delegate", "file.write", "shell.exec", "connector.use"],
                       actor=self.uid, **pick)
        self.helper = mk("Helper", "冒烟检查：被委托的成员", [], ["file.write", "shell.exec"] if sandbox != "off" else [],
                         actor=self.uid, **pick)
        if self.fake is not None:
            from .engine import _provider_vault
            self.net.vault.put(_provider_vault("matrix-check"), {"api_key": "sk-matrix-check-" + secrets.token_hex(8)})
        self.project = self.net.create_project("dsh 冒烟检查", "", [self.lead.id, self.helper.id], actor=self.uid)
        self.chat = self.net.project_chat(self.project.id, actor=self.uid)
        # 审批检查写的“工作目录以外”：不能放在 /tmp 下。dsh 的内层沙箱把 /tmp 换成空的 tmpfs，那里的路径在 Agent 的 bash 里
        # 不存在，写入报 ENOENT 而不是只读，dsh 不当它是沙箱拒绝、不发起升权，这一项就时好时坏（2026-11-11 在 runc 与 gVisor 下都复现）。
        # 主目录下的路径在沙箱里可见但只读：写入报 EROFS，稳定地走到升权申请；批准后写在主目录里也写得成。
        cache = Path.home() / ".cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.outside = Path(tempfile.mkdtemp(prefix="matrix-dsh-check-outside-", dir=cache))

    def probe(self, command: str) -> str:
        """假模型驱动：新建一个用假供应商的 Agent，在它的私聊里让它的 bash 执行 command，返回工具结果（假服务收到的）。
        每次新建 Agent：假服务只在会话里还没有工具结果时下指令。"""
        assert self.fake is not None
        a = self.net.create_agent(f"Probe{secrets.token_hex(3)}", "冒烟检查：假模型驱动的探针", [],
                                  ["shell.exec", "file.write"], model_preset="probe", actor=self.uid)
        self.fake.probe, n = command, len(self.fake.requests)
        conv = self.net.direct_chat(a.id, actor=self.uid)
        self.net.send_message(conv.id, "跑一下", actor=self.uid)
        self.net.wait_quiet(self.timeout_s)
        reqs = self.fake.requests[n:]
        if not reqs:
            fails = [m.text for m in conv.messages if m.role == "system"]
            raise RuntimeError("假模型没收到请求" + (f"：{fails[-1][:200]}" if fails else ""))
        return " ".join(x for r in reqs for x in r["results"])

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
            for q in self.net.pending_questions(None):  # 提问：有选项选最后一个，没有就写一句
                if q.id not in seen:
                    seen[q.id] = q
                    self.net.answer_question(q.id, [{"id": i["id"], "selected": [i["options"][-1]["label"]]}
                                                    if i["options"] else {"id": i["id"], "custom": "冒烟检查"}
                                                    for i in q.items])
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
        # 云侧不审批（不变量 14）：工作目录以外写不进，升权申请没有应答者（dsh 回 unavailable），网络也不建审批记录。
        # 让模型去申请：万一覆盖层又带上了应答者，这里会自动批准，文件写出来或有了记录就变红
        target = live.outside / f"smoke-{nonce()}.txt"
        text, _ = live.ask(f"用 bash 执行 `echo hi > {target}`。这个路径在工作目录以外，沙箱会拒绝；"
                           "被拒绝时按提示带上理由申请升权，重试同一条命令一次；还是不行就停下。完成后只回复 DONE。",
                           approve=True)
        records = [a for a in live.net.store.approvals.values() if a.kind not in ("connector", "connector_write")]
        if target.exists():
            return FAIL, "写到了工作目录以外：沙箱没起作用，或升权申请被批准了"
        if records:
            return FAIL, f"产生了审批记录（{len(records)} 条）：云侧不该再审批，覆盖层不该有应答者"
        return OK, "工作目录以外写不进，也没有产生审批记录（云侧不审批）"

    def question() -> tuple[str, str]:
        a, b = f"甲-{nonce()}", f"乙-{nonce()}"
        text, asked = live.ask(f"冒烟检查：用 ask_user_question 问我一个问题“选哪个？”，两个选项分别是 {a} 和 {b}；"
                               "拿到回答后只回复我选的那个选项的原文。")
        questions = [x for x in asked if type(x).__name__ == "Question"]
        if not questions:
            return FAIL, f"没有出现提问（提问工具或应答者没装上）：{text[:120]!r}"
        return (OK, "提问工具、应答者、网络转给人、回答回到原来的调用都通") if b in text \
            else (FAIL, f"问了，但回答没回到模型：{text[:120]!r}")

    def home() -> tuple[str, str]:
        code = nonce()
        text, _ = live.ask(f"用 bash 执行 `echo HOME-{code} > agent-home/smoke-note.md`，然后只回复 DONE。")
        f = live.net.agent_home(live.lead.id) / "smoke-note.md"
        if not f.is_file() or code not in f.read_text(encoding="utf-8"):
            return FAIL, f"私有目录没写回：{text[:120]!r}"
        labels = json.loads((live.net.store.root / "flow" / f"{live.lead.id}.json").read_text(encoding="utf-8"))
        return (OK, "私有目录写回、记了来源标签") if "smoke-note.md" in labels else (FAIL, "写回了但没有来源标签")

    def send_file() -> tuple[str, str]:
        code = nonce()
        text, _ = live.ask(f"用 bash 执行 `echo FILE-{code} > smoke-file.txt`，再调用网络工具 send_file 把 smoke-file.txt 发出来，"
                           "然后只回复 SENT。")
        att = [a for m in live.chat.messages if m.role == "agent" for a in m.attachments if a["name"] == "smoke-file.txt"]
        if not att:
            return FAIL, f"没有附上文件：{text[:120]!r}"
        data = (live.net.store.attachment_dir(live.chat.id) / att[-1]["sha"]).read_text(encoding="utf-8")
        return (OK, "send_file 回读工作目录里的文件") if code in data else (FAIL, f"附件内容不对：{data[:80]!r}")

    def deliver() -> tuple[str, str]:
        # 委托交付的产物进委托方的工作目录（沙箱方式下读执行者的检查点，作为材料放进委托方的 deliveries/）
        code = nonce()
        text, _ = live.ask(f"用网络工具 delegate 委托 Helper，authority 授予 [\"file.write\", \"shell.exec\"]：让它用 bash 执行 `echo D-{code} > smoke-deliver.txt`，"
                           "然后只回复 DONE。等它交付后验收，再用 bash 执行 `cat deliveries/*/smoke-deliver.txt`，只回复命令的输出。")
        tasks = [t for t in live.net.store.tasks.values() if t.delegatee_id == live.helper.id
                 and "smoke-deliver.txt" in (t.result or {}).get("files", [])]
        if not tasks:
            return FAIL, f"交付里没有产物 smoke-deliver.txt：{text[:120]!r}"
        return (OK, "执行者的产物随交付进了委托方的工作目录") if f"D-{code}" in text \
            else (FAIL, f"产物交了，但委托方读不到：{text[:120]!r}")

    def restore() -> tuple[str, str]:
        # 沙箱重建：让 Agent 记住编号、写产物，再把这个执行单元的沙箱连目录销毁，下一轮从检查点恢复
        code = nonce()
        live.ask(f"记住这个编号：R-{code}。用 bash 执行 `echo R-{code} > smoke-restore.txt`，然后只回复 OK。")
        unit = f"seat:{live.chat.id}/{live.lead.id}"
        live.net.sandbox.destroy(unit)
        text, _ = live.ask("先用 bash 执行 `cat smoke-restore.txt`。然后回复：上一轮让你记住的编号，以及文件里的内容。")
        ck = live.net.checkpoints.load(unit)
        if f"R-{code}" not in text:
            return FAIL, f"重建后不记得编号或产物没恢复：{text[:160]!r}"
        return OK, f"沙箱销毁后从检查点恢复：记得编号、产物在（会话包 {len(ck.sessions) if ck else 0} 份）"

    def isolation() -> tuple[str, str]:
        # 阶段 0 的读隔离、进程隔离在沙箱里照样成立：假模型让 Agent 的 bash 读数据目录与别的单元、看别的进程
        data = live.net.store.root
        tag = nonce()
        marks = {"DATA": data / "state.json", "VAULT": data / "vault.key", "HOME_OTHER": data / "agents" / "x" / "n.md",
                 "OTHER_UNIT": data / "tasks" / "task-other" / "r.md", "SERVER_HOME": Path.home() / f".matrix-check-{tag}"}
        for p in marks.values():
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                p.write_text(f"MARK-{tag}", encoding="utf-8")
        arg = f"matrix-check-proc-{tag}"
        outside = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", arg])
        probe = "; ".join([f"cat {json.dumps(str(p))} >/dev/null 2>&1 && echo {n}=READ || echo {n}=blocked"
                           for n, p in marks.items()] +
                          [f"echo PROC_SEEN=$(grep -l '{arg[:-1]}[{arg[-1]}]' /proc/[0-9]*/cmdline 2>/dev/null | wc -l)",
                           "echo INNER=$(tr '\\0' ' ' </proc/1/cmdline | cut -d' ' -f1 | xargs basename)"])
        try:
            text = live.probe(probe)
        finally:
            outside.kill()
            marks["SERVER_HOME"].unlink(missing_ok=True)
        seen = dict(re.findall(r"(\w+)=(READ|blocked|\d+)", text))
        if not any(k in seen for k in marks):
            return FAIL, f"探针没跑起来：{text[:200]!r}"
        bad = [k for k in marks if seen.get(k) == "READ"] + (["PROC"] if seen.get("PROC_SEEN", "0") != "0" else [])
        inner = re.search(r"INNER=([\w.-]+)", text)
        if bad:
            return FAIL, f"沙箱里读得到 / 看得到：{'、'.join(bad)}"
        if not inner or inner.group(1) != "bwrap":
            return FAIL, (f"Agent 的命令不在 dsh 的内层 bwrap 里（1 号进程：{inner.group(1) if inner else '看不到'}）："
                          "dsh 退到了更弱的沙箱。runc 档要放开 seccomp 与 /proc 遮挡，或改用 gVisor")
        return OK, ("数据目录、vault.key、别人的私有目录、别的执行单元、服务主目录都读不到，看不到沙箱之外的进程；"
                    "Agent 的命令在 dsh 的内层 bwrap 里")

    def resource_limit() -> tuple[str, str]:
        # 配额（第 8 节验收表的 resource-limit）：超额申请内存、无限起进程，被沙箱的上限挡住；之后别的执行单元照常运行
        import base64
        mem_py = 'b = bytearray(3 << 30)\nb[::4096] = b"x" * len(b[::4096])\nprint("MEM=allocated")\n'
        pid_py = ('import subprocess as s\nps = []\ntry:\n    for _ in range(900):\n        ps.append(s.Popen(["sleep", "30"]))\n'
                  'except OSError:\n    pass\nprint("PIDS=" + ("limited" if len(ps) < 900 else "unlimited"), len(ps))\n'
                  'for p in ps:\n    p.kill()\n')
        b64 = lambda t: base64.b64encode(t.encode()).decode()  # noqa: E731 —— 一个词，模型照抄不会抄错
        probe = (f"echo {b64(mem_py)} | base64 -d | python3 2>/dev/null; echo MEMRC=$?; "
                 f"echo {b64(pid_py)} | base64 -d | python3 2>&1 | tail -1")
        text = live.probe(probe)
        mem, pids = re.search(r"MEMRC=(\d+)", text), re.search(r"PIDS=(\w+)", text)
        if not mem or not pids:
            return FAIL, f"探针没跑起来：{text[:200]!r}"
        quota = bool(live.net.sandbox_report().get("quota"))
        limited = mem.group(1) != "0" and not re.search(r"(?m)^MEM=allocated", text) and pids.group(1) == "limited"
        code = nonce()   # 别的执行单元不受影响：另一个执行单元照常跑完一条命令
        others_ok = f"ALIVE-{code}" in live.probe(f"echo ALIVE-{code}")
        detail = f"内存申请退出码 {mem.group(1)}，进程数 {pids.group(1)}；之后别的执行单元{'照常' if others_ok else '没跑通'}"
        if not quota:
            return WARN, f"这个后端没有配额：{detail}"
        return (OK, f"超额内存与无限起进程都被挡住：{detail}") if limited and others_ok else (FAIL, detail)

    out = [("reply", reply), ("resume", resume), ("skill", skill), ("delegate", delegate),
           ("connector", connector), ("approval", approval), ("question", question)]
    if live.sandbox != "off":
        out += [("home", home), ("send-file", send_file), ("deliver", deliver), ("restore", restore),
                ("isolation", isolation), ("resource-limit", resource_limit)]
    return out


def live_checks(rt: DshRuntime, report: Report, root: Path, timeout_s: float, only: set[str],
                model: dict[str, str] | None = None, sandbox: str = "off", listen: str = "127.0.0.1",
                port: int = 0) -> None:
    live = Live(rt, root, timeout_s, model, sandbox, listen, port)
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
        if live.net.sandbox is not None:
            for unit in list(live.net.store.sandboxes):
                live.net.sandbox.stop(unit)
        live.server.should_exit = True
        live.mcp.server.shutdown()
        if live.fake is not None:
            live.fake.server.shutdown()
        shutil.rmtree(live.outside, ignore_errors=True)


def main() -> None:
    from . import utf8_stdio
    utf8_stdio()
    p = argparse.ArgumentParser(prog="matrix-dsh-check", description="对真实的 dsh 跑一遍冒烟检查（升级 dsh 前后用）")
    p.add_argument("--dsh-bin", default="dsh")
    p.add_argument("--profile", default="headless")
    p.add_argument("--live", action="store_true", help="实际跑几轮（要调模型）")
    p.add_argument("--only", default="", help="只跑这些实际检查，逗号分隔：reply,resume,skill,delegate,connector,approval,question；"
                        "--sandbox 时另有 home,send-file,deliver,restore,isolation,resource-limit")
    p.add_argument("--model", default="", help="实际检查的 Agent 用这个模型，provider/model（默认用 dsh 的默认模型），"
                   "如本机用账号登录时 deepseek-account/deepseek-flash")
    p.add_argument("--timeout", type=float, default=300, help="每项实际检查最多等多少秒")
    p.add_argument("--keep", action="store_true", help="保留临时数据目录")
    p.add_argument("--listen", default="127.0.0.1", help="--live 的临时服务监听在哪（docker 后端：沙箱网络能到的地址，"
                   "如沙箱网络的网关；和管理器配置的 connect_url 一致）")
    p.add_argument("--port", type=int, default=0, help="--live 的临时服务端口（默认随机；docker 后端要和 connect_url 一致）")
    p.add_argument("--root", default="", help="临时数据放在哪个目录下（docker 后端：数据目录要落在管理器允许的前缀下）")
    p.add_argument("--sandbox", choices=["off", "local", "docker"], default="off",
                   help="--live 用云侧执行面的沙箱方式跑（local 后端：本机子进程 + 外层 bwrap），另加 home、send-file、"
                        "restore（沙箱销毁后从检查点恢复）、isolation（沙箱里的读隔离与进程隔离）")
    p.add_argument("--isolation", choices=["required", "preferred", "off"], default="",
                   help="云侧外层沙箱的档位（默认读 MATRIX_CLOUD_ISOLATION，没有就是 preferred）")
    args = p.parse_args()

    if shutil.which(args.dsh_bin) is None and not Path(args.dsh_bin).is_file():
        raise SystemExit(f"找不到 dsh：{args.dsh_bin}")
    rt = DshRuntime(args.dsh_bin, args.profile, isolation=args.isolation or None)
    if args.root:
        Path(args.root).mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="matrix-dsh-check-", dir=args.root or None))
    report = Report()
    ok = static_checks(rt, report, root)
    if ok:
        provider_checks(rt, report, root)
        isolation_checks(rt, report, root)
    if args.live and ok:
        provider, _, name = args.model.partition("/")
        live_checks(rt, report, root, args.timeout, {x.strip() for x in args.only.split(",") if x.strip()},
                    {"provider": provider, "model": name} if name else None, args.sandbox, args.listen, args.port)
    elif args.live:
        print("静态检查没过，不跑实际检查。")
    if report.failed or args.keep:
        print(f"\n临时数据：{root}（各轮的 dsh 事件在 data/conversations/*/ 下的 dsh-events.jsonl）")
    else:
        shutil.rmtree(root, ignore_errors=True)
    verdict = "有检查没过" if report.failed else "没有失败，但有提醒" if any(s == WARN for s, _, _ in report.rows) else "全部通过"
    print(f"\n{verdict}：" + "  ".join(f"{MARK[s]}{n}" for s, n, _ in report.rows))
    sys.exit(1 if report.failed else 0)


if __name__ == "__main__":
    main()
