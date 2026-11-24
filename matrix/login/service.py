"""登录入口与进行中的登录。部署方在 `login.json`（默认 `<data>/login.json`，`--login` / `MATRIX_LOGIN`，改了就生效）里写：

    {"public_url": "https://matrix.corp.example",
     "password_login": true,
     "providers": [
       {"id": "corp", "name": "公司账号", "driver": "oauth2",
        "auto_create": true, "link_by_email": false, "allow_domains": ["corp.example"],
        "config": {...驱动自己的配置，见各驱动...}}
     ]}

- `public_url`：用户在浏览器里打开 Matrix 用的地址。回调地址是 `<public_url>/api/auth/sso/<id>/callback`，要在身份系统那边登记。
  不写时按请求里的地址拼（只适合本机试）。
- `password_login`：false = 有可用的入口时关掉邮箱密码的注册与登录，只留管理员与业务系统账号（应急与只读页面）。
- `providers[].id`：2–32 位小写字母、数字、`-`、`_`，字母开头。定了就不要改：用户的绑定按它记。
- `auto_create`（默认 true）：第一次用公司账号登录的人自动建 Matrix 账号。false = 只让已经有账号的人登录。
- `link_by_email`（默认 false）：公司账号的邮箱和一个已有的 Matrix 账号相同时，把两者当成同一个人。
  只在身份系统的邮箱确实归本人所有（公司统一分配、改不了）时打开：打开后，谁能在身份系统里拿到某个邮箱，谁就能进那个账号。
- `allow_domains`：只放这些邮箱域名的人进来；不写 = 不限。
- `config` 里的字符串可以写 `${环境变量}`：密钥放在环境变量（`deploy/.env`）里，不要直接写进文件。变量名里要带 `KEY`：
  服务进程的环境会传给云侧的 dsh，dsh 按名字不把这类变量交给 Agent 的命令（docs/reference/security.md）。

规则（驱动不用管）：
- 一次登录只认发起它的那个浏览器：发起时给浏览器一个一次性的 cookie，回调时凭它找到这次登录，再核对 state。
  10 分钟内有效，用过（不论成败）即作废。
- 身份怎么对到 Matrix 用户在 auth.sso_login；登录状态仍是 Matrix 自己的 cookie。
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .base import Driver, Flow, Identity, LoginError
from .registry import load_drivers

FLOW_COOKIE = "matrix_login"
FLOW_TTL_S = 600
MAX_FLOWS = 2000          # 发起登录不需要登录：同时进行的有上限
PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class Provider:
    id: str
    name: str                            # 登录页按钮上的名字
    driver: Driver
    auto_create: bool = True
    link_by_email: bool = False
    allow_domains: tuple[str, ...] = ()

    def public(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name}


def _expand(value: Any) -> Any:
    """把配置里的 ${环境变量} 换成值；没设的变量报错（不悄悄当空串）。"""
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            if "KEY" not in m.group(1):  # 服务进程的环境会传给云侧的 dsh：它按名字（带 KEY）不把变量交给 Agent 的命令
                raise LoginError(f"环境变量 {m.group(1)} 的名字里要带 KEY（这样云侧 Agent 的命令才看不到它）")
            if m.group(1) not in os.environ:
                raise LoginError(f"环境变量 {m.group(1)} 没有设")
            return os.environ[m.group(1)]
        return _ENV.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


class LoginService:
    def __init__(self, path: Path | None, drivers: dict[str, type[Driver]] | None = None,
                 now: Callable[[], float] = time.time):
        """drivers：可用的驱动类；不传 = 内置的加上 entry point 注册的。"""
        self.path, self.now = path, now
        self.driver_errors: dict[str, str] = {}
        if drivers is None:
            drivers, self.driver_errors = load_drivers()
        self.drivers = drivers
        self.errors: dict[str, str] = {}                 # 入口 id（或 "login.json"）-> 为什么没启用
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._providers: list[Provider] = []
        self._public_url = ""
        self._password_login = True
        self._flows: dict[str, dict[str, Any]] = {}      # 浏览器的一次性 cookie -> 这次登录

    # ---- 配置 ----
    def _load(self) -> None:
        """调用方持有锁。按修改时间热加载；整份读不了时沿用上一份，某个入口不对只跳过它。"""
        if self.path is None or not self.path.is_file():
            self._providers, self._public_url, self._password_login, self._mtime, self.errors = [], "", True, None, {}
            return
        m = self.path.stat().st_mtime
        if m == self._mtime:
            return
        self._mtime = m
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("providers", []), list):
                raise LoginError('要是 {"providers": [...]}')
            public_url = str(raw.get("public_url") or "").rstrip("/")
            if public_url and not re.match(r"^https?://[^/\s]+(/\S*)?$", public_url):
                raise LoginError("public_url 要是 http(s) 地址")
        except (ValueError, OSError, LoginError) as e:
            self.errors = {"login.json": str(e)}
            print(f"[matrix] 登录配置 {self.path} 读不了，沿用上一份：{e}", file=sys.stderr)
            return
        providers: list[Provider] = []
        errors: dict[str, str] = {}
        for i, p in enumerate(raw.get("providers", [])):
            pid = str(p.get("id", "")) if isinstance(p, dict) else ""
            try:
                providers.append(self._provider(p, pid, {x.id for x in providers}))
            except LoginError as e:
                errors[pid or f"#{i + 1}"] = str(e)
            except Exception as e:  # 外部驱动的构造函数写错了：跳过这个入口，不拖垮服务
                errors[pid or f"#{i + 1}"] = f"{type(e).__name__}: {e}"
        for pid, why in errors.items():
            print(f"[matrix] 登录入口 {pid} 没有启用：{why}", file=sys.stderr)
        self._providers, self._public_url, self.errors = providers, public_url, errors
        self._password_login = raw.get("password_login", True) is not False

    def _provider(self, p: Any, pid: str, taken: set[str]) -> Provider:
        if not isinstance(p, dict):
            raise LoginError("每个入口要是一个对象")
        if not PROVIDER_ID.match(pid):
            raise LoginError("id 要是 2–32 位小写字母、数字、-、_，字母开头")
        if pid in taken:
            raise LoginError("id 重复")
        cls = self.drivers.get(str(p.get("driver") or ""))
        if cls is None:
            raise LoginError(f"没有驱动「{p.get('driver')}」（可用：{'、'.join(sorted(self.drivers))}）")
        config = p.get("config") or {}
        domains = p.get("allow_domains") or []
        if not isinstance(config, dict) or not isinstance(domains, list):
            raise LoginError("config 要是对象，allow_domains 要是数组")
        return Provider(id=pid, name=str(p.get("name") or pid)[:40], driver=cls(_expand(config)),
                        auto_create=p.get("auto_create", True) is not False, link_by_email=p.get("link_by_email") is True,
                        allow_domains=tuple(str(d).strip().lower().lstrip("@") for d in domains if str(d).strip()))

    def providers(self) -> list[Provider]:
        with self._lock:
            self._load()
            return list(self._providers)

    @property
    def password_login(self) -> bool:
        """邮箱密码还能不能用。没有可用的入口时总是能用：配置写坏了不能把所有人关在外面。"""
        with self._lock:
            self._load()
            return self._password_login or not self._providers

    def public(self) -> dict[str, Any]:
        """给登录页的：有哪些入口、邮箱密码还开不开。"""
        return {"providers": [p.public() for p in self.providers()], "password": self.password_login}

    # ---- 一次登录 ----
    def begin(self, provider_id: str, base_url: str) -> tuple[str, str]:
        """发起登录。返回（浏览器要跳去的地址，要给这个浏览器设的一次性 cookie 值）。"""
        with self._lock:
            self._load()
            p = next((x for x in self._providers if x.id == provider_id), None)
            if p is None:
                raise LoginError("没有这个登录入口")
            t = self.now()
            for k in [k for k, f in self._flows.items() if f["expires_at"] <= t]:
                del self._flows[k]
            if len(self._flows) >= MAX_FLOWS:
                raise LoginError("正在登录的人太多，请稍后再试")
            root = self._public_url or base_url.rstrip("/")
        flow = Flow(state=secrets.token_urlsafe(24), redirect_uri=f"{root}/api/auth/sso/{p.id}/callback")
        url = _guard(p, lambda: p.driver.authorize_url(flow))
        if not isinstance(url, str) or not re.match(r"^https?://", url):
            raise LoginError("登录驱动给的地址不是 http(s) 地址")
        key = secrets.token_urlsafe(32)
        with self._lock:
            self._flows[key] = {"provider": p, "flow": flow, "expires_at": t + FLOW_TTL_S}
        return url, key

    def complete(self, provider_id: str, key: str | None, params: dict[str, str]) -> tuple[Provider, Identity]:
        """回调。凭浏览器的一次性 cookie 找到这次登录、核对 state，再让驱动认人。不论成败，这次登录都作废。"""
        with self._lock:
            entry = self._flows.pop(key or "", None)
        if entry is None or entry["provider"].id != provider_id or entry["expires_at"] <= self.now():
            raise LoginError("这次登录已经过期，或不是从这个浏览器发起的，请重新登录")
        p, flow = entry["provider"], entry["flow"]
        if not hmac.compare_digest(str(params.get(p.driver.state_param, "")), flow.state):
            raise LoginError("回调与发起的登录对不上，请重新登录")
        ident = _guard(p, lambda: p.driver.identify(flow, params))
        if not isinstance(ident, Identity) or not str(ident.subject).strip():
            raise LoginError("登录驱动没有给出用户标识")
        return p, ident


def _guard(p: Provider, call: Callable[[], Any]) -> Any:
    """调驱动。LoginError 原样给人看；别的异常只记类型（消息里可能带令牌），给人看一句笼统的。"""
    try:
        return call()
    except LoginError:
        raise
    except Exception as e:
        print(f"[matrix] 登录入口 {p.id} 的驱动出错：{type(e).__name__}", file=sys.stderr)
        raise LoginError("登录驱动出错，请联系管理员") from e
