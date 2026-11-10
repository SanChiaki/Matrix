"""云端连接器目录：部署方维护的一份 JSON 文件，增减连接器不用重启（同 model_catalog 的做法）。

文件（默认 `<data>/connectors.json`，`--connectors` / `MATRIX_CONNECTORS` 可改）::

    {
      "connectors": [
        {"id": "github", "name": "GitHub", "description": "读写仓库、议题、PR",
         "url": "https://api.githubcopilot.com/mcp/", "domains": ["github.com"],
         "auth": {"fields": [{"key": "token", "label": "个人访问令牌", "secret": true,
                              "header": "Authorization", "template": "Bearer {value}"}]},
         "skills": [{"name": "triage-issues", "description": "整理议题", "body": "……"}]}
      ],
      "allow": ["mcp.corp.example", "10.20.0.0/16"],
      "proxy": "auto"
    }

- 接入方式目前只有远程 MCP（Streamable HTTP）：由网络带上凭证代为调用，运行时拿不到凭证。
  命令行工具、本地 MCP 类的连接器要在运行时机器上跑、凭证在本地，云端不支持，只在用户自己电脑上用。
- `auth.fields`：授权时让人填的字段，按 `header` + `template` 拼成请求头（`{value}` 换成填的值）。
  `secret` 的字段只进加密保管，任何接口都不回显。没有字段的连接器不需要授权（公开服务）。
- `domains`：这个服务的链接所在的域名（含子域名）。项目资料里的链接据此认出要用哪个连接器，
  出现在项目的连接器面板里（docs/reference/connectors-skills.md）。可省略。
- `embed`：部署方允许把这些域名下的页面嵌进项目页（外部页面标签）。默认不允许；要允许就得写 `domains`。
  嵌进来的页面用看的人自己在那个系统的登录，Matrix 不给它任何东西（docs/reference/connectors-skills.md）。
- `skills`：连接器自带的技能（docs/connectors-and-skills.md）。名字规则同技能。
- 没有这个文件：不提供云端连接器。文件写坏了保留上一份能用的。
- 这份文件里的地址由部署方负责（可以是内网、可以用 http）；用户自定义的连接器另走 `mcp_client.public_only`，只连 https 公网。
- `allow`：用户自定义的连接器（我的连接器、项目里写的）也可以连的内网目标，写法同 fetch.json 的 `allow`：
  域名含子域名（`*.x` 与 `x` 一样），也可以写 IP 或网段（CIDR）。写进来的主机可以是内网地址、可以用 http；
  白名单里的域名解析到哪里都放行（内网 DNS 由部署方负责），其余域名解析出的每个地址都得是公网或落在白名单的网段里。
  可省略（默认空：自定义连接器只连 https 公网）。只写这一项时 `connectors` 写空列表。
- `proxy`：目录里的连接器连接时怎么用环境变量里的代理（`HTTPS_PROXY` 等）。`auto`（默认）内网地址直连、其余照环境变量
  （内网指：`allow` 里的主机、写成非公网 IP 的、本机解析出非公网地址的）；`env` 全照环境变量（`NO_PROXY` 自己配）；
  `direct` 一律直连。每个连接器也可以单写 `proxy` 盖过顶层的。自定义连接器从不走代理，不受它影响。
"""

from __future__ import annotations

import json
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import web_fetch
from .mcp_client import PROXY_MODES
from .skills import SkillError, check_name

