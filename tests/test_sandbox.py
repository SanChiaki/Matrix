"""插件沙箱的命令拼装（matrix/sandbox.py）：不依赖 bwrap，三端可跑。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from matrix import sandbox


@pytest.fixture
def fake_bwrap(monkeypatch):
    monkeypatch.setattr(sandbox, "_bwrap", lambda: "/usr/bin/bwrap")


def _symlinks(cmd: list[str]) -> list[tuple[str, str]]:
    return [(cmd[i + 1], cmd[i + 2]) for i, a in enumerate(cmd) if a == "--symlink"]


def test_links_in_system_dirs_are_not_recreated(fake_bwrap, monkeypatch, tmp_path):
    # 部署常见的链：.venv/bin/python3 → /usr/local/bin/python3 → python3.13。系统目录整块只读挂进来了，
    # 里面的链接原样就在，再 --symlink 旧版 bwrap 会报 “Can't make symlink ... File exists”，探测失败。
    hops = [("/opt/app/.venv/bin/python3", "/opt/venv-link/python3"),
            ("python3.13", "/usr/local/bin/python3"), ("python3.10", "/usr/bin/python3"), ("usr/bin", "/bin")]
    monkeypatch.setattr(sandbox, "_symlink_hops", lambda path: hops)
    cmd = sandbox.wrap(["/opt/venv-link/python3", "-c", "1"], ro=[], rw=[tmp_path])
    links = [(t, l) for t, l in _symlinks(cmd) if l not in sandbox._SYSTEM_DIRS]  # 系统目录本身是链接时照旧重建
    assert links == [("/opt/app/.venv/bin/python3", "/opt/venv-link/python3")]


def test_links_in_mounted_dirs_are_not_recreated(fake_bwrap, monkeypatch, tmp_path):
    venv = tmp_path / "venv"
    venv.mkdir()
    monkeypatch.setattr(sandbox, "_symlink_hops", lambda path: [("/opt/py/bin/python3", str(venv) + "/bin/python3")])
    cmd = sandbox.wrap([str(venv) + "/bin/python3"], ro=[venv], rw=[tmp_path / "work"])
    assert ["--ro-bind", str(venv), str(venv)] == cmd[cmd.index(str(venv)) - 1:cmd.index(str(venv)) + 2]
    assert not [l for _, l in _symlinks(cmd) if l.startswith(str(venv))]


def test_other_links_are_recreated(fake_bwrap, monkeypatch, tmp_path):
    hops = [("../py/bin/python3", "/opt/tools/python3"), ("3.14.1", "/opt/uv/python/3.14")]
    monkeypatch.setattr(sandbox, "_symlink_hops", lambda path: hops)
    cmd = sandbox.wrap(["/opt/tools/python3"], ro=[], rw=[tmp_path])
    assert [x for x in _symlinks(cmd) if x[1] not in sandbox._SYSTEM_DIRS] == hops


def test_under_system_dir():
    assert sandbox._under_system_dir("/usr")
    assert sandbox._under_system_dir("/usr/local/bin/python3")
    assert sandbox._under_system_dir("/lib64/ld-linux-x86-64.so.2")
    assert not sandbox._under_system_dir("/usrx/bin/python3")
    assert not sandbox._under_system_dir("/opt/usr/bin/python3")
    assert not sandbox._under_system_dir("/home/me/.local/share/uv/python/bin/python3")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上建符号链接要额外权限")
def test_symlink_hops_follows_chain(tmp_path):
    real = tmp_path / "py" / "bin"
    real.mkdir(parents=True)
    (real / "python3.14").write_text("", encoding="utf-8")
    os.symlink("python3.14", real / "python3")
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    os.symlink(str(real / "python3"), venv / "python3")
    assert sandbox._symlink_hops(str(venv / "python3")) == [(str(real / "python3"), str(venv / "python3")),
                                                            ("python3.14", str(real / "python3"))]
