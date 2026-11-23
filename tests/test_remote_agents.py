"""外部平台 Agent（docs/remote-agents.md，不变量 28）：平台只来自部署方目录；以 HTTP 流式会话接进来当成员；
平台回调 Matrix 的 MCP 凭 Matrix 签的密钥 + 平台会话 id，只认 Matrix 发起、还没结束的那一轮；调用方的授权只透传他自己的。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from matrix.connector_catalog import ConnectorCatalog
from matrix.engine import ExecContext, Network, NetworkError, StagingRefused
from matrix.remote_agents import PlatformCatalog, PlatformError, parse_platforms
from matrix.runtime import ScriptedRuntime
from matrix.server import create_app
from tests.test_connections import SECRET, FakeMCP, catalog_file
from tests.test_users import users

sys.path.insert(0, str(Path(__file__).parent.parent / "examples" / "remote-agent"))
from demo_platform import API_KEY, DemoPlatform, report  # noqa: E402


@pytest.fixture
def fake():
    f = FakeMCP()
    yield f
    f.close()


class Env:
    def __init__(self, tmp_path: Path, fake: FakeMCP | None = None):
        data = tmp_path / "data"
        data.mkdir()
        self.net_holder: list[Network] = []
        self.demo = DemoPlatform(mcp_post=self._mcp_post, file_post=self._file_post, file_get=self._file_get)
        plats = tmp_path / "agent-platforms.json"
        plats.write_text(json.dumps({"platforms": [
            {"id": "demo", "name": "演示平台", "url": self.demo.url, "bind_wait_s": 3},
            {"id": "demo-sso", "name": "演示平台（带调用方授权）", "url": self.demo.url, "bind_wait_s": 3,
             "caller": {"header": "X-User-Token", "connector": "docs", "field": "token"}},
        ]}), encoding="utf-8")
        self.net = Network(ScriptedRuntime(lambda call: "云侧"), data, seed=False, platforms=PlatformCatalog(plats),
                           connectors=ConnectorCatalog(catalog_file(tmp_path, fake.url)) if fake else None)
        self.client = TestClient(create_app(self.net))

    def _mcp_post(self, url: str, headers: dict[str, str], body: dict) -> dict:
        r = self.client.post("/mcp/remote", json=body, headers=headers)
        return r.json() if r.content else {}

    def _file_post(self, url: str, headers: dict[str, str], data: bytes) -> tuple[int, dict]:
        r = self.client.post(url.removeprefix("http://matrix"), content=data, headers=headers)
        return r.status_code, r.json()

    def _file_get(self, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        r = self.client.get(url.removeprefix("http://matrix"), headers=headers)
        return r.status_code, r.content

    def connect_mcp(self, agent_id: str, actor: str, platform_agent: str = "expense") -> str:
        """扮演维护人在平台网页上给这个 Agent 填 MCP：Matrix 的地址 + Matrix 签的密钥。"""
        k = self.net.issue_remote_key(agent_id, actor=actor)
        self.demo.mcp[platform_agent] = {"url": "http://matrix" + k["path"], "headers": {k["header"]: k["value"]}}
        return k["key"]

    def close(self):
        self.demo.close()


@pytest.fixture
def env(tmp_path: Path, fake: FakeMCP):
    e = Env(tmp_path, fake)
    yield e
    e.close()


def test_platform_catalog_validation():
    """不变量 28：平台地址只来自部署方目录，目录写坏了不生效。"""
    with pytest.raises(PlatformError, match="url"):
        parse_platforms({"platforms": [{"id": "x", "url": "ftp://x"}]})
    with pytest.raises(PlatformError, match="请求头"):
        parse_platforms({"platforms": [{"id": "x", "url": "http://x", "mcp_session_header": "bad header"}]})
    with pytest.raises(PlatformError, match="connector"):
        parse_platforms({"platforms": [{"id": "x", "url": "http://x", "caller": {"header": "X-T"}}]})
    assert parse_platforms({"platforms": [{"id": "x", "url": "http://x/"}]})[0].url == "http://x"


def test_basic_access_runs_on_the_platform_and_keeps_secrets_out(env: Env):
    """不变量 28：添加时从平台读名片；API Key 只进保管库；没接 MCP 时它只能干活、不能委托。"""
    net = env.net
    maya, = users(net, "maya@x.io")
    with pytest.raises(NetworkError, match="没有平台"):
        net.add_remote_agent("nope", "expense", API_KEY, actor=maya)
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    assert a.name == "报销助手（演示平台）" and a.remote["platform"] == "demo"
    assert API_KEY not in (net.store.root / "state.json").read_text(encoding="utf-8")
    assert net.capabilities(a).level("delegate") == "advisory"  # 没签 MCP 密钥：基础接入
    dm = net.direct_chat(a.id, actor=maya)
    net.send_message(dm.id, "我的报销到哪了", actor=maya, background=False)
    assert dm.messages[-1].text == "没有调用方的授权"
    net.send_message(dm.id, "再问一次", actor=maya, background=False)
    sid = dm.seats[a.id].dsh_session_id
    assert env.demo.sessions[sid] and len(env.demo.sessions[sid]) == 2  # 续接同一个平台会话
    view = net.agent_view(a, maya)
    assert view["remote"]["key_issued"] is False and "key_sha" not in json.dumps(view)
    evs = json.dumps([e.data for e in net.bus.since(0)], ensure_ascii=False, default=str)
    assert API_KEY not in evs


def test_full_access_callbacks_land_in_the_matrix_round(env: Env):
    """不变量 28：配了 Matrix 的 MCP 后，平台的回调凭密钥 + 会话 id 落到 Matrix 发起的那一轮，权限照网络算；
    接上之后委托由网络强制。"""
    net = env.net
    maya, = users(net, "maya@x.io")
    helper = net.create_agent("Helper", "帮忙", [], [], actor=maya)
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    env.connect_mcp(a.id, maya)
    g = net.create_project("报销", "", [a.id, helper.id], actor=maya)
    chat = net.project_chat(g.id, actor=maya)
    net.send_message(chat.id, f"@{a.name} 看看有哪些成员", actor=maya, background=False)
    reply = chat.messages[-1].text
    assert reply.startswith("成员：") and "Helper" in reply
    assert a.remote["mcp_at"] and net.capabilities(a).level("delegate") == "enforced"
    net.send_message(chat.id, f"@{a.name} 委托「Helper」查一下", actor=maya, background=False)
    net.wait_idle(chat.id)
    t = next(iter(net.store.tasks.values()))
    assert t.delegator_id == a.id and t.delegatee_id == helper.id
    # 那一轮结束了：拿着旧会话 id 再回调，一律拒绝
    sid = chat.seats[a.id].dsh_session_id
    out = env._mcp_post("", {"Authorization": env.demo.mcp["expense"]["headers"]["Authorization"], "X-Session-Id": sid},
                        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_members", "arguments": {}}})
    assert out["error"]["code"] == -32001


def test_callbacks_before_the_session_id_wait_and_strangers_are_refused(env: Env):
    """不变量 28：建会话与第一条消息同一次请求时，MCP 调用比会话 id 先到：先等，流里报出 id 后落到那一轮；
    密钥不对 401；在平台上直接聊天的会话（Matrix 不认识的 id）等不到就拒绝。"""
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    key = env.connect_mcp(a.id, maya)
    dm = net.direct_chat(a.id, actor=maya)
    net.send_message(dm.id, "抢跑", actor=maya, background=False)
    assert dm.messages[-1].text.startswith("抢跑查到：")
    assert "MCP 拒绝" not in dm.messages[-1].text
    r = env.client.post("/mcp/remote", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                        headers={"Authorization": "Bearer wrong", "X-Session-Id": "x"})
    assert r.status_code == 401
    started = time.time()
    r = env.client.post("/mcp/remote", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_members", "arguments": {}}},
                        headers={"Authorization": f"Bearer {key}", "X-Session-Id": "s-direct-chat"})
    assert r.json()["error"]["code"] == -32001 and time.time() - started >= 2.5
    old = key
    env.connect_mcp(a.id, maya)  # 重签：旧密钥作废
    r = env.client.post("/mcp/remote", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                        headers={"Authorization": f"Bearer {old}", "X-Session-Id": "x"})
    assert r.status_code == 401


def test_caller_authorization_is_passed_through_only_from_the_caller(env: Env):
    """不变量 28（与不变量 27、19）：平台要调用方授权时，只透传这一轮调用方自己的；他没授权就不发起会话，
    不退回到添加它的人的授权。"""
    net = env.net
    admin, maya, ann = users(net, "admin@x.io", "maya@x.io", "ann@x.io")
    net.create_agent("Maya 的助理", "助理", [], [], actor=maya)
    a = net.add_remote_agent("demo-sso", "expense", API_KEY, actor=maya)
    net.authorize_connector("docs", {"token": SECRET}, maya)  # 维护人自己有，也不借给别人
    net.request_public(a.id, "报销入口", actor=maya)
    net.review_public(a.id, True, actor=admin)
    dm = net.direct_chat(a.id, actor=ann)
    calls = len(env.demo.seen_headers)
    net.send_message(dm.id, "我的报销到哪了", actor=ann, background=False)
    assert len(env.demo.seen_headers) == calls  # 没发起会话
    assert any("还没有授权「文档库」" in m.text for m in dm.messages if m.role == "system")
    net.authorize_connector("docs", {"token": SECRET}, ann)
    net.resume_conversation(dm.id, actor=ann)  # 授权之后接着做
    net.send_message(dm.id, "连好了", actor=ann, background=False)
    assert dm.messages[-1].text == "收到了调用方的授权"
    sent = env.demo.seen_headers[-1]
    assert sent.get("X-User-Token") == SECRET and sent.get("Authorization") == f"Bearer {API_KEY}"


def test_stopping_a_round_interrupts_the_platform_session(env: Env):
    """不变量 28：人终止这一轮时调平台的会话中断接口，会话保留；之后这个会话的回调不再认。"""
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    dm = net.direct_chat(a.id, actor=maya)
    net.send_message(dm.id, "慢慢来", actor=maya)
    deadline = time.time() + 5
    while time.time() < deadline and not dm.seats.get(a.id, None) or \
            (dm.seats.get(a.id) and not dm.seats[a.id].dsh_session_id and time.time() < deadline):
        time.sleep(0.05)
    sid = dm.seats[a.id].dsh_session_id
    net.stop_run(f"{dm.id}/{a.id}", actor=maya)
    net.wait_idle(dm.id)
    assert sid in env.demo.interrupted
    assert dm.seats[a.id].blocked and dm.seats[a.id].dsh_session_id == sid


def test_platform_probe_without_a_session_lists_tools_but_cannot_call(env: Env):
    """不变量 28：平台保存 MCP 配置时先试连（不在任何会话里）：initialize、ping、tools/list 不要会话 id、不等待，
    tools/list 只给工具说明；真正调用工具必须带 Matrix 发起、还没结束的会话 id。密钥照样要对，试连不算“连上过”。"""
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    key = env.connect_mcp(a.id, maya)
    auth = {"Authorization": f"Bearer {key}"}
    post = lambda body, h=auth: env.client.post("/mcp/remote", json=body, headers=h)  # noqa: E731
    started = time.time()
    assert post({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).json()["result"]["serverInfo"]["name"] == "matrix"
    assert post({"jsonrpc": "2.0", "id": 2, "method": "ping"}).json()["result"] == {}
    names = {t["name"] for t in post({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}).json()["result"]["tools"]}
    assert {"list_members", "delegate"} <= names
    probe = post({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, {**auth, "X-Session-Id": "s-not-yet"}).json()
    assert probe["result"]["tools"] and time.time() - started < 2  # 没绑定的会话也不等
    assert post({"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    out = post({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_members", "arguments": {}}}).json()
    assert out["error"]["code"] == -32600
    assert post({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, {"Authorization": "Bearer wrong"}).status_code == 401
    assert not a.remote["mcp_at"]  # 试连不算完整接入


class _SseClient:
    """旧版 MCP 传输的客户端：后台线程读 SSE 流，事件按到达顺序进队列。"""

    def __init__(self, base: str, headers: dict[str, str]):
        import queue
        import threading

        import httpx
        self.events: queue.Queue = queue.Queue()
        self._http = httpx.Client(base_url=base, timeout=10)
        self._cm = self._http.stream("GET", "/mcp/remote", headers={**headers, "Accept": "text/event-stream"})
        self.resp = self._cm.__enter__()
        if self.resp.status_code == 200:
            threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        ev, data = "message", []
        try:
            for line in self.resp.iter_lines():
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
                elif not line and data:
                    self.events.put((ev, "\n".join(data)))
                    ev, data = "message", []
        except Exception:  # noqa: BLE001,S110 — 流被关掉：读线程就此结束
            return

    def next(self, timeout: float = 5) -> tuple[str, str]:
        return self.events.get(timeout=timeout)

    def post(self, url: str, body: dict, headers: dict[str, str] | None = None):
        return self._http.post(url, json=body, headers=headers or {})

    def close(self):
        import socket
        sock = (ns := self.resp.extensions.get("network_stream")) and ns.get_extra_info("socket")
        if sock is not None:  # 读线程还卡在 recv 上：直接断开连接，平台断线就是这样
            sock.shutdown(socket.SHUT_RDWR)
        self._cm.__exit__(None, None, None)
        self._http.close()


def test_legacy_sse_transport_follows_the_same_rules(env: Env):
    """不变量 28：只支持旧版 MCP 传输（HTTP+SSE）的平台用同一个地址、同一把密钥接入：GET 开流，第一条 endpoint 事件
    给出消息地址，POST 过去回 202、响应从流里回来。密钥不对开不了流；消息地址只对开着的流有效；
    tools/call 照旧只认 Matrix 发起、还没结束的那一轮，平台会话 id 可以只在开流时带。"""
    import socket
    import threading

    import uvicorn

    from matrix.runtime import ToolBridge
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    key = env.connect_mcp(a.id, maya)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(create_app(net), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    clients: list[_SseClient] = []
    try:
        bad = _SseClient(base, {"Authorization": "Bearer wrong"})
        clients.append(bad)
        assert bad.resp.status_code == 401

        c = _SseClient(base, {"Authorization": f"Bearer {key}"})
        clients.append(c)
        ev, url = c.next()
        assert ev == "endpoint" and url.startswith("/mcp/remote/messages?session_id=")
        assert c.post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                      {"Authorization": f"Bearer {key}"}).status_code == 202
        ev, data = c.next()
        assert ev == "message" and json.loads(data)["result"]["serverInfo"]["name"] == "matrix"
        assert c.post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202  # 没带密钥：沿用开流的
        assert c.post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).status_code == 202
        ev, data = c.next()  # 通知没有响应，下一条就是 tools/list 的
        assert {"list_members", "delegate"} <= {x["name"] for x in json.loads(data)["result"]["tools"]}
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_members", "arguments": {}}}
        c.post(url, call)
        assert json.loads(c.next()[1])["error"]["code"] == -32600  # 不在任何一轮里
        assert c.post(url, call, {"Authorization": "Bearer wrong"}).status_code == 401
        assert c.post("/mcp/remote/messages?session_id=nope", call).status_code == 404

        net.remote_bindings.bind(a.id, "s-legacy", ToolBridge(url="", token="", handler=lambda n, _: (f"调用了 {n}", False)))
        c2 = _SseClient(base, {"Authorization": f"Bearer {key}", "X-Session-Id": "s-legacy"})  # 会话 id 只在开流时带
        clients.append(c2)
        url2 = c2.next()[1]
        c2.post(url2, call)
        out = json.loads(c2.next()[1])["result"]
        assert out == {"content": [{"type": "text", "text": "调用了 list_members"}], "isError": False}
        assert a.remote["mcp_at"]
        net.remote_bindings.unbind(a.id, "s-legacy")
        c2.close()
        clients.remove(c2)
        for _ in range(50):  # 流断了，它的消息地址作废
            if c.post(url2, call).status_code == 404:
                break
            time.sleep(0.05)
        assert c.post(url2, call).status_code == 404
    finally:
        for x in clients:
            x.close()
        server.should_exit = True
        t.join(timeout=5)


def test_platform_files_are_staged_then_sent_in_the_same_round(env: Env):
    """外部平台 Agent 发文件：平台把文件 POST 到 /mcp/remote/files 拿 file_id，同一轮 send_file(file_id=…) 附在回复上；
    暂存只属于那一轮，一轮结束就删，下一轮拿旧 file_id 发不出去；没有 file_id 时 path 读不到平台上的文件，提示先暂存。"""
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    assert env.net.issue_remote_key(a.id, actor=maya)["files_path"] == "/mcp/remote/files"
    env.connect_mcp(a.id, maya)
    dm = net.direct_chat(a.id, actor=maya)
    net.send_message(dm.id, "发文件给我", actor=maya, background=False)
    code, staged = env.demo.uploads[-1]
    assert code == 200 and staged["file_id"].startswith("stg_") and staged["size"] == len(report("expense"))
    reply = dm.messages[-1]
    assert "已附上 报销助手汇总.md" in reply.text and [x["name"] for x in reply.attachments] == ["报销助手汇总.md"]
    f, meta = net.attachment_file(dm.id, reply.attachments[0]["sha"], actor=maya)
    assert f.read_bytes() == report("expense") and meta["mime"].startswith("text/")
    assert not any((net.store.root / "staging").iterdir())  # 一轮结束：暂存删掉，发出去的在对话的附件区
    net.send_message(dm.id, f"转发「{staged['file_id']}」", actor=maya, background=False)
    assert "找不到暂存文件" in dm.messages[-1].text and not dm.messages[-1].attachments
    # 外部 Agent 给 path：文件在它自己的平台上，Matrix 读不到，提示先暂存
    ctx = ExecContext(kind="seat", id=f"{dm.id}/{a.id}", agent_id=a.id, conversation_id=dm.id, project_id=None,
                      authority=[], constraints=[], depth=0, chain=[a.id])
    text, bad = net._tool_send_file(ctx, {"path": "report.md"})
    assert bad and "/mcp/remote/files" in text


def test_staging_needs_the_key_and_a_live_matrix_round(env: Env, monkeypatch: pytest.MonkeyPatch):
    """不变量 28：暂存和 tools/call 一样，只认 Matrix 签的密钥 + Matrix 发起、还没结束的会话 id，认过了才收请求体；
    超过附件上限 413。"""
    import matrix.server as server
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    key = env.connect_mcp(a.id, maya)
    post = lambda h: env.client.post("/mcp/remote/files?name=x.txt", content=b"hi", headers=h)  # noqa: E731
    assert post({"Authorization": "Bearer wrong", "X-Session-Id": "x"}).status_code == 401
    assert post({"Authorization": f"Bearer {key}"}).status_code == 400
    assert post({"Authorization": f"Bearer {key}", "X-Session-Id": "s-direct-chat"}).status_code == 403  # 平台上直接聊的
    dm = net.direct_chat(a.id, actor=maya)
    net.send_message(dm.id, "发文件", actor=maya, background=False)
    sid = dm.seats[a.id].dsh_session_id
    # 不限文件类型（二进制照收，类型按 Content-Type 或文件名猜），只清洗文件名：清洗后为空 400
    live = ExecContext(kind="seat", id="x", agent_id=a.id, conversation_id=dm.id, project_id=None,
                       authority=[], constraints=[], depth=0, chain=[])
    net._contexts["t-live"] = live
    with pytest.raises(StagingRefused) as e:
        net.stage_remote_file(live, "....", b"x")
    assert e.value.status == 400
    out = net.stage_remote_file(live, "../../a/模型.qzx9", bytes(range(256)))
    assert out["name"] == "模型.qzx9" and live.staged[out["file_id"]]["mime"] == "application/octet-stream"
    net._contexts.pop("t-live")
    net._drop_staged(live)
    r = post({"Authorization": f"Bearer {key}", "X-Session-Id": sid})  # 那一轮已经结束
    assert r.status_code == 403 and "已经结束" in r.json()["detail"]
    monkeypatch.setattr(server, "ATTACHMENT_LIMIT", 10)
    net.send_message(dm.id, "再发文件", actor=maya, background=False)
    assert env.demo.uploads[-1][0] == 413 and "暂存失败（413）" in dm.messages[-1].text
    assert not dm.messages[-1].attachments


def test_two_platform_agents_pass_files_in_chat_and_along_a_delegation(env: Env):
    """两个外部平台 Agent 经 Matrix 传文件：群聊里 A 发的，B 被 @ 时 GET /mcp/remote/files/<sha> 取得到；
    A 委托 B 时 attachments 写暂存的 file_id 转交，B 在任务里取到，又 send_file 随交付交回，A 收交付那一轮取得到。
    取文件只给这一轮本来拿得到的（同云侧挂进 attachments/ 的口径），别的 404。"""
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    b = net.add_remote_agent("demo", "travel", API_KEY, actor=maya)
    env.connect_mcp(a.id, maya, "expense")
    key_b = env.connect_mcp(b.id, maya, "travel")
    g = net.create_project("报销", "", [a.id, b.id], actor=maya)
    chat = net.project_chat(g.id, actor=maya)
    # 群聊：A 发的文件，B 下次被 @ 时取得到
    net.send_message(chat.id, f"@{a.name} 发文件", actor=maya, background=False)
    assert [x["name"] for x in chat.messages[-1].attachments] == ["报销助手汇总.md"]
    net.send_message(chat.id, f"@{b.name} 看看", actor=maya, background=False)
    assert env.demo.downloads[-1] == ("travel", 200, report("expense"))
    assert "收到文件：报销助手汇总.md" in chat.messages[-1].text
    # 委托：A 暂存后把 file_id 写进 attachments；B 在任务里取到，再发一份自己的随交付交回
    n = len(env.demo.downloads)
    net.send_message(chat.id, f"@{a.name} 委托「{b.name}」带文件：收下附件，再发文件", actor=maya, background=False)
    net.wait_idle(chat.id)
    t = next(t for t in net.store.tasks.values() if t.delegatee_id == b.id)
    assert [x["name"] for x in t.attachments] == ["报销助手汇总.md"]
    assert [x["name"] for x in t.sent_files] == ["差旅助手汇总.md"] and not t.pending_files
    assert t.result["sent_files"] == ["差旅助手汇总.md"]
    got = env.demo.downloads[n:]
    assert ("travel", 200, report("expense")) in got and ("expense", 200, report("travel")) in got
    # 人在任务详情里下载得到交回的文件；任务里发的不进群聊
    f, _ = net.attachment_file(chat.id, t.sent_files[0]["sha"], actor=maya)
    assert f.read_bytes() == report("travel")
    assert all("差旅助手汇总.md" not in [x["name"] for x in m.attachments] for m in chat.messages)
    # 一轮结束：拿 B 的密钥和旧会话 id 取文件，一律 403；没登录的陌生密钥 401
    r = env.client.get(f"/mcp/remote/files/{t.sent_files[0]['sha']}",
                       headers={"Authorization": f"Bearer {key_b}", "X-Session-Id": t.dsh_session_id})
    assert r.status_code == 403
    assert env.client.get(f"/mcp/remote/files/{t.sent_files[0]['sha']}",
                          headers={"Authorization": "Bearer x", "X-Session-Id": "y"}).status_code == 401


def test_platform_agent_only_fetches_files_of_its_round(env: Env):
    """取文件的口径：这一轮拿得到的才给，别的对话里的附件 404（不变量 13、28）。"""
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    env.connect_mcp(a.id, maya)
    other = net.direct_chat(net.create_agent("Other", "o", [], [], actor=maya).id, actor=maya)
    secret = net.upload_attachment(other.id, "机密.txt", b"top secret", "text/plain", actor=maya)
    dm = net.direct_chat(a.id, actor=maya)
    net.send_message(dm.id, "发文件", actor=maya, background=False)
    live = ExecContext(kind="seat", id=f"{dm.id}/{a.id}", agent_id=a.id, conversation_id=dm.id, project_id=None,
                       authority=[], constraints=[], depth=0, chain=[a.id])
    with net._lock:
        mine = [x["sha"] for x in net._attachments_for(live)]
    assert secret["sha"] not in mine and len(mine) == 1
    net.remote_bindings.bind(a.id, "s-live", type("T", (), {"token": "t-live", "names": None})())
    net._contexts["t-live"] = live
    key = env.connect_mcp(a.id, maya)
    h = {"Authorization": f"Bearer {key}", "X-Session-Id": "s-live"}
    assert env.client.get(f"/mcp/remote/files/{secret['sha']}", headers=h).status_code == 404
    r = env.client.get(f"/mcp/remote/files/{mine[0]}", headers=h)
    assert r.status_code == 200 and r.content == report("expense") and "attachment" in r.headers["content-disposition"]
    net._contexts.pop("t-live")
    net.remote_bindings.unbind(a.id, "s-live")
