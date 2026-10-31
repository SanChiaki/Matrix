"""插件服务与挂点（docs/plugin-hooks.md，不变量 24）：清单里的 service 与 hooks、只从部署方目录安装、托管的 stdio MCP 进程、
插件工具经网络工具通道、mount / context / after 三种挂点、信息流，以及示例插件「项目 Wiki」走通一遍。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from matrix import plugins as pluginfs
from matrix.engine import ExecContext, Network, NetworkError
from matrix.models import MCP_PREFIX
from matrix.plugin_check import lint, load
from matrix.plugin_service import ServiceError, ServiceHost, StdioClient, resolve_command, service_env
from matrix.runtime import ScriptCall, ScriptedRuntime
from matrix.scripted import scenario_handler
from tests.test_users import users

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "examples" / "plugins.json"
WIKI = ROOT / "examples" / "plugins" / "llm-wiki"
MATERIALS = ROOT / "examples" / "llm-wiki" / "materials"


def wiki_manifest(**over) -> dict:
    m = json.loads((WIKI / "plugin.json").read_text(encoding="utf-8"))
    m.update(over)
    return m


def setup(tmp_path: Path, handler=scenario_handler, authority=("file.write",)):
    seen: list[dict] = []

    def spy(call: ScriptCall) -> str:  # 记下每一轮工作目录里挂了什么（运行后会丢弃）
        wiki = call.cwd / "plugins" / "llm-wiki" / "wiki"
        seen.append({"agent": call.agent_id, "mounted": sorted(p.name for p in wiki.glob("*")) if wiki.is_dir() else None,
                     "tools": list(call.tool_names), "prompt": call.prompt})
        return handler(call)
    rt = ScriptedRuntime(spy)
    net = Network(rt, tmp_path / "data", seed=False, plugins=pluginfs.PluginCatalog(CATALOG))
    host, = users(net, "host@x.io")
    net.ensure_assistant(host)
    keeper = net.create_agent("知识管家", "维护项目 Wiki", [], list(authority), actor=host)
    g = net.create_project("白板定价", "新产品定价调研", [keeper.id], actor=host)
    return net, rt, host, keeper, g, seen


def service_data(net: Network, g) -> Path:
    return net.store.plugin_data_dir(g.id, "llm-wiki") / "service"


# ================= 清单 =================
@pytest.mark.parametrize("over, why", [
    ({"service": {"command": "rm -rf /", "tools": {"x": "read"}}}, "command"),
    ({"service": {"command": "python", "tools": {}}}, "service.tools"),
    ({"service": {"command": "python", "tools": {"x": "admin"}}}, "read、write 或 hook"),
    ({"service": {"command": "python", "transport": "http", "tools": {"x": "read"}}}, "stdio"),
    ({"hooks": {"context": [{"tool": "write", "label": "x"}]}}, "read 或 hook 类"),
    ({"hooks": {"context": [{"tool": "nope", "label": "x"}]}}, "不在 service.tools"),
    ({"hooks": {"context": [{"resource": "wiki:///a.md", "tool": "index", "label": "x"}]}}, "二选一"),
    ({"hooks": {"context": [{"resource": "../etc/passwd", "label": "x"}]}}, "资源 URI"),
    ({"hooks": {"context": [{"resource": "wiki:///a.md", "label": "x", "budget": 99999}]}}, "budget"),
    ({"hooks": {"mount": [{"resources": "wiki:///../", "as": "w"}]}}, "资源 URI"),
    ({"service": {"command": "python", "url": "https://x.io/mcp", "tools": {"x": "read"}}}, "二选一"),
    ({"service": {"url": "http://10.0.0.5/mcp", "tools": {"x": "read"}}}, "https"),
    ({"service": {"command": "python", "tools": {"a.b": "read"}}}, "工具名无效"),
    ({"name": "x" * 60}, "最长 64"),
    ({"hooks": {"after": [{"on": "message.added", "notify": {"to": "a", "text": "b"}}]}}, "request.accepted"),
    ({"hooks": {"router": []}}, "只能有"),
])
def test_manifest_rejects_bad_service_and_hooks(over: dict, why: str):
    with pytest.raises(pluginfs.PluginError, match=why):
        pluginfs.parse_manifest(wiki_manifest(**over))


def test_hooks_need_service():
    """mount、context 读的是插件服务的数据目录：没有 service 不能声明；页面申请 service.read 也要有 service。"""
    m = wiki_manifest()
    del m["service"]
    with pytest.raises(pluginfs.PluginError, match="要有 service"):
        pluginfs.parse_manifest(m)
    with pytest.raises(pluginfs.PluginError, match="service"):
        pluginfs.parse_manifest({**m, "hooks": {}})


def test_example_wiki_package_is_valid():
    """示例插件过 matrix-plugin check：服务、挂点、三个技能、页面。"""
    files = load(WIKI)
    m = pluginfs.check_package(files)
    assert m.service is not None and dict(m.service.tools)["write"] == "write"
    assert [c.resource for c in m.contexts] == ["wiki:///wiki/index.md"] and m.mounts[0].as_ == "wiki"
    assert m.afters[0].on == "request.accepted" and m.afters[0].to == "{agent}"
    assert {p.parts[1] for p in files if p.parts[0] == "skills"} == {"wiki-ingest", "wiki-query", "wiki-lint"}
    assert not [w for w in lint(files, m) if "service" not in w]


# ================= 只从部署方目录安装、进程环境 =================
def test_upload_with_service_is_refused(tmp_path: Path):
    """不变量 24：服务端要执行的代码只来自部署方目录；主持人上传的包带 service 一律拒绝。"""
    net, _, host, _, g, _ = setup(tmp_path)
    entries = [(f"llm-wiki/{p.as_posix()}", b) for p, b in load(WIKI).items()]
    with pytest.raises(NetworkError, match="部署方的插件目录"):
        net.upload_plugin(g.id, entries, actor=host)
    assert "llm-wiki" not in g.plugins
    net.install_plugin(g.id, "llm-wiki", actor=host)  # 目录里的同一个包可以装
    assert "llm-wiki" in g.plugins


def test_service_env_has_no_secrets(tmp_path: Path, monkeypatch):
    """不变量 24：插件服务进程不拿任何凭证——环境里只有运行程序必需的变量，HOME 指到它自己的数据目录。"""
    monkeypatch.setenv("MATRIX_VAULT_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    monkeypatch.setenv("MATRIX_TOKEN", "t")
    env = service_env(tmp_path)
    assert not {"MATRIX_VAULT_KEY", "DEEPSEEK_API_KEY", "MATRIX_TOKEN"} & set(env)
    assert env["HOME"] == str(tmp_path) and "PATH" in env
    probe = tmp_path / "probe.py"
    probe.write_text("import json, os, sys\n"
                     "for line in sys.stdin:\n"
                     "    m = json.loads(line)\n"
                     "    if m.get('id') is None: continue\n"
                     "    r = {'tools': [{'name': 'env'}]} if m['method'] == 'tools/list' else {}\n"
                     "    if m['method'] == 'tools/call': r = {'content': [{'type': 'text', 'text': ','.join(sorted(os.environ))}]}\n"
                     "    print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': r}), flush=True)\n", encoding="utf-8")
    c = StdioClient(resolve_command("python", ("{plugin}/probe.py",), tmp_path, tmp_path / "data"), tmp_path / "data")
    c.start()
    try:
        names, _ = c.call("env", {})
    finally:
        c.stop()
    assert "DEEPSEEK_API_KEY" not in names and "MATRIX_VAULT_KEY" not in names and "HOME" in names


def test_service_host_restarts_and_gives_up(tmp_path: Path):
    """进程按 (项目, 插件) 复用，版本换了重启；连续起不来 3 次就暂停，stop（停用再启用）清零。"""
    host = ServiceHost()
    argv = resolve_command("python", ("{plugin}/service/wiki_engine.py", "--root", "{data}"), WIKI, tmp_path)
    a = host.client("p", "w", "1", argv, tmp_path)
    assert host.client("p", "w", "1", argv, tmp_path) is a
    b = host.client("p", "w", "2", argv, tmp_path)
    assert b is not a and not a.alive() and b.alive()
    host.stop("p", "w")
    bad = ["definitely-not-a-program-xyz"]
    for _ in range(3):
        with pytest.raises(ServiceError):
            host.client("p", "bad", "1", bad, tmp_path)
    with pytest.raises(ServiceError, match="已暂停"):
        host.client("p", "bad", "1", bad, tmp_path)
    host.stop("p", "bad")
    with pytest.raises(ServiceError, match="起不来"):
        host.client("p", "bad", "1", bad, tmp_path)
    host.stop_all()


# ================= 工具只在本项目里、写入受权限与信息流约束 =================
def test_service_tools_only_in_project_runs(tmp_path: Path):
    """不变量 24：插件工具只出现在装了它的项目里的运行；助理对话、私聊、别的项目都没有。"""
    net, rt, host, keeper, g, seen = setup(tmp_path, handler=lambda call: "ok")
    net.install_plugin(g.id, "llm-wiki", actor=host)
    net.send_message(net.project_chat(g.id, actor=host).id, "@知识管家 在吗", actor=host, background=False)
    assert MCP_PREFIX + "llm-wiki__search" in seen[-1]["tools"] and MCP_PREFIX + "llm-wiki__write" in seen[-1]["tools"]
    other = net.create_project("别的项目", "", [keeper.id], actor=host)
    net.send_message(net.project_chat(other.id, actor=host).id, "@知识管家 在吗", actor=host, background=False)
    assert MCP_PREFIX + "llm-wiki__search" not in seen[-1]["tools"] and seen[-1]["mounted"] is None
    net.send_message(net.direct_chat(keeper.id, actor=host).id, "在吗", actor=host, background=False)
    assert MCP_PREFIX + "llm-wiki__search" not in seen[-1]["tools"]


def _ctx(net: Network, agent, g, **kw) -> ExecContext:
    return ExecContext(kind="seat", id=f"{net.project_chat(g.id).id}/{agent.id}", agent_id=agent.id,
                       conversation_id=net.project_chat(g.id).id, project_id=g.id,
                       authority=kw.pop("authority", ["file.write"]), constraints=[], depth=0, chain=[agent.id],
                       services=kw.pop("services", True), **kw)


def call(net: Network, ctx: ExecContext, name: str, **args) -> tuple[str, bool]:
    return net.handle_tool(_register(net, ctx), name, args)


def _register(net: Network, ctx: ExecContext) -> str:
    import secrets
    token = secrets.token_urlsafe(8)
    net._contexts[token] = ctx
    return token


def test_write_tools_need_file_write_and_pass_flow(tmp_path: Path):
    """不变量 24、22：写类工具要有 file.write；会话读过越界的来源（例如私下对话）时，写进插件数据等于让项目里
    当时不在场的人看到，网络不放行。读类工具不受影响。白名单以外的工具调不到。"""
    net, _, host, keeper, g, _ = setup(tmp_path, handler=lambda call: "ok")
    guest, = users(net, "guest@x.io")
    net.install_plugin(g.id, "llm-wiki", actor=host)
    net.invite(g.id, "guest@x.io", actor=host)
    net.respond_invitation(g.id, True, actor=guest)  # 有别人的项目
    args = {"topic": "t", "slug": "s", "title": "T", "content": "c"}
    text, err = call(net, _ctx(net, keeper, g, authority=[]), "llm-wiki__add_source", **args)
    assert err and "file.write" in text
    text, err = call(net, _ctx(net, keeper, g, sources=["me"]), "llm-wiki__add_source", **args)
    assert err and "不放行" in text
    text, err = call(net, _ctx(net, keeper, g, sources=["me"]), "llm-wiki__index")
    assert not err and "项目 Wiki 目录" in text
    text, err = call(net, _ctx(net, keeper, g), "llm-wiki__shutdown")
    assert err and "没有开出" in text
    text, err = call(net, _ctx(net, keeper, g, services=False), "llm-wiki__index")
    assert err and "未知工具" in text  # 不在装了它的项目里的运行：没有这个工具
    text, err = call(net, _ctx(net, keeper, g), "llm-wiki__add_source", **args)
    assert not err and "所有参与方都看得到" in text
    log = (net.store.project_dir(g.id).parent / "log.jsonl").read_text(encoding="utf-8")
    assert '"plugin.service.call"' in log and '"add_source"' in log and '"content"' not in log  # 留痕不记参数


def test_page_bridge_reads_only(tmp_path: Path):
    """不变量 20、24：插件页面经桥只能调只读工具，写类工具一律拒绝。"""
    net, _, host, _, g, _ = setup(tmp_path, handler=lambda call: "ok")
    net.install_plugin(g.id, "llm-wiki", actor=host)
    assert "项目 Wiki 目录" in net.plugin_bridge(g.id, "llm-wiki", "service.call", {"tool": "index"}, actor=host)
    with pytest.raises(NetworkError, match="只读"):
        net.plugin_bridge(g.id, "llm-wiki", "service.call",
                          {"tool": "write", "arguments": {"path": "a/b", "content": "# x", "summary": "x"}}, actor=host)


def test_disable_stops_service_and_hooks(tmp_path: Path):
    """不变量 24：停用后进程停掉、工具与挂点全部失效；重新启用恢复。"""
    net, _, host, keeper, g, seen = setup(tmp_path, handler=lambda call: "ok")
    net.install_plugin(g.id, "llm-wiki", actor=host)
    chat = net.project_chat(g.id, actor=host)
    net.send_message(chat.id, "@知识管家 在吗", actor=host, background=False)
    assert seen[-1]["mounted"] == ["index.md", "log.md"] and "项目 Wiki 目录" in seen[-1]["prompt"]
    assert net.services.running(g.id, "llm-wiki")
    net.set_plugin_enabled(g.id, "llm-wiki", False, actor=host)
    assert not net.services.running(g.id, "llm-wiki")
    net.send_message(chat.id, "@知识管家 还在吗", actor=host, background=False)
    assert seen[-1]["mounted"] is None and MCP_PREFIX + "llm-wiki__search" not in seen[-1]["tools"]
    net.uninstall_plugin(g.id, "llm-wiki", actor=host)
    assert service_data(net, g).joinpath("wiki", "index.md").is_file()  # 卸载不删数据


# ================= 示例：项目 Wiki 走一遍 =================
def test_llm_wiki_end_to_end(tmp_path: Path):
    """不变量 24：装上项目 Wiki → 收进资料（插件技能 + 按原名调用的插件工具写进服务）→ 下一轮提示词带 wiki 目录（context）、
    工作目录有只读副本（mount）→ 提问带出处作答 → 验收后网络代验收的人 @ 它写回（after），写回的页面出现在 wiki 里。"""
    net, rt, host, keeper, g, seen = setup(tmp_path)
    for f in sorted(MATERIALS.glob("*.md")):
        net.publish_file(g.id, f.name, f.read_bytes(), actor=host)
    info = net.install_plugin(g.id, "llm-wiki", actor=host)
    assert info["service"]["tools"] and info["hooks"]["context"][0]["resource"] == "wiki:///wiki/index.md"
    chat = net.project_chat(g.id, actor=host)

    net.send_message(chat.id, "@知识管家 把项目资料收进 wiki", actor=host, background=False)
    reply = next(m for m in reversed(chat.messages) if m.role == "agent").text
    assert "已把 2 份资料编进 wiki" in reply and "没有发现问题" in reply
    data = service_data(net, g)
    assert (data / "wiki" / "research" / "market-report.md").is_file()
    assert len(list((data / "raw").rglob("*.md"))) == 2
    index = (data / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "协同白板竞品定价调研" in index

    net.send_message(chat.id, "@知识管家 竞品的定价大概在什么区间？", actor=host, background=False)
    assert "### 项目 Wiki 目录（来自插件「项目 Wiki」）" in seen[-1]["prompt"] and "协同白板竞品定价调研" in seen[-1]["prompt"]
    assert seen[-1]["mounted"] == ["index.md", "log.md", "research"]
    answer = next(m for m in reversed(chat.messages) if m.role == "agent").text
    assert "8–12 美元" in answer and "(wiki/research/market-report.md)" in answer
    assert "注意" not in answer  # 这一轮收到了目录

    req = next(r for r in net.store.requests.values() if r.title.startswith("竞品的定价"))
    assert req.status == "in_review"
    net.review_request(req.id, True, actor=host)
    net.wait_quiet(timeout=20)  # after 挂点发的消息在后台唤醒它
    wake = next(m for m in chat.messages if m.role == "user" and "已验收" in m.text)
    assert wake.via == "项目 Wiki" and wake.user_id == host and keeper.id in wake.mentions
    done = next(m for m in reversed(chat.messages) if m.role == "agent").text
    assert "写回 wiki" in done
    pages = list((data / "wiki" / "decisions").glob("*.md"))
    assert pages and "8–12 美元" in pages[0].read_text(encoding="utf-8")
    log = (data / "wiki" / "log.md").read_text(encoding="utf-8")
    assert log.count("] ingest |") == 3
    lint_report = net.plugin_bridge(g.id, "llm-wiki", "service.call", {"tool": "lint"}, actor=host)
    assert "没有发现问题" in lint_report


def test_after_hook_skips_private_requests(tmp_path: Path):
    """不变量 24、22：项目会话里的用户任务只属于下发的人，验收后不能因为插件被带进群聊。"""
    def handler(call: ScriptCall) -> str:
        if "做一件事" in call.prompt and MCP_PREFIX + "open_request" in call.tool_names:
            r = call.tool("open_request", title="私下的事", expected_outcome="x")
            rid = r.split("任务 ")[1].split("。")[0]
            call.tool("deliver_request", request_id=rid, summary="私下的结论")
        return "ok"
    net, _, host, _, g, _ = setup(tmp_path, handler=handler)
    net.install_plugin(g.id, "llm-wiki", actor=host)
    session = net.create_session(g.id, "私下", actor=host)
    net.send_message(session.id, "做一件事", actor=host, background=False)
    req = next(r for r in net.store.requests.values() if r.title == "私下的事")
    net.review_request(req.id, True, actor=host)
    chat = net.project_chat(g.id, actor=host)
    assert not any("已验收" in m.text for m in chat.messages if m.role == "user")


def test_wiki_engine_lint_catches_ungrounded_numbers(tmp_path: Path):
    """示例引擎的确定性体检：页面里的数字要原样出现在它引用的原始资料里；坏链接、孤儿页都报出来。"""
    argv = resolve_command("python", ("{plugin}/service/wiki_engine.py", "--root", "{data}"), WIKI, tmp_path)
    c = StdioClient(argv, tmp_path)
    c.start()
    try:
        out, err = c.call("add_source", {"topic": "m", "slug": "r", "title": "R", "content": "增长 42% ，用户 1200 人"})
        assert not err and "raw/m/r.md" in out
        out, err = c.call("add_source", {"topic": "m", "slug": "r", "title": "R", "content": "x"})
        assert "raw/m/r-2.md" in out  # 原始资料不可变：同名另起一份
        page = "# R\n\n> 来源：x；原始资料：[R](../../raw/m/r.md)；更新：2026-10-30\n\n- 增长 42%\n- 用户 1300 人\n- [坏](../m/nope.md)\n"
        assert not c.call("write", {"path": "m/r", "content": page, "summary": "R 的要点"})[1]
        (tmp_path / "wiki" / "m" / "orphan.md").write_text("# 孤儿\n", encoding="utf-8")
        report, _ = c.call("lint", {})
        assert "1300" in report and "42%" not in report and "坏链接" in report and "孤儿页" in report
        c.call("write", {"path": "m/r", "content": page.replace("1300", "1200"), "summary": "R 的要点"})
        hist, _ = c.call("history", {"path": "wiki/m/r.md"})
        old, _ = c.call("read", {"path": "wiki/m/r.md", "version": hist.split("- ")[1].strip()})
        assert "1300" in old
        with pytest.raises(ServiceError, match="unknown method"):
            c._request("nope/method", {}, 5)
        assert c.call("read", {"path": "../../etc/passwd"})[1]
    finally:
        c.stop()
    assert os.path.isdir(tmp_path / "history")


# ================= 部署方的远程服务 =================
def test_remote_service_partitions_by_signed_project_token(tmp_path: Path, monkeypatch):
    """不变量 24：服务也可以是部署方运行的远程 MCP。网络每次调用带一个只对这个项目、这个插件有效的签名令牌，
    服务按它切分数据：两个项目各写各的，伪造或缺少令牌一律 401；Matrix 这边没配密钥时这个插件的工具不出现。"""
    import importlib.util
    import shutil
    import threading
    spec = importlib.util.spec_from_file_location("wiki_engine", WIKI / "service" / "wiki_engine.py")
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    server = engine.make_http_server(0, tmp_path / "remote", b"s3cret")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
    try:
        pkg = tmp_path / "catalog" / "llm-wiki"
        shutil.copytree(WIKI, pkg)
        m = json.loads((pkg / "plugin.json").read_text(encoding="utf-8"))
        m["service"] = {"url": url, "tools": m["service"]["tools"]}
        (pkg / "plugin.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        shutil.rmtree(pkg / "service")
        (tmp_path / "catalog" / "plugins.json").write_text('{"plugins": ["llm-wiki"]}', encoding="utf-8")
        seen: list[dict] = []

        def spy(call: ScriptCall) -> str:
            seen.append({"tools": list(call.tool_names), "prompt": call.prompt})
            return scenario_handler(call)
        net = Network(ScriptedRuntime(spy), tmp_path / "data", seed=False,
                      plugins=pluginfs.PluginCatalog(tmp_path / "catalog" / "plugins.json"))
        host, = users(net, "host@x.io")
        net.ensure_assistant(host)
        keeper = net.create_agent("知识管家", "维护项目 Wiki", [], ["file.write"], actor=host)
        a = net.create_project("甲", "", [keeper.id], actor=host)
        b = net.create_project("乙", "", [keeper.id], actor=host)
        for g in (a, b):
            net.install_plugin(g.id, "llm-wiki", actor=host)

        net.send_message(net.project_chat(a.id, actor=host).id, "@知识管家 在吗", actor=host, background=False)
        assert MCP_PREFIX + "llm-wiki__search" not in seen[-1]["tools"]  # 部署方没配密钥：这个插件这一轮不可用

        monkeypatch.setenv("MATRIX_PLUGIN_SECRET_LLM_WIKI", "s3cret")
        net.services.stop(a.id, "llm-wiki")
        for f in sorted(MATERIALS.glob("*.md")):
            net.publish_file(a.id, f.name, f.read_bytes(), actor=host)
        chat = net.project_chat(a.id, actor=host)
        net.send_message(chat.id, "@知识管家 把项目资料收进 wiki", actor=host, background=False)
        assert "已把 2 份资料编进 wiki" in next(x for x in reversed(chat.messages) if x.role == "agent").text
        net.send_message(chat.id, "@知识管家 竞品的定价大概在什么区间？", actor=host, background=False)
        assert "协同白板竞品定价调研" in seen[-1]["prompt"]  # context 挂点经远程服务的资源取到目录
        assert (tmp_path / "remote" / a.id / "wiki" / "research" / "market-report.md").is_file()
        assert not (tmp_path / "remote" / b.id / "wiki" / "research").exists()  # 乙项目的 wiki 里没有甲的东西

        import urllib.error
        import urllib.request
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}).encode()
        for auth in ("", f"Bearer v1.{a.id}.9999999999.{'0' * 64}"):
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "Authorization": auth})
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5)
            assert e.value.code == 401
    finally:
        server.shutdown()
