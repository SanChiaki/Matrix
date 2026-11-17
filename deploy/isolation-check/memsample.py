"""跑一条命令，每 0.2 秒采样它的整棵进程树，按类别记峰值常驻内存（RSS）。控制面（matrix-dsh-check 进程本身）单独记。
用法：python memsample.py -- <命令...>；最后打出 MEM_* 开头的几行。"""
import os
import subprocess
import sys
import time
from collections import defaultdict


def children_map() -> dict[int, list[int]]:
    m: dict[int, list[int]] = defaultdict(list)
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
                    m[int(f.read().rsplit(")", 1)[1].split()[1])].append(int(pid))
            except (OSError, IndexError, ValueError):
                pass
    return m


def info(pid: int) -> tuple[str, int, str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            rss = next((int(line.split()[1]) for line in f if line.startswith("VmRSS:")), 0)
        try:
            exe = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
        except OSError:
            exe = ""
        return cmd, rss, exe
    except OSError:
        return "", 0, ""


def kind(cmd: str, exe: str) -> str:
    if exe == "node":
        return "dsh"
    if "matrix-dsh-check" in cmd:
        return "control"
    if "mcp_server.py" in cmd or "mcp_connectors.py" in cmd or "web_fetch.py" in cmd:
        return "mcp"
    return "other"


proc = subprocess.Popen(sys.argv[sys.argv.index("--") + 1:])
peak_total, peak_snapshot, peak_ctl, peak_dsh = 0, {}, 0, 0
while proc.poll() is None:
    m = children_map()
    todo, tree = list(m[proc.pid]), []
    while todo:
        p = todo.pop()
        tree.append(p)
        todo += m[p]
    by: dict[str, int] = defaultdict(int)
    ndsh = 0
    for p in tree:
        cmd, rss, exe = info(p)
        k = kind(cmd, exe)
        by[k] += rss
        ndsh += k == "dsh"
    peak_ctl = max(peak_ctl, by.pop("control", 0), info(proc.pid)[1])  # 根进程就是检查程序（控制面）
    peak_dsh = max(peak_dsh, ndsh)
    total = sum(by.values())
    if total > peak_total:
        peak_total, peak_snapshot = total, dict(by)
    time.sleep(0.2)
print(f"MEM_EXEC_PEAK_MB={peak_total // 1024}（容器里各进程 RSS 相加，共享页会重复算；以宿主看到的 HOST_PEAK_MEM 为准） " + " ".join(f"{k}={v // 1024}MB" for k, v in sorted(peak_snapshot.items())))
print(f"MEM_CONTROL_PEAK_MB={peak_ctl // 1024}")
print(f"MEM_MAX_CONCURRENT_DSH={peak_dsh}")
sys.exit(proc.returncode)
