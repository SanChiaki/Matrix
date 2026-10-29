"""Agent 把外部链接加进项目资料（add_link）：档案写入只经档案工具并留痕（不变量 17），连接器面板按域名认出。"""

from __future__ import annotations

import json
from pathlib import Path

from matrix.connector_catalog import ConnectorCatalog
from matrix.engine import Network
from matrix.models import MCP_PREFIX
from matrix.runtime import ScriptCall, ScriptedRuntime
from tests.test_users import users


def setup(tmp_path: Path, handler):
    (tmp_path / "connectors.json").write_text(json.dumps({"connectors": [
        {"id": "boards", "name": "看板系统", "url": "https://bi.example.com/mcp", "domains": ["bi.example.com"]}]}),
        encoding="utf-8")
    net = Network(ScriptedRuntime(handler), tmp_path / "data", seed=False,
                  connectors=ConnectorCatalog(tmp_path / "connectors.json"))
    host, = users(net, "host@x.io")
    a = net.create_agent("Dash", "做看板", [], [], actor=host)
    g = net.create_project("交付", "", [a.id], actor=host)
    return net, host, a, g


def test_agent_adds_and_updates_a_link(tmp_path: Path):
    """不变量 17：add_link 是档案工具（不需要 delegate），写进资料、记日志、群聊里公开一条；同一个地址再加是更新；
    只收 http(s)；域名对得上连接器目录的，连接器面板认出它。"""
    out: list[str] = []

    def handler(call: ScriptCall) -> str:
        out.append(call.tool("add_link", name="华东周报看板（草稿）", url="https://bi.example.com/d/42", note="草稿"))
        out.append(call.tool("add_link", name="华东周报看板", url="https://bi.example.com/d/42"))
        out.append(call.tool("add_link", name="坏的", url="javascript:alert(1)"))
        return "ok"

    net, host, a, g = setup(tmp_path, handler)
    net.send_message(net.project_chat(g.id, actor=host).id, "@Dash 建个看板", actor=host, background=False)
    assert [r.name for r in g.refs] == ["华东周报看板"] and g.refs[0].note == "草稿"
    assert "已加进项目资料" in out[0] and "已更新项目资料" in out[1] and "http 或 https" in out[2]
    log = [json.loads(x) for x in (net.store.project_dir(g.id).parent / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(e["action"], e["by_agent"]) for e in log if e["action"].startswith("ref.")] == \
        [("ref.add", a.id), ("ref.update", a.id)]
    chat = net.project_chat(g.id, actor=host)
    assert any("Dash 把链接加进了资料「华东周报看板（草稿）」" in m.text for m in chat.messages)
    panel = net.project_detail(g.id, actor=host)["connector_panel"]["services"]
    assert any(s.get("connector_id") == "boards" for s in panel), panel


def test_add_link_only_in_projects(tmp_path: Path):
    """私聊里没有档案工具。"""
    seen: list[list[str]] = []

    def handler(call: ScriptCall) -> str:
        seen.append(call.tool_names)
        return "ok"

    net, host, a, _ = setup(tmp_path, handler)
    net.send_message(net.direct_chat(a.id, actor=host).id, "你好", actor=host, background=False)
    assert MCP_PREFIX + "add_link" not in seen[-1]
