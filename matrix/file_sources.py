"""外部文件来源（docs/files-across-platforms.md）：Agent 用 fetch_file(source, ref) 把放在别处的文件拉进这一轮的暂存区。

各平台交文件的方式不一样：有的回答里给一个平台自己的文件 id，有的把文件放进第三方暂存、回答里给一条临时链接。
Matrix 不猜回答里哪些是文件，也不让模型给任意地址：部署方在来源目录里登记“从哪里、凭什么取”，
模型（或平台在流里报的 `file` 事件）只给来源 id 和 ref。

来源目录（默认 `<data>/file-sources.json`，`--file-sources` / `MATRIX_FILE_SOURCES`，改了就生效）::

    {"sources": [
      {"id": "corp-ai", "name": "公司 AI 平台的文件", "url": "https://ai.corp.example/api/files/{ref}/content",
       "auth": "platform", "platform": "corp-ai", "hint": "平台回答里给的文件 id（file-…）"},
      {"id": "oss", "name": "对象存储的临时链接", "url": "{ref}", "hosts": ["oss.corp.example"],
       "hint": "回答里 https://oss.corp.example/… 开头的下载链接"}
    ]}

- `url`：取文件的地址模板，`{ref}` 换成（URL 编码过的）ref。整个就是 `{ref}` 时 ref 是一条完整的链接。
- `hosts`：每一跳（含重定向）都必须落在这里（域名含子域名，也可以写 IP / 网段，可以是内网）。
  不写时取 `url` 模板里的主机；`url` 是 `{ref}` 时必须写。
- `ref`：ref 要整个匹配的正则。不写时：模板来源只收 `[A-Za-z0-9._~-]{1,200}`（一个 id），链接来源只收 http(s) 链接。
- `auth`：`none`（默认，不带任何凭证）或 `platform`：带上外部平台 Agent 发起会话用的平台凭证（同 agent-platforms.json 的 `auth`），
  只给那个平台的 Agent 用，`platform` 写平台 id。凭证只发给第一跳的主机，重定向到别的主机时不带。
- `max_mb`：单个文件上限，不超过附件上限（20 MB）。
- `hint`：写进提示词，告诉模型这个来源的 ref 长什么样。
"""

from __future__ import annotations

import json
import re
import sys
import threading
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .web_fetch import FetchError, FetchPolicy, check_url, fetch_file, parse_policy

SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
DEFAULT_ID_REF = r"[A-Za-z0-9._~-]{1,200}"
WHOLE = "{ref}"
LIMIT_MB = 20


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class FileSource:
    id: str
    name: str
    url: str
    hosts: tuple[str, ...]
    ref: str
    auth: str = "none"
    platform: str | None = None
    max_mb: int = LIMIT_MB
    hint: str = ""

    @property
    def whole(self) -> bool:
        return self.url == WHOLE

    def usable_by(self, platform: str | None) -> bool:
        """这个来源能给谁用：不带凭证的给所有 Agent；带平台凭证的只给那个平台的 Agent。"""
        return self.auth == "none" or (self.auth == "platform" and platform is not None and platform == self.platform)

    def policy(self) -> FetchPolicy:
        return parse_policy({"allow": list(self.hosts), "public": False})

    def resolve(self, ref: str) -> str:
        """ref → 实际地址。ref 不合规定就抛 SourceError（不发请求）。"""
        ref = (ref or "").strip()
        if not ref or len(ref) > 2000 or not re.fullmatch(self.ref, ref):
            raise SourceError(f"ref 不符合来源「{self.name}」的格式" + (f"（{self.hint}）" if self.hint else ""))
        url = ref if self.whole else self.url.replace(WHOLE, quote(ref, safe=""))
        try:
            check_url(url)
        except FetchError as e:
            raise SourceError(str(e)) from e
        return url

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "hint": self.hint}


