"""云侧预置模型目录（matrix/model_catalog.py）：部署方维护文件、改了即生效；云侧 Agent 只能选目录里的模型，
下架的改用默认；端侧不受约束；没有文件时保持自由填写。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from matrix.engine import Network, NetworkError
from matrix.model_catalog import ModelCatalog, parse_catalog
from matrix.runtime import ScriptCall, ScriptedRuntime, build_patch_yaml
from matrix.server import create_app
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
    from matrix import auth
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


def test_catalog_save_checks_rev_and_content(tmp_path: Path):
    """管理后台整份保存：先校验，带着读到时的版本，不覆盖别处的改动；写坏的不落盘。"""
    from matrix.model_catalog import CatalogConflict
    cat = ModelCatalog(tmp_path / "models.json")
    assert cat.admin()["rev"] == "" and cat.active is False
    cat.save({"models": [FLASH]}, rev="")  # 第一次保存就建出文件、启用目录
    assert cat.active and [p.id for p in cat.presets()] == ["flash"]
    rev = cat.admin()["rev"]
    with pytest.raises(CatalogConflict):
        cat.save({"models": []}, rev="")  # 拿着旧版本
    with pytest.raises(ValueError, match="default 不在"):
        cat.save({"models": [FLASH], "default": "nope"}, rev=rev)
    with pytest.raises(ValueError, match="重复"):
        cat.save({"models": [FLASH, FLASH]}, rev=rev)
    assert cat.admin()["rev"] == rev  # 不合法的没有写进去
    cat.save({"models": [FLASH, OPUS], "default": "opus"}, rev=rev)
    assert cat.default().id == "opus" and len(cat.presets()) == 2
    saved = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))
    assert saved["default"] == "opus" and saved["models"][1]["reasoning_effort"] == "high"
    assert "description" not in saved["models"][0]  # 空字段不写


def test_admin_configures_models_for_cloud_agents(tmp_path: Path):
    """管理员在后台配置模型；用户创建云侧 Agent 时只能选这些；不是管理员看不到也改不了。"""
    net = Network(ScriptedRuntime(lambda c: "ok"), tmp_path, seed=False)
    admin, user = TestClient(create_app(net)), TestClient(create_app(net))
    login(admin, "admin@example.com")
    login(user, "user@example.com")
    free = user.post("/api/agents", json={"name": "Free", "description": "启用目录前自己填的模型",
                                          "model": {"provider": "hi-code-claude", "model": "claude-opus-5-5",
                                                    "reasoning_effort": "high"}}).json()
    assert user.get("/api/admin/models").status_code == 404
    assert user.put("/api/admin/models", json={"models": [FLASH]}).status_code == 404

    d = admin.get("/api/admin/models").json()
    assert d["active"] is False and d["models"] == []
    assert [(u["model"], u["agents"]) for u in d["unlisted"]] == [("claude-opus-5-5", 1)]
    seq = net.bus.last_seq
    d = admin.put("/api/admin/models", json={"models": [FLASH], "default": "flash", "rev": d["rev"]}).json()
    assert d["active"] is True and [m["id"] for m in d["models"]] == ["flash"]
    assert any(ev.type == "models.updated" for ev in net.bus.wait(seq, 0.1))  # 打开着的页面据此刷新模型列表
    info = lambda: next(a for a in user.get("/api/network").json()["agents"] if a["id"] == free["id"])["model_info"]  # noqa: E731
    assert info()["unavailable"] is True  # 对不上：改用默认
    assert admin.put("/api/admin/models", json={"models": [FLASH], "rev": ""}).status_code == 409  # 旧版本

    # 把在用的模型加进目录：对得上，Agent 自动用回原来的
    d = admin.put("/api/admin/models", json={"models": [FLASH, OPUS], "default": "flash", "rev": d["rev"]}).json()
    assert {m["id"]: m["agents"] for m in d["models"]} == {"flash": 0, "opus": 1} and d["unlisted"] == []
    assert info()["source"] == "preset" and info()["preset"] == "opus"

    # 用户这边：/api/models 就是后台配的目录；创建云侧 Agent 只能选里面的
    m = user.get("/api/models").json()
    assert m["active"] and [x["id"] for x in m["models"]] == ["flash", "opus"] and m["default"] == "flash"
    a = user.post("/api/agents", json={"name": "Picked", "description": "选了预置模型", "model_preset": "opus"}).json()
    assert a["model_info"]["preset"] == "opus"
    bad = user.post("/api/agents", json={"name": "Bad", "description": "不在目录里",
                                         "model": {"provider": "x", "model": "y"}})
    assert bad.status_code == 409 and "不在预置模型里" in bad.json()["detail"]
    assert admin.put("/api/admin/models", json={"models": [{**FLASH, "provider": ""}], "rev": d["rev"]}).status_code == 422
    assert admin.get("/api/admin/models").json()["models"][1]["agents"] == 2


HI = {"id": "hi-code", "name": "Hi Code", "api": "anthropic-messages", "base_url": "https://api.example.com/v1"}


def test_catalog_providers_are_checked(tmp_path: Path):
    """平台配的供应商：协议、地址、推理强度整份校验；文件里只有协议与地址，混进来的密钥写不进去。"""
    ours = {"id": "o", "label": "Opus", "provider": "hi-code", "model": "claude-opus-5-5", "reasoning_effort": "high",
            "context_window": 200000, "image": True}
    presets, _, providers = parse_catalog({"models": [FLASH, ours], "providers": [HI]})
    assert providers[0].key_env == "MATRIX_LLM_HI_CODE_API_KEY"  # 名字里有 KEY：dsh 把它从 Agent 的命令环境里滤掉
    assert presets[1].context_window == 200000 and presets[1].image is True
    for bad, why in (({"models": [], "providers": [{**HI, "api": "grpc"}]}, "api 只能是"),
                     ({"models": [], "providers": [{**HI, "base_url": "file:///etc/passwd"}]}, "base_url"),
                     ({"models": [], "providers": [HI, HI]}, "重复"),
                     ({"models": [{**ours, "reasoning_effort": "turbo"}], "providers": [HI]}, "推理强度"),
                     ({"models": [{**ours, "max_tokens": -1}], "providers": [HI]}, "正整数")):
        with pytest.raises(ValueError, match=why):
            parse_catalog(bad)
    # dsh 里已配置的供应商不校验推理强度（取值由 dsh 那边决定）
    assert parse_catalog({"models": [{**OPUS, "reasoning_effort": "turbo"}]})[0][0].reasoning_effort == "turbo"

    cat = ModelCatalog(tmp_path / "models.json")
    cat.save({"models": [ours], "providers": [{**HI, "api_key": "sk-secret"}]}, "")
    text = (tmp_path / "models.json").read_text(encoding="utf-8")
    assert "sk-secret" not in text and json.loads(text)["providers"] == [HI]
    assert cat.provider("hi-code").base_url == HI["base_url"] and cat.provider("deepseek-official") is None


def test_provider_key_only_reaches_the_dsh_process(tmp_path: Path):
    """供应商密钥（安全红线：凭证只进 vault）：管理员只写不读；不进目录文件、API、事件；
    云侧这一轮只放进运行时的环境变量，覆盖层里只有变量名；端侧 Agent 拿不到；删掉供应商密钥一起删。"""
    seen: list = []

    def handler(call: ScriptCall) -> str:
        seen.append((call._req.llm_provider, dict(call._req.env)))
        return "ok"

    net = Network(ScriptedRuntime(handler), tmp_path, seed=False)
    admin, user = TestClient(create_app(net)), TestClient(create_app(net))
    login(admin, "admin@example.com")
    login(user, "user@example.com")
    secret = "sk-provider-" + "x" * 20
    ours = {"id": "opus", "label": "Opus", "provider": "hi-code", "model": "claude-opus-5-5", "reasoning_effort": "high"}
    seq = net.bus.last_seq
    rev = admin.get("/api/admin/models").json()["rev"]
    r = admin.put("/api/admin/models", json={"models": [FLASH, ours], "default": "flash", "rev": rev,
                                              "providers": [{**HI, "api_key": secret}]})
    assert r.status_code == 200 and r.json()["providers"] == [{**HI, "has_key": True}]
    assert secret not in r.text and secret not in admin.get("/api/admin/models").text
    assert secret not in (tmp_path / "models.json").read_text(encoding="utf-8") and secret not in (tmp_path / "state.json").read_text(encoding="utf-8")
    assert secret.encode() not in (tmp_path / "vault" / "model-provider-hi-code.bin").read_bytes()  # 加密保存
    assert "base_url" not in user.get("/api/models").text  # 用户只看到模型，看不到供应商地址

    a = net.create_agent("A", "A", [], [], model_preset="opus")
    b = net.create_agent("B", "B", [], [])  # 默认模型用的是 dsh 里已配置的供应商
    talk(net, a, None, "x")
    talk(net, b, None, "y")
    (prov, env), (none, empty) = seen
    assert env == {"MATRIX_LLM_HI_CODE_API_KEY": secret} and none is None and empty == {}
    assert prov["id"] == "hi-code" and secret not in json.dumps(prov)
    assert prov["config"]["apiKeyEnv"] == "MATRIX_LLM_HI_CODE_API_KEY"
    assert prov["config"]["models"] == [{"id": "claude-opus-5-5", "name": "Opus", "input": ["text"],
                                         "reasoningEfforts": {"high": "high"}}]
    overlay = build_patch_yaml([], model={"provider": "hi-code", "model": "claude-opus-5-5"}, llm_provider=prov)
    assert "- id: llm-pi-ai" in overlay and secret not in overlay
    assert all(secret not in json.dumps(ev.data, ensure_ascii=False, default=str) for ev in net.bus.wait(seq, 0.1))
    assert secret not in (tmp_path / "usage.jsonl").read_text(encoding="utf-8")

    a.device_runtime = "dev_x"  # 端侧：不受目录约束，也拿不到平台的供应商与密钥
    assert net._llm_provider(a) == (None, {})

    # 不填密钥 = 沿用；删掉供应商，密钥一起删
    rev = admin.get("/api/admin/models").json()["rev"]
    r = admin.put("/api/admin/models", json={"models": [FLASH, ours], "rev": rev, "providers": [HI]})
    assert r.json()["providers"][0]["has_key"] is True
    r = admin.put("/api/admin/models", json={"models": [FLASH], "rev": r.json()["rev"]})
    assert r.json()["providers"] == [] and not (tmp_path / "vault" / "model-provider-hi-code.bin").exists()
