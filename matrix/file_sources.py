"""外部文件来源（docs/files-across-platforms.md）：Agent 用 fetch_file(source, ref) 把放在别处的文件拉进这一轮的暂存区。

各平台交文件的方式不一样：有的回答里给一个平台自己的文件 id，有的把文件放进第三方暂存、回答里给一条临时链接。
Matrix 不猜回答里哪些是文件，也不让模型给任意地址：部署方在来源目录里登记“从哪里、凭什么取”，
模型（或平台在流里报的 `file` 事件）只给来源 id 和 ref。

来源目录（默认 `<data>/file-sources.json`，`--file-sources` / `MATRIX_FILE_SOURCES`，改了就生效）::

    {"sources": [
      {"id": "corp-ai", "name": "公司 AI 平台的文件", "url": "https://ai.corp.example/api/files/{ref}/content",
       "ref": "file-[0-9a-f]{24}", "auth": "platform", "for": ["corp-ai"], "hint": "平台回答里给的文件 id（file-…）"},
      {"id": "oss", "name": "对象存储的临时链接", "url": "{ref}", "hosts": ["oss.corp.example"],
       "for": ["corp-ai", "biz-agent", "cloud"], "hint": "回答里 https://oss.corp.example/… 开头的下载链接"},
      {"id": "dms", "name": "文档中心", "url": "https://dms.corp.example/api/docs/{ref}", "ref": "DOC-\\\\d{6,}",
       "auth": "secret", "header": "X-Api-Key", "template": "{value}", "for": ["*"], "cache": false}
    ]}

- `url`：取文件的地址模板，`{ref}` 换成（URL 编码过的）ref。整个就是 `{ref}` 时 ref 是一条完整的链接。
- `hosts`：每一跳（含重定向）都必须落在这里（域名含子域名，也可以写 IP / 网段，可以是内网）。
  不写时取 `url` 模板里的主机；`url` 是 `{ref}` 时必须写。
- `ref`：ref 要整个匹配的正则。不写时：模板来源只收 `[A-Za-z0-9._~-]{1,200}`（一个 id），链接来源只收 http(s) 链接。
- `for`：谁能用。平台 id、`remote`（所有外部平台的 Agent）、`cloud`（云侧）、`device`（端侧）、`*`（所有）。
  不带凭证的来源默认 `*`；带凭证的必须写。旧写法 `"platform": "x"` 等于 `"for": ["x"]`。
- `auth`：取文件时以谁的身份——
  `none`（默认）：不带任何凭证；
  `platform`：带**发起这次取文件的那个 Agent 自己所在平台**的凭证（请求头按那个平台的 `auth` 写法），所以一个来源可以给几个平台用，
  各带各的，只给外部平台的 Agent；
  `secret`：带部署方给这个来源存的密钥（保管库 `file-source-<id>`，`matrix file-source-secret <id>` 从标准输入写入），
  请求头是 `header`（默认 Authorization）、写法是 `template`（默认 `Bearer {value}`）。
  凭证只发给第一跳的主机，重定向到别的主机时不带。
- `cache`：同一个对话里取过的同一个 ref 再取时直接用已有的那份（默认是）；ref 指向的内容会变时写 false。
- `scope`：这把凭证能取到谁的文件。`user`（默认）：平台按用户隔离（或不带凭证）；`app`：应用级凭证（如 Matrix 在平台上的一个
  应用 API Key），能取到平台上所有用户的文件、平台自己不隔离——这时由 Matrix 隔离：只取**属于这个对话**的文件 id
  （这一轮平台报出的、正文扫描认出的，或这个对话里以前报过、取过、传上去过的），别的一律拒绝；公共 Agent 的服务面也能用它。
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
WHO = re.compile(r"^(\*|[a-z0-9][a-z0-9_-]{0,39})$")
HEADER = re.compile(r"^[A-Za-z0-9-]{1,64}$")
AUTHS = ("none", "platform", "secret")
DEFAULT_ID_REF = r"[A-Za-z0-9._~-]{1,200}"
WHOLE = "{ref}"
LIMIT_MB = 20


class SourceError(ValueError):
    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        self.status = status   # 来源返回的 HTTP 状态码（有的话）


@dataclass(frozen=True)
class FileSource:
    id: str
    name: str
    url: str
    hosts: tuple[str, ...]
    ref: str
    auth: str = "none"
    for_: tuple[str, ...] = ("*",)
    max_mb: int = LIMIT_MB
    hint: str = ""
    header: str = "Authorization"
    template: str = "Bearer {value}"
    cache: bool = True
    explicit_ref: bool = False   # 部署方明确写了 ref 正则（从回复正文里认 id 时只用这样的 id 来源）
    scope: str = "user"          # user | app（应用级凭证：Matrix 按对话隔离，只取属于这个对话的文件 id）

    @property
    def whole(self) -> bool:
        return self.url == WHOLE

    def usable_by(self, platform: str | None, placement: str = "") -> bool:
        """这个来源能给谁用。platform：外部平台 Agent 所在的平台（其余为 None）；placement：cloud | device | remote。
        带平台凭证的只给外部平台的 Agent（云侧、端侧没有平台凭证）。"""
        placement = placement or ("remote" if platform else "cloud")
        if self.auth == "platform" and placement != "remote":
            return False
        return any(w == "*" or w == placement or (placement == "remote" and w == platform) for w in self.for_)

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
        if auth not in AUTHS:
            raise SourceError(f"{sid} 的 auth 只能是 {' / '.join(AUTHS)}")
        who = s.get("for", [s["platform"]] if s.get("platform") else None)
        if who is None:
            if auth != "none":
                raise SourceError(f"{sid} 带凭证（auth 是 {auth}）：要写 for（给哪些平台 / cloud / device 用）")
            who = ["*"]
        if isinstance(who, str):
            who = [who]
        if not isinstance(who, list) or not who or not all(isinstance(w, str) and WHO.match(w) for w in who):
            raise SourceError(f"{sid} 的 for 要是平台 id、remote、cloud、device 或 * 的列表")
        if auth == "platform" and {"cloud", "device"} & set(who):
            raise SourceError(f"{sid} 用平台凭证（auth 是 platform）：云侧、端侧的 Agent 没有平台凭证，for 里不能写 cloud / device")
        header = str(s.get("header") or "Authorization")
        if not HEADER.match(header):
            raise SourceError(f"{sid} 的 header 不合法：{header!r}")
        template = str(s.get("template") or "Bearer {value}")
        if "{value}" not in template:
            raise SourceError(f"{sid} 的 template 要含 {{value}}")
        if "cache" in s and not isinstance(s["cache"], bool):
            raise SourceError(f"{sid} 的 cache 要是 true 或 false")
        scope = str(s.get("scope") or "user")
        if scope not in ("user", "app"):
            raise SourceError(f"{sid} 的 scope 只能是 user 或 app")
        if scope == "app" and auth == "none":
            raise SourceError(f"{sid} 不带凭证（auth 是 none）：用不着 scope: app")
        try:
            max_mb = int(s.get("max_mb", LIMIT_MB))
        except (TypeError, ValueError) as e:
            raise SourceError(f"{sid} 的 max_mb 要是整数") from e
        out.append(FileSource(id=sid, name=str(s.get("name") or sid)[:80], url=url,
                              hosts=tuple(h.strip() for h in hosts), ref=ref, auth=auth, for_=tuple(who),
                              max_mb=max(1, min(LIMIT_MB, max_mb)), hint=str(s.get("hint") or "")[:200],
                              header=header, template=template, cache=s.get("cache", True),
                              explicit_ref=bool(s.get("ref")), scope=scope))
    return out


def route(sources: list[FileSource], ref: str) -> FileSource:
    """平台在流里报的文件该按哪个来源取（docs/files-across-platforms.md 6.1）：ref 恰好匹配一个来源的 ref 格式才取。
    一个都不匹配、或匹配上几个（部署方的正则没分开），都拒绝，不猜。"""
    ref = (ref or "").strip()
    hit = [s for s in sources if ref and re.fullmatch(s.ref, ref)]
    if len(hit) == 1:
        return hit[0]
    if not hit:
        raise SourceError("这个文件 id 不符合这个平台任何一个文件来源的格式（" + "、".join(s.id for s in sources) + "）")
    raise SourceError("这个文件 id 同时符合几个文件来源的格式（" + "、".join(s.id for s in hit) +
                      "）：请部署方把这几个来源的 ref 正则写得能区分开")


URL_IN_TEXT = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
EDGE_L, EDGE_R = r"(?<![A-Za-z0-9_~%/-])", r"(?![A-Za-z0-9_~%/?#=&-])"


def scannable(src: FileSource) -> bool:
    """从回复正文里认 id 时用得上的来源：链接来源（链接的主机还得在 hosts 里），或明确写了 ref 正则的 id 来源
    （默认的 id 格式几乎匹配任何一个词，不能拿来扫正文）。"""
    return src.whole or src.explicit_ref


def find_refs(sources: list[FileSource], text: str) -> list[str]:
    """从一段正文里认出可能是这些来源里的文件的 ref（按出现顺序、去重）。只是候选：取之前照旧要 route() 恰好匹配一个来源。
    链接只认主机在某个链接来源 hosts 里的（别的链接一律不碰、不解析），末尾的标点去掉。"""
    out: list[str] = []
    for src in sources:
        if not scannable(src):
            continue
        if src.whole:
            policy = src.policy()
            for m in URL_IN_TEXT.finditer(text or ""):
                url = m.group(0).rstrip(".,;:!?)]'")
                host = (urlsplit(url).hostname or "").rstrip(".").lower()
                literal = None
                try:
                    import ipaddress
                    literal = str(ipaddress.ip_address(host))
                except ValueError:
                    pass
                if host and (policy.named(host) or (literal is not None and policy.listed(literal))):
                    out.append(url)
            continue
        body = src.ref.removeprefix("^").removesuffix("$")
        try:
            pattern = re.compile(EDGE_L + "(?:" + body + ")" + EDGE_R)
        except re.error:
            continue
        out += [m.group(0) for m in pattern.finditer(text or "")]
    return list(dict.fromkeys(x for x in out if x))


def overlaps(sources: list[FileSource]) -> list[tuple[str, str]]:
    """ref 格式写得一模一样的来源（含都用默认格式的）：同一个平台的 files.sources 里不能有，否则分不出该按哪个取。
    两条不同的正则会不会重叠没法静态判断，交给 route() 在运行时拒绝。"""
    out = []
    for i, a in enumerate(sources):
        out += [(a.id, b.id) for b in sources[i + 1:] if a.ref == b.ref]
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

    def usable(self, platform: str | None, placement: str = "") -> list[FileSource]:
        return [s for s in self.items() if s.usable_by(platform, placement)]


def disposition_name(header: str) -> str:
    """Content-Disposition 里的文件名（filename* 优先）；没有就是空。"""
    if not header:
        return ""
    m = Message()
    m["content-disposition"] = header
    name = m.get_filename() or ""
    return name.strip()


def fetch(src: FileSource, ref: str, headers: dict[str, str] | None = None,
          resolver: Any = None, url: str | None = None) -> tuple[bytes, str, str]:
    """按来源取一个文件：返回（内容, 内容类型, 建议的文件名）。不放行、取不到时抛 SourceError。
    url：驱动的 file_auth 换出来的实际下载地址（预签名链接等）；ref 照样要合来源的格式，这个地址照样每一跳都要在 hosts 里。"""
    resolved = src.resolve(ref)
    if url:
        try:
            check_url(url)
        except FetchError as e:
            raise SourceError(f"驱动给的下载地址不对：{e}") from e
    try:
        kw = {"resolver": resolver} if resolver is not None else {}
        body, mime, final, disp = fetch_file(src.policy(), url or resolved, src.max_mb * 1_000_000, headers or {}, **kw)
    except FetchError as e:
        raise SourceError(str(e), status=e.status) from e
    name = disposition_name(disp) or unquote(Path(urlsplit(final).path).name) or "file"
    return body, mime, name
