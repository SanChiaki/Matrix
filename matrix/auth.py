"""用户与登录：邮箱 + 密码（scrypt，仅标准库），登录令牌经 HttpOnly cookie 传递（docs/control-plane.md 第 8 节，C0）。

- 令牌在 state.json 里只存 sha256：库泄露不等于会话泄露。
- 会话闲置 SESSION_IDLE_S 过期、最长 SESSION_MAX_S（MATRIX_SESSION_IDLE_DAYS / MATRIX_SESSION_MAX_DAYS）；登录、改密码、
  重置密码都换新令牌，改密码与重置后这个人其余的会话全部作废。“我的会话”可以逐个下线。
- 登录、注册、重置按账号与来源地址限流（`Throttle`），连续失败后指数退避，限流期间正确的密码也不放行。
- 忘了密码由管理员发一条一次性的重置链接（`issue_reset` / `reset_password`），用户自己设新密码，管理员看不到新密码。
没有邮箱验证（没有发信服务）；OIDC 单点登录是 C4。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from typing import Any

from . import compat
from .models import Device, User, new_id
from .store import Store

COOKIE = "matrix_session"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


class AuthError(ValueError):
    pass


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, **SCRYPT).hex()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _check_password(password: str) -> None:
    if len(password) < 8:
        raise AuthError("密码至少 8 位")


def _days(name: str, default: float) -> float:
    try:
        v = float(compat.env(name) or default)
    except ValueError:
        return default * 86400
    return (v if v > 0 else default) * 86400


def session_limits() -> tuple[float, float]:
    """（闲置多久过期, 最长多久）秒。"""
    return _days("SESSION_IDLE_DAYS", 7), _days("SESSION_MAX_DAYS", 30)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def register(store: Store, email: str, password: str, meta: dict[str, str] | None = None) -> tuple[User, str, bool]:
    """建用户并登录；返回 (用户, 登录令牌, 是否第一个用户)。第一个用户认领所有无主的旧数据。"""
    email = normalize_email(email)
    if not EMAIL_RE.match(email):
        raise AuthError("邮箱格式不对")
    _check_password(password)
    salt = secrets.token_bytes(16)
    with store.lock:
        if any(u.email == email for u in store.users.values()):
            raise AuthError("这个邮箱已经注册过")
        first = not store.users
        user = User(id=new_id("user"), email=email, password_hash=_hash(password, salt), salt=salt.hex())
        store.users[user.id] = user
        if first:
            store.claim_orphans(user.id)
        token = _new_session(store, user.id, meta)
        store.save()
    return user, token, first


def _verify(user: User | None, password: str) -> bool:
    # 不存在的邮箱也算一次哈希，不让响应时间暴露邮箱是否注册过
    expected = user.password_hash if user else _hash(password, b"0" * 16)
    got = _hash(password, bytes.fromhex(user.salt) if user else b"1" * 16)
    return user is not None and hmac.compare_digest(expected, got)


def login(store: Store, email: str, password: str, meta: dict[str, str] | None = None,
          old_token: str | None = None) -> tuple[User, str]:
    """登录换一个新令牌；带着旧 cookie 来登录的，旧的那个会话作废（不叠着留）。"""
    email = normalize_email(email)
    with store.lock:
        user = next((u for u in store.users.values() if u.email == email), None)
        if not _verify(user, password):
            raise AuthError("邮箱或密码不对")
        assert user is not None
        if old_token:
            store.sessions.pop(token_hash(old_token), None)
        token = _new_session(store, user.id, meta)
        store.save()
    return user, token


def logout(store: Store, token: str | None) -> None:
    with store.lock:
        if token and store.sessions.pop(token_hash(token), None) is not None:
            store.save()


TOUCH_S = 300   # 最近使用时间多久写一次盘（每个请求都写 state.json 太贵；闲置按天算，差几分钟无所谓）


def user_of(store: Store, token: str | None, now: float | None = None) -> User | None:
    """cookie 里的令牌 → 用户。过期（闲置太久或到了最长期限）的会话删掉、按没登录处理。"""
    if not token:
        return None
    now = time.time() if now is None else now
    h = token_hash(token)
    with store.lock:
        s = store.sessions.get(h)
        if s is None:
            return None
        idle, longest = session_limits()
        if now - s["last_used"] > idle or now - s["created_at"] > longest or s["user_id"] not in store.users:
            del store.sessions[h]
            store.save()
            return None
        if now - s["last_used"] > TOUCH_S:
            s["last_used"] = now
            store.save()
        return store.users[s["user_id"]]


def sessions_of(store: Store, user_id: str, current: str | None) -> list[dict[str, Any]]:
    """“我的会话”：不含令牌与哈希，只有会话 id、时间与来源。"""
    me = token_hash(current) if current else ""
    with store.lock:
        rows = [{"id": s["id"], "created_at": s["created_at"], "last_used": s["last_used"], "ip": s.get("ip", ""),
                 "ua": s.get("ua", ""), "current": h == me} for h, s in store.sessions.items() if s["user_id"] == user_id]
    return sorted(rows, key=lambda r: (not r["current"], -r["last_used"]))


def revoke_session(store: Store, user_id: str, session_id: str) -> None:
    with store.lock:
        h = next((h for h, s in store.sessions.items() if s["user_id"] == user_id and s["id"] == session_id), None)
        if h is None:
            raise AuthError("会话不存在")
        del store.sessions[h]
        store.save()


def revoke_others(store: Store, user_id: str, keep: str | None) -> int:
    """下线这个人除 keep 以外的所有会话；返回下线了几个。"""
    k = token_hash(keep) if keep else ""
    with store.lock:
        gone = [h for h, s in store.sessions.items() if s["user_id"] == user_id and h != k]
        for h in gone:
            del store.sessions[h]
        if gone:
            store.save()
    return len(gone)


def change_password(store: Store, user_id: str, old: str, new: str, meta: dict[str, str] | None = None) -> str:
    """改密码：核对旧密码，换新令牌，这个人其余的会话（含发起的这一个的旧令牌）全部作废；返回新的登录令牌。"""
    _check_password(new)
    salt = secrets.token_bytes(16)
    digest = _hash(new, salt)
    with store.lock:
        user = store.users.get(user_id)
        if not _verify(user, old):
            raise AuthError("原密码不对")
        assert user is not None
        user.password_hash, user.salt = digest, salt.hex()
        store.sessions = {h: s for h, s in store.sessions.items() if s["user_id"] != user_id}
        token = _new_session(store, user_id, meta)
        store.save()
    return token


# ---- 管理员重置密码：一次性链接，用户自己设新密码 ----
# 链接里的令牌只在签发时返回一次，state.json 里只存 sha256；每人同时只有一条（再发一条，旧的作废），
# RESET_TTL_S 内有效，用过即作废。重置后这个人所有的登录都失效（端侧连接器令牌不受影响，在“设备”里另行吊销）。
RESET_TTL_S = 24 * 3600


def _reset_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_reset(store: Store, user_id: str, by: str | None = None) -> tuple[str, float]:
    """给 user_id 签发重置令牌；返回 (令牌明文, 过期时间)。调用方负责确认 by 是管理员。"""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with store.lock:
        if user_id not in store.users:
            raise AuthError("用户不存在")
        for h in [h for h, r in store.resets.items() if r["user_id"] == user_id]:
            del store.resets[h]
        store.resets[_reset_hash(token)] = {"user_id": user_id, "by": by, "created_at": now,
                                            "expires_at": now + RESET_TTL_S}
        store.save()
    return token, now + RESET_TTL_S


def _reset_entry(store: Store, token: str) -> tuple[str, User]:
    """调用方持有锁。有效的令牌 → (哈希, 用户)；过期的顺手删掉。"""
    h = _reset_hash(token or "")
    r = store.resets.get(h)
    user = store.users.get(r["user_id"]) if r else None
    if r is not None and (user is None or r["expires_at"] <= time.time()):
        del store.resets[h]
        store.save()
        user = None
    if user is None:
        raise AuthError("重置链接无效或已过期，请找管理员重新发一条")
    return h, user


def reset_info(store: Store, token: str) -> str:
    """重置页显示是哪个账号：令牌有效时返回邮箱。"""
    with store.lock:
        return _reset_entry(store, token)[1].email


def reset_password(store: Store, token: str, password: str, meta: dict[str, str] | None = None) -> tuple[User, str]:
    """用重置令牌设新密码：令牌作废、这个人原有的登录全部失效，再登录一次；返回 (用户, 新的登录令牌)。"""
    _check_password(password)
    salt = secrets.token_bytes(16)
    digest = _hash(password, salt)  # 在锁外算：scrypt 慢
    with store.lock:
        h, user = _reset_entry(store, token)
        del store.resets[h]
        user.password_hash, user.salt = digest, salt.hex()
        store.sessions = {t: s for t, s in store.sessions.items() if s["user_id"] != user.id}
        session = _new_session(store, user.id, meta)
        store.save()
    return user, session


# ---- 端侧连接器令牌（与登录 cookie 分开：可多条、按设备吊销、不随退出登录失效） ----
def _device_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_device_token(store: Store, user_id: str, name: str = "") -> tuple[Device, str]:
    """签发一个连接器令牌；明文只在这里返回一次，之后只存哈希。"""
    token = secrets.token_urlsafe(32)
    dev = Device(id=new_id("dev"), user_id=user_id, name=(name.strip() or "未命名设备")[:80],
                 token_hash=_device_hash(token))
    with store.lock:
        store.devices[dev.id] = dev
        store.save()
    return dev, token


def device_of(store: Store, token: str | None) -> Device | None:
    """令牌 → 设备；吊销的不认。"""
    if not token:
        return None
    h = _device_hash(token)
    for d in store.devices.values():
        if d.revoked_at is None and hmac.compare_digest(d.token_hash, h):
            return d
    return None


def revoke_device(store: Store, device_id: str, actor: str | None) -> Device:
    with store.lock:
        d = store.devices.get(device_id)
        if d is None or (actor is not None and d.user_id != actor):
            raise AuthError("设备不存在")
        if not d.revoked:
            d.revoked_at = time.time()
            store.save()
    return d


# ---- 设备码配对（docs/device-runtimes.md 第 2.5 节） ----
# 连接器申请一对码：device_code 是它自己轮询用的密钥（高熵，不给人看），user_code 显示给人、在已登录的网页里批准。
# 批准即签发一条普通设备令牌，经轮询只交付一次。全在内存里：服务重启后没批准的申请作废，连接器重新 setup 即可。
PAIR_TTL_S = 600
PAIR_INTERVAL_S = 2
PAIR_MAX_PENDING = 64     # 申请不需要登录：同时等待的申请有上限
PAIR_MAX_MISSES = 10      # 每个用户在 PAIR_TTL_S 内最多输错这么多次用户码（码只有 8 位，挡住逐个试）
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混的 0/O、1/I


class PairingLimit(AuthError):
    """申请太多 / 输错太多。"""


def normalize_user_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def display_user_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


class Pairings:
    def __init__(self, now: Any = time.time):
        self.now = now
        self._lock = threading.Lock()
        self._by_device: dict[str, dict[str, Any]] = {}   # device_code -> 申请
        self._misses: dict[str, list[float]] = {}         # user_id -> 输错的时间

    def _sweep(self) -> None:
        """调用方持有锁。"""
        t = self.now()
        for k in [k for k, p in self._by_device.items() if p["expires_at"] <= t]:
            del self._by_device[k]

    def start(self, name: str, hostname: str, harnesses: list[str]) -> dict[str, Any]:
        with self._lock:
            self._sweep()
            if sum(p["status"] == "pending" for p in self._by_device.values()) >= PAIR_MAX_PENDING:
                raise PairingLimit("配对申请太多，请稍后再试")
            user_code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
            device_code = secrets.token_urlsafe(32)
            self._by_device[device_code] = {
                "user_code": user_code, "status": "pending", "expires_at": self.now() + PAIR_TTL_S,
                "name": name, "hostname": hostname, "harnesses": harnesses, "created_at": self.now()}
        return {"device_code": device_code, "user_code": display_user_code(user_code),
                "expires_in": PAIR_TTL_S, "interval": PAIR_INTERVAL_S}

    def lookup(self, user_code: str, user_id: str) -> tuple[str, dict[str, Any]]:
        """人输入的码 → 等待中的申请。输错计数；超限后一律不受理（含正确的码）。"""
        code = normalize_user_code(user_code)
        with self._lock:
            self._sweep()
            t = self.now()
            misses = [m for m in self._misses.get(user_id, []) if m > t - PAIR_TTL_S]
            self._misses[user_id] = misses
            if len(misses) >= PAIR_MAX_MISSES:
                raise PairingLimit("输错次数太多，请稍后再试")
            for device_code, p in self._by_device.items():
                if p["status"] == "pending" and hmac.compare_digest(p["user_code"], code):
                    return device_code, p
            misses.append(t)
        raise AuthError("配对码不存在或已过期")

    def view(self, p: dict[str, Any]) -> dict[str, Any]:
        """批准页看到的内容：不含 device_code。"""
        return {"user_code": display_user_code(p["user_code"]), "name": p["name"], "hostname": p["hostname"],
                "harnesses": list(p["harnesses"]), "created_at": p["created_at"], "expires_at": p["expires_at"]}

    def approve(self, store: Store, user_code: str, user_id: str, name: str = "") -> Device:
        device_code, p = self.lookup(user_code, user_id)
        dev, token = issue_device_token(store, user_id, name.strip() or p["name"])
        with store.lock:
            dev.hostname, dev.harnesses = p["hostname"], list(p["harnesses"])
            store.save()
        with self._lock:
            p.update(status="approved", token=token, device_id=dev.id, name=dev.name, email=_email(store, user_id))
        return dev

    def deny(self, user_code: str, user_id: str) -> None:
        _, p = self.lookup(user_code, user_id)
        with self._lock:
            p["status"] = "denied"

    def poll(self, device_code: str) -> dict[str, Any]:
        """连接器轮询。批准后的令牌只交付一次；拒绝也只报告一次。"""
        with self._lock:
            self._sweep()
            p = self._by_device.get(device_code or "")
            if p is None:
                return {"status": "expired"}
            if p["status"] == "pending":
                return {"status": "pending"}
            del self._by_device[device_code]
            if p["status"] == "denied":
                return {"status": "denied"}
            return {"status": "approved", "token": p["token"], "device_id": p["device_id"],
                    "device_name": p["name"], "email": p["email"]}


def _email(store: Store, user_id: str) -> str:
    u = store.users.get(user_id)
    return u.email if u is not None else ""


def _new_session(store: Store, user_id: str, meta: dict[str, str] | None = None) -> str:
    """调用方持有锁。令牌明文只返回这一次，状态里只存哈希。meta：来源地址与 User-Agent 摘要（给“我的会话”看）。"""
    token = secrets.token_urlsafe(32)
    now = time.time()
    m = meta or {}
    store.sessions[token_hash(token)] = {"id": secrets.token_hex(8), "user_id": user_id, "created_at": now,
                                         "last_used": now, "ip": str(m.get("ip", ""))[:64], "ua": str(m.get("ua", ""))[:120]}
    return token
