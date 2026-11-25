"""state.json 写入止血（M2，docs/control-plane.md、方案文档“完整迁移流程”）：fsync、保留上一份、读不了退到上一份、
保存耗时与等锁时间两个指标。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from matrix.engine import Network
from matrix.runtime import ScriptedRuntime
from matrix.server import create_app, prometheus_text
from matrix.store import Store, TimedRLock, write_durable


def test_save_keeps_previous_version_as_bak(tmp_path: Path):
    s = Store(tmp_path, seed=False)
    s.sessions["a"] = {"user_id": "u", "created_at": 1}
    s.save()
    s.sessions["b"] = {"user_id": "u", "created_at": 2}
    s.save()
    now = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    bak = json.loads((tmp_path / "state.json.bak").read_text(encoding="utf-8"))
    assert set(now["sessions"]) == {"a", "b"} and set(bak["sessions"]) == {"a"}
    assert not (tmp_path / "state.json.tmp").exists()


def test_corrupt_state_falls_back_to_bak(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    s = Store(tmp_path, seed=False)
    s.sessions["a"] = {"user_id": "u", "created_at": 1}
    s.save()
    s.save()  # .bak 里也有 a
    (tmp_path / "state.json").write_text('{"agents": [', encoding="utf-8")  # 写到一半断电
    again = Store(tmp_path, seed=False)
    assert again.loaded_from == "state.json.bak"
    assert "读不了" in capsys.readouterr().err
    assert list(tmp_path.glob("state.json.corrupt-*")), "坏掉的那份留给人看，不覆盖"
    again.save()
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["agents"] == []


def test_missing_state_with_bak_loads_bak(tmp_path: Path):
    s = Store(tmp_path, seed=True)
    names = sorted(a.name for a in s.agents.values())
    s.save()
    os.replace(tmp_path / "state.json", tmp_path / "state.json.bak")  # 改名替换到一半被打断
    again = Store(tmp_path, seed=False)
    assert again.loaded_from == "state.json.bak"
    assert sorted(a.name for a in again.agents.values()) == names


def test_corrupt_state_without_bak_refuses_to_start(tmp_path: Path):
    (tmp_path / "state.json").write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="从备份恢复"):
        Store(tmp_path, seed=False)
    assert (tmp_path / "state.json").read_text(encoding="utf-8") == "", "没有可退的就什么都不动"


def test_write_durable_without_hardlinks_still_keeps_bak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    p, bak = tmp_path / "x.json", tmp_path / "x.json.bak"
    write_durable(p, "1", backup=bak)
    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("no links")))
    write_durable(p, "2", backup=bak)
    assert p.read_text(encoding="utf-8") == "2" and bak.read_text(encoding="utf-8") == "1"


_WRITER = """
import sys, time
from pathlib import Path
from matrix.store import Store
s = Store(Path(sys.argv[1]), seed=False)
i = 0
while True:
    i += 1
    for j in range(200):  # 让每次保存有点分量（sandbox_orphans 读回时原样保留，用它当计数）
        s.sandbox_orphans[f"u{i}-{j}"] = float(i)
    s.save()
"""


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL 只在 POSIX 上有")
def test_kill_during_save_leaves_readable_state(tmp_path: Path):
    """压测里反复 kill -9 再启动，状态都能读（M2 的验收）。"""
    script = tmp_path / "writer.py"
    script.write_text(_WRITER, encoding="utf-8")
    data = tmp_path / "data"
    seen = 0
    for i in range(6):
        proc = subprocess.Popen([sys.executable, str(script), str(data)], cwd=Path(__file__).parents[1])
        deadline = time.time() + 20
        while not (data / "state.json.bak").exists() and time.time() < deadline:  # 等它真的开始反复保存
            time.sleep(0.02)
        time.sleep(0.05 + 0.07 * i)
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        s = Store(data, seed=False)  # 不抛异常就是读得了
        assert len(s.sandbox_orphans) >= seen, "确认写下去的不会倒退到更早"
        seen = len(s.sandbox_orphans)
    assert seen > 0


def test_lock_wait_and_save_timings_recorded(tmp_path: Path):
    s = Store(tmp_path, seed=False)
    s.save()
    m = s.metrics()
    assert m["backend"] == "json" and m["save"]["count"] >= 2 and m["bytes"] > 0
    lock = TimedRLock()
    lock.acquire()
    t = threading.Thread(target=lambda: (lock.acquire(), lock.release()))
    t.start()
    time.sleep(0.05)
    lock.release()
    t.join()
    w = lock.waits.snapshot()
    assert w["count"] == 1 and w["max_s"] >= 0.04
    with lock:  # 可重入，没有争用不计时
        with lock:
            pass
    assert lock.waits.snapshot()["count"] == 1


def test_prometheus_text_shape(tmp_path: Path):
    text = prometheus_text(Store(tmp_path, seed=False).metrics())
    assert 'matrix_store_save_seconds{quantile="0.99"}' in text and "matrix_lock_wait_seconds_count" in text


def test_metrics_endpoint_only_for_admin_or_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    net = Network(ScriptedRuntime(lambda call: "好的"), tmp_path, seed=False)
    c = TestClient(create_app(net))
    assert c.get("/metrics").status_code == 404
    assert c.post("/api/auth/register", json={"email": "a@example.com", "password": "password-123"}).status_code == 200
    assert c.get("/metrics").status_code == 200, "最早注册的人是管理员"
    assert c.get("/api/admin/storage").json()["save"]["count"] >= 1
    other = TestClient(create_app(net))
    assert other.post("/api/auth/register", json={"email": "b@example.com", "password": "password-123"}).status_code == 200
    assert other.get("/metrics").status_code == 404 and other.get("/api/admin/storage").status_code == 404
    anon = TestClient(create_app(net))
    monkeypatch.setenv("MATRIX_METRICS_TOKEN", "scrape-secret")
    assert anon.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 404
    r = anon.get("/metrics", headers={"Authorization": "Bearer scrape-secret"})
    assert r.status_code == 200 and "matrix_store_bytes" in r.text
