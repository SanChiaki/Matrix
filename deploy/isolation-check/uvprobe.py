"""把 `uv sync` 拆开计时：慢在下载，还是慢在往虚拟环境里放文件；放法（UV_LINK_MODE）与位置（容器根文件系统 / 挂进来的卷）各差多少。
在容器里以 matrix 用户运行，/kit 里要有 pyproject.toml 与 uv.lock，/vol 是挂进来的卷。每行：
  UV <项目位置>/<缓存位置> <放法> <cold|warm> <ok|FAIL> <秒>
cold = 缓存是空的（下载 + 放文件），warm = 缓存已有、离线（只放文件）。"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLACES = {"root": Path(tempfile.mkdtemp(prefix="uvp-")), "vol": Path("/vol")}
if not os.access("/vol", os.W_OK):
    del PLACES["vol"]


def sync(proj: Path, cache: Path, mode: str | None, offline: bool) -> tuple[str, float]:
    shutil.rmtree(proj / ".venv", ignore_errors=True)
    env = dict(os.environ, UV_CACHE_DIR=str(cache))
    env.pop("UV_LINK_MODE", None)
    if mode:
        env["UV_LINK_MODE"] = mode
    cmd = ["uv", "sync", "--frozen", "--no-install-project", *(["--offline"] if offline else [])]
    t = time.monotonic()
    r = subprocess.run(cmd, cwd=proj, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    return ("ok" if r.returncode == 0 else "FAIL " + r.stderr.strip().splitlines()[-1][:120] if r.stderr.strip() else "FAIL"), \
        time.monotonic() - t


def project(base: Path, name: str) -> Path:
    p = base / name
    shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True)
    for f in ("pyproject.toml", "uv.lock"):
        shutil.copy(f"/kit/{f}", p / f)
    return p


for where, cwhere in (("root", "root"), ("vol", "vol"), ("vol", "root"), ("root", "vol")):
    if where not in PLACES or cwhere not in PLACES:
        continue
    proj, cache = project(PLACES[where], f"proj-{where}-{cwhere}"), PLACES[cwhere] / f"cache-{where}-{cwhere}"
    shutil.rmtree(cache, ignore_errors=True)
    status, secs = sync(proj, cache, None, offline=False)
    print(f"UV {where}/{cwhere} default cold {status} {secs:.2f}", flush=True)
    if not status.startswith("ok"):
        continue
    for mode in (None, "copy", "hardlink", "symlink"):
        status, secs = sync(proj, cache, mode, offline=True)
        print(f"UV {where}/{cwhere} {mode or 'default'} warm {status} {secs:.2f}", flush=True)
    files = sum(1 for _ in (proj / ".venv").rglob("*"))
    print(f"UV {where}/{cwhere} venv_files={files}", flush=True)
    shutil.rmtree(proj, ignore_errors=True)
    shutil.rmtree(cache, ignore_errors=True)
sys.exit(0)
