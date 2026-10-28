"""把连接器装成用户级后台服务（docs/device-runtimes.md 第 2.6 节）：开机 / 登录后自动运行，崩溃自动重启。

- macOS：`~/Library/LaunchAgents/<LABEL>.plist`，`launchctl bootstrap gui/<uid>`。
- Linux：`~/.config/systemd/user/matrix-connect.service`，`systemctl --user enable --now`。
- Windows：任务计划程序里当前用户的“登录时”任务（`schtasks /Create /XML`，不需要管理员），定义另存在
  `%LOCALAPPDATA%\\matrix-connect\\task.xml`。用 pythonw 跑（没有控制台窗口），日志写文件；每 5 分钟重复触发、
  已在运行就忽略，进程退出后最多 5 分钟重新拉起（相当于 KeepAlive / Restart=always）。
  装完要等连接器在日志里写下启动行才算成功，没起来就撤销任务并报错（任务计划程序自己不报告这个）。
  不做成 Windows 服务：那要管理员，而且以 SYSTEM 身份运行，拿不到用户自己的 harness 凭据。
- 其他系统：不支持，提示前台运行 `matrix-connect run`。

服务定义里只有可执行文件、配置文件路径和 PATH（让服务找得到 claude / codex / dsh），**不含令牌**。
Windows 的任务以用户身份运行，用的是用户自己的环境变量（PATH 里有 npm 装的 harness），不另写 PATH。
"""

from __future__ import annotations

import getpass
import os
import platform
import plistlib
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable
from xml.sax.saxutils import escape

LABEL = "dev.matrix.connect"
UNIT = "matrix-connect.service"
TASK = "MatrixConnect"  # Windows 任务计划程序里的任务名

Run = Callable[[list[str]], None]


class ServiceError(RuntimeError):
    pass


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=False)
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


def windows_user() -> str:
    domain, name = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{name}" if domain else name


def _started(log: Path, since: float, timeout: float = 15) -> bool:
    """任务真的跑起来了：连接器启动时往日志里写一行（main._log_to）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if log.stat().st_mtime >= since:
                return True
        except FileNotFoundError:
            pass
        time.sleep(0.5)
    return False


def windows_launcher(exe: str, python: str | None = None) -> Path:
    """后台跑用的 pythonw.exe：优先取正在运行 setup 的 Python 旁边那个（它的环境里装着 matrix）。
    uv tool 在 Windows 上把 matrix-connect.exe 复制（不是链接）进 ~/.local/bin，旁边没有 pythonw；
    PATH 上的 pythonw 多半是别的 Python，没装 matrix，不能用。其次才看 matrix-connect.exe 旁边（venv 的 Scripts）。
    直接跑 matrix-connect.exe 会一直挂着一个控制台窗口。"""
    python = python or sys.executable
    tried = [Path(exe).with_name("pythonw.exe")]
    if python and not is_ephemeral(python):  # uvx 的临时环境会被清理
        tried.insert(0, Path(python).with_name("pythonw.exe"))
    for p in tried:
        if p.exists():
            return p
    raise ServiceError(f"找不到 {' 或 '.join(map(str, tried))}：后台服务要用 pythonw 运行（没有控制台窗口）")


def windows_task_xml(launcher: Path, config: Path, log: Path, user: str, workdir: Path) -> str:
    q = lambda s: escape(f'"{s}"')  # noqa: E731 —— 路径可能带空格
    args = f"-m matrix.connector.main run --config {q(config)} --log {q(log)}"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Matrix connector (matrix-connect)</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
      <Repetition><Interval>PT5M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(str(launcher))}</Command><Arguments>{args}</Arguments><WorkingDirectory>{escape(str(workdir))}</WorkingDirectory></Exec>
  </Actions>
</Task>
"""


def windows_log(home: Path) -> Path:
    return _windows_dir(home) / "matrix-connect.log"


def _windows_dir(home: Path) -> Path:
    base = os.environ.get("LOCALAPPDATA") if home == Path.home() else None
    return Path(base or home / "AppData" / "Local") / "matrix-connect"


def unit_path(system: str | None = None, home: Path | None = None) -> Path:
    system, home = system or platform.system(), Path(home or Path.home())
    if system == "Darwin":
        return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if system == "Linux":
        return home / ".config" / "systemd" / "user" / UNIT
    if system == "Windows":
        return _windows_dir(home) / "task.xml"
    raise ServiceError(f"{system} 上还不支持后台服务；请前台运行 matrix-connect run")


def install(exe: str, config: Path, *, system: str | None = None, home: Path | None = None,
            run: Run = _run, env_path: str | None = None, python: str | None = None,
            started: Callable[[Path, float], bool] = _started) -> Path:
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
        path.write_text(launchd_plist(exe, config, env_path, log), encoding="utf-8")
        run(["launchctl", "bootstrap", domain, str(path)])
    elif system == "Windows":
        log = windows_log(home)
        xml = windows_task_xml(windows_launcher(exe, python), Path(config), log, windows_user(), home)
        path.write_text(xml, encoding="utf-16")  # schtasks /XML 要 UTF-16
        try:  # 已经装过：先停掉在跑的那个，再按新定义注册
            run(["schtasks", "/End", "/TN", TASK])
        except ServiceError:
            pass
        run(["schtasks", "/Create", "/TN", TASK, "/XML", str(path), "/F"])
        since = time.time() - 1
        run(["schtasks", "/Run", "/TN", TASK])
        # 任务计划程序不报告进程起没起来。常见的坑：在打包应用（MSIX，如桌面应用内置的终端）里运行时，
        # AppData 的写入被重定向进包的私有目录，任务看不到这里装的 Python，会一直挂着、也没有日志
        if not started(log, since):
            uninstall(system=system, home=home, run=run)
            raise ServiceError(f"任务没能启动连接器（{log} 没有写入）。请在普通的 PowerShell 窗口里重新运行"
                               " matrix-connect setup，或前台运行 matrix-connect run 查看报错")
    else:
        path.write_text(systemd_unit(exe, config, env_path), encoding="utf-8")
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
        elif system == "Windows":
            run(["schtasks", "/End", "/TN", TASK])
        else:
            run(["systemctl", "--user", "disable", "--now", UNIT])
    except ServiceError:
        pass  # 没在运行：照样删掉定义
    if system == "Windows":
        run(["schtasks", "/Delete", "/TN", TASK, "/F"])
    path.unlink()
    if system == "Linux":
        try:
            run(["systemctl", "--user", "daemon-reload"])
        except ServiceError:
            pass
    return path


def runs_from(exe: str, *, system: str | None = None, home: Path | None = None) -> bool | None:
    """装着的服务是不是跑这份安装（exe 所在目录）。没装返回 None。升级换了安装位置后（如包改名后
    uv tool 装进了新目录）旧服务还在跑旧的那份，`matrix-connect install` 重新装一次即可。"""
    system = system or platform.system()
    path = unit_path(system, home)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-16" if system == "Windows" else "utf-8", errors="replace")
    where = str((PureWindowsPath if system == "Windows" else PurePosixPath)(exe).parent)  # 按服务所在系统的路径写法
    return where in text or escape(where) in text


def log_hint(system: str | None = None) -> str:
    system = system or platform.system()
    if system == "Darwin":
        return f"日志：{Path.home() / 'Library/Logs/matrix-connect.log'}"
    if system == "Windows":
        return f"日志：{windows_log(Path.home())}"
    return f"日志：journalctl --user -u {UNIT} -f"
