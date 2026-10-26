"""matrix-dsh-check 里不依赖真实 dsh 的部分：配置解析，以及插件清单与运行时的覆盖层保持一致。"""

from __future__ import annotations

from matrix.dsh_check import KNOWN_TOOLS, disabled_in, plugin_ids
from matrix.runtime import ALWAYS_DISABLED, AUTHORITY_PLUGINS, build_patch_yaml

CONFIG = """\
- id: tool-bash
  disabled: true
- id: tool-web
  options:
    disabled: true
- id: skill-filesystem
  options:
    includeDefaultRoots: false
- id: tool-fs
"""


def test_plugin_ids_and_disabled():
    assert plugin_ids(CONFIG) == {"tool-bash", "tool-web", "skill-filesystem", "tool-fs"}
    assert disabled_in(CONFIG, "tool-bash")
    assert not disabled_in(CONFIG, "tool-web")      # 选项里的 disabled 不算插件被禁用
    assert not disabled_in(CONFIG, "tool-fs")       # 最后一个、没有属性
    assert not disabled_in(CONFIG, "tool-missing")


def test_overlay_parsed_back():
    """没有任何权限的覆盖层：始终禁用的与按权限禁用的插件，解析回来都是禁用的。"""
    overlay = build_patch_yaml([])
    for pid in [*ALWAYS_DISABLED, *(p for ps in AUTHORITY_PLUGINS.values() for p in ps)]:
        assert disabled_in(overlay, pid), pid
    granted = build_patch_yaml(list(AUTHORITY_PLUGINS))
    assert not any(disabled_in(granted, p) for ps in AUTHORITY_PLUGINS.values() for p in ps)


def test_managed_tools_are_known():
    """运行时按权限禁用或始终禁用的工具插件，冒烟检查都认得（否则升级时会被当成新工具误报）。"""
    managed = {p for ps in AUTHORITY_PLUGINS.values() for p in ps} | set(ALWAYS_DISABLED)
    assert {p for p in managed if p.startswith("tool-")} <= KNOWN_TOOLS


def test_provider_overlay_names_only_the_key_env():
    """平台配的供应商：覆盖层里是 llm-pi-ai 这一段，密钥只有环境变量名（冒烟检查的 provider / key-hidden 在真实 dsh 上核对）。"""
    from matrix.model_catalog import ModelPreset, Provider, llm_config
    prov = Provider(id="hi", name="Hi", api="openai-completions", base_url="https://api.example.com/v1")
    preset = ModelPreset(id="p", label="P", provider="hi", model="m1")
    overlay = build_patch_yaml([], model=preset.run_model(), llm_provider={"id": "hi", "config": llm_config(prov, preset)})
    assert {"llm-pi-ai", "agent-default-model"} <= plugin_ids(overlay)
    assert '"apiKeyEnv": "MATRIX_LLM_HI_API_KEY"' in overlay
    assert "llm-pi-ai" not in plugin_ids(build_patch_yaml([], model=preset.run_model()))  # 不用平台供应商时不碰它
