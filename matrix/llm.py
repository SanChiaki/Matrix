"""网络自己调一次模型：给插件服务的 MCP sampling 用（docs/plugin-hooks.md 第 14 节）。

Agent 的运行由运行时（dsh）调模型；这里只服务一种场合：插件服务请网络替它生成一段文本。用的是部署方模型目录里
平台配的供应商（`model_catalog.Provider`，协议 + 地址；密钥在保管库），dsh 里自己配置的供应商这里用不了。

只做文本、非流式、一次请求一次回复：
- `openai-completions`：`POST <base_url>/chat/completions`，`Authorization: Bearer`。
- `anthropic-messages`：`POST <base_url>/v1/messages`（base_url 已经以 `/v1` 结尾时不再加），`x-api-key`。
- `openai-responses`：不支持（插件用模型时报清楚原因）。

只用标准库；按环境变量里的代理走（和 dsh 一样）。错误信息不带密钥、只带状态码和回应开头一段。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .model_catalog import ModelPreset, Provider

TIMEOUT = 120.0
ERROR_BODY = 300  # 供应商报错时，回应正文最多带多少字符


class LlmError(RuntimeError):
    pass


@dataclass(frozen=True)
class Completion:
    text: str
    stop_reason: str          # MCP 的 stopReason：endTurn | maxTokens | stopSequence | 供应商原样的其他值
    usage: dict[str, int]     # 供应商报告的原始 token 字段（只留整数），用量台账按 usage.tokens 归一


def _post(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 —— 地址来自部署方的模型目录
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")[:ERROR_BODY] if e.fp else ""
        raise LlmError(f"模型服务回了 {e.code}：{text}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LlmError(f"连不上模型服务：{getattr(e, 'reason', e)}") from e
    except ValueError as e:
        raise LlmError("模型服务回的不是 JSON") from e
    if not isinstance(data, dict):
        raise LlmError("模型服务回的不是 JSON 对象")
    return data


def _ints(d: Any) -> dict[str, int]:
    return {k: v for k, v in (d or {}).items() if isinstance(v, int) and not isinstance(v, bool)} if isinstance(d, dict) else {}


def complete(prov: Provider, preset: ModelPreset, key: str | None, *, system: str, messages: list[dict[str, str]],
             max_tokens: int, temperature: float | None = None, stop: list[str] | None = None,
             timeout: float = TIMEOUT) -> Completion:
    """messages：[{role: user | assistant, text}]。"""
    base = prov.base_url.rstrip("/")
    if prov.api == "openai-completions":
        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {"role": m["role"], "content": m["text"]} for m in messages]
        field = str((prov.compat or {}).get("maxTokensField") or "max_tokens")
        body: dict[str, Any] = {"model": preset.model, "messages": msgs, field: max_tokens, "stream": False}
        if temperature is not None:
            body["temperature"] = temperature
        if stop:
            body["stop"] = stop
        data = _post(base + "/chat/completions", {"Authorization": f"Bearer {key}"} if key else {}, body, timeout)
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise LlmError("模型服务的回应里没有 choices[0].message") from e
        reason = {"stop": "endTurn", "length": "maxTokens"}.get(choice.get("finish_reason"), choice.get("finish_reason") or "endTurn")
        return Completion(str(text), str(reason), _ints(data.get("usage")))
    if prov.api == "anthropic-messages":
        body = {"model": preset.model, "max_tokens": max_tokens,
                "messages": [{"role": m["role"], "content": m["text"]} for m in messages]}
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        if stop:
            body["stop_sequences"] = stop
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        headers = {"anthropic-version": "2023-06-01", **({"x-api-key": key} if key else {})}
        data = _post(url, headers, body, timeout)
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise LlmError("模型服务的回应里没有 content")
        text = "".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        reason = {"end_turn": "endTurn", "max_tokens": "maxTokens", "stop_sequence": "stopSequence"}.get(
            data.get("stop_reason"), data.get("stop_reason") or "endTurn")
        return Completion(text, str(reason), _ints(data.get("usage")))
    raise LlmError(f"供应商「{prov.name}」的协议是 {prov.api}，插件用模型暂时只支持 openai-completions 与 anthropic-messages")
