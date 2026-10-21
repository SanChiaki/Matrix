"""云侧预置模型目录（agentnet/model_catalog.py）：部署方维护文件、改了即生效；云侧 Agent 只能选目录里的模型，
下架的改用默认；端侧不受约束；没有文件时保持自由填写。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentnet.engine import Network, NetworkError
from agentnet.model_catalog import ModelCatalog, parse_catalog
from agentnet.runtime import ScriptCall, ScriptedRuntime
from agentnet.server import create_app
from tests.test_api import login
from tests.test_network import talk

FLASH = {"id": "flash", "label": "DeepSeek Flash", "provider": "deepseek-official", "model": "deepseek-flash"}
OPUS = {"id": "opus", "label": "Claude Opus", "provider": "hi-code-claude", "model": "claude-opus-5-5",
        "reasoning_effort": "high"}


def write_catalog(path: Path, models: list[dict], default: str | None = None) -> None:
    """写目录文件并推进修改时间（同一秒内连写两次时文件系统的 mtime 可能不变）。"""
    data: dict = {"models": models}
    if default:
        data["default"] = default
    old = path.stat().st_mtime if path.exists() else 0
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.utime(path, (old + 10, old + 10))


def recording(models: list):
    def handler(call: ScriptCall) -> str:
        models.append(call._req.model)
        return "ok"
    return handler


def test_no_catalog_keeps_free_form(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda c: "ok"), tmp_path)
    assert net.models.active is False
    a = net.create_agent("A", "A", [], [], model={"provider": "p", "model": "m"})
    assert a.model == {"provider": "p", "model": "m"} and a.model_preset is None
    assert net.model_info(a)["source"] == "custom"
    with pytest.raises(NetworkError, match="没有配置预置模型"):
        net.create_agent("B", "B", [], [], model_preset="flash")


def test_cloud_agent_picks_preset_and_runtime_gets_its_model(tmp_path: Path):
    write_catalog(tmp_path / "models.json", [FLASH, OPUS], default="flash")
    seen: list = []
    net = Network(ScriptedRuntime(recording(seen)), tmp_path)
    a = net.create_agent("A", "A", [], [], model_preset="opus")
    b = net.create_agent("B", "B", [], [])  # 不选 = 目录默认
    assert a.model_preset == "opus" and a.model is None
    talk(net, a, None, "x")
    talk(net, b, None, "y")
    assert seen == [{"provider": "hi-code-claude", "model": "claude-opus-5-5", "reasoning_effort": "high"},
                    {"provider": "deepseek-official", "model": "deepseek-flash"}]
    assert net.model_info(b)["source"] == "default" and net.model_info(b)["label"] == "DeepSeek Flash"


def test_cloud_agent_cannot_use_unlisted_model(tmp_path: Path):
    write_catalog(tmp_path / "models.json", [FLASH])
    net = Network(ScriptedRuntime(lambda c: "ok"), tmp_path)
    with pytest.raises(NetworkError, match="没有这个预置模型"):
        net.create_agent("A", "A", [], [], model_preset="gpt-9")
    with pytest.raises(NetworkError, match="不在预置模型里"):
        net.create_agent("A", "A", [], [], model={"provider": "x", "model": "y"})
    # 旧客户端给的具体配置对得上目录里的某个模型：算选了它
    a = net.create_agent("A", "A", [], [], model={"provider": "deepseek-official", "model": "deepseek-flash"})
    assert a.model_preset == "flash" and a.model is None


def test_removed_preset_falls_back_to_default_without_restart(tmp_path: Path):
    path = tmp_path / "models.json"
    write_catalog(path, [FLASH, OPUS], default="flash")
    seen: list = []
    net = Network(ScriptedRuntime(recording(seen)), tmp_path)
    a = net.create_agent("A", "A", [], [], model_preset="opus")
    write_catalog(path, [FLASH], default="flash")  # 部署方下架 opus：不重启
    info = net.model_info(a)
    assert info["source"] == "default" and info["unavailable"] is True and info["preset"] == "flash"
    talk(net, a, None, "x")
    assert seen == [{"provider": "deepseek-official", "model": "deepseek-flash"}]
    assert a.model_preset == "opus"  # 选择保留：重新上架后自动恢复
    write_catalog(path, [FLASH, OPUS], default="flash")
    assert net.model_info(a) == {"source": "preset", "preset": "opus", "label": "Claude Opus", "unavailable": False,
                                 "model": {"provider": "hi-code-claude", "model": "claude-opus-5-5",
                                           "reasoning_effort": "high"}}
    path.unlink()  # 删掉文件：回到不启用目录
    assert net.models.active is False and net.model_info(a)["source"] == "runtime"


def test_legacy_free_form_agent_after_catalog_enabled(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda c: "ok"), tmp_path)
    a = net.create_agent("A", "A", [], [], model={"provider": "old", "model": "legacy"})
    b = net.create_agent("B", "B", [], [], model={"provider": "deepseek-official", "model": "deepseek-flash"})
    write_catalog(tmp_path / "models.json", [FLASH])  # 没有 default
    assert net.model_info(a) == {"source": "runtime", "preset": None, "model": None, "label": None, "unavailable": True}
    assert net.model_info(b)["source"] == "preset" and net.model_info(b)["preset"] == "flash"
    net.update_agent(a.id, description="改个描述")  # 不动模型就不校验，旧配置保留
    assert a.model == {"provider": "old", "model": "legacy"}
    net.update_agent(a.id, model=None, model_preset="flash")
    assert a.model is None and a.model_preset == "flash"


def test_device_agent_not_bound_by_catalog(tmp_path: Path):
    write_catalog(tmp_path / "models.json", [FLASH], default="flash")
    seen: list = []
    net = Network(ScriptedRuntime(recording(seen)), tmp_path)
    a = net.create_agent("A", "A", [], [])
    a.device_runtime = "my-laptop"  # 端侧：用户自己 harness 里的模型
    net.update_agent(a.id, model={"provider": "anthropic", "model": "sonnet"})
    assert a.model == {"provider": "anthropic", "model": "sonnet"} and net.model_info(a)["source"] == "custom"
    with pytest.raises(NetworkError, match="只用于平台云侧"):
        net.update_agent(a.id, model_preset="flash")
    net.update_agent(a.id, model=None)
    assert net.model_info(a)["source"] == "runtime" and net.run_model(a) is None  # harness 默认，不套用云侧默认


def test_invalid_catalog_does_not_lift_restriction(tmp_path: Path):
    path = tmp_path / "models.json"
    path.write_text("{not json", encoding="utf-8")
    cat = ModelCatalog(path)
    assert cat.active is True and cat.presets() == [] and cat.error  # 第一次就坏：启用但为空
    write_catalog(path, [FLASH])
    assert [p.id for p in cat.presets()] == ["flash"]
    old = path.stat().st_mtime
    path.write_text(json.dumps({"models": [FLASH, {"id": "x"}]}), encoding="utf-8")
    os.utime(path, (old + 10, old + 10))
    assert [p.id for p in cat.presets()] == ["flash"] and "缺少 provider" in cat.error  # 坏了沿用上一份
    for bad, why in (({"models": [FLASH, FLASH]}, "重复"), ({"models": [FLASH], "default": "nope"}, "default"),
                     ({"models": [{**FLASH, "id": "a b"}]}, "id 无效"), ([], "顶层")):
        with pytest.raises(ValueError, match=why):
            parse_catalog(bad)


def test_models_api_and_agent_preset(tmp_path: Path):
    path = tmp_path / "models.json"
    write_catalog(path, [FLASH, OPUS], default="flash")
    net = Network(ScriptedRuntime(lambda c: "ok"), tmp_path, seed=False)
    c = TestClient(create_app(net))
    assert c.get("/api/models").status_code == 401  # 要登录
    login(c)
    cat = c.get("/api/models").json()
    assert cat["active"] is True and cat["default"] == "flash" and [m["id"] for m in cat["models"]] == ["flash", "opus"]

    a = c.post("/api/agents", json={"name": "Writer", "description": "写作", "model_preset": "opus"}).json()
    assert a["model_preset"] == "opus" and a["model_info"]["label"] == "Claude Opus"
    assert c.post("/api/agents", json={"name": "Bad", "description": "xx", "model_preset": "nope"}).status_code == 409
    assert c.patch(f"/api/agents/{a['id']}", json={"description": "写作与润色"}).json()["model_preset"] == "opus"
    r = c.patch(f"/api/agents/{a['id']}", json={"model": None, "model_preset": None}).json()
    assert r["model_preset"] is None and r["model_info"]["source"] == "default"
    assert c.patch(f"/api/agents/{a['id']}", json={"model": {"provider": "x", "model": "y"}}).status_code == 409

    write_catalog(path, [OPUS])  # 增减模型即时反映到 API
    assert [m["id"] for m in c.get("/api/models").json()["models"]] == ["opus"]


def test_moving_between_cloud_and_device_keeps_a_valid_model(tmp_path: Path):
    from agentnet import auth
    write_catalog(tmp_path / "models.json", [FLASH, OPUS], default="flash")
    net = Network(ScriptedRuntime(lambda c: "ok"), tmp_path, seed=False)
    user, _, _ = auth.register(net.store, "owner@example.com", "password-123")
    dev, _ = auth.issue_device_token(net.store, user.id, "laptop")
    a = net.create_agent("A", "A", [], [], model_preset="opus", actor=user.id)
    net.update_agent(a.id, device_runtime=dev.id, actor=user.id)  # 去端侧：预置模型不适用，改回 harness 默认
    assert a.model_preset is None and a.model is None
    net.update_agent(a.id, model={"provider": "hi-code-claude", "model": "claude-opus-5-5", "reasoning_effort": "high"},
                     actor=user.id)
    net.update_agent(a.id, device_runtime=None, actor=user.id)  # 回云侧：对得上目录就沿用
    assert a.model_preset == "opus" and a.model is None
    net.update_agent(a.id, device_runtime=dev.id, actor=user.id)
    net.update_agent(a.id, model={"provider": "anthropic", "model": "sonnet"}, actor=user.id)
    net.update_agent(a.id, device_runtime=None, actor=user.id)  # 对不上：改回默认，不报错
    assert a.model_preset is None and a.model is None and net.model_info(a)["source"] == "default"
