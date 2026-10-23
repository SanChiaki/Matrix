"""云侧 Agent 的预置模型目录：由部署方维护的一份 JSON 文件，增减模型不用重启。

文件（默认 `<data>/models.json`，`--models` / `MATRIX_MODELS` 可改）::

    {
      "default": "flash",
      "models": [
        {"id": "flash", "label": "DeepSeek Flash", "provider": "deepseek-official", "model": "deepseek-flash"},
        {"id": "opus", "label": "Claude Opus", "provider": "hi-code-claude", "model": "claude-opus-5-5",
         "reasoning_effort": "high", "description": "长文档与复杂推理"}
      ]
    }

- 没有这个文件：不启用目录，云侧 Agent 照旧可以自由填 provider / model（旧行为）。
- 有这个文件：云侧 Agent 只能从列表里选（`Agent.model_preset`），不选则用 `default`，再没有则用运行时默认。
  从列表里删掉一个模型，用它的 Agent 下一轮起改用默认模型，UI 标出“已下架”。
- 每次读取时比较文件的修改时间，变了就重新加载。文件写坏了保留上一份能用的；第一次就读不出来时按
  “启用、但列表为空”处理（只能用运行时默认），不因为写错文件而放开限制。
- 端侧 Agent 用的是用户自己 harness 里的模型，不受这个目录约束。
"""

from __future__ import annotations

import json
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRESET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class ModelPreset:
    id: str
    label: str
    provider: str
    model: str
    reasoning_effort: str | None = None
    description: str = ""

    def run_model(self) -> dict[str, str]:
        """交给运行时的形式（与 `Agent.model` 相同）。"""
        m = {"provider": self.provider, "model": self.model}
        if self.reasoning_effort:
            m["reasoning_effort"] = self.reasoning_effort
        return m

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "description": self.description, **self.run_model()}


def parse_catalog(data: Any) -> tuple[list[ModelPreset], str | None]:
    """校验目录内容；有任何一处不对就整份拒绝（抛 ValueError），避免悄悄丢掉某个模型。"""
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ValueError("顶层必须是 {\"models\": [...]}")
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
        presets.append(ModelPreset(id=pid, label=str(m.get("label") or model).strip(), provider=provider,
                                   model=model, reasoning_effort=str(m.get("reasoning_effort") or "").strip() or None,
                                   description=str(m.get("description") or "").strip()))
    default = data.get("default")
    if default is not None and not any(p.id == default for p in presets):
        raise ValueError(f"default 不在 models 里：{default!r}")
    return presets, default


class ModelCatalog:
    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._presets: list[ModelPreset] = []
        self._default: str | None = None
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
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            self._presets, self._default = parse_catalog(json.loads(self.path.read_text(encoding="utf-8")))
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

    def match(self, model: dict[str, str] | None) -> ModelPreset | None:
        """旧数据 / 旧客户端给的是 {provider, model, reasoning_effort?}：找到同样配置的预置模型。"""
        if not model:
            return None
        return next((p for p in self.presets() if p.run_model() == model), None)

    def public(self) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            return {"active": self._active, "default": self._default, "models": [p.public() for p in self._presets]}
