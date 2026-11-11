"""重活：在容器里以 matrix 用户跑一组典型的 Agent 重负载，记每一步耗时（run.sh 在 runc 与 runsc 下各跑一遍对比）。
每步打一行 HEAVY <步骤> <ok|FAIL> <秒>；失败的步骤把输出末尾打在后面。要联网（git、PyPI、npm）。"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

W = Path(tempfile.mkdtemp(prefix="heavy-"))
REPO = os.environ.get("HEAVY_REPO", "https://github.com/SanChiaki/Matrix.git")


def step(name: str, fn) -> None:
    t = time.monotonic()
    try:
        fn()
        status, tail = "ok", ""
    except subprocess.CalledProcessError as e:
        status, tail = "FAIL", ((e.stdout or "") + (e.stderr or ""))[-400:].replace("\n", " | ")
    except Exception as e:  # noqa: BLE001
        status, tail = "FAIL", repr(e)[:400]
    print(f"HEAVY {name:<14} {status:<4} {time.monotonic() - t:7.1f}s {tail}", flush=True)


def sh(*cmd: str, cwd: Path = W, timeout: int = 900) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                   timeout=timeout)


def small_files() -> None:
    d = W / "small"
    d.mkdir()
    for i in range(20000):
        (d / f"f{i:05d}.txt").write_bytes(b"x" * 1024)
    total = sum(len(p.read_bytes()) for p in d.iterdir())
    assert total == 20000 * 1024
    shutil.rmtree(d)


def big_file() -> None:
    p = W / "big.bin"
    block = os.urandom(1 << 20)
    with open(p, "wb") as f:
        for _ in range(512):
            f.write(block)
        f.flush()
        os.fsync(f.fileno())
    with open(p, "rb") as f:
        while f.read(1 << 20):
            pass
    p.unlink()


def npm_install() -> None:
    d = W / "npmproj"
    d.mkdir()
    (d / "package.json").write_text('{"name": "heavy", "version": "1.0.0", "private": true}', encoding="utf-8")
    sh("npm", "install", "--no-audit", "--no-fund", "typescript", "vite", "eslint", cwd=d)


def pip_install() -> None:
    sh("uv", "venv", str(W / "venv"))
    sh("uv", "pip", "install", "--python", str(W / "venv" / "bin" / "python"), "numpy", "pandas", "requests")


step("small-files", small_files)    # 2 万个小文件：写、读、删（系统调用密集）
step("big-file", big_file)          # 512 MB 顺序写（带 fsync）再读
step("git-clone", lambda: sh("git", "clone", "--depth", "1", REPO, str(W / "repo")))
step("pip-install", pip_install)    # 建虚拟环境、装 numpy pandas requests
step("npm-install", npm_install)    # 装 typescript vite eslint
step("uv-sync", lambda: sh("uv", "sync", "--frozen", cwd=W / "repo"))
step("pytest", lambda: sh("uv", "run", "--frozen", "pytest", "-q", "-p", "no:cacheprovider",
                          "tests/test_network.py", "tests/test_flow.py", cwd=W / "repo"))
step("tsc", lambda: sh("npx", "--no-install", "tsc", "--version", cwd=W / "npmproj"))
shutil.rmtree(W, ignore_errors=True)
sys.exit(0)
