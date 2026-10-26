"""云侧 Agent 的预置模型目录：由部署方维护的一份 JSON 文件，增减模型不用重启。

文件（默认 `<data>/models.json`，`--models` / `MATRIX_MODELS` 可改）::

    {
      "default": "flash",
      "models": [
        {"id": "flash", "label": "DeepSeek Flash", "provider": "deepseek-official", "model": "deepseek-flash"},
        {"id": "opus", "label": "Claude Opus", "provider": "hi-code-claude", "model": "claude-opus-5-5",
         "reasoning_effort": "high", "description": "长文档与复杂推理"}
      ],
      "providers": [
        {"id": "hi-code", "name": "Hi Code", "api": "anthropic-messages", "base_url": "https://api.example.com"}
      ]
    }

- 没有这个文件：不启用目录，云侧 Agent 照旧可以自由填 provider / model（旧行为）。
- 有这个文件：云侧 Agent 只能从列表里选（`Agent.model_preset`），不选则用 `default`，再没有则用运行时默认。
  从列表里删掉一个模型，用它的 Agent 下一轮起改用默认模型，UI 标出“已下架”。
- 每次读取时比较文件的修改时间，变了就重新加载。文件写坏了保留上一份能用的；第一次就读不出来时按
  “启用、但列表为空”处理（只能用运行时默认），不因为写错文件而放开限制。
- 端侧 Agent 用的是用户自己 harness 里的模型，不受这个目录约束。
- 管理后台也编辑这份文件（`save`）：先整份校验，写临时文件再替换；带着读到时的版本（内容的 sha256），
  文件在这期间被别处改过就拒绝，不覆盖别人的改动。
- 供应商（`providers`）：平台自己配的 OpenAI / Anthropic 兼容服务（协议、地址），模型的 `provider` 写它的 id 就用它；
  不是这里的 id 的，按 dsh 里已配置的供应商名处理（如 `deepseek-official`）。供应商的密钥不在这个文件里：
  由网络加密保管（`vault/model-provider-<id>.bin`），每轮只放进那个 dsh 进程的环境变量（`Provider.key_env`）。
  覆盖层里只写这一轮用到的那一个供应商（dsh 对 `llm-pi-ai` 的配置是整段替换，research: deepseek-harness#model-providers）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PRESET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# dsh `llm-pi-ai` 支持的协议与推理强度（research: deepseek-harness#model-providers）
PROVIDER_APIS = ("openai-completions", "openai-responses", "anthropic-messages")
REASONING_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

class CatalogConflict(ValueError):
    """保存时文件已经不是读到的那一版。"""


@dataclass(frozen=True)
class Provider:
    """平台配的模型供应商。密钥不在这里（见模块说明）。"""
    id: str
    name: str
    api: str
    base_url: str

    @property
    def key_env(self) -> str:
        """交给 dsh 的密钥环境变量名。名字里要有 KEY：dsh 按名字把这类变量从 Agent 的命令环境里滤掉。"""
        return "MATRIX_LLM_" + re.sub(r"[^A-Z0-9]", "_", self.id.upper()) + "_API_KEY"

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "api": self.api, "base_url": self.base_url}

@dataclass(frozen=True)
class ModelPreset:
    id: str
    label: str
    provider: str
    model: str
    reasoning_effort: str | None = None
    description: str = ""
    # 只对平台配的供应商生效（dsh 里已配置的供应商由 dsh 自己的配置决定）
    context_window: int | None = None
    max_tokens: int | None = None
    image: bool = False

    def run_model(self) -> dict[str, str]:
        """交给运行时的形式（与 `Agent.model` 相同）。"""
        m = {"provider": self.provider, "model": self.model}
        if self.reasoning_effort:
            m["reasoning_effort"] = self.reasoning_effort
        return m

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "description": self.description, **self.run_model(),
                "context_window": self.context_window, "max_tokens": self.max_tokens, "image": self.image}


def llm_config(prov: Provider, preset: ModelPreset) -> dict[str, Any]:
    """给 dsh 覆盖层的 `llm-pi-ai` 供应商配置：只有这一个模型，密钥只写环境变量名。"""
    m: dict[str, Any] = {"id": preset.model, "name": preset.label, "input": ["text", "image"] if preset.image else ["text"]}
    if preset.context_window:
        m["contextWindow"] = preset.context_window
    if preset.max_tokens:
        m["maxTokens"] = preset.max_tokens
    if preset.reasoning_effort:  # Matrix 用 dsh 的强度名，原样交给供应商
        m["reasoningEfforts"] = {preset.reasoning_effort: preset.reasoning_effort}
    return {"api": prov.api, "baseURL": prov.base_url, "displayName": prov.name, "apiKeyEnv": prov.key_env,
            "models": [m]}

def _positive(v: Any, where: str) -> int | None:
    if v in (None, ""):
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not 0 < v <= 100_000_000:
        raise ValueError(f"{where} 必须是正整数")
    return v

def parse_providers(data: Any) -> list[Provider]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError("providers 必须是列表")
    out: list[Provider] = []
    for i, p in enumerate(data):
        if not isinstance(p, dict):
            raise ValueError(f"providers[{i}] 必须是对象")
        pid = str(p.get("id") or "").strip()
        if not PRESET_ID.match(pid):
            raise ValueError(f"providers[{i}].id 无效：{pid!r}（字母、数字、. _ -，最长 64）")
        if any(x.id == pid for x in out):
            raise ValueError(f"providers[{i}].id 重复：{pid}")
        api = str(p.get("api") or "").strip()
        if api not in PROVIDER_APIS:
            raise ValueError(f"providers[{i}]（{pid}）的 api 只能是 {' / '.join(PROVIDER_APIS)}")
        url = str(p.get("base_url") or "").strip()
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname or any(c.isspace() for c in url) or len(url) > 500:
            raise ValueError(f"providers[{i}]（{pid}）的 base_url 必须是 http(s) 地址")
        out.append(Provider(id=pid, name=str(p.get("name") or pid).strip(), api=api, base_url=url))
    return out

def parse_catalog(data: Any) -> tuple[list[ModelPreset], str | None, list[Provider]]:
    """校验目录内容；有任何一处不对就整份拒绝（抛 ValueError），避免悄悄丢掉某个模型。"""
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ValueError("顶层必须是 {\"models\": [...]}")
    providers = parse_providers(data.get("providers"))
    ours = {p.id for p in providers}
    presets: list[ModelPreset] = []
    for i, m in enumerate(data["models"]):
        if not isinstance(m, dict):
            raise ValueError(f"models[{i}] 必须是对象")
        pid = str(m.get("id") or "").strip()
        provider, model = str(m.get("provider") or "").strip(), str(m.get("model") or "").strip()
        if not PRESET_ID.match(pid):
            raise ValueError(f"models[{i}].id 无效：{pid!r}（字母、数字、. _ -，最长 64）")
        if any(p.id == pid for p in presets):
            raise ValueError(f"models[{i}].id 重复：{pid}")
        if not provider or not model:
            raise ValueError(f"models[{i}]（{pid}）缺少 provider 或 model")
        effort = str(m.get("reasoning_effort") or "").strip() or None
        if provider in ours and effort and effort not in REASONING_EFFORTS:
            raise ValueError(f"models[{i}]（{pid}）的推理强度只能是 {' / '.join(REASONING_EFFORTS)}")
        presets.append(ModelPreset(id=pid, label=str(m.get("label") or model).strip(), provider=provider,
                                   model=model, reasoning_effort=effort,
                                   description=str(m.get("description") or "").strip(),
                                   context_window=_positive(m.get("context_window"), f"models[{i}].context_window"),
                                   max_tokens=_positive(m.get("max_tokens"), f"models[{i}].max_tokens"),
                                   image=m.get("image") is True))
    default = data.get("default")
    if default is not None and not any(p.id == default for p in presets):
        raise ValueError(f"default 不在 models 里：{default!r}")
    return presets, default, providers


class ModelCatalog:
    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._presets: list[ModelPreset] = []
        self._default: str | None = None
        self._providers: list[Provider] = []
        self._active = False
        self.error: str | None = None  # 最近一次加载失败的原因（只给部署方看日志）

    def _refresh(self) -> None:
        """调用方持有锁。"""
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime
        except OSError:  # 文件删掉了：回到不启用目录
            self._mtime, self._presets, self._default, self._active, self.error = None, [], None, False, None
            self._providers = []
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            self._presets, self._default, self._providers = parse_catalog(
                json.loads(self.path.read_text(encoding="utf-8")))
            self.error = None
        except (OSError, ValueError) as e:  # json.JSONDecodeError 是 ValueError
            self.error = str(e)
            print(f"[matrix] 模型目录 {self.path} 无效，{'沿用上一份' if self._active else '只允许运行时默认'}：{e}",
                  file=sys.stderr)
        self._active = True

    @property
    def active(self) -> bool:
        with self._lock:
            self._refresh()
            return self._active

    def presets(self) -> list[ModelPreset]:
        with self._lock:
            self._refresh()
            return list(self._presets)

    def get(self, preset_id: str | None) -> ModelPreset | None:
        return next((p for p in self.presets() if p.id == preset_id), None) if preset_id else None

    def default(self) -> ModelPreset | None:
        with self._lock:
            self._refresh()
            return next((p for p in self._presets if p.id == self._default), None)

    def providers(self) -> list[Provider]:
        with self._lock:
            self._refresh()
            return list(self._providers)

    def provider(self, provider_id: str | None) -> Provider | None:
        """平台配的供应商；dsh 里已配置的（不在目录里的名字）返回 None。"""
        return next((p for p in self.providers() if p.id == provider_id), None) if provider_id else None

    def match(self, model: dict[str, str] | None) -> ModelPreset | None:
        """旧数据 / 旧客户端给的是 {provider, model, reasoning_effort?}：找到同样配置的预置模型。"""
        if not model:
            return None
        return next((p for p in self.presets() if p.run_model() == model), None)

    def public(self) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            return {"active": self._active, "default": self._default, "models": [p.public() for p in self._presets]}

    def _rev(self) -> str:
        """调用方持有锁。文件内容的版本；没有文件为空串。"""
        try:
            return hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path else ""
        except OSError:
            return ""

    def admin(self) -> dict[str, Any]:
        """管理后台看到的：目录、版本、文件位置、最近一次加载失败的原因。"""
        with self._lock:
            self._refresh()
            return {"active": self._active, "default": self._default, "models": [p.public() for p in self._presets],
                    "providers": [p.public() for p in self._providers],
                    "rev": self._rev(), "path": str(self.path) if self.path else None, "error": self.error}

    def save(self, data: Any, rev: str) -> None:
        """整份替换目录（启用目录也是它：第一次保存就建出文件）。不合法抛 ValueError，版本不对抛 CatalogConflict。"""
        if self.path is None:
            raise ValueError("没有模型目录文件")
        presets, default, providers = parse_catalog(data)
        out: dict[str, Any] = {"default": default} if default else {}
        out["models"] = [{k: v for k, v in p.public().items() if v} for p in presets]
        if providers:  # 只写这几个字段：密钥（api_key 之类）即使混在 data 里也进不了文件
            out["providers"] = [p.public() for p in providers]
        text = json.dumps(out, ensure_ascii=False, indent=2) + "\n"
        with self._lock:
            if self._rev() != rev:
                raise CatalogConflict("模型目录刚被别处改过，刷新后再改")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)
            self._mtime = None  # 同一时间粒度内连改两次时 mtime 可能不变：强制重读
            self._refresh()
