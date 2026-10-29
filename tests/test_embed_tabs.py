"""外部页面标签：资料里的链接可以嵌进项目页，只限部署方在连接器目录里允许嵌入（embed）的域名；只有主持人设，
档案变化留痕；按地址认，主持人指定连接器不能绕过；部署方撤了允许，标签就不显示。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from matrix.connector_catalog import ConnectorCatalog, parse_catalog
from matrix.engine import Network, NetworkError
from matrix.runtime import ScriptedRuntime
from tests.test_users import client, joint, users


def catalog(tmp_path: Path, embed: bool = True) -> Path:
    p = tmp_path / "connectors.json"
    p.write_text(json.dumps({"connectors": [
        {"id": "boards", "name": "看板系统", "url": "https://bi.example.com/mcp", "domains": ["bi.example.com"], "embed": embed},
        {"id": "docs", "name": "文档库", "url": "https://docs.example.com/mcp", "domains": ["docs.example.com"]}]}),
        encoding="utf-8")
    return p


def setup(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path / "data", seed=False,
                  connectors=ConnectorCatalog(catalog(tmp_path)))
    host, guest = users(net, "host@x.io", "guest@x.io")
    g, _ = joint(net, host, "guest@x.io", guest)
    board = net.add_ref(g.id, "华东周报", "https://bi.example.com/d/1", actor=host)
    doc = net.add_ref(g.id, "需求文档", "https://docs.example.com/p/9", actor=host)
    return net, host, guest, g, board, doc


def test_catalog_embed_needs_domains():
    with pytest.raises(ValueError, match="embed"):
        parse_catalog({"connectors": [{"id": "x", "url": "https://x.io/mcp", "embed": True}]})


def test_only_allowed_domains_become_tabs(tmp_path: Path):
    net, host, guest, g, board, doc = setup(tmp_path)
    with pytest.raises(NetworkError, match="只有主持人"):
        net.set_ref_tab(g.id, board.id, True, actor=guest)
    with pytest.raises(NetworkError, match="不在部署方允许嵌入"):
        net.set_ref_tab(g.id, doc.id, True, actor=host)
    net.set_ref_connector(g.id, doc.id, "boards", actor=host)  # 指定连接器不能绕过域名白名单
    with pytest.raises(NetworkError, match="不在部署方允许嵌入"):
        net.set_ref_tab(g.id, doc.id, True, actor=host)
    net.set_ref_tab(g.id, board.id, True, actor=host)
    d = net.project_detail(g.id, actor=guest)  # 参与者也看得到这个标签
    assert d["embed_tabs"] == [{"ref_id": board.id, "label": "华东周报", "url": "https://bi.example.com/d/1", "service": "看板系统"}]
    assert d["embeddable"] == {board.id: "看板系统"}
    assert "主持人把「华东周报」设成了项目页上的标签（看板系统）" in net.project_chat(g.id, actor=host).messages[-1].text
    log = (net.store.project_dir(g.id).parent / "log.jsonl").read_text(encoding="utf-8")
    assert '"action": "ref.tab"' in log
    net.set_ref_tab(g.id, board.id, False, actor=host)
    assert net.project_detail(g.id, actor=host)["embed_tabs"] == []


def test_tab_disappears_when_deployer_withdraws_embed(tmp_path: Path):
    net, host, _, g, board, _ = setup(tmp_path)
    net.set_ref_tab(g.id, board.id, True, actor=host)
    net.connectors = ConnectorCatalog(catalog(tmp_path, embed=False))
    assert net.project_detail(g.id, actor=host)["embed_tabs"] == []
    assert board.tab  # 设置保留：部署方再允许就回来


def test_tab_api(tmp_path: Path):
    net, host, guest, g, board, _ = setup(tmp_path)
    h, gu = client(net, "host@x.io"), client(net, "guest@x.io")
    assert gu.put(f"/api/projects/{g.id}/refs/{board.id}/tab", json={"enabled": True}).status_code == 409
    r = h.put(f"/api/projects/{g.id}/refs/{board.id}/tab", json={"enabled": True})
    assert r.status_code == 200 and r.json()["embed_tabs"][0]["ref_id"] == board.id
    assert h.get("/api/connectors").json()[0]["embed"] is True
