"""接入已有的看板系统（examples/dashboard-system）：公开的 HTTP API 经标准 MCP 包装层登记成连接器。
Agent 建草稿、把链接加进资料；有人说发布时，网络按工具标注先给所有者确认（不变量 19），同意才发布。"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from matrix.connector_catalog import ConnectorCatalog
from matrix.engine import Network
from matrix.runtime import ScriptedRuntime
from matrix.scripted import scenario_handler
from tests.test_users import users

EXAMPLE = Path(__file__).resolve().parents[1] / "examples"
sys.path.insert(0, str(EXAMPLE / "dashboard-system"))
sys.path.insert(0, str(EXAMPLE / "delivery-dashboard"))
import board_api  # noqa: E402
import board_mcp  # noqa: E402
import delivery_data_mcp  # noqa: E402


@pytest.fixture
def services():
    data = delivery_data_mcp.serve(0)
    api = board_api.serve(0)
    mcp = board_mcp.serve(0, f"http://127.0.0.1:{api.server_address[1]}")
    for s in (data, api, mcp):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield {"data": f"http://127.0.0.1:{data.server_address[1]}/mcp", "api": f"http://127.0.0.1:{api.server_address[1]}",
           "mcp": f"http://127.0.0.1:{mcp.server_address[1]}/mcp"}
    for s in (data, api, mcp):
        s.shutdown()


def setup(tmp_path: Path, urls: dict):
    catalog = json.loads((EXAMPLE / "dashboard-system" / "connectors.json").read_text(encoding="utf-8"))
    for c in catalog["connectors"]:
        c["url"] = urls["data"] if c["id"] == "delivery-data" else urls["mcp"]
    (tmp_path / "connectors.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    net = Network(ScriptedRuntime(scenario_handler), tmp_path / "data", seed=False,
                  connectors=ConnectorCatalog(tmp_path / "connectors.json"))
    zhang, = users(net, "zhang@demo.local")
    net.ensure_assistant(zhang)
    dash = net.create_agent("看板 Agent", "在看板系统里做看板", [], ["connector.use"], actor=zhang)
    net.authorize_connector("delivery-data", {"token": "demo-huadong"}, actor=zhang, agent_ids=[dash.id])
    net.authorize_connector("boards", {}, actor=zhang, agent_ids=[dash.id])
    g = net.create_project("华东交付", "", [dash.id], actor=zhang)
    return net, zhang, g, net.project_chat(g.id, actor=zhang)


def published(api: str) -> list[dict]:
    with urllib.request.urlopen(f"{api}/api/dashboards") as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


def ask_publish(net: Network, chat_id: str, zhang: str):
    t = threading.Thread(target=net.send_message, args=(chat_id, "@看板 Agent 发布这个看板"),
                         kwargs={"actor": zhang, "background": False})
    t.start()
    end = time.time() + 10
    while time.time() < end:
        ap = [x for x in net.pending_approvals(zhang) if x.kind == "connector_write"]
        if ap:
            return t, ap[0]
        time.sleep(0.02)
    raise AssertionError("发布前没有确认卡")


def test_draft_then_confirmed_publish(tmp_path: Path, services: dict):
    """不变量 19：建草稿不问；发布前给所有者确认卡（参数原样），同意才发；链接进资料，连接器面板认出看板系统。"""
    net, zhang, g, chat = setup(tmp_path, services)
    net.send_message(chat.id, "@看板 Agent 在看板系统里做一个华东交付周报看板", actor=zhang, background=False)
    assert "草稿建好了" in chat.messages[-1].text
    b, = published(services["api"])
    assert b["status"] == "草稿" and b["title"] == "华东交付周报"
    ref, = g.refs
    assert ref.location == b["url"] and ref.note.startswith("草稿") and "board.localhost" in ref.location
    panel = net.project_detail(g.id, actor=zhang)["connector_panel"]["services"]
    assert any(s.get("connector_id") == "boards" for s in panel)

    t, ap = ask_publish(net, chat.id, zhang)
    assert ap.tool == "publish_dashboard" and ap.args == {"id": b["id"]} and "公开" in ap.reason
    assert published(services["api"])[0]["status"] == "草稿"  # 等确认时没有发出
    net.decide_approval(ap.id, True, actor=zhang)
    t.join(10)
    assert published(services["api"])[0]["status"] == "已发布"
    assert g.refs[0].note.startswith("已发布 v1") and len(g.refs) == 1
    assert "已发布 v1" in chat.messages[-1].text


def test_rejected_publish_is_not_sent(tmp_path: Path, services: dict):
    """不变量 19：所有者不同意，看板系统里仍是草稿，Agent 如实说没发布。"""
    net, zhang, g, chat = setup(tmp_path, services)
    net.send_message(chat.id, "@看板 Agent 在看板系统里做一个华东交付周报看板", actor=zhang, background=False)
    t, ap = ask_publish(net, chat.id, zhang)
    net.decide_approval(ap.id, False, actor=zhang)
    t.join(10)
    assert published(services["api"])[0]["status"] == "草稿"
    assert "没有发布" in chat.messages[-1].text and g.refs[0].note.startswith("草稿")


def test_wrapper_checks_ids_before_building_paths():
    """包装层把 id 拼进 HTTP 路径：不是看板系统的 id 格式就不发请求（接入指南里的“参数先校验”）。"""
    with pytest.raises(RuntimeError, match="id 不对"):
        board_mcp.Api("http://127.0.0.1:9").call("get_dashboard", {"id": "../../admin"})
