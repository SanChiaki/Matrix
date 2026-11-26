"""matrix-connect：在用户电脑上运行的连接器。

    matrix-connect setup http://127.0.0.1:8765   # 配对（浏览器里批准）+ 装成后台服务
    matrix-connect run                            # 前台运行（后台服务就是跑这个）
    matrix-connect status                         # 本机配置与检测到的 harness
    matrix-connect install                        # 按已有配置重新装后台服务（升级后用，不重新配对）
    matrix-connect uninstall                      # 卸载后台服务（可加 --forget 删掉本机配置）

一台电脑一个连接器：本机装了的 claude / codex / dsh 都会报告给网络，Agent 选“设备 + harness”。
令牌只存在本机配置文件里（0600），不出现在命令行与服务定义中。

不装 uv / git / Python 也能用：设备页给的安装脚本下载独立二进制（`packaging/`，docs/device-runtimes.md 第 2.7 节）。
二进制里没有 Python 解释器，harness 按 `[sys.executable, <脚本>]` 拉起的 MCP 服务由二进制自己跑（`run_bundled_script`）。

旧用法仍然可用（手动签发的令牌）：

    matrix-connect --url ws://127.0.0.1:8765/api/devices/connect --token <设备令牌> --driver claude-code

连接器只发出站连接；socket 断开后按退避重连，重连期间网络里的任务在排队，不算失败。
"""

from __future__ import annotations

import argparse
import importlib
import random
import shutil
import socket
import sys
import time
import webbrowser
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from ..device import DeviceError
from . import config as cfg
from . import pairing, service
from .bridge import LocalBridge
from .client import Connector
from .drivers.base import Driver
from .drivers.claude import ClaudeDriver
from .drivers.codex import CodexDriver
from .drivers.dsh import DshDriver

# 独立二进制（PyInstaller）里没有 Python 解释器，sys.executable 是二进制自己。网络工具、连接器、受控抓取这三个
# stdio MCP 服务照旧写成 [sys.executable, <脚本路径>]（runtime.py、drivers/claude.py），二进制看到这样的参数就跑对应模块
BUNDLED_SCRIPTS = {"mcp_server.py": "matrix.mcp_server", "mcp_connectors.py": "matrix.mcp_connectors",
                   "web_fetch.py": "matrix.web_fetch"}


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def run_bundled_script(argv: list[str]) -> bool:
    """独立二进制被当成 Python 拉起一个打包进来的脚本时，就地运行它；不是这种调用返回 False。"""
    if not frozen() or not argv:
        return False
    module = BUNDLED_SCRIPTS.get(Path(argv[0]).name)
    if module is None:
        return False
    sys.argv = list(argv)
    importlib.import_module(module).main()  # 三个模块都是 `if __name__ == "__main__": main()`
    return True

# 顺序即默认优先级：第一个检测到的是这台设备的默认 harness
BUILTIN_DRIVERS = {"claude-code": ClaudeDriver, "codex": CodexDriver, "dsh": DshDriver}
DRIVER_GROUP = "matrix.connect_drivers"


def load_drivers() -> tuple[dict[str, Any], dict[str, str]]:
    """内置驱动，加上装在同一个环境里、经 entry point「matrix.connect_drivers」注册的外部驱动
    （docs/device-driver-authoring.md）。外部驱动排在内置的前面：装了它，它就是这台设备的默认 harness。
    返回（名字 -> 驱动类，加载失败的 entry point -> 原因）；加载失败的跳过，不影响连接器启动。"""
    external: dict[str, Any] = {}
    errors: dict[str, str] = {}
    try:
        eps = list(entry_points(group=DRIVER_GROUP))
    except Exception as e:  # noqa: BLE001
        return dict(BUILTIN_DRIVERS), {"*": f"{type(e).__name__}: {e}"}
    for ep in eps:
        try:
            cls = ep.load()
        except Exception as e:  # noqa: BLE001 —— 外部包写错了
            errors[ep.name] = f"{type(e).__name__}: {e}"
            continue
        name = getattr(cls, "name", None)
        if not isinstance(cls, type) or not isinstance(name, str) or not name:
            errors[ep.name] = "不是带 name 的驱动类（见 matrix.connector.drivers.base.Driver）"
        elif name in BUILTIN_DRIVERS or name in external:
            errors[ep.name] = f"驱动名「{name}」重名"
        else:
            external[name] = cls
    return {**external, **BUILTIN_DRIVERS}, errors


