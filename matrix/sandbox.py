"""插件服务的沙箱（docs/plugin-install.md 第 4 节，不变量 24）。

主持人上传的托管插件（代码没经部署方审核）只在沙箱里跑；部署方目录里的托管插件有沙箱时也进（部署方可以关掉）。
第一期只有 Linux 的 bubblewrap（`bwrap`）：
- 新的挂载、进程、IPC、主机名命名空间；默认也是新的网络命名空间（不能联网），只有部署方目录里声明了 `network` 的插件共享网络。
- 文件系统只有：系统目录（/usr 等，只读）、Python 解释器与依赖环境（只读）、插件包（只读）、这个安装自己的数据目录（读写）、
  一个空的 /tmp。Matrix 的数据目录（别的项目、别人的私有目录、保管库密钥）在沙箱里不存在。
- 资源上限（setrlimit）：地址空间、打开文件数、单个文件大小、CPU 时间；不生成 core。
没有 bwrap、或者 bwrap 在这台机器上跑不起来（比如容器禁了用户命名空间）时 `available()` 为假：主持人上传的托管插件拒绝安装，
目录里的照旧由网络直接托管（部署方审核过）。`MATRIX_PLUGIN_SANDBOX=off` 关掉。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import compat

MEMORY = 4 << 30          # 地址空间上限（字节）
OPEN_FILES = 256
FILE_SIZE = 256 << 20     # 单个文件最大
CPU_SECONDS = 3600        # 一个进程一生最多用多少 CPU 秒：用完被系统停掉，下次调用时网络重新拉起
_SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64")
_ETC = ("/etc/ld.so.cache", "/etc/localtime", "/etc/nsswitch.conf", "/etc/passwd", "/etc/group")
_ETC_NET = ("/etc/resolv.conf", "/etc/hosts", "/etc/ssl", "/etc/ca-certificates", "/etc/pki")

_lock = threading.Lock()
_probe: tuple[bool, str] | None = None


def _bwrap() -> str | None:
    return shutil.which("bwrap") if sys.platform.startswith("linux") else None


def python_dirs(exe: str | None = None) -> list[Path]:
    """运行 Python 要读的目录：虚拟环境、它背后的解释器安装。"""
    exe = exe or sys.executable
    real = Path(os.path.realpath(exe))
    dirs = {Path(sys.prefix), Path(sys.base_prefix), real.parent.parent}
    return sorted(d for d in dirs if d.is_dir() and not any(str(d).startswith(s + "/") or str(d) == s
                                                             for s in _SYSTEM_DIRS))


def _symlink_hops(path: str) -> list[tuple[str, str]]:
    """解释器常是一串符号链接（虚拟环境 → uv 的“3.14”目录 → 具体版本）：沿途每个是链接的路径前缀都要在沙箱里重建，
    返回 [(链接内容, 链接路径)]。"""
    out: list[tuple[str, str]] = []
    todo, seen = [path], set()
    while todo:
        p = todo.pop()
        if p in seen or len(seen) > 40:
            continue
        seen.add(p)
        parts = Path(p).parts
        for i in range(2, len(parts) + 1):
            prefix = str(Path(*parts[:i]))
            if os.path.islink(prefix):
                target = os.readlink(prefix)
                out.append((target, prefix))
                todo.append(str(Path(prefix).parent / target) + "/" + "/".join(parts[i:]) if i < len(parts)
                            else str(Path(prefix).parent / target))
                break
    return list(dict.fromkeys(out))


def wrap(argv: list[str], *, ro: list[Path], rw: list[Path], network: bool = False) -> list[str]:
    """把一条命令包进 bwrap。rw[0] 是工作目录。"""
    exe = _bwrap()
    assert exe is not None
    cmd = [exe, "--die-with-parent", "--new-session", "--unshare-all", "--cap-drop", "ALL"]
    if network:
        cmd.append("--share-net")
    cmd += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for d in _SYSTEM_DIRS:
        p = Path(d)
        if p.is_symlink():
            cmd += ["--symlink", os.readlink(p), d]
        elif p.is_dir():
            cmd += ["--ro-bind", d, d]
    for f in (*_ETC, *(_ETC_NET if network else ())):
        if os.path.exists(f):
            cmd += ["--ro-bind", f, f]
    seen: set[str] = set()
    for p in ro:
        if p.exists() and str(p) not in seen:
            seen.add(str(p))
            cmd += ["--ro-bind", str(p), str(p)]
    for target, link in _symlink_hops(argv[0]):
        if not any(link == str(p) or link.startswith(str(p) + "/") for p in ro):  # 已经整个挂进来的目录里的链接不用重建
            cmd += ["--symlink", target, link]
    for p in rw:
        p.mkdir(parents=True, exist_ok=True)
        cmd += ["--bind", str(p), str(p)]
    cmd += ["--chdir", str(rw[0]), "--", *argv]
    return cmd


def limits() -> None:
    """在子进程里、exec 之前调（preexec_fn）：资源上限，bwrap 和它启动的程序都继承。"""
    import resource
    for res, value in ((resource.RLIMIT_AS, MEMORY), (resource.RLIMIT_NOFILE, OPEN_FILES),
                       (resource.RLIMIT_FSIZE, FILE_SIZE), (resource.RLIMIT_CPU, CPU_SECONDS),
                       (resource.RLIMIT_CORE, 0)):
        try:
            _, hard = resource.getrlimit(res)
            resource.setrlimit(res, (value if hard == resource.RLIM_INFINITY else min(value, hard), hard))
        except (ValueError, OSError):
            pass


def available() -> tuple[bool, str]:
    """这台机器上有没有能用的沙箱：（能不能用, 说明）。第一次调用时真跑一次探测，之后用缓存。"""
    global _probe
    if (compat.env("PLUGIN_SANDBOX") or "").lower() in ("off", "0", "false", "no"):
        return False, "部署方关掉了沙箱（MATRIX_PLUGIN_SANDBOX=off）"
    with _lock:
        if _probe is not None:
            return _probe
        exe = _bwrap()
        if exe is None:
            _probe = (False, "这台机器上没有 bubblewrap（bwrap）：目前只支持 Linux" if sys.platform.startswith("linux")
                      else "沙箱目前只支持 Linux（bubblewrap）")
            return _probe
        import tempfile
        with tempfile.TemporaryDirectory(prefix="matrix-sandbox-probe-") as tmp:
            try:
                r = subprocess.run(wrap([sys.executable, "-c", "print('ok')"], ro=python_dirs(), rw=[Path(tmp)]),
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
                ok = r.returncode == 0 and r.stdout.strip() == "ok"
                why = "" if ok else (r.stderr.strip().splitlines() or [f"退出码 {r.returncode}"])[-1]
            except (OSError, subprocess.TimeoutExpired) as e:
                ok, why = False, str(e)
        _probe = (True, "bubblewrap") if ok else (False, f"bubblewrap 在这台机器上跑不起来：{why}")
        return _probe


def reset_probe() -> None:
    """测试用：清掉探测缓存。"""
    global _probe
    with _lock:
        _probe = None
