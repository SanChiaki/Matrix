"""把连接器装成用户级后台服务（docs/device-runtimes.md 第 2.6 节）：开机 / 登录后自动运行，崩溃自动重启。

- macOS：`~/Library/LaunchAgents/<LABEL>.plist`，`launchctl bootstrap gui/<uid>`。
- Linux：`~/.config/systemd/user/matrix-connect.service`，`systemctl --user enable --now`。
- 其他系统：不支持，提示前台运行 `matrix-connect run`。

服务定义里只有可执行文件、配置文件路径和 PATH（让服务找得到 claude / codex / dsh），**不含令牌**。
"""

from __future__ import annotations

import os
import platform
import plistlib
import subprocess
from pathlib import Path
from typing import Callable

LABEL = "dev.matrix.connect"
UNIT = "matrix-connect.service"

Run = Callable[[list[str]], None]


class ServiceError(RuntimeError):
    pass


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise ServiceError(f"{' '.join(cmd)} 失败：{(r.stderr or r.stdout).strip()}")


def is_ephemeral(exe: str) -> bool:
    """uvx 的临时环境（uv 缓存里）会被清理，不能写进开机自启。"""
    p = str(exe).replace("\\", "/")
    return "/.cache/uv/" in p or "/archive-v" in p or "/uv/cache/" in p


def launchd_plist(exe: str, config: Path, env_path: str, log: Path) -> str:
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(exe), "run", "--config", str(config)],
        "EnvironmentVariables": {"PATH": env_path},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }).decode()


def _q(s: str) -> str:
    return f'"{s}"' if any(c.isspace() for c in s) else s


def systemd_unit(exe: str, config: Path, env_path: str) -> str:
    return "\n".join([
        "[Unit]",
        "Description=Matrix connector (matrix-connect)",
        "After=network-online.target",
        "",
        "[Service]",
        f"ExecStart={_q(str(exe))} run --config {_q(str(config))}",
        f"Environment=PATH={env_path}",
        "Restart=always",
        "RestartSec=10",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def unit_path(system: str | None = None, home: Path | None = None) -> Path:
    system, home = system or platform.system(), Path(home or Path.home())
    if system == "Darwin":
        return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if system == "Linux":
        return home / ".config" / "systemd" / "user" / UNIT
    raise ServiceError(f"{system} 上还不支持后台服务；请前台运行 matrix-connect run")


def install(exe: str, config: Path, *, system: str | None = None, home: Path | None = None,
            run: Run = _run, env_path: str | None = None) -> Path:
    system, home = system or platform.system(), Path(home or Path.home())
    path = unit_path(system, home)
    env_path = env_path or os.environ.get("PATH", "/usr/bin:/bin")
    path.parent.mkdir(parents=True, exist_ok=True)
    if system == "Darwin":
        log = home / "Library" / "Logs" / "matrix-connect.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        domain = f"gui/{os.getuid()}"
        try:  # 已经装过：先卸载旧的，再按新定义加载
            run(["launchctl", "bootout", domain, str(path)])
        except ServiceError:
            pass
        path.write_text(launchd_plist(exe, config, env_path, log))
        run(["launchctl", "bootstrap", domain, str(path)])
    else:
        path.write_text(systemd_unit(exe, config, env_path))
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", "--now", UNIT])
    return path


def uninstall(*, system: str | None = None, home: Path | None = None, run: Run = _run) -> Path | None:
    system = system or platform.system()
    path = unit_path(system, home)
    if not path.exists():
        return None
    try:
        if system == "Darwin":
            run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)])
        else:
            run(["systemctl", "--user", "disable", "--now", UNIT])
    except ServiceError:
        pass  # 没在运行：照样删掉定义
    path.unlink()
    if system == "Linux":
        try:
            run(["systemctl", "--user", "daemon-reload"])
        except ServiceError:
            pass
    return path


def log_hint(system: str | None = None) -> str:
    system = system or platform.system()
    if system == "Darwin":
        return f"日志：{Path.home() / 'Library/Logs/matrix-connect.log'}"
    return f"日志：journalctl --user -u {UNIT} -f"
