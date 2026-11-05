"""插件用模型（docs/plugin-hooks.md 第 14 节，不变量 24）：托管的插件服务经 MCP sampling 请网络调模型。
清单要声明 service.model（安装确认卡写明、记在主持人的用量里）；网络用部署方模型目录的默认模型（平台配的供应商），
限每次生成长度与每天次数；服务进程本身仍然不拿密钥、不联网；没声明的、停用的一律拒绝。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from matrix import plugins as pluginfs, sandbox
from matrix.engine import NeedsConfirm, Network, NetworkError
from matrix.llm import LlmError, complete
from matrix.model_catalog import ModelCatalog, ModelPreset, Provider
from matrix.plugin_check import lint
from matrix.runtime import ScriptedRuntime
from tests.test_users import users

KEY = "sk-plugin-model-" + "x" * 16

# 一个最小的 stdio MCP 服务：工具 ask 把问题经 sampling 交给网络，回模型的回答；env 列出自己的环境变量名
SERVICE = r'''
import json, os, sys

def send(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)

def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None

while True:
    m = read()
    if m is None:
        break
    if m.get("id") is None:
        continue
    method = m["method"]
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
              "serverInfo": {"name": "asker", "version": "1"}, "_sampling": "sampling" in m["params"]["capabilities"]}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": [{"name": "ask"}, {"name": "env"}]}})
    elif method == "tools/call" and m["params"]["name"] == "env":
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": ",".join(sorted(os.environ))}]}})
    elif method == "tools/call":
        a = m["params"]["arguments"]
        req = {"messages": [{"role": "user", "content": {"type": "text", "text": a["q"]}}],
               "systemPrompt": "只回答一句", "maxTokens": a.get("max", 99999)}
        req.update(a.get("extra") or {})
        send({"jsonrpc": "2.0", "id": "s1", "method": "sampling/createMessage", "params": req})
        r = read()
        if "error" in r:
            out = {"content": [{"type": "text", "text": "拒绝：" + r["error"]["message"]}], "isError": True}
        else:
            out = {"content": [{"type": "text", "text": r["result"]["content"]["text"] + "|" + r["result"]["stopReason"]}]}
        send({"jsonrpc": "2.0", "id": m["id"], "result": out})
'''


def manifest(model: dict | None = None) -> dict:
    svc = {"command": "python", "args": ["{plugin}/service/asker.py"], "tools": {"ask": "read", "env": "read"}}
    if model is not None:
        svc["model"] = model
    return {"name": "asker", "version": "1.0.0", "title": "提问器", "description": "测试用：经网络调模型", "service": svc}


class FakeOpenAI(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOpenAI.calls.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                 "x-api-key": self.headers.get("x-api-key"), "body": body})
        if self.path.endswith("/messages"):
            out = {"content": [{"type": "text", "text": "答：" + body["messages"][-1]["content"]}],
                   "stop_reason": "end_turn", "usage": {"input_tokens": 7, "output_tokens": 3}}
        else:
            out = {"choices": [{"message": {"content": "答：" + body["messages"][-1]["content"]}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}}
        data = json.dumps(out, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a) -> None:
        pass


@pytest.fixture
def llm():
    FakeOpenAI.calls = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def setup(tmp_path: Path, base_url: str, model: dict | None = None, provider: str = "plat"):
    pkg = tmp_path / "catalog" / "asker"
    (pkg / "service").mkdir(parents=True)
    (pkg / "plugin.json").write_text(json.dumps(manifest(model), ensure_ascii=False), encoding="utf-8")
    (pkg / "service" / "asker.py").write_text(SERVICE, encoding="utf-8")
    (tmp_path / "catalog" / "plugins.json").write_text('{"plugins": ["asker"]}', encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "models.json").write_text(json.dumps({
        "default": "flash",
        "models": [{"id": "flash", "label": "Flash", "provider": provider, "model": "flash-1", "max_tokens": 4000}],
        "providers": [{"id": "plat", "name": "平台", "api": "openai-completions", "base_url": base_url + "/v1"}]}),
        encoding="utf-8")
    net = Network(ScriptedRuntime(lambda call: "ok"), data, seed=False,
                  plugins=pluginfs.PluginCatalog(tmp_path / "catalog" / "plugins.json"))
    net.vault.put("model-provider-plat", {"api_key": KEY})
    host, = users(net, "host@x.io")
    g = net.create_project("测试", "", [], actor=host)
    return net, host, g


def ask(net: Network, g, q: str, **args) -> tuple[str, bool]:
    m = net._manifest(g, "asker")
    return net._service_call(g, "asker", m, "ask", {"q": q, **args}, by_user=g.owner_id)


USE = {"why": "把交付说明提炼成记忆", "max_tokens": 1000, "daily_calls": 2}


# ================= 清单 =================
@pytest.mark.parametrize("model, why", [
    ({"max_tokens": 10}, "why"),
    ({"why": "x", "max_tokens": 0}, "max_tokens"),
    ({"why": "x", "max_tokens": 99999}, "max_tokens"),
    ({"why": "x", "daily_calls": 5000}, "daily_calls"),
    ("yes", "必须是对象"),
])
def test_manifest_model_is_checked(model, why: str):
    with pytest.raises(pluginfs.PluginError, match=why):
        pluginfs.parse_manifest(manifest(model))


def test_remote_service_cannot_declare_model():
    """远程服务在作者那边运行：网络不替它调模型。"""
    m = manifest(USE)
    m["service"] = {"url": "https://x.io/mcp", "tools": {"ask": "read"}, "model": USE}
    with pytest.raises(pluginfs.PluginError, match="只给托管服务"):
        pluginfs.parse_manifest(m)


def test_lint_matches_declaration_with_code():
    files = {Path("service/asker.py"): SERVICE.encode()}
    assert any("没有 service.model" in w for w in lint(files, pluginfs.parse_manifest(manifest())))
    assert not any("service.model" in w for w in lint(files, pluginfs.parse_manifest(manifest(USE))))
    quiet = {Path("service/asker.py"): b"print('hi')"}
    assert any("没看到 sampling" in w for w in lint(quiet, pluginfs.parse_manifest(manifest(USE))))


# ================= 网络替插件调模型 =================
def test_install_card_names_model_use_even_from_catalog(tmp_path: Path, llm):
    """花的是主持人的用量：部署方目录里的插件也要主持人确认，卡上写明用来做什么、上限、记谁的账。"""
    net, host, g = setup(tmp_path, llm, USE)
    with pytest.raises(NeedsConfirm) as e:
        net.install_plugin(g.id, "asker", actor=host)
    assert any("把交付说明提炼成记忆" in x and "每天最多 2 次" in x and "主持人" in x for x in e.value.items)
    assert "asker" not in g.plugins
    net.install_plugin(g.id, "asker", confirm=True, actor=host)
    assert g.plugins["asker"]["runs"] in ("host", "sandbox")  # 有沙箱时目录里的托管插件也进沙箱


def test_service_samples_through_network(tmp_path: Path, llm):
    """服务经 sampling 拿到回答；网络用平台供应商的地址与密钥，生成长度取清单与模型上限里小的；
    用量台账记在主持人名下（场合 plugin），项目日志只记次数与 token，不记内容。服务进程里没有密钥。"""
    net, host, g = setup(tmp_path, llm, USE)
    net.install_plugin(g.id, "asker", confirm=True, actor=host)
    text, err = ask(net, g, "今天周几")
    assert not err and text == "答：今天周几|endTurn"
    call, = FakeOpenAI.calls
    assert call["path"] == "/v1/chat/completions" and call["auth"] == f"Bearer {KEY}"
    assert call["body"]["model"] == "flash-1" and call["body"]["max_tokens"] == 1000  # 请求的 99999 → 清单的 1000
    assert call["body"]["messages"][0] == {"role": "system", "content": "只回答一句"}
    row, = [e for e in net.usage.read() if e["venue"] == "plugin"]
    assert row["user"] == host and row["agent"] is None and row["project"] == g.id and row["runtime"] == "plugin:asker"
    assert row["ok"] and row["usage"]["total_tokens"] == 17 and row["model"] == "plat / flash-1"
    log = (net.store.project_dir(g.id).parent / "log.jsonl").read_text(encoding="utf-8")
    assert '"plugin.model.call"' in log and "今天周几" not in log
    names, _ = net._service_call(g, "asker", net._manifest(g, "asker"), "env", {}, by_user=host)
    assert "MATRIX_LLM" not in names and KEY not in names


def test_daily_cap_and_bad_requests_are_refused(tmp_path: Path, llm):
    """每天的次数到了就拒绝（拒绝不算一次）；非文本内容、includeContext 拒绝，服务拿到错误、不会卡住。"""
    net, host, g = setup(tmp_path, llm, USE)
    net.install_plugin(g.id, "asker", confirm=True, actor=host)
    text, err = ask(net, g, "x", extra={"includeContext": "allServers"})
    assert err and "includeContext" in text
    text, err = ask(net, g, "x", extra={"messages": [{"role": "user", "content": {"type": "image", "data": ""}}]})
    assert err and "只支持文本" in text
    assert not ask(net, g, "一")[1] and not ask(net, g, "二")[1]
    text, err = ask(net, g, "三")
    assert err and "今天已经调了 2 次" in text
    assert len(FakeOpenAI.calls) == 2
    net._plugin_model_calls.clear()  # 进程重启：从台账重新数，仍然是 2
    assert "今天已经调了 2 次" in ask(net, g, "四")[0]


@pytest.mark.skipif(not sandbox.available()[0], reason="没有可用的沙箱（bubblewrap）")
def test_sandboxed_upload_samples_without_network(tmp_path: Path, llm):
    """主持人自己上传的托管插件在沙箱里、不能联网，照样能经网络用模型：模型请求走的是它和网络之间的 stdio。"""
    net, host, g = setup(tmp_path, llm, USE)
    pkg = tmp_path / "catalog" / "asker"
    entries = [(f"asker/{p.relative_to(pkg).as_posix()}", p.read_bytes()) for p in pkg.rglob("*") if p.is_file()]
    (tmp_path / "catalog" / "plugins.json").write_text('{"plugins": []}', encoding="utf-8")
    net.upload_plugin(g.id, entries, confirm=True, actor=host)
    assert g.plugins["asker"]["runs"] == "sandbox"
    text, err = ask(net, g, "沙箱里")
    assert not err and text == "答：沙箱里|endTurn"


def test_undeclared_or_disabled_plugin_cannot_sample(tmp_path: Path, llm):
    """清单没声明的：网络在 initialize 时不声明 sampling，请求回错误；停用后挂点与工具都没有了。"""
    net, host, g = setup(tmp_path, llm, None)
    net.install_plugin(g.id, "asker", actor=host)
    text, err = ask(net, g, "x")
    assert err and "没有在清单里声明 service.model" in text and not FakeOpenAI.calls
    net.set_plugin_enabled(g.id, "asker", False, actor=host)
    with pytest.raises(NetworkError, match="已停用"):
        ask(net, g, "x")


def test_model_must_come_from_a_platform_provider(tmp_path: Path, llm):
    """默认模型用的是 dsh 里自己配置的供应商时，网络不知道地址与密钥：拒绝并说清楚怎么配。"""
    net, host, g = setup(tmp_path, llm, USE, provider="deepseek-official")
    net.install_plugin(g.id, "asker", confirm=True, actor=host)
    text, err = ask(net, g, "x")
    assert err and "平台配的" in text and not FakeOpenAI.calls


# ================= 模型客户端 =================
def test_anthropic_messages_protocol(llm):
    prov = Provider("a", "A", "anthropic-messages", llm)
    out = complete(prov, ModelPreset("m", "M", "a", "claude-x"), "k1", system="s",
                   messages=[{"role": "user", "text": "hi"}], max_tokens=50, stop=["。"])
    assert out.text == "答：hi" and out.stop_reason == "endTurn" and out.usage == {"input_tokens": 7, "output_tokens": 3}
    call, = FakeOpenAI.calls
    assert call["path"] == "/v1/messages" and call["x-api-key"] == "k1" and call["body"]["system"] == "s"
    assert call["body"]["stop_sequences"] == ["。"]


def test_unsupported_protocol_and_unreachable_provider():
    with pytest.raises(LlmError, match="openai-responses"):
        complete(Provider("r", "R", "openai-responses", "http://127.0.0.1:9"), ModelPreset("m", "M", "r", "x"), None,
                 system="", messages=[{"role": "user", "text": "x"}], max_tokens=5)
    with pytest.raises(LlmError, match="连不上"):
        complete(Provider("o", "O", "openai-completions", "http://127.0.0.1:9"), ModelPreset("m", "M", "o", "x"), "k",
                 system="", messages=[{"role": "user", "text": "x"}], max_tokens=5, timeout=3)
