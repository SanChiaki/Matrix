"""连接器（docs/connectors-and-skills.md）：授权与加密保管、网络代调用、权限收窄、共享项目里用到时先问所有者、
电脑上的连接器（上报与只开允许的）、项目的连接器面板。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from matrix import auth
from matrix.connector_catalog import ConnectorCatalog, parse_catalog
from matrix.engine import Network, NetworkError
from matrix.mcp_client import MCPError, RemoteMCP
from matrix.models import CONNECTOR_PREFIX
from matrix.runtime import ConnectorBridge, ScriptCall, ScriptedRuntime, build_patch_yaml
from matrix.vault import Vault, VaultError
from tests.test_users import client, joint, users

SECRET = "ghp_super-secret-token-123"


class FakeMCP:
    """一个远程 MCP 服务（Streamable HTTP）：要 Bearer 令牌，记下收到的调用。"""

    def __init__(self, sse: bool = False):
        self.sse = sse
        self.calls: list[dict] = []
        self.auth_seen: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def do_POST(self):  # noqa: N802
                msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.auth_seen.append(self.headers.get("Authorization", ""))
                if self.headers.get("Authorization") != f"Bearer {SECRET}":
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b"bad token")
                    return
                if "id" not in msg:
                    self.send_response(202)
                    self.end_headers()
                    return
                m = msg["method"]
                if m == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "fake"}}
                elif m == "tools/list":
                    result = {"tools": [{"name": "search_docs", "description": "搜索文档",
                                         "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}]}
                else:
                    outer.calls.append(msg["params"])
                    result = {"content": [{"type": "text", "text": f"找到：{msg['params']['arguments'].get('q')}"}]}
                body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Mcp-Session-Id", "sess-1")
                if outer.sse:
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n')
                    self.wfile.write(b"event: message\ndata: " + body + b"\n\n")
                else:
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake():
    f = FakeMCP()
    yield f
    f.close()


def catalog_file(tmp_path: Path, url: str) -> Path:
    p = tmp_path / "connectors.json"
    p.write_text(json.dumps({"connectors": [{
        "id": "docs", "name": "文档库", "description": "公司文档", "url": url, "domains": ["docs.example.com"],
        "auth": {"fields": [{"key": "token", "label": "访问令牌", "header": "Authorization",
                             "template": "Bearer {value}"}]},
        "skills": [{"name": "doc-search", "description": "怎么查公司文档", "body": "先搜标题"}]}]}), encoding="utf-8")
    return p


def network(tmp_path: Path, handler, url: str) -> Network:
    data = tmp_path / "data"
    data.mkdir()
    return Network(ScriptedRuntime(handler), data, seed=False, connectors=ConnectorCatalog(catalog_file(tmp_path, url)))


def search(call: ScriptCall) -> str:
    """有连接器就查一次，没有就说没有。"""
    if "docs__search_docs" in call.connector_tools():
        return call.connector("docs__search_docs", q="季度目标")
    return "没有连接器"


# ================= 授权与保管 =================
def test_authorize_keeps_the_secret_out_of_state_events_and_api(tmp_path: Path, fake: FakeMCP):
    """不变量 19：凭证只进加密保管，不进 state.json、事件、API 返回。"""
    net = network(tmp_path, search, fake.url)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("Helper", "助理", [], ["connector.use"], actor=alice)
    c = client(net, "alice@x.io")
    assert c.get("/api/connectors").json()[0] == {
        "id": "docs", "name": "文档库", "description": "公司文档", "domains": ["docs.example.com"],
        "fields": [{"key": "token", "label": "访问令牌", "secret": True}],
        "skills": [{"name": "doc-search", "description": "怎么查公司文档"}]}  # 不含地址
    r = c.post("/api/me/connections", json={"connector_id": "docs", "values": {"token": SECRET}})
    assert r.status_code == 200, r.text
    conn = r.json()
    assert conn["status"] == "ok" and conn["agents"] == [a.id]  # 新授权默认勾给助理
    assert SECRET not in r.text and SECRET not in c.get("/api/me/connections").text
    data = tmp_path / "data"
    assert SECRET not in (data / "state.json").read_text(encoding="utf-8")
    vault_file = data / "vault" / f"{conn['id']}.bin"
    assert vault_file.is_file() and SECRET.encode() not in vault_file.read_bytes()
    if os.name != "nt":  # Windows 没有 POSIX 权限位：数据目录由 ACL 限定
        assert oct(vault_file.stat().st_mode & 0o777) == "0o600" and oct((data / "vault.key").stat().st_mode & 0o777) == "0o600"
    assert all(SECRET not in json.dumps(e.data, ensure_ascii=False) for e in net.bus.since(0))
    # 填错：试连失败，状态写明原因，凭证仍然不回显
    bad = c.post("/api/me/connections", json={"connector_id": "docs", "values": {"token": "wrong"}}).json()
    assert bad["status"] == "error" and "401" in bad["error"] and "wrong" not in json.dumps(bad)
    assert c.post("/api/me/connections", json={"connector_id": "docs", "values": {}}).status_code == 409
    assert c.post("/api/me/connections", json={"connector_id": "nope", "values": {}}).status_code == 409


def test_connections_are_invisible_to_others(tmp_path: Path, fake: FakeMCP):
    """不变量 13：别人的连接器按不存在处理。"""
    net = network(tmp_path, search, fake.url)
    alice, bob = users(net, "alice@x.io", "bob@x.io")
    conn = net.authorize_connector("docs", {"token": SECRET}, alice)
    assert net.my_connections(bob) == []
    b = client(net, "bob@x.io")
    assert b.delete(f"/api/me/connections/{conn['id']}").status_code == 404
    assert b.get(f"/api/me/connections/{conn['id']}/calls").status_code == 404
    bob_agent = net.create_agent("BobAgent", "b", [], ["connector.use"], actor=bob)
    with pytest.raises(NetworkError, match="没有这个连接器"):
        net.update_agent(bob_agent.id, actor=bob, connections=[conn["id"]])
    ev = next(e for e in net.bus.since(0) if e.type == "connection.updated")
    assert net.event_view(ev.type, ev.data, alice) is not None and net.event_view(ev.type, ev.data, bob) is None


def test_vault_rejects_a_wrong_key(tmp_path: Path):
    from cryptography.fernet import Fernet
    Vault(tmp_path, key=Fernet.generate_key().decode()).put("c1", {"token": "x"})
    with pytest.raises(VaultError):
        Vault(tmp_path, key=Fernet.generate_key().decode()).get("c1")


def test_catalog_validation():
    good = {"connectors": [{"id": "gh", "url": "https://x/mcp"}]}
    assert parse_catalog(good)[0].name == "gh"
    for bad in ({"connectors": [{"id": "Bad Id", "url": "https://x"}]},
                {"connectors": [{"id": "gh", "url": "ftp://x"}]},
                {"connectors": [{"id": "gh", "url": "https://x", "auth": {"fields": [{"key": "k", "header": "Bad Header!"}]}}]},
                {"connectors": [{"id": "gh", "url": "https://x", "skills": [{"name": "不合法", "description": "x"}]}]},
                {"connectors": [{"id": "gh", "url": "https://x"}, {"id": "gh", "url": "https://y"}]}):
        with pytest.raises(ValueError):
            parse_catalog(bad)


# ================= 网络代调用 =================
@pytest.mark.parametrize("sse", [False, True])
def test_network_calls_the_service_on_the_agents_behalf(tmp_path: Path, sse: bool):
    """运行时只拿到执行上下文令牌；网络按令牌认定能用的连接器，带上凭证代为调用，并记下调用（不含内容）。"""
    f = FakeMCP(sse=sse)
    try:
        seen = {}

        def handler(call: ScriptCall) -> str:
            seen["req"] = call._req  # noqa: SLF001 —— 检查这一轮交给运行时的东西里没有凭证
            seen["skills"] = call.skills
            return search(call)
        net = network(tmp_path, handler, f.url)
        alice, = users(net, "alice@x.io")
        a = net.create_agent("Helper", "助理", [], ["connector.use"], actor=alice)
        conn = net.authorize_connector("docs", {"token": SECRET}, alice)
        dm = net.direct_chat(a.id, actor=alice)
        net.send_message(dm.id, "查一下季度目标", actor=alice, background=False)
        assert dm.messages[-1].text == "找到：季度目标"
        assert f.calls == [{"name": "search_docs", "arguments": {"q": "季度目标"}}]
        req = seen["req"]
        assert SECRET not in repr(req.__dict__) and SECRET not in req.prompt
        assert "文档库（工具以 `mcp__connectors__docs__` 开头）" in req.prompt  # 本轮可用的连接器
        assert seen["skills"] == ["doc-search"]  # 连接器自带的技能
        calls = net.connection_calls(conn["id"], alice)
        assert calls[0]["tool"] == "search_docs" and calls[0]["ok"] and "季度目标" not in json.dumps(calls[0])
        # 覆盖层里挂的是本地桥，只带地址与令牌
        y = build_patch_yaml(["connector.use"], connectors=req.connectors)
        assert "MATRIX_CONNECTOR_URL" in y and SECRET not in y and "mcp-connectors" in y
    finally:
        f.close()


def test_connector_needs_authority_and_narrows_along_delegation(tmp_path: Path, fake: FakeMCP):
    """不变量 1、3、19：没有 connector.use 就没有连接器；委托不授予它，下游这一轮也没有。"""
    def handler(call: ScriptCall) -> str:
        if call.agent_id == lead.id and "委托" in call.prompt and call.can_delegate and call.turn == 1:
            call.tool("delegate", agent="Researcher", mission="查季度目标", expected_outcome="一句话",
                      authority=["delegate"])
            return "已委托"
        if call.agent_id == lead.id:
            return "收到"  # 委托方自己有 connector.use，这里只看下游
        return search(call)
    net = network(tmp_path, handler, fake.url)
    alice, = users(net, "alice@x.io")
    lead = net.create_agent("Lead", "协调", [], ["delegate", "connector.use"], actor=alice)
    researcher = net.create_agent("Researcher", "查资料", [], ["connector.use"], actor=alice)
    plain = net.create_agent("Plain", "没有 connector.use", [], [], actor=alice)
    conn = net.authorize_connector("docs", {"token": SECRET}, alice, agent_ids=[lead.id, researcher.id, plain.id])
    dm = net.direct_chat(plain.id, actor=alice)
    net.send_message(dm.id, "查", actor=alice, background=False)
    assert dm.messages[-1].text == "没有连接器"
    g = net.create_project("P", "", [lead.id, researcher.id], actor=alice)
    chat = net.project_chat(g.id, actor=alice)
    net.send_message(chat.id, "@Lead 委托 Researcher 去查", actor=alice, background=False)
    net.wait_quiet(10)
    t = next(iter(net.store.tasks.values()))
    assert "connector.use" not in t.granted_authority
    assert t.result and "没有连接器" in t.result.get("summary", "")
    assert fake.calls == []


def test_calls_outside_the_effective_set_are_refused_and_recorded(tmp_path: Path, fake: FakeMCP):
    """不变量 10、11：网络只认令牌对应的这一轮；调用这一轮没有的连接器工具被拒绝并留痕。"""
    net = network(tmp_path, search, fake.url)
    assert net.connector_call("no-such-token", "docs__search_docs", {}) == ("执行上下文无效或已结束", True)
    assert net.connector_tools("no-such-token") == []
    alice, = users(net, "alice@x.io")
    a = net.create_agent("Helper", "助理", [], ["connector.use"], actor=alice)

    def sneaky(call: ScriptCall) -> str:
        bridge = call._req.connectors  # noqa: SLF001
        return "无桥" if bridge is None else bridge.call("other__steal", {})[0]
    net.runtime = ScriptedRuntime(sneaky)
    net.authorize_connector("docs", {"token": SECRET}, alice)
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "试试", actor=alice, background=False)
    assert "这一轮不能用 other__steal" in dm.messages[-1].text
    assert any(e.type == "authority.violation" and "other__steal" in e.data["detail"] for e in net.bus.since(0))
    from matrix.engine import _violations
    assert _violations([{"tool": f"{CONNECTOR_PREFIX}docs__search_docs"}], []) == \
        [f"无 connector.use 权限却调用了连接器 {CONNECTOR_PREFIX}docs__search_docs"]


def test_revoke_removes_the_secret_and_the_ticks(tmp_path: Path, fake: FakeMCP):
    net = network(tmp_path, search, fake.url)
    alice, = users(net, "alice@x.io")
    a = net.create_agent("Helper", "助理", [], ["connector.use"], actor=alice)
    conn = net.authorize_connector("docs", {"token": SECRET}, alice)
    net.revoke_connection(conn["id"], alice)
    assert not (tmp_path / "data" / "vault" / f"{conn['id']}.bin").exists()
    assert net.store.agents[a.id].connections == [] and net.my_connections(alice) == []


def test_remote_client_reports_auth_errors(fake: FakeMCP):
    with pytest.raises(MCPError, match="401"):
        RemoteMCP(fake.url, {"Authorization": "Bearer nope"}).list_tools()
    r = RemoteMCP(fake.url, {"Authorization": f"Bearer {SECRET}"})
    assert [t["name"] for t in r.list_tools()] == ["search_docs"] and r.session_id == "sess-1"
    assert r.call_tool("search_docs", {"q": "x"}) == ("找到：x", False)


def test_connector_bridge_script_speaks_mcp_and_fails_soft():
    """mcp_connectors.py：网络不可达时 tools/list 为空（不让整轮起不来），tools/call 回 isError。"""
    from matrix.runtime import CONNECTOR_SCRIPT
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "docs__x", "arguments": {}}}]
    out = subprocess.run([sys.executable, str(CONNECTOR_SCRIPT)], input="\n".join(json.dumps(m) for m in msgs),
                         capture_output=True, text=True, timeout=20, encoding="utf-8",
                         env={**os.environ, "MATRIX_CONNECTOR_URL": "http://127.0.0.1:9/x", "MATRIX_TOKEN": "t",
                              "MATRIX_TOOL_TIMEOUT": "3"}).stdout
    res = [json.loads(x) for x in out.splitlines()]
    assert res[1]["result"]["tools"] == [] and res[2]["result"]["isError"] is True


# ================= 共享项目：用到时先问所有者 =================
def ask_round(net: Network, chat_id: str, text: str, actor: str, decide=None) -> list:
    """在后台跑一轮；出现连接器申请就交给 decide(申请) 处理。返回这一轮出现过的申请。"""
    seen: list = []
    t = threading.Thread(target=net.send_message, args=(chat_id, text), kwargs={"actor": actor, "background": False})
    t.start()
    while t.is_alive():
        for ap in net.pending_approvals(None):
            if ap.kind == "connector" and ap.id not in {x.id for x in seen}:
                seen.append(ap)
                if decide is not None:
                    decide(ap)
        t.join(0.02)
    return seen


def test_shared_projects_ask_the_owner_before_using_their_connector(tmp_path: Path, fake: FakeMCP):
    """不变量 16、19：共享项目里我的 Agent 第一次要用我的连接器时先问我；只这一次 / 这个项目里都可以 / 不行。
    别人看不到我的授权。"""
    net = network(tmp_path, search, fake.url)
    host, guest = users(net, "host@x.io", "guest@x.io")
    ga = net.create_agent("GuestAgent", "参与者的", [], ["connector.use"], actor=guest)
    conn = net.authorize_connector("docs", {"token": SECRET}, guest)
    g, chat = joint(net, host, "guest@x.io", guest)
    # 不行：调用被拦下，不碰服务，也不记违规
    asks = ask_round(net, chat.id, "@GuestAgent 查", host, lambda ap: net.decide_approval(ap.id, False, actor=guest))
    assert len(asks) == 1 and asks[0].owner_id == guest and asks[0].requested_by == host
    assert "host@x.io在「J」里让 GuestAgent 用你的「文档库」" in asks[0].reason
    assert "没有同意" in chat.messages[-1].text and fake.calls == []
    with pytest.raises(NetworkError):
        net.decide_approval(asks[0].id, True, actor=host)  # 只有所有者能决定（已处理过，也按不存在 / 已处理拒绝）
    # 只这一轮：这一轮能用，下一轮再问
    ok_once = lambda ap: net.decide_approval(ap.id, True, actor=guest, scope="once")  # noqa: E731
    assert len(ask_round(net, chat.id, "@GuestAgent 再查", host, ok_once)) == 1
    assert chat.messages[-1].text == "找到：季度目标"
    # 这个项目里都可以：写进授权，之后不再问
    ok_all = lambda ap: net.decide_approval(ap.id, True, actor=guest, scope="project")  # noqa: E731
    assert len(ask_round(net, chat.id, "@GuestAgent 查第三次", host, ok_all)) == 1
    assert ask_round(net, chat.id, "@GuestAgent 查第四次", host) == []
    assert chat.messages[-1].text == "找到：季度目标"
    assert net.project_detail(g.id, actor=host)["connector_grants"] == {}
    assert net.project_detail(g.id, actor=guest)["connector_grants"] == {ga.id: {conn["id"]: "always"}}
    # 面板里选“不行”：这个项目里不再给它
    net.set_connector_grant(g.id, ga.id, conn["id"], "deny", actor=guest)
    assert ask_round(net, chat.id, "@GuestAgent 查第五次", host) == []
    assert chat.messages[-1].text == "没有连接器"
    with pytest.raises(NetworkError):
        net.set_connector_grant(g.id, ga.id, conn["id"], "always", actor=host)  # 不是我的 Agent
    internal = net.create_project("Mine", "", [ga.id], actor=guest)
    with pytest.raises(NetworkError, match="只有你自己的项目"):
        net.set_connector_grant(internal.id, ga.id, conn["id"], "always", actor=guest)


def test_connector_ask_waits_once_per_round_and_times_out(tmp_path: Path, fake: FakeMCP):
    """不变量 19：同一轮同一个连接器只问一次；所有者一直不理，调用按没同意处理。"""
    def twice(call: ScriptCall) -> str:
        return " | ".join(call.connector("docs__search_docs", q=q) for q in ("一", "二"))
    net = network(tmp_path, twice, fake.url)
    net.limits.connector_ask_timeout_s = 0.2
    host, guest = users(net, "host@x.io", "guest@x.io")
    ga = net.create_agent("GuestAgent", "参与者的", [], ["connector.use"], actor=guest)
    net.authorize_connector("docs", {"token": SECRET}, guest)
    g, chat = joint(net, host, "guest@x.io", guest)
    asks = ask_round(net, chat.id, "@GuestAgent 查两次", host)
    assert len(asks) == 1 and asks[0].status == "cancelled"
    assert chat.messages[-1].text.count("没有同意") == 2 and fake.calls == []


def test_connector_panel_gathers_four_sources_without_accounts(tmp_path: Path, fake: FakeMCP):
    """连接器面板：资料里的链接（按域名认或主持人指定）、插件声明、实际用过、手动添加，合成每个服务一行；
    就绪状态按成员给，不给是谁的账号；每人只看到自己的授权。"""
    net = network(tmp_path, search, fake.url)
    host, guest = users(net, "host@x.io", "guest@x.io")
    ha = net.create_agent("HostAgent", "主持人的", [], ["connector.use"], actor=host)
    ga = net.create_agent("GuestAgent", "参与者的", [], ["connector.use"], actor=guest)
    g, chat = joint(net, host, "guest@x.io", guest, [ha.id])
    net.add_ref(g.id, "需求文档", "https://team.docs.example.com/prd", actor=host)
    wiki = net.add_ref(g.id, "内部 wiki", "https://wiki.corp.example/x", actor=host)
    net.add_ref(g.id, "本地路径", "/srv/repo", actor=host)
    net.add_connector_need(g.id, "github", "查议题", actor=host)
    with pytest.raises(NetworkError):
        net.add_connector_need(g.id, "linear", actor=guest)  # 只有主持人能手动添加
    panel = lambda who: net.project_detail(g.id, actor=who)["connector_panel"]  # noqa: E731
    p = panel(guest)
    assert [x["name"] for x in p["services"]] == ["github", "文档库"]
    docs = next(x for x in p["services"] if x["connector_id"] == "docs")
    assert [s["kind"] for s in docs["sources"]] == ["ref"] and docs["sources"][0]["name"] == "需求文档"
    assert [u["location"] for u in p["unrecognized"]] == ["https://wiki.corp.example/x"]
    status = lambda who: {m["agent"]: m["status"] for m in next(  # noqa: E731
        x for x in panel(who)["services"] if x["connector_id"] == "docs")["members"]}
    assert status(guest) == {"HostAgent": "unauthorized", "GuestAgent": "unauthorized"}
    conn = net.authorize_connector("docs", {"token": SECRET}, guest)
    assert status(guest)["GuestAgent"] == "ask"
    net.set_connector_grant(g.id, ga.id, conn["id"], "always", actor=guest)
    assert status(host)["GuestAgent"] == "ready"
    net.set_connector_grant(g.id, ga.id, conn["id"], "deny", actor=guest)
    assert status(host)["GuestAgent"] == "not_enabled"
    mine = lambda who: next(x for x in panel(who)["services"] if x["connector_id"] == "docs")["mine"]  # noqa: E731
    assert mine(host) == [] and [(m["agent"], m["grant"]) for m in mine(guest)] == [("GuestAgent", "deny")]
    shown = json.dumps(net.project_detail(g.id, actor=host)["connector_panel"], ensure_ascii=False)
    assert conn["id"] not in shown and SECRET not in shown
    # 主持人指定认不出的链接用哪个连接器
    with pytest.raises(NetworkError):
        net.set_ref_connector(g.id, wiki.id, "docs", actor=guest)
    net.set_ref_connector(g.id, wiki.id, "docs", actor=host)
    p = panel(host)
    assert p["unrecognized"] == [] and len(next(x for x in p["services"] if x["connector_id"] == "docs")["sources"]) == 2
    # 实际用过的记进来；主持人可以收起
    net.set_connector_grant(g.id, ga.id, conn["id"], "always", actor=guest)
    net.send_message(chat.id, "@GuestAgent 查", actor=host, background=False)
    used = [s for s in next(x for x in panel(host)["services"] if x["connector_id"] == "docs")["sources"] if s["kind"] == "used"]
    assert used and used[0]["count"] == 1 and used[0]["last_agent"] == "GuestAgent"
    net.clear_connector_usage(g.id, "docs", actor=host)
    assert not [s for s in next(x for x in panel(host)["services"] if x["connector_id"] == "docs")["sources"] if s["kind"] == "used"]
    assert any("在连接器面板里添加了「github」" in m.text for m in net.store.conversations[chat.id].messages)


def test_connector_grants_and_panel_over_http(tmp_path: Path, fake: FakeMCP):
    net = network(tmp_path, search, fake.url)
    host, guest = users(net, "host@x.io", "guest@x.io")
    ga = net.create_agent("GuestAgent", "参与者的", [], ["connector.use"], actor=guest)
    conn = net.authorize_connector("docs", {"token": SECRET}, guest)
    g, _chat = joint(net, host, "guest@x.io", guest)
    net.add_ref(g.id, "需求文档", "https://docs.example.com/prd", actor=host)
    c = client(net, "guest@x.io")
    r = c.put(f"/api/projects/{g.id}/grants", json={"agent_id": ga.id, "connection_id": conn["id"], "grant": "always"})
    assert r.status_code == 200 and r.json()["connector_grants"] == {ga.id: {conn["id"]: "always"}}
    assert c.put(f"/api/projects/{g.id}/grants", json={"agent_id": ga.id, "connection_id": conn["id"],
                                                         "grant": "maybe"}).status_code == 422
    h = client(net, "host@x.io")
    assert h.get(f"/api/projects/{g.id}").json()["connector_grants"] == {}
    assert h.post(f"/api/projects/{g.id}/needs", json={"connector_id": "github"}).status_code == 200
    ref = net.store.projects[g.id].refs[0]
    assert h.put(f"/api/projects/{g.id}/refs/{ref.id}/connector", json={"connector_id": "github"}).status_code == 200
    assert c.put(f"/api/projects/{g.id}/refs/{ref.id}/connector", json={"connector_id": ""}).status_code == 409


def test_suggest_connector_only_pings_the_owner(tmp_path: Path, fake: FakeMCP):
    def handler(call: ScriptCall) -> str:
        return call.tool("suggest_connector", connector="飞书文档", reason="要读需求文档")
    net = network(tmp_path, handler, fake.url)
    host, guest = users(net, "host@x.io", "guest@x.io")
    ga = net.create_agent("GuestAgent", "参与者的", [], [], actor=guest)
    g, chat = joint(net, host, "guest@x.io", guest)
    net.send_message(chat.id, "@GuestAgent 读一下需求", actor=host, background=False)
    card = next(m for m in net.store.conversations[chat.id].messages if "想用连接器「飞书文档」" in m.text)
    assert card.audience == [guest] and net.can_see_message(card, host) is False
    assert net.my_connections(guest) == []  # 只是提醒，没有授权任何东西


# ================= 电脑上的连接器 =================
def device_hello(services: dict[str, list[str]] | None, enforce: bool = True):
    from matrix.device import HELLO_OK, PROTOCOL, capabilities_from_hello
    runtimes = []
    for name, names in (services or {"claude-code": []}).items():
        item = {"runtime": name, "capabilities": {"enforcement": {"connector.use": "enforced"} if enforce else {},
                                                  "skills": "native"}}
        if services is not None:
            item["connectors"] = names
        runtimes.append(item)
    return capabilities_from_hello({"type": HELLO_OK, "protocol": PROTOCOL, "runtime": runtimes[0]["runtime"],
                                    "runtimes": runtimes})


def device_net(tmp_path: Path, services: dict[str, list[str]] | None, enforce: bool = True):
    from matrix.device import RUN_RESULT, DeviceRuntime, MemoryLink
    from tests.test_device import connector
    net = Network(ScriptedRuntime(lambda c: "cloud"), tmp_path, seed=False)
    alice, guest = users(net, "alice@x.io", "guest@x.io")
    dev, _ = auth.issue_device_token(net.store, alice, "laptop")
    a = net.create_agent("Local", "端侧", [], ["connector.use"], actor=alice, device_runtime=dev.id)
    rt = DeviceRuntime(MemoryLink(serve=lambda m: None), device_hello(services, enforce))
    connector(rt, script=lambda msg, deliver: deliver({"type": RUN_RESULT, "run_id": msg["run_id"], "final_text": "done"}))
    net.register_runtime(dev.id, rt)
    return net, alice, guest, dev, a, rt


def last_request(rt):
    from matrix.device import RUN_REQUEST
    return [m for m in rt.link.sent if m["type"] == RUN_REQUEST][-1]


def test_device_services_become_connections_and_only_allowed_ones_are_sent(tmp_path: Path):
    """电脑上的 AI 工具里配置的服务：上线时只报名字；新出现的默认勾给那台电脑上的 Agent；
    run.request 里只放这一轮允许的（共享项目里要所有者选“这个项目里都可以”）。"""
    net, alice, guest, dev, a, rt = device_net(tmp_path, {"claude-code": ["notion", "linear"]})
    conns = net.my_connections(alice)
    assert {c["name"] for c in conns} == {"notion", "linear"} and all(c["kind"] == "device" for c in conns)
    assert set(net.store.agents[a.id].connections) == {c["id"] for c in conns}
    dm = net.direct_chat(a.id, actor=alice)
    net.send_message(dm.id, "在吗", actor=alice, background=False)
    req = last_request(rt)
    assert sorted(req["connectors"]) == ["linear", "notion"] and "电脑上已经配好的服务" in req["prompt"]
    notion = next(c["id"] for c in conns if c["name"] == "notion")
    net.update_agent(a.id, actor=alice, connections=[notion])
    net.send_message(dm.id, "再来", actor=alice, background=False)
    assert last_request(rt)["connectors"] == ["notion"]
    # 联合项目里默认关
    g = net.create_project("J", "", [], actor=guest)
    net.invite(g.id, "alice@x.io", actor=guest)
    net.respond_invitation(g.id, True, actor=alice)
    chat = net.project_chat(g.id, actor=guest)
    net.send_message(chat.id, "@Local 来", actor=guest, background=False)
    assert last_request(rt)["connectors"] == []  # 电脑上的服务网络拦不到调用：没有“用到时问”，要所有者选“都可以”
    net.set_connector_grant(g.id, a.id, notion, "always", actor=alice)
    net.send_message(chat.id, "@Local 再来", actor=guest, background=False)
    assert last_request(rt)["connectors"] == ["notion"]
    # 云端的连接器不能勾给电脑上的 Agent，反过来也一样
    cloud_agent = net.create_agent("Cloud", "云端", [], ["connector.use"], actor=alice)
    with pytest.raises(NetworkError, match="只能给运行在"):
        net.update_agent(cloud_agent.id, actor=alice, connections=[notion])


def test_device_services_that_disappear_are_marked_missing(tmp_path: Path):
    from matrix.device import DeviceRuntime, MemoryLink
    net, alice, guest, dev, a, rt = device_net(tmp_path, {"claude-code": ["notion"]})
    net.register_runtime(dev.id, DeviceRuntime(MemoryLink(serve=lambda m: None), device_hello({"claude-code": []})))
    c = net.my_connections(alice)[0]
    assert c["status"] == "missing"


def test_old_clients_report_nothing_and_unenforced_harnesses_get_no_connectors(tmp_path: Path):
    net, alice, *_ = device_net(tmp_path, None)
    assert net.my_connections(alice) == []
    net2, alice2, _g, _d, a2, rt2 = device_net(tmp_path / "b", {"codex": ["x"]}, enforce=False)
    dm = net2.direct_chat(a2.id, actor=alice2)
    net2.send_message(dm.id, "hi", actor=alice2, background=False)
    assert last_request(rt2)["connectors"] == [] and "connector.use" not in last_request(rt2)["authority"]


def test_old_device_agents_keep_their_local_services(tmp_path: Path):
    """旧数据：有连接器之前电脑上的 Agent 一直能用本机工具里配的服务，加载时补上 connector.use。"""
    from matrix.store import Store
    state = {"agents": [{"id": "a1", "name": "L", "description": "d", "capabilities": [], "authority": ["file.write"],
                         "device_runtime": "dev-1"},
                        {"id": "a2", "name": "C", "description": "d", "capabilities": [], "authority": []}]}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    s = Store(tmp_path)
    assert s.agents["a1"].authority == ["file.write", "connector.use"] and s.agents["a2"].authority == []
    s.save()
    s.agents["a1"].authority = ["file.write"]
    s.save()
    assert Store(tmp_path).agents["a1"].authority == ["file.write"]  # 迁移只做一次：之后所有者关掉就是关掉


# ================= 驱动：只开允许的服务 =================
def test_claude_driver_loads_only_allowed_services(tmp_path: Path):
    from matrix.connector.drivers.base import RunSpec
    from matrix.connector.drivers.claude import ClaudeDriver
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({"mcpServers": {"notion": {"type": "http", "url": "https://n", "headers": {"k": "v"}},
                                              "linear": {"type": "stdio", "command": "linear-mcp"}}}), encoding="utf-8")
    d = ClaudeDriver(bridge_url="http://127.0.0.1:1", config_path=cfg)
    assert sorted(d.services()) == ["linear", "notion"] and d.capabilities().enforces("connector.use")
    spec = RunSpec(run_id="r", agent_id="a", context_id="c", prompt="", workdir=tmp_path / "w", authority=[],
                   tool_names=["open_request"], connectors=["notion"])
    servers = d.mcp_servers(spec)
    assert set(servers) == {"matrix_net", "notion"}
    cmd = d.command(spec, tmp_path / "cfg.json")
    assert "--strict-mcp-config" in cmd and str(tmp_path / "cfg.json") in cmd
    old = RunSpec(run_id="r", agent_id="a", context_id="c", prompt="", workdir=tmp_path / "w", authority=[],
                  tool_names=[], connectors=None)
    assert "--strict-mcp-config" not in d.command(old)  # 旧服务端：不干预
    # 技能拷进 .claude/skills：网络下发的在前，私有目录里的补上不重名的
    w = tmp_path / "w"
    for root, name, text in ((w / "skills", "a", "net"), (w / "agent-home" / "skills", "a", "home"),
                             (w / "agent-home" / "skills", "b", "home")):
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "SKILL.md").write_text(text, encoding="utf-8")
    dest = d.mount_skills(w)
    assert (dest / "a" / "SKILL.md").read_text(encoding="utf-8") == "net" and (dest / "b" / "SKILL.md").read_text(encoding="utf-8") == "home"


def test_claude_driver_keeps_users_own_matrix_service(tmp_path: Path):
    """用户自己配了一个叫 matrix 的服务（如 matrix.org 聊天）：不和网络工具撞名，勾给它就照常能用。"""
    from matrix.connector.drivers.base import RunSpec
    from matrix.connector.drivers.claude import ClaudeDriver
    from matrix.models import MCP_SERVER
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({"mcpServers": {"matrix": {"type": "stdio", "command": "matrix-chat-mcp"}}}), encoding="utf-8")
    d = ClaudeDriver(bridge_url="http://127.0.0.1:1", config_path=cfg)
    spec = RunSpec(run_id="r", agent_id="a", context_id="c", prompt="", workdir=tmp_path / "w", authority=[],
                   tool_names=["open_request"], connectors=["matrix"])
    servers = d.mcp_servers(spec)
    assert MCP_SERVER != "matrix" and servers["matrix"]["command"] == "matrix-chat-mcp" and MCP_SERVER in servers


def test_codex_driver_disables_services_that_are_not_allowed(monkeypatch, tmp_path: Path):
    from matrix.connector.drivers.base import RunSpec
    from matrix.connector.drivers import codex as codex_mod
    d = codex_mod.CodexDriver()
    monkeypatch.setattr(d, "services", lambda: ["notion", "my.server"])
    spec = RunSpec(run_id="r", agent_id="a", context_id="c", prompt="", workdir=tmp_path, authority=[],
                   tool_names=[], connectors=["notion"])
    cmd = d.command(spec)
    assert '-c' in cmd and 'mcp_servers."my.server".enabled=false' in cmd and not any("notion" in x for x in cmd)
    assert d.capabilities().enforces("connector.use") and d.capabilities().skills == "prompt"


def test_dsh_driver_disables_unallowed_mcp_plugins(monkeypatch, tmp_path: Path):
    from matrix.connector.drivers.dsh import DshDriver
    dump = ("- id: tool-web\n  name: '@deepseek-ai/dsh-tool-web'\n"
            "- id: my-notion\n  name: '@deepseek-ai/dsh-mcp-client'\n  config:\n    serverName: notion\n"
            "- id: my-linear\n  name: '@deepseek-ai/dsh-mcp-client'\n  config:\n    serverName: linear\n")
    d = DshDriver(bridge_url="http://127.0.0.1:1")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=dump))
    assert d.mcp_plugins() == {"notion": "my-notion", "linear": "my-linear"}
    y = build_patch_yaml([], extra_disabled=["my-linear"])
    assert "- id: my-linear\n  disabled: true" in y


def test_custom_connector_public_only(tmp_path):
    """不变量 19 / 安全：自定义的云端 MCP 地址只能是 https 公网地址，内网、回环、http 一律拒绝；凭证只进保管库。"""
    from matrix.mcp_client import MCPError, check_public_url
    for bad in ["http://example.com/mcp", "https://127.0.0.1/mcp", "https://localhost/mcp", "https://10.0.0.5/x",
                "https://169.254.169.254/latest", "https://[::1]/x", "https://user:pw@example.com/", "ftp://x"]:
        with pytest.raises(MCPError):
            check_public_url(bad)
    net = Network(ScriptedRuntime(lambda c: "ok"), tmp_path, seed=False)
    alice, = users(net, "alice@x.io")
    c = client(net, "alice@x.io")
    r = c.post("/api/me/connections/custom", json={"name": "内网", "url": "https://127.0.0.1:8813/mcp",
                                                   "header": "Authorization", "value": "Bearer s3cret"})
    assert r.status_code == 409 and "s3cret" not in r.text
    assert c.get("/api/me/connections").json() == []