CONNECTOR_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
HEADER = re.compile(r"^[A-Za-z0-9-]{1,64}$")
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


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
    domains: tuple[str, ...] = ()            # 这个服务的链接所在的域名（含子域名）：项目资料里的链接据此认出要用它
    embed: bool = False                      # 部署方允许把这些域名下的页面嵌进项目页（外部页面标签）
    proxy: str = ""                          # 代理模式（mcp_client.PROXY_MODES）；空 = 用目录顶层的

    def matches(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower() if url.lower().startswith(("https://", "http://")) else ""
        return bool(host) and any(host == d or host.endswith("." + d) for d in self.domains)

    def headers(self, values: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for f in self.fields:
            v = str(values.get(f.key, ""))
            if v:
                out[f.header] = f.template.replace("{value}", v)
        return out

    def public(self) -> dict[str, Any]:
        """给人看的：不含地址（实现细节），只有名字、描述、要填什么、自带哪些技能。"""
        return {"id": self.id, "name": self.name, "description": self.description, "domains": list(self.domains),
                "embed": self.embed,
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
        domains = c.get("domains") or []
        if not isinstance(domains, list) or not all(isinstance(d, str) and DOMAIN.match(d.strip().lower()) for d in domains):
            raise ValueError(f"connectors[{i}]（{cid}）.domains 要是域名列表，如 [\"feishu.cn\"]")
        proxy = _proxy_mode(c.get("proxy", ""), f"connectors[{i}]（{cid}）.proxy")
        if c.get("embed") and not domains:
            raise ValueError(f"connectors[{i}]（{cid}）允许嵌入（embed）就要写 domains：只嵌这些域名下的页面")
        out.append(ConnectorSpec(id=cid, name=str(c.get("name") or cid).strip(), url=url,
                                 description=str(c.get("description") or "").strip(),
                                 fields=tuple(fields_), skills=tuple(skills_),
                                 domains=tuple(dict.fromkeys(d.strip().lower() for d in domains)),
                                 embed=bool(c.get("embed")), proxy=proxy))
    return out


def _proxy_mode(v: Any, where: str) -> str:
    if v in ("", None):
        return ""
    if v not in PROXY_MODES:
        raise ValueError(f"{where} 要是 {' / '.join(PROXY_MODES)} 之一：{v!r}")
    return str(v)


def parse_proxy(data: Any) -> str:
    """顶层的 `proxy`：目录里的连接器怎么走代理，默认 auto（内网地址直连）。"""
    return _proxy_mode(data.get("proxy", "") if isinstance(data, dict) else "", "proxy") or "auto"


def parse_allow(data: Any) -> web_fetch.FetchPolicy:
    """顶层的 `allow`：自定义连接器可以连的内网目标（同 fetch.json 的写法）。只用来放行内网，公网照常可连。"""
    allow = data.get("allow", []) if isinstance(data, dict) else []
    try:
        p = web_fetch.parse_policy({"allow": allow})
    except ValueError as e:
        raise ValueError(f"allow：{e}") from e
    return web_fetch.FetchPolicy(domains=p.domains, networks=p.networks, public=True)


class ConnectorCatalog:
    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._specs: list[ConnectorSpec] = []
        self._allow = web_fetch.FetchPolicy()
        self._proxy = "auto"
        self.error: str | None = None

    def _refresh(self) -> None:
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime, self._specs, self._allow, self.error = None, [], web_fetch.FetchPolicy(), None
            self._proxy = "auto"
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._specs, self._allow, self._proxy = parse_catalog(data), parse_allow(data), parse_proxy(data)
            self.error = None
        except (OSError, ValueError) as e:
            self.error = str(e)
            print(f"[matrix] 连接器目录 {self.path} 无效，沿用上一份：{e}", file=sys.stderr)

    def specs(self) -> list[ConnectorSpec]:
        with self._lock:
            self._refresh()
            return list(self._specs)

    def allow(self) -> web_fetch.FetchPolicy | None:
        """部署方给自定义连接器的内网白名单；没写就是 None（只连 https 公网）。"""
        with self._lock:
            self._refresh()
            return self._allow if self._allow.allow_entries() else None

    def proxy_mode(self, connector_id: str | None) -> str:
        """这个目录连接器现在的代理模式：连接器单写的优先，否则顶层的（默认 auto）。"""
        spec = self.get(connector_id)
        with self._lock:
            return (spec.proxy if spec is not None and spec.proxy else "") or self._proxy

    def get(self, connector_id: str | None) -> ConnectorSpec | None:
        return next((s for s in self.specs() if s.id == connector_id), None) if connector_id else None

    @property
    def version(self) -> float | None:
        with self._lock:
            self._refresh()
            return self._mtime
