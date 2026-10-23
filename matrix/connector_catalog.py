"""云端连接器目录：部署方维护的一份 JSON 文件，增减连接器不用重启（同 model_catalog 的做法）。

文件（默认 `<data>/connectors.json`，`--connectors` / `MATRIX_CONNECTORS` 可改）::

    {
      "connectors": [
        {"id": "github", "name": "GitHub", "description": "读写仓库、议题、PR",
         "url": "https://api.githubcopilot.com/mcp/",
         "auth": {"fields": [{"key": "token", "label": "个人访问令牌", "secret": true,
                              "header": "Authorization", "template": "Bearer {value}"}]},
         "skills": [{"name": "triage-issues", "description": "整理议题", "body": "……"}]}
      ]
    }

- 接入方式目前只有远程 MCP（Streamable HTTP）：由网络带上凭证代为调用，运行时拿不到凭证。
  命令行工具、本地 MCP 类的连接器要在运行时机器上跑、凭证在本地，云端不支持，只在用户自己电脑上用。
- `auth.fields`：授权时让人填的字段，按 `header` + `template` 拼成请求头（`{value}` 换成填的值）。
  `secret` 的字段只进加密保管，任何接口都不回显。没有字段的连接器不需要授权（公开服务）。
- `skills`：连接器自带的技能（docs/connectors-and-skills.md）。名字规则同技能。
- 没有这个文件：不提供云端连接器。文件写坏了保留上一份能用的。
- 这份文件里的地址由部署方负责；用户自定义的连接器另走 `mcp_client.public_only`，只连公网。
"""

from __future__ import annotations

import json
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .skills import SkillError, check_name

CONNECTOR_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
HEADER = re.compile(r"^[A-Za-z0-9-]{1,64}$")


@dataclass(frozen=True)
class AuthField:
    key: str
    label: str
    header: str
    template: str = "{value}"
    secret: bool = True

    def public(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "secret": self.secret}


@dataclass(frozen=True)
class CatalogSkill:
    name: str
    description: str
    body: str


@dataclass(frozen=True)
class ConnectorSpec:
    id: str
    name: str
    url: str
    description: str = ""
    fields: tuple[AuthField, ...] = ()
    skills: tuple[CatalogSkill, ...] = field(default_factory=tuple)

    def headers(self, values: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for f in self.fields:
            v = str(values.get(f.key, ""))
            if v:
                out[f.header] = f.template.replace("{value}", v)
        return out

    def public(self) -> dict[str, Any]:
        """给人看的：不含地址（实现细节），只有名字、描述、要填什么、自带哪些技能。"""
        return {"id": self.id, "name": self.name, "description": self.description,
                "fields": [f.public() for f in self.fields],
                "skills": [{"name": s.name, "description": s.description} for s in self.skills]}


def parse_catalog(data: Any) -> list[ConnectorSpec]:
    if not isinstance(data, dict) or not isinstance(data.get("connectors"), list):
        raise ValueError("顶层必须是 {\"connectors\": [...]}")
    out: list[ConnectorSpec] = []
    for i, c in enumerate(data["connectors"]):
        if not isinstance(c, dict):
            raise ValueError(f"connectors[{i}] 必须是对象")
        cid = str(c.get("id") or "").strip()
        if not CONNECTOR_ID.match(cid):
            raise ValueError(f"connectors[{i}].id 无效：{cid!r}（小写字母、数字、_ -，最长 40）")
        if any(x.id == cid for x in out):
            raise ValueError(f"connectors[{i}].id 重复：{cid}")
        url = str(c.get("url") or "").strip()
        if not url.startswith(("https://", "http://")):
            raise ValueError(f"connectors[{i}]（{cid}）缺少 http(s) 地址")
        fields_: list[AuthField] = []
        for j, f in enumerate((c.get("auth") or {}).get("fields") or []):
            key, header = str(f.get("key") or "").strip(), str(f.get("header") or "").strip()
            if not key or not HEADER.match(header):
                raise ValueError(f"connectors[{i}].auth.fields[{j}] 要有 key 与合法的 header")
            fields_.append(AuthField(key=key, label=str(f.get("label") or key), header=header,
                                     template=str(f.get("template") or "{value}"), secret=bool(f.get("secret", True))))
        skills_: list[CatalogSkill] = []
        for j, s in enumerate(c.get("skills") or []):
            try:
                name = check_name(str(s.get("name") or ""))
            except SkillError as e:
                raise ValueError(f"connectors[{i}].skills[{j}]：{e}") from e
            if not str(s.get("description") or "").strip():
                raise ValueError(f"connectors[{i}].skills[{j}]（{name}）缺少 description")
            skills_.append(CatalogSkill(name, str(s["description"]).strip(), str(s.get("body") or "")))
        out.append(ConnectorSpec(id=cid, name=str(c.get("name") or cid).strip(), url=url,
                                 description=str(c.get("description") or "").strip(),
                                 fields=tuple(fields_), skills=tuple(skills_)))
    return out


class ConnectorCatalog:
    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._specs: list[ConnectorSpec] = []
        self.error: str | None = None

    def _refresh(self) -> None:
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime, self._specs, self.error = None, [], None
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            self._specs = parse_catalog(json.loads(self.path.read_text(encoding="utf-8")))
            self.error = None
        except (OSError, ValueError) as e:
            self.error = str(e)
            print(f"[matrix] 连接器目录 {self.path} 无效，沿用上一份：{e}", file=sys.stderr)

    def specs(self) -> list[ConnectorSpec]:
        with self._lock:
            self._refresh()
            return list(self._specs)

    def get(self, connector_id: str | None) -> ConnectorSpec | None:
        return next((s for s in self.specs() if s.id == connector_id), None) if connector_id else None

    @property
    def version(self) -> float | None:
        with self._lock:
            self._refresh()
            return self._mtime