def parse_sources(raw: Any) -> list[FileSource]:
    if not isinstance(raw, dict) or not isinstance(raw.get("sources", []), list):
        raise SourceError("来源目录要是 {\"sources\": [...]}")
    out: list[FileSource] = []
    for s in raw.get("sources", []):
        if not isinstance(s, dict):
            raise SourceError("每个来源要是一个对象")
        sid, url = str(s.get("id", "")), str(s.get("url", "")).strip()
        if not SOURCE_ID.match(sid):
            raise SourceError(f"来源 id 不合法：{sid!r}")
        if any(x.id == sid for x in out):
            raise SourceError(f"来源 id 重复：{sid}")
        if url != WHOLE:
            if url.count(WHOLE) != 1:
                raise SourceError(f"{sid} 的 url 要含一个 {{ref}}，或者整个就是 {{ref}}")
            try:
                check_url(url.replace(WHOLE, "x"))
            except FetchError as e:
                raise SourceError(f"{sid} 的 url：{e}") from e
        hosts = s.get("hosts")
        if hosts is None:
            if url == WHOLE:
                raise SourceError(f"{sid} 的 url 是 {{ref}}（ref 是整条链接）：要写 hosts，只取这些主机上的文件")
            hosts = [urlsplit(url.replace(WHOLE, "x")).hostname or ""]
        if not isinstance(hosts, list) or not hosts or not all(isinstance(h, str) and h.strip() for h in hosts):
            raise SourceError(f"{sid} 的 hosts 要是非空的字符串列表")
        try:
            parse_policy({"allow": hosts, "public": False})
        except ValueError as e:
            raise SourceError(f"{sid} 的 hosts：{e}") from e
        ref = str(s.get("ref") or (r"https?://\S+" if url == WHOLE else DEFAULT_ID_REF))
        try:
            re.compile(ref)
        except re.error as e:
            raise SourceError(f"{sid} 的 ref 不是合法的正则：{e}") from e
        auth = str(s.get("auth") or "none")
        if auth not in ("none", "platform"):
            raise SourceError(f"{sid} 的 auth 只能是 none 或 platform")
        platform = str(s["platform"]) if s.get("platform") else None
        if auth == "platform" and not platform:
            raise SourceError(f"{sid} 的 auth 是 platform：要写 platform（哪个外部平台的凭证）")
        try:
            max_mb = int(s.get("max_mb", LIMIT_MB))
        except (TypeError, ValueError) as e:
            raise SourceError(f"{sid} 的 max_mb 要是整数") from e
        out.append(FileSource(id=sid, name=str(s.get("name") or sid)[:80], url=url,
                              hosts=tuple(h.strip() for h in hosts), ref=ref, auth=auth, platform=platform,
                              max_mb=max(1, min(LIMIT_MB, max_mb)), hint=str(s.get("hint") or "")[:200]))
    return out


class SourceCatalog:
    """部署方的来源目录：按修改时间热加载；写坏了保留上一份能用的。"""

    def __init__(self, path: Path | None):
        self.path = path
        self._mtime: float | None = None
        self._items: list[FileSource] = []
        self._lock = threading.Lock()

    def items(self) -> list[FileSource]:
        with self._lock:
            if self.path is None or not self.path.is_file():
                self._items, self._mtime = [], None
                return []
            mtime = self.path.stat().st_mtime
            if mtime != self._mtime:
                self._mtime = mtime
                try:
                    self._items = parse_sources(json.loads(self.path.read_text(encoding="utf-8")))
                except (OSError, ValueError) as e:
                    print(f"[matrix] 文件来源目录 {self.path} 读不了，沿用上一份：{e}", file=sys.stderr)
            return list(self._items)

    def get(self, sid: str | None) -> FileSource | None:
        return next((s for s in self.items() if s.id == sid), None)

    def usable(self, platform: str | None) -> list[FileSource]:
        return [s for s in self.items() if s.usable_by(platform)]


def disposition_name(header: str) -> str:
    """Content-Disposition 里的文件名（filename* 优先）；没有就是空。"""
    if not header:
        return ""
    m = Message()
    m["content-disposition"] = header
    name = m.get_filename() or ""
    return name.strip()


def fetch(src: FileSource, ref: str, headers: dict[str, str] | None = None,
          resolver: Any = None) -> tuple[bytes, str, str]:
    """按来源取一个文件：返回（内容, 内容类型, 建议的文件名）。不放行、取不到时抛 SourceError。"""
    url = src.resolve(ref)
    try:
        kw = {"resolver": resolver} if resolver is not None else {}
        body, mime, final, disp = fetch_file(src.policy(), url, src.max_mb * 1_000_000, headers or {}, **kw)
    except FetchError as e:
        raise SourceError(str(e)) from e
    name = disposition_name(disp) or unquote(Path(urlsplit(final).path).name) or "file"
    return body, mime, name