DRIVERS, DRIVER_ERRORS = load_drivers()

def build_driver(name: str, *, bridge_url: str = "", bin_path: str | None = None) -> Driver:
    cls = DRIVERS[name]
    if name not in BUILTIN_DRIVERS:  # 外部驱动：约定的构造参数（docs/device-driver-authoring.md）
        return cls(bin_path=bin_path, bridge_url=bridge_url)
    if name == "dsh":
        return cls(bin_path or "dsh", bridge_url)  # type: ignore[call-arg]
    if name == "claude-code":
        return cls(bin_path or "claude", bridge_url)  # type: ignore[call-arg]
    return cls(bin_path or "codex")  # type: ignore[call-arg]

def detect_drivers(only: list[str] | None = None, *, bridge_url: str = "") -> list[Driver]:
    """本机装了的 harness（按 DRIVERS 的顺序）；`only` 限定范围。"""
    names = [n for n in DRIVERS if not only or n in only]
    found = [build_driver(n, bridge_url=bridge_url) for n in names]
    return [d for d in found if d.available()]

def serve(url: str, token: str, drivers: list[Driver], workroot: Path, bridge: LocalBridge) -> None:
    """连上并一直服务；断线按退避重连。"""
    workroot = Path(workroot).expanduser()
    workroot.mkdir(parents=True, exist_ok=True)
    names = "、".join(d.name for d in drivers)
    attempt = 0
    try:
        while True:
            conn = Connector(drivers, workroot, bridge=bridge)
            try:
                conn.connect(url, token)
                attempt = 0
                print(f"已连接：{url}（{names}，工作目录 {workroot}）", flush=True)
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

def run(url: str, token: str, driver_name: str, workroot: Path, bin_path: str | None = None) -> None:
    """旧用法：一个指定的 harness。"""
    bridge = LocalBridge()
    driver = build_driver(driver_name, bridge_url=bridge.url, bin_path=bin_path)
    if not driver.available():
        bridge.close()
        print(f"找不到 {driver_name} 的可执行文件，请先安装或在 PATH 里提供", file=sys.stderr)
        raise SystemExit(2)
    serve(url, token, [driver], workroot, bridge)

# ---- 子命令 ----
def _no_config(path: Path) -> None:
    print(f"还没有配置（{path}）。先运行：matrix-connect setup <服务器地址>", file=sys.stderr)
    raise SystemExit(2)

LOG_MAX_BYTES = 5_000_000

