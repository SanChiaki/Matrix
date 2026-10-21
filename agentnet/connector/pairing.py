"""设备码配对的连接器一侧（docs/device-runtimes.md 第 2.5 节）。

申请一对码 → 把用户码和网址给人看（并尝试打开浏览器）→ 轮询，直到人在已登录的网页里批准或拒绝。
批准后拿到一条普通设备令牌，只交付这一次。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .config import Config, normalize_server

Post = Callable[[str, dict[str, Any]], dict[str, Any]]


class PairingError(RuntimeError):
    pass


def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("detail")
        except Exception:  # noqa: BLE001
            detail = None
        raise PairingError(f"服务器拒绝（{e.code}）：{detail or e.reason}") from e
    except (urllib.error.URLError, OSError) as e:
        raise PairingError(f"连不上服务器：{e}") from e


def pair(server: str, *, name: str = "", hostname: str = "", harnesses: list[str] | None = None,
         post: Post = http_post, show: Callable[[dict[str, Any]], None] = lambda p: None,
         sleep: Callable[[float], None] = time.sleep, workroot: str | None = None) -> Config:
    """跑完一次配对，返回要保存的配置。`show(p)` 收到 {user_code, verify_url, expires_in}。"""
    base = normalize_server(server)
    p = post(f"{base}/api/devices/pair", {"name": name, "hostname": hostname, "harnesses": list(harnesses or [])})
    shown = {**p, "verify_url": base + str(p.get("verify_path", ""))}
    show(shown)
    interval = max(1, int(p.get("interval") or 2))
    deadline = time.monotonic() + float(p.get("expires_in") or 600)
    while time.monotonic() < deadline:
        r = post(f"{base}/api/devices/pair/poll", {"device_code": p["device_code"]})
        status = r.get("status")
        if status == "approved":
            conf = Config(server=base, token=str(r["token"]), device_id=str(r.get("device_id") or ""),
                          device_name=str(r.get("device_name") or name), email=str(r.get("email") or ""))
            if workroot:
                conf.workroot = workroot
            return conf
        if status == "denied":
            raise PairingError("配对被拒绝")
        if status != "pending":
            raise PairingError("配对码已过期，请重新运行 setup")
        sleep(interval)
    raise PairingError("配对码已过期，请重新运行 setup")
