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
from matrix.engine import Network, NetworkError
from matrix.remote_agents import PlatformCatalog, PlatformError, parse_platforms
from matrix.runtime import ScriptedRuntime
from matrix.server import create_app
from tests.test_connections import SECRET, FakeMCP, catalog_file
from tests.test_users import users

sys.path.insert(0, str(Path(__file__).parent.parent / "examples" / "remote-agent"))
from demo_platform import API_KEY, DemoPlatform  # noqa: E402


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
        self.demo = DemoPlatform(mcp_post=self._mcp_post)
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

    def connect_mcp(self, agent_id: str, actor: str) -> str:
        """扮演维护人在平台网页上给这个 Agent 填 MCP：Matrix 的地址 + Matrix 签的密钥。"""
        k = self.net.issue_remote_key(agent_id, actor=actor)
        self.demo.mcp["expense"] = {"url": "http://matrix" + k["path"], "headers": {k["header"]: k["value"]}}
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
                        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
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
    r = env.client.post("/mcp/remote", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
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