def _log_to(path: Path) -> None:
    """输出写进日志文件：Windows 的后台服务用 pythonw 跑，没有 stdout / stderr。超过上限时滚动一份。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        path.replace(path.with_name(path.name + ".1"))
    f = open(path, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115 —— 进程结束前一直用
    sys.stdout = sys.stderr = f
    print(f"—— 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} ——")  # service.install 靠它确认任务起来了

def cmd_run(args: argparse.Namespace) -> None:
    if args.log:
        _log_to(Path(args.log).expanduser())
    path = Path(args.config).expanduser()
    conf = cfg.load(path)
    if conf is None:
        _no_config(path)
    bridge = LocalBridge()
    drivers = detect_drivers(conf.drivers or None, bridge_url=bridge.url)
    if not drivers:
        bridge.close()
        print("本机没有找到 claude / codex / dsh。装好其中一个并登录后再运行。", file=sys.stderr)
        raise SystemExit(2)
    serve(conf.ws_url, conf.token, drivers, Path(conf.workroot), bridge)

def cmd_status(args: argparse.Namespace) -> None:
    path = Path(args.config).expanduser()
    conf = cfg.load(path)
    if conf is None:
        _no_config(path)
    found = [d.name for d in detect_drivers(conf.drivers or None)]
    unit = None
    try:
        unit = service.unit_path()
    except service.ServiceError:
        pass
    print(f"服务器：{conf.server}")
    print(f"设备：{conf.device_name or '未命名'}（{conf.device_id or '?'}）· 账号 {conf.email or '?'}")
    print(f"harness：{'、'.join(found) or '本机没有找到 claude / codex / dsh'}")
    print(f"工作目录：{conf.workroot}")
    print(f"后台服务：{'已安装 ' + str(unit) if unit and unit.exists() else '未安装'}")
    if unit and unit.exists() and service.runs_from(_exe()) is False:
        print("  后台服务跑的不是这份安装（升级前的旧版本？）：运行 matrix-connect install 重新装一次")
    for ep, why in DRIVER_ERRORS.items():
        print(f"外部驱动 {ep} 没加载上：{why}")
    print(f"配置：{path}")

def _exe() -> str:
    if frozen():  # 独立二进制：就是正在跑的这个（~/.local/bin 里的是指向它的链接）
        return str(Path(sys.executable).resolve())
    found = shutil.which("matrix-connect")
    return str(Path(found).resolve()) if found else str(Path(sys.argv[0]).resolve())

def _install_service(config_path: Path) -> None:
    exe = _exe()
    if service.is_ephemeral(exe):
        print("这是 uvx 的临时环境，不能装成开机自启。要后台运行，先安装再 setup（设备页有免依赖的安装命令）：\n"
              "  uv tool install git+https://github.com/SanChiaki/Matrix.git\n"
              "现在可以先前台运行：matrix-connect run")
        return
    try:
        unit = service.install(exe, config_path)
    except service.ServiceError as e:
        print(f"没能装成后台服务：{e}\n可以前台运行：matrix-connect run", file=sys.stderr)
        return
    print(f"已装成后台服务（{unit}），登录后自动运行。{service.log_hint()}")

def cmd_setup(args: argparse.Namespace) -> None:
    path = Path(args.config).expanduser()
    host = socket.gethostname()
    name = args.name or host.removesuffix(".local")
    found = [d.name for d in detect_drivers(args.driver or None)]
    if not found:
        print("提示：本机没有找到 claude / codex / dsh；配对照常进行，装好后重启连接器即可。", file=sys.stderr)

    def show(p: dict) -> None:
        print(f"\n在浏览器里批准这台设备：\n  {p['verify_url']}\n  配对码：{p['user_code']}"
              f"（{int(p.get('expires_in', 600)) // 60} 分钟内有效）\n"
              f"只批准你刚刚在这台电脑上看到的配对码。\n", flush=True)
        if not args.no_browser:
            try:
                webbrowser.open(p["verify_url"])
            except Exception:  # noqa: BLE001 —— 没有浏览器（SSH 等）：人自己打开
                pass

    agent = None
    if args.agent:  # 一键创建：配对成功时顺带建好这个 Agent，绑到这台电脑上
        agent = {"name": args.agent, "description": args.agent_description or f"{args.agent}（{name}）"}
        if args.agent_harness:
            agent["harness"] = args.agent_harness
    made: dict[str, Any] = {}
    try:
        if args.code:  # 一次性配对令牌（业务系统替你签的）：直接换设备令牌，不用在浏览器里批准
            conf = pairing.enroll(args.server, args.code, name=name, hostname=host, harnesses=found,
                                  workroot=args.workroot, agent=agent, result=made)
        else:
            conf = pairing.pair(args.server, name=name, hostname=host, harnesses=found, show=show,
                                workroot=args.workroot, agent=agent, result=made)
    except (pairing.PairingError, ValueError) as e:
        print(f"配对失败：{e}", file=sys.stderr)
        raise SystemExit(1) from e
    conf.drivers = list(args.driver or [])
    cfg.save(conf, path)
    print(f"已配对：{conf.device_name} → {conf.email}（{'、'.join(found) or '暂无 harness'}）")
    if made.get("agent_id"):  # 以服务器说的为准：重名时它会改名；签令牌的人指定了别的 Agent 时建的是那个
        print(f"已建好 Agent「{made.get('agent_name') or (agent or {}).get('name', '')}」，绑在这台电脑上")
    elif agent:
        print("提示：服务器没有建 Agent（它的版本可能还不支持一键创建），请到 Matrix 网页里建一个绑到这台设备。", file=sys.stderr)
    if args.no_service:
        print("运行：matrix-connect run")
        return
    _install_service(path)

def cmd_install(args: argparse.Namespace) -> None:
    """按本机已有的配置重新装后台服务，不重新配对（设备和绑在它上面的 Agent 不变）。
    升级后用：服务定义指向的是装它时的那份安装，换了安装位置（如包改名后 uv tool 装进了新目录）要重新装一次。"""
    path = Path(args.config).expanduser()
    if cfg.load(path) is None:
        _no_config(path)
    _install_service(path)

def cmd_uninstall(args: argparse.Namespace) -> None:
    try:
        removed = service.uninstall()
    except service.ServiceError as e:
        print(str(e), file=sys.stderr)
        removed = None
    print(f"已卸载后台服务（{removed}）" if removed else "没有安装后台服务")
    if args.forget:
        path = Path(args.config).expanduser()
        path.unlink(missing_ok=True)
        print(f"已删除本机配置（{path}）。服务器上的设备请在「设备」页吊销。")

def _legacy(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="matrix-connect", description="Matrix 端侧连接器（手动令牌）")
    p.add_argument("--url", required=True, help="wss://<服务器>/api/devices/connect")
    p.add_argument("--token", required=True, help="设备令牌（POST /api/devices 签发）")
    p.add_argument("--driver", choices=sorted(DRIVERS), default="claude-code")
    p.add_argument("--workroot", default=str(Path.home() / ".matrix"), help="本机工作目录根（Agent 私有目录在其下的 .homes/）")
    p.add_argument("--bin", default=None, help="harness 可执行文件路径（默认按名称查找）")
    args = p.parse_args(argv)
    run(args.url, args.token, args.driver, Path(args.workroot), args.bin)

def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if run_bundled_script(argv):  # 先于 utf8_stdio：MCP 服务自己管 stdio
        return
    from .. import utf8_stdio
    utf8_stdio()
    if any(a == "--url" or a.startswith("--url=") for a in argv):
        _legacy(argv)
        return
    p = argparse.ArgumentParser(prog="matrix-connect", description="Matrix 端侧连接器：把本机 harness 接入网络")
    sub = p.add_subparsers(dest="cmd", required=True)
    conf_opt = {"default": str(cfg.default_path()), "help": "本机配置文件"}

    s = sub.add_parser("setup", help="配对这台电脑（浏览器里批准）并装成后台服务")
    s.add_argument("server", help="服务器地址，如 http://127.0.0.1:8765")
    s.add_argument("--name", default="", help="设备名称（默认用主机名）")
    s.add_argument("--driver", action="append", choices=sorted(DRIVERS),
                   help="只用这些 harness（可重复）；默认本机装了的全用")
    s.add_argument("--workroot", default=None, help="本机工作目录根（默认 ~/.matrix）")
    s.add_argument("--no-service", action="store_true", help="不装后台服务")
    s.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    s.add_argument("--code", default="", help="一次性配对令牌（POST /api/devices/enroll 签发）：直接配对，不用在浏览器里批准")
    s.add_argument("--agent", default="", help="一键创建：配对成功时顺带建一个这个名字的 Agent，绑到这台电脑上")
    s.add_argument("--agent-description", default="", help="这个 Agent 的简介（默认“<名字>（<设备名>）”）")
    s.add_argument("--agent-harness", default="", help="这个 Agent 用本机的哪个 harness（默认这台设备的默认 harness）")
    s.add_argument("--config", **conf_opt)
    s.set_defaults(func=cmd_setup)

    r = sub.add_parser("run", help="前台运行（后台服务跑的就是它）")
    r.add_argument("--config", **conf_opt)
    r.add_argument("--log", default=None, help="输出写进这个文件（后台服务用）")
    r.set_defaults(func=cmd_run)

    st = sub.add_parser("status", help="查看本机配置与检测到的 harness")
    st.add_argument("--config", **conf_opt)
    st.set_defaults(func=cmd_status)

    i = sub.add_parser("install", help="按已有配置重新装后台服务（不重新配对；升级后用）")
    i.add_argument("--config", **conf_opt)
    i.set_defaults(func=cmd_install)

    u = sub.add_parser("uninstall", help="卸载后台服务")
    u.add_argument("--forget", action="store_true", help="同时删掉本机配置（含令牌）")
    u.add_argument("--config", **conf_opt)
    u.set_defaults(func=cmd_uninstall)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":  # Windows 后台服务：pythonw -m matrix.connector.main run …
    main()
