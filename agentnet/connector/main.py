"""matrix-connect：在用户电脑上运行的连接器。

    matrix-connect --url wss://matrix.example.com/api/devices/connect --token <设备令牌> \
                   --driver dsh|claude-code|codex --workroot ~/.matrix

令牌在服务器的「设置 → 设备」里签发（`POST /api/devices`），可随时吊销。
连接器只发出站连接；socket 断开后按退避重连，重连期间网络里的任务在排队，不算失败。
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

from ..device import DeviceError
from .bridge import LocalBridge
from .client import Connector
from .drivers.base import Driver
from .drivers.claude import ClaudeDriver
from .drivers.codex import CodexDriver
from .drivers.dsh import DshDriver

DRIVERS = {"dsh": DshDriver, "claude-code": ClaudeDriver, "codex": CodexDriver}


def build_driver(name: str, *, bridge_url: str = "", bin_path: str | None = None) -> Driver:
    cls = DRIVERS[name]
    if name == "dsh":
        return cls(bin_path or "dsh", bridge_url)  # type: ignore[call-arg]
    if name == "claude-code":
        return cls(bin_path or "claude", bridge_url)  # type: ignore[call-arg]
    return cls(bin_path or "codex")  # type: ignore[call-arg]


def run(url: str, token: str, driver_name: str, workroot: Path, bin_path: str | None = None) -> None:
    workroot = Path(workroot).expanduser()
    workroot.mkdir(parents=True, exist_ok=True)
    bridge = LocalBridge()
    driver = build_driver(driver_name, bridge_url=bridge.url, bin_path=bin_path)
    if not driver.available():
        bridge.close()
        print(f"找不到 {driver_name} 的可执行文件，请先安装或在 PATH 里提供", file=sys.stderr)
        raise SystemExit(2)
    attempt = 0
    try:
        while True:
            conn = Connector(driver, workroot, bridge=bridge)
            try:
                conn.connect(url, token)
                attempt = 0
                print(f"已连接：{url}（{driver.name}，工作目录 {workroot}）", flush=True)
                conn.serve_forever()
            except DeviceError as e:
                attempt += 1
                wait = min(60, 2 ** min(attempt, 6)) + random.uniform(0, 1)
                print(f"连接中断（{e}），{wait:.0f} 秒后重连…", file=sys.stderr, flush=True)
                time.sleep(wait)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Matrix 端侧连接器：把本机 harness 接入网络")
    p.add_argument("--url", required=True, help="wss://<服务器>/api/devices/connect")
    p.add_argument("--token", required=True, help="设备令牌（POST /api/devices 签发）")
    p.add_argument("--driver", choices=sorted(DRIVERS), default="claude-code")
    p.add_argument("--workroot", default=str(Path.home() / ".matrix"), help="本机工作目录根")
    p.add_argument("--bin", default=None, help="harness 可执行文件路径（默认按名称查找）")
    args = p.parse_args(argv)
    run(args.url, args.token, args.driver, Path(args.workroot), args.bin)
