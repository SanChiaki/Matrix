"""交付看板示例（examples/delivery-dashboard，docs/project-publishing.md 第 13 节）：在发布与订阅之上搭起来的项目。
用演示的交付数据服务（真的走远程 MCP）跑一遍：看板 Agent 用所有者授权的数据做看板、登记；主持人发布技能包与看板交付件；
读者的助理订阅后按快照回答；数据权限跟着人走。"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from matrix import plugins as pluginfs
from matrix.connector_catalog import ConnectorCatalog
from matrix.engine import Network
from matrix.plugin_check import lint, load
from matrix.runtime import ScriptedRuntime
from matrix.scripted import scenario_handler
from tests.test_users import users

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "delivery-dashboard"
sys.path.insert(0, str(EXAMPLE))
import delivery_data_mcp  # noqa: E402


@pytest.fixture
def data_service():
    server = delivery_data_mcp.serve(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/mcp"
    server.shutdown()


def network(tmp_path: Path, url: str) -> Network:
    catalog = json.loads((EXAMPLE / "connectors.json").read_text(encoding="utf-8"))
    catalog["connectors"][0]["url"] = url
    (tmp_path / "connectors.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    return Network(ScriptedRuntime(scenario_handler), tmp_path / "data", seed=False,
                   connectors=ConnectorCatalog(tmp_path / "connectors.json"),
                   plugins=pluginfs.PluginCatalog(EXAMPLE / "plugins.json"))


def test_example_plugin_package_is_valid():
    """示例插件过 matrix-plugin check（同安装时的校验），声明了能在发布物里打开的看板标签与要用的连接器。"""
    files = load(EXAMPLE / "plugins" / "delivery-dashboard")
    m = pluginfs.check_package(files)
    assert m.release_views == ("board",) and m.connectors == ("delivery-data",)
    assert not lint(files, m)


def test_delivery_dashboard_end_to_end(tmp_path: Path, data_service: str):
    """不变量 23：看板 Agent 用张工的授权（只能看华东）做看板；张工发布给李总；李总的助理按快照回答，
    在有不是读者的人的项目里不挂；刷新后跟随档案切新版本。"""
    net = network(tmp_path, data_service)
    zhang, li = users(net, "zhang@demo.local", "li@demo.local")
    for u in (zhang, li):
        net.ensure_assistant(u)
    dash = net.create_agent("看板 Agent", "做交付看板", [], ["file.write", "connector.use"], actor=zhang)
    conn = net.authorize_connector("delivery-data", {"token": "demo-huadong"}, actor=zhang, agent_ids=[dash.id])
    assert conn["status"] == "ok"
    g = net.create_project("华东交付", "", [dash.id], actor=zhang)
    net.install_plugin(g.id, "delivery-dashboard", actor=zhang)
    chat = net.project_chat(g.id, actor=zhang)

    net.send_message(chat.id, "@看板 Agent 做一个华东交付周报看板，每天刷新", actor=zhang, background=False)
    rec = net.plugin_records(g.id, "delivery-dashboard", "dashboards", actor=zhang)[0]
    assert rec["fields"] | {"owner_agent": None} == {"title": "华东交付周报", "file": "huadong-weekly.json", "scope": "华东",
                                                      "owner_agent": None, "status": "草稿", "refresh": "每天"}
    spec = json.loads(net.project_file(g.id, "huadong-weekly.json", actor=zhang).read_text(encoding="utf-8"))
    assert spec["scope"] == "华东" and {k["label"] for k in spec["kpis"]} >= {"在建项目", "按期率", "延期项目"}
    assert spec["charts"][0]["points"][-1]["y"] == spec["kpis"][1]["value"]  # 走势的最后一周就是现在的按期率
    assert "已生成" in chat.messages[-1].text

    net.send_message(chat.id, "@看板 Agent 做一个华南交付周报看板", actor=zhang, background=False)
    assert "没有 华南 的数据权限" in chat.messages[-1].text  # 数据权限跟着人走，不绕过

    fact = net.create_release(g.id, "huadong-weekly", "fact", "华东交付周报看板", files=["huadong-weekly.json"],
                              view={"plugin": "delivery-dashboard", "tab": "board"}, readers=["li@demo.local"],
                              follow=True, actor=zhang)
    method = net.create_release(g.id, "dash-method", "method", "做交付看板的技能包", skills=["delivery-dashboard"],
                                public=True, actor=zhang)
    li_bot = net.assistant_of(li)
    net.set_subscription(fact.id, li_bot.id, True, actor=li)
    dm = net.direct_chat(li_bot.id, actor=li)
    net.send_message(dm.id, "华东这周交付情况怎么样？", actor=li, background=False)
    answer = dm.messages[-1].text
    assert "华东交付周报看板」v1（快照于" in answer and "按期率" in answer

    net.send_message(chat.id, "@看板 Agent 刷新看板「华东交付周报」", actor=zhang, background=False)
    assert fact.version == 2 and method.version == 1  # 跟随档案：看板文件更新，交付件切新版本
    assert net.release_bridge(fact.id, "context", {}, actor=li)["release"]["version"] == 2
