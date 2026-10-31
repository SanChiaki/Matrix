"""插件从哪里来（docs/plugin-install.md）：部署方的安装策略、按地址取包、后台管理的目录、主持人的上架申请。

三条来源与它们的信任：
- 部署方目录：启动参数给的 `plugins.json`（文件，部署方维护）+ 管理后台加进来的（`<data>/plugin-catalog/`，从 git 拉、上传、
  或批准的上架申请）。部署方审核过，带服务的插件可以由网络托管、按清单联网。
- 主持人自己装：上传插件包，或填一个地址（`.zip` 插件包或 `plugin.json`）。带服务的只有两种能装：
  托管代码进沙箱（不能联网），或者服务就在那个地址的主机上（远程服务）。是否放行由这里的策略决定。
- 上架申请：主持人把包交给管理员审核，批准后进目录。

本模块只做文件、网络取包与策略判断，不碰项目与权限；那些在 engine。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import plugins as pluginfs
from . import web_fetch
from .models import PLUGIN_LIMIT, new_id

POLICY_FILE = "plugin-policy.json"
MANAGED_DIR = "plugin-catalog"
SUBMISSIONS_DIR = "plugin-submissions"
REMOTE_MODES = ("off", "public", "listed")
GIT_TIMEOUT = 120
PENDING_PER_USER = 5
SUBMISSIONS_PER_USER = 50


class SourceError(ValueError):
    pass


def package_hash(files: dict[Path, bytes]) -> str:
    """插件包的指纹：按路径排序后逐个文件哈希。同一份内容在哪里算都一样。"""
    h = hashlib.sha256()
    for rel in sorted(files, key=lambda p: p.as_posix()):
        h.update(rel.as_posix().encode() + b"\0" + hashlib.sha256(files[rel]).digest())
    return h.hexdigest()


# ================= 策略 =================
@dataclass(frozen=True)
class InstallPolicy:
    """部署方对“主持人自己装插件”的策略（管理后台可改）。
    remote：off 不许装远程服务插件；public 公网主机都行（加上 allow 里的内网目标）；listed 只许 allow 里的。
    allow：域名（含子域名）、IP 或网段，写进来的也可以是内网、可以用 http。deny：域名（含子域名），优先于 allow。
    hosted_upload：主持人上传的托管插件（进沙箱）。sandbox_catalog：目录里的托管插件也进沙箱（有沙箱时）。"""
    remote: str = "public"
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    hosted_upload: bool = True
    sandbox_catalog: bool = True

    def fetch_policy(self) -> web_fetch.FetchPolicy:
        p = web_fetch.parse_policy({"allow": list(self.allow)})
        return web_fetch.FetchPolicy(domains=p.domains, networks=p.networks, public=self.remote == "public")

    def to_dict(self) -> dict[str, Any]:
        return {"remote": self.remote, "allow": list(self.allow), "deny": list(self.deny),
                "hosted_upload": self.hosted_upload, "sandbox_catalog": self.sandbox_catalog}


def parse_policy(data: Any) -> InstallPolicy:
    if not isinstance(data, dict):
        raise SourceError("策略要是对象")
    remote = str(data.get("remote", "public"))
    if remote not in REMOTE_MODES:
        raise SourceError(f"remote 只能是 {'/'.join(REMOTE_MODES)}")

    def hosts(key: str) -> tuple[str, ...]:
        v = data.get(key) or []
        if not isinstance(v, list) or len(v) > 200 or not all(isinstance(x, str) for x in v):
            raise SourceError(f"{key} 要是字符串列表（最多 200 项）")
        return tuple(dict.fromkeys(x.strip().lower() for x in v if x.strip()))
    allow, deny = hosts("allow"), hosts("deny")
    try:
        web_fetch.parse_policy({"allow": list(allow)})
    except ValueError as e:
        raise SourceError(str(e)) from e
    for d in deny:
        if not web_fetch.DOMAIN.match(d.removeprefix("*.").removeprefix(".")):
            raise SourceError(f"deny 里的 {d!r} 不是域名")
    for key in ("hosted_upload", "sandbox_catalog"):
        if key in data and not isinstance(data[key], bool):
            raise SourceError(f"{key} 要是 true 或 false")
    return InstallPolicy(remote, allow, deny, data.get("hosted_upload", True), data.get("sandbox_catalog", True))


class PolicyStore:
    """`<data>/plugin-policy.json`：没有这个文件就用默认策略。"""

    def __init__(self, root: Path):
        self.path = root / POLICY_FILE
        self._lock = threading.Lock()

    def get(self) -> InstallPolicy:
        with self._lock:
            try:
                return parse_policy(json.loads(self.path.read_text(encoding="utf-8")))
            except FileNotFoundError:
                return InstallPolicy()
            except (OSError, ValueError) as e:
                print(f"[matrix] 插件安装策略 {self.path} 无效，按最严处理：{e}")
                return InstallPolicy(remote="off", hosted_upload=False)

    def save(self, data: Any) -> InstallPolicy:
        p = parse_policy(data)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(p.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        return p


def _host_of(url: str) -> tuple[str, str]:
    try:
        scheme, host, _ = web_fetch.check_url(url)
    except web_fetch.FetchError as e:
        raise SourceError(str(e)) from e
    return scheme, host


def check_remote(policy: InstallPolicy, url: str, resolve: bool = True) -> str:
    """主持人装的远程服务、或填的插件地址，部署方放不放行。返回主机名；不放行抛 SourceError。
    resolve：再按解析出的地址查一遍（内网地址只有 allow 里写了才放行）。"""
    if policy.remote == "off":
        raise SourceError("部署方关掉了远程插件：只能装部署方目录里的、或上传的插件")
    scheme, host = _host_of(url)
    if any(host == d.removeprefix("*.").removeprefix(".") or host.endswith("." + d.removeprefix("*.").removeprefix("."))
           for d in policy.deny):
        raise SourceError(f"部署方禁止了 {host}")
    fp = policy.fetch_policy()
    literal = web_fetch._literal(host)
    listed = fp.named(host) or (literal is not None and fp.listed(literal))
    if scheme != "https" and not listed:
        raise SourceError(f"{host} 要用 https（http 只给部署方白名单里的主机）")
    if policy.remote == "listed" and not listed:
        raise SourceError(f"{host} 不在部署方允许的插件主机里")
    if resolve:
        try:
            web_fetch.plan(fp, url)
        except web_fetch.FetchError as e:
            raise SourceError(str(e)) from e
    return host


def fetch_package(policy: InstallPolicy, url: str) -> tuple[dict[Path, bytes], str]:
    """按地址取插件：`.zip` 是整个插件包，否则当作 plugin.json（只有清单，没有页面与技能）。返回（文件, 主机名）。
    清单里的 service.url 必须在同一个主机上：主持人在确认卡上看到的就是数据会去的地方。"""
    host = check_remote(policy, url)
    try:
        body, mime, final = web_fetch.fetch_bytes(policy.fetch_policy(), url, PLUGIN_LIMIT,
                                                  "application/zip, application/json")
    except web_fetch.FetchError as e:
        raise SourceError(str(e)) from e
    if _host_of(final)[1] != host:
        raise SourceError(f"{url} 重定向到了别的主机：{final}")
    try:
        if body[:2] == b"PK" or mime == "application/zip":
            files = pluginfs.package_files(pluginfs.unzip(body))
        else:
            json.loads(body.decode("utf-8"))
            files = {Path(pluginfs.MANIFEST): body}
        m = pluginfs.check_package(files)
    except (UnicodeDecodeError, ValueError) as e:
        raise SourceError(f"{url} 不是插件包或插件清单：{e}") from e
    if m.service is not None and m.service.url and _host_of(m.service.url)[1] != host:
        raise SourceError(f"插件服务的地址（{_host_of(m.service.url)[1]}）要和插件地址在同一个主机（{host}）上")
    return files, host


def service_host(m: pluginfs.Manifest) -> str | None:
    return urlsplit(m.service.url).hostname if m.service is not None and m.service.url else None


def disclosure(m: pluginfs.Manifest, runs: str | None) -> list[str]:
    """主持人自己装带服务的插件时，确认卡上一条条写明：插件代码在哪里跑、项目里的什么内容会交给它。
    部署方目录里的插件不出这张卡（部署方审核过）。"""
    if m.service is None:
        return []
    if runs == "remote":
        where = service_host(m)
        out = [f"插件服务在 {where}，不由本部署运行；它那边存的数据由插件作者保管，卸载插件不会删掉"]
        to = f"发到 {where}"
    else:
        out = ["插件代码在本部署的沙箱里运行：不能联网，只能读写这个安装自己的数据目录，内存、文件与 CPU 有上限"]
        if m.service.requirements:
            out.append(f"安装时按锁文件 {m.service.requirements} 下载依赖（只装预编译包，逐个校验哈希）")
        to = "交给插件代码"
    tools = [n for n, c in m.service.tools if c in ("read", "write")]
    if tools:
        out.append(f"Agent 调插件工具（{'、'.join(tools[:6])}{'…' if len(tools) > 6 else ''}）时，参数会{to}")
    for r in m.routes:
        out.append(f"项目群聊里的每条新消息（正文、作者、@ 了谁）和成员名单会{to}，由它建议"
                   + "、".join({"wake": "叫谁来看", "claim": "谁认领"}[k] for k in r.may))
    if any(a.tool for a in m.afters):
        out.append(f"用户任务验收后，任务标题与交付摘要会{to}")
    if m.contexts or m.mounts:
        out.append("每轮运行前从插件服务读内容，放进 Agent 的提示词或工作目录")
    if runs == "remote":
        out.append("每次调用都带项目 id 和调用者（Agent 名称或成员 id）；安装后生成一把签名密钥，只显示一次，配到插件服务上")
    return out


# ================= 后台管理的目录 =================
@dataclass
class ManagedCatalog:
    """管理后台加进目录的插件：`<data>/plugin-catalog/<名字>/` 是包，`index.json` 记来源、版本与指纹。"""

    root: Path
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def dir(self) -> Path:
        return self.root / MANAGED_DIR

    def _index(self) -> dict[str, Any]:
        try:
            data = json.loads((self.dir / "index.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, idx: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "index.tmp"
        tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.dir / "index.json")

    def entries(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: v for k, v in self._index().items() if (self.dir / k / pluginfs.MANIFEST).is_file()}

    def add(self, files: dict[Path, bytes], source: dict[str, Any], by: str | None,
            reserved: set[str] = frozenset()) -> tuple[pluginfs.Manifest, dict[str, Any]]:
        m = pluginfs.check_package(files)
        if m.name in reserved:
            raise SourceError(f"部署方的 plugins.json 里已经有 {m.name}：同名的只能在那份文件里改")
        with self._lock:
            idx = self._index()
            pluginfs.install(self.dir, files)
            entry = {"version": m.version, "title": m.title, "sha256": package_hash(files), "source": source,
                     "added_by": by, "added_at": time.time()}
            idx[m.name] = entry
            self._save(idx)
        return m, entry

    def remove(self, name: str) -> None:
        with self._lock:
            idx = self._index()
            if name not in idx:
                raise SourceError(f"目录里没有后台加的 {name}")
            del idx[name]
            shutil.rmtree(self.dir / name, ignore_errors=True)
            self._save(idx)

    def items(self) -> list[pluginfs.CatalogPlugin]:
        out = []
        for name, info in self.entries().items():
            try:
                out.append(pluginfs.CatalogPlugin(self.dir / name, pluginfs.read_manifest(self.dir / name),
                                                  origin="managed", info=info))
            except pluginfs.PluginError as e:
                print(f"[matrix] 后台目录里的插件 {name} 无效：{e}")
        return out


def git_package(url: str, ref: str = "", path: str = "") -> tuple[dict[Path, bytes], str]:
    """从 git 仓库取一个插件包：浅克隆（只许 https，本机路径给测试与内网镜像用），返回（文件, 提交号）。
    不拉子模块，不跑仓库里的任何东西。"""
    url, ref, path = url.strip(), ref.strip(), path.strip().strip("/")
    if not url or url.startswith("-") or ref.startswith("-"):
        raise SourceError("仓库地址无效")
    sub = pluginfs.safe_rel(path) if path else Path()
    if sub is None:
        raise SourceError(f"仓库里的路径无效：{path!r}")
    env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
           and not k.upper().startswith("GIT_CONFIG")}  # 环境里的 git 配置可能带凭证（http.extraHeader）：一律不用
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="https:file", GIT_CONFIG_NOSYSTEM="1")
    with tempfile.TemporaryDirectory(prefix="matrix-plugin-git-") as tmp:
        dest = Path(tmp) / "repo"
        cmd = ["git", "-c", "core.symlinks=false", "clone", "--depth", "1", "--no-recurse-submodules"]
        cmd += ["--branch", ref] if ref else []
        try:
            r = subprocess.run([*cmd, "--", url, str(dest)], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=GIT_TIMEOUT)
            if r.returncode != 0:
                raise SourceError(f"拉取仓库失败：{(r.stderr or r.stdout).strip().splitlines()[-1:] or r.returncode}")
            commit = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], env=env, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SourceError(f"拉取仓库失败：{e}") from e
        src = dest / sub
        if not (src / pluginfs.MANIFEST).is_file():
            raise SourceError(f"仓库的 {path or '根目录'} 下没有 {pluginfs.MANIFEST}")
        try:
            files = pluginfs.dir_files(src)
        except (OSError, pluginfs.PluginError) as e:
            raise SourceError(str(e)) from e
    return files, commit


@dataclass
class CombinedCatalog:
    """网络看到的部署方目录：plugins.json（文件）+ 后台管理的，同名以文件为准。接口同 PluginCatalog。"""

    files: pluginfs.PluginCatalog
    managed: ManagedCatalog

    @property
    def error(self) -> str | None:
        return self.files.error

    @property
    def path(self) -> Path | None:
        return self.files.path

    def items(self) -> list[pluginfs.CatalogPlugin]:
        out = self.files.items()
        names = {x.manifest.name for x in out}
        return out + [x for x in self.managed.items() if x.manifest.name not in names]

    def get(self, name: str) -> pluginfs.CatalogPlugin | None:
        return next((x for x in self.items() if x.manifest.name == name), None)


# ================= 上架申请 =================
@dataclass
class Submissions:
    """主持人交给管理员审核的插件包：`<data>/plugin-submissions/<id>/meta.json` + `pkg/`。"""

    root: Path
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def dir(self) -> Path:
        return self.root / SUBMISSIONS_DIR

    def _meta(self, sid: str) -> dict[str, Any] | None:
        if not sid.startswith("sub-") or "/" in sid or "\\" in sid:
            return None
        try:
            return json.loads((self.dir / sid / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, meta: dict[str, Any]) -> None:
        d = self.dir / meta["id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def list(self, by: str | None = None) -> list[dict[str, Any]]:
        out = []
        if self.dir.is_dir():
            for d in self.dir.iterdir():
                m = self._meta(d.name)
                if m is not None and (by is None or m.get("by_user") == by):
                    out.append(m)
        return sorted(out, key=lambda m: -m.get("created_at", 0))

    def get(self, sid: str) -> dict[str, Any]:
        m = self._meta(sid)
        if m is None:
            raise SourceError(f"unknown submission {sid}")
        return m

    def files(self, sid: str) -> dict[Path, bytes]:
        self.get(sid)
        return pluginfs.dir_files(self.dir / sid / "pkg")

    def add(self, files: dict[Path, bytes], by: str, note: str, lint: list[str]) -> dict[str, Any]:
        m = pluginfs.check_package(files)
        with self._lock:
            mine = self.list(by)
            if len(mine) >= SUBMISSIONS_PER_USER:
                raise SourceError(f"你已经提交过 {SUBMISSIONS_PER_USER} 个申请，请找管理员清理")
            if sum(1 for x in mine if x["status"] == "pending") >= PENDING_PER_USER:
                raise SourceError(f"你已经有 {PENDING_PER_USER} 个待审核的申请，等管理员处理后再提交")
            meta = {"id": new_id("sub"), "by_user": by, "note": " ".join(note.split())[:500], "name": m.name,
                    "version": m.version, "title": m.title, "description": m.description,
                    "sha256": package_hash(files), "lint": lint, "status": "pending", "created_at": time.time(),
                    "review_note": "", "reviewed_by": None, "decided_at": None,
                    "files": sorted(p.as_posix() for p in files)}
            pkg = self.dir / meta["id"] / "pkg"
            for rel, b in files.items():
                (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
                (pkg / rel).write_bytes(b)
            self._write(meta)
        return meta

    def decide(self, sid: str, status: str, by: str | None, note: str) -> dict[str, Any]:
        with self._lock:
            meta = self.get(sid)
            if meta["status"] != "pending":
                raise SourceError(f"申请 {sid} 已经处理过")
            meta.update(status=status, reviewed_by=by, review_note=" ".join(note.split())[:500], decided_at=time.time())
            self._write(meta)
        return meta
