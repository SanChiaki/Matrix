"""用户与登录：邮箱 + 密码（scrypt，仅标准库），登录令牌存在 state.json 里，经 HttpOnly cookie 传递。

这是最简实现：没有邮箱验证、找回密码、令牌过期轮换；令牌在退出登录时作废。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time

from .models import User, new_id
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


def register(store: Store, email: str, password: str) -> tuple[User, str, bool]:
    """建用户并登录；返回 (用户, 登录令牌, 是否第一个用户)。第一个用户认领所有无主的旧数据。"""
    email = normalize_email(email)
    if not EMAIL_RE.match(email):
        raise AuthError("邮箱格式不对")
    if len(password) < 8:
        raise AuthError("密码至少 8 位")
    salt = secrets.token_bytes(16)
    with store.lock:
        if any(u.email == email for u in store.users.values()):
            raise AuthError("这个邮箱已经注册过")
        first = not store.users
        user = User(id=new_id("user"), email=email, password_hash=_hash(password, salt), salt=salt.hex())
        store.users[user.id] = user
        if first:
            store.claim_orphans(user.id)
        token = _new_session(store, user.id)
        store.save()
    return user, token, first


def login(store: Store, email: str, password: str) -> tuple[User, str]:
    email = normalize_email(email)
    with store.lock:
        user = next((u for u in store.users.values() if u.email == email), None)
        # 不存在的邮箱也算一次哈希，不让响应时间暴露邮箱是否注册过
        expected = user.password_hash if user else _hash(password, b"0" * 16)
        got = _hash(password, bytes.fromhex(user.salt) if user else b"1" * 16)
        if user is None or not hmac.compare_digest(expected, got):
            raise AuthError("邮箱或密码不对")
        token = _new_session(store, user.id)
        store.save()
    return user, token


def logout(store: Store, token: str | None) -> None:
    with store.lock:
        if token and store.sessions.pop(token, None) is not None:
            store.save()


def user_of(store: Store, token: str | None) -> User | None:
    if not token:
        return None
    s = store.sessions.get(token)
    return store.users.get(s["user_id"]) if s else None


def _new_session(store: Store, user_id: str) -> str:
    """调用方持有锁。"""
    token = secrets.token_urlsafe(32)
    store.sessions[token] = {"user_id": user_id, "created_at": time.time()}
    return token
