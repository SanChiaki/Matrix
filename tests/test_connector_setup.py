"""连接器的一机多 harness、配置、设备码配对与后台服务（docs/device-runtimes.md 第 2.4–2.6 节）。"""
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from matrix.connector import config as cfg
from matrix.connector import pairing, service
from matrix.connector.client import Connector
from matrix.connector.drivers.base import RunOutcome
from matrix.connector.main import detect_drivers, main
from matrix.models import HARNESSES
from matrix.server import create_app
from tests.test_devices import build
from tests.test_users import client


class FakeDriver:
    def __init__(self, name, installed=True):
        self.name = name
        self.installed = installed
        self.ran = []

    def capabilities(self):
        from matrix.runtime import RuntimeCapabilities
        return RuntimeCapabilities(name=self.name, placement="device", enforcement={"web.research": "enforced"})

    def available(self):
        return self.installed

    def run(self, spec, ctx, cancel):
        self.ran.append(spec.run_id)
        return RunOutcome(final_text=f"{self.name} done")


def run_msg(run_id, harness=None):
    return {"run_id": run_id, "agent_id": "a", "context_id": f"c-{run_id}", "prompt": "p",
            "workdir_ref": f"a/c-{run_id}", "authority": [], "tool_names": [], "harness": harness}


def wait_for(cond, n=200):
    for _ in range(n):
        if cond():
            return True
        time.sleep(0.01)
    return False


# ================= 一机多 harness =================
def test_connector_routes_each_run_to_its_harness(tmp_path):
    claude, codex = FakeDriver("claude-code"), FakeDriver("codex")
    c = Connector([claude, codex], tmp_path)
    sent = []
    c.send = sent.append
    c._start(run_msg("r1", "codex"))
    c._start(run_msg("r2"))                 # 不指定：默认（第一个）
    c._start(run_msg("r3", "dsh"))          # 本机没有：直接回失败，不落到别的 harness 上
    assert wait_for(lambda: len(sent) == 3)
    by_run = {m["run_id"]: m for m in sent}
    assert by_run["r1"]["final_text"] == "codex done" and by_run["r2"]["final_text"] == "claude-code done"
    assert by_run["r3"]["ok"] is False and "dsh" in by_run["r3"]["error"]
    assert codex.ran == ["r1"] and claude.ran == ["r2"]
    c.close()


def test_hello_lists_every_driver():
    c = Connector([FakeDriver("claude-code"), FakeDriver("codex")], Path("/tmp"))
    msg = c.hello_message()
    assert msg["runtime"] == "claude-code" and [r["runtime"] for r in msg["runtimes"]] == ["claude-code", "codex"]
    from matrix.device import capabilities_from_hello
    assert capabilities_from_hello(msg).harnesses == ["claude-code", "codex"]
    c.close()


def test_detect_drivers_keeps_only_installed(monkeypatch):
    import matrix.connector.main as m
    monkeypatch.setattr(m, "build_driver", lambda name, **kw: FakeDriver(name, installed=name != "dsh"))
    assert [d.name for d in detect_drivers()] == ["claude-code", "codex"]
    assert [d.name for d in detect_drivers(["codex", "dsh"])] == ["codex"]


def test_connector_drivers_are_known_harnesses():
    """连接器的驱动名必须是网络认得的 harness，否则 Agent 绑不上去。"""
    from matrix.connector.main import DRIVERS
    assert set(DRIVERS) <= set(HARNESSES)


# ================= 配置 =================
def test_config_round_trip_is_private(tmp_path):
    path = tmp_path / "conf" / "config.json"
    cfg.save(cfg.Config(server="https://m.example", token="secret", device_id="dev-1", device_name="笔记本"), path)
    if os.name != "nt":  # Windows 没有 POSIX 权限位：配置在用户目录下，由 ACL 限定本人可读
        assert stat.S_IMODE(path.stat().st_mode) == 0o600        # 令牌只有本人可读
    got = cfg.load(path)
    assert got.token == "secret" and got.ws_url == "wss://m.example/api/devices/connect"
    assert cfg.load(tmp_path / "none.json") is None


def test_server_urls():
    assert cfg.normalize_server("127.0.0.1:8765/") == "http://127.0.0.1:8765"
    assert cfg.normalize_server("https://m.example/") == "https://m.example"
    assert cfg.Config(server="http://127.0.0.1:8765", token="t").ws_url == "ws://127.0.0.1:8765/api/devices/connect"
    with pytest.raises(ValueError):
        cfg.normalize_server("ftp://x")


# ================= 设备码配对（真实 API） =================
def test_pair_against_the_server(tmp_path):
    net = build(tmp_path / "srv")
    anon = TestClient(create_app(net))
    alice = client(net, "alice@x.io")
    shown = []

    def post(url, body):
        path = url.removeprefix("http://testserver")
        return anon.post(path, json=body).json()

    def show(p):  # 人看到码后在网页里批准
        shown.append(p)
        threading.Timer(0.05, lambda: alice.post(f"/api/devices/pair/{p['user_code']}/approve", json={})).start()

    conf = pairing.pair("http://testserver", name="笔记本", harnesses=["codex"], post=post, show=show,
                        sleep=lambda s: time.sleep(0.02))
    assert shown and conf.device_name == "笔记本" and conf.email == "alice@x.io"
    assert net.store.devices[conf.device_id].harnesses == ["codex"]
    from matrix import auth
    assert auth.device_of(net.store, conf.token).id == conf.device_id


def test_pair_denied_raises(tmp_path):
    net = build(tmp_path / "srv")
    anon = TestClient(create_app(net))
    alice = client(net, "alice@x.io")
    post = lambda url, body: anon.post(url.removeprefix("http://testserver"), json=body).json()
    show = lambda p: alice.post(f"/api/devices/pair/{p['user_code']}/deny", json={})
    with pytest.raises(pairing.PairingError, match="拒绝"):
        pairing.pair("http://testserver", post=post, show=show, sleep=lambda s: None)


# ================= 后台服务 =================
def test_launchd_plist_runs_the_connector(tmp_path):
    text = service.launchd_plist("/opt/bin/matrix-connect", tmp_path / "config.json", "/usr/bin:/opt/bin",
                                 tmp_path / "log")
    assert "<string>/opt/bin/matrix-connect</string>" in text and "<string>run</string>" in text
    assert "<key>KeepAlive</key>" in text and "/usr/bin:/opt/bin" in text and "secret" not in text


def test_systemd_unit_runs_the_connector(tmp_path):
    text = service.systemd_unit("/opt/bin/matrix-connect", tmp_path / "config.json", "/usr/bin")
    assert "ExecStart=/opt/bin/matrix-connect run --config" in text and "Restart=always" in text
    assert "Environment=PATH=/usr/bin" in text


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="launchd 的 gui/<uid> 域只在 POSIX 上有意义")
def test_install_writes_the_unit_and_loads_it(tmp_path):
    ran = []
    home = tmp_path / "home"
    path = service.install("/opt/bin/matrix-connect", tmp_path / "config.json", system="Darwin", home=home,
                           run=lambda cmd: ran.append(cmd), env_path="/usr/bin")
    assert path == home / "Library/LaunchAgents" / f"{service.LABEL}.plist" and path.exists()
    assert any("bootstrap" in c for c in ran)
    service.uninstall(system="Darwin", home=home, run=lambda cmd: ran.append(cmd))
    assert not path.exists() and any("bootout" in c for c in ran)

    ran.clear()
    unit = service.install("/opt/bin/matrix-connect", tmp_path / "config.json", system="Linux", home=home,
                           run=lambda cmd: ran.append(cmd), env_path="/usr/bin")
    assert unit == home / ".config/systemd/user/matrix-connect.service" and unit.exists()
    assert ["systemctl", "--user", "enable", "--now", "matrix-connect.service"] in ran
    with pytest.raises(service.ServiceError):
        service.install("/x", tmp_path / "c.json", system="Plan9", home=home, run=lambda c: None)


def test_windows_install_registers_a_logon_task(tmp_path):
    """Windows：当前用户的登录任务，用 pythonw 跑（没有控制台窗口），日志写文件，定义里不含令牌。"""
    ran, up = [], lambda log, since: True
    home, scripts = tmp_path / "home", tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    exe = scripts / "matrix-connect.exe"
    exe.write_text("")
    py = str(scripts / "python.exe")  # 运行 setup 的 Python 和 matrix-connect.exe 在同一个 venv
    config = tmp_path / "my config.json"
    cfg.save(cfg.Config(server="http://127.0.0.1:8765", token="s3cret-token"), config)
    with pytest.raises(service.ServiceError):  # 没有 pythonw.exe：不装
        service.install(str(exe), config, system="Windows", home=home, run=ran.append, python=py, started=up)
    (scripts / "pythonw.exe").write_text("")

    path = service.install(str(exe), config, system="Windows", home=home, run=ran.append, python=py, started=up)
    assert path == home / "AppData/Local/matrix-connect/task.xml" and path.exists()
    xml = path.read_text(encoding="utf-16")
    assert xml.startswith('<?xml version="1.0" encoding="UTF-16"?>')
    assert "<LogonTrigger>" in xml and "<MultipleInstancesPolicy>IgnoreNew" in xml
    assert f"<Command>{scripts / 'pythonw.exe'}</Command>" in xml
    assert f'-m matrix.connector.main run --config "{config}"' in xml
    assert f'--log "{home / "AppData/Local/matrix-connect/matrix-connect.log"}"' in xml
    assert "s3cret-token" not in xml
    assert ["schtasks", "/Create", "/TN", service.TASK, "/XML", str(path), "/F"] in ran
    assert ran[-1] == ["schtasks", "/Run", "/TN", service.TASK]

    ran.clear()
    assert service.uninstall(system="Windows", home=home, run=ran.append) == path
    assert not path.exists() and ["schtasks", "/Delete", "/TN", service.TASK, "/F"] in ran
    assert service.uninstall(system="Windows", home=home, run=ran.append) is None

    # 任务注册了却没把连接器跑起来（如在打包应用的终端里装的）：撤销任务并报错，不报“已装好”
    ran.clear()
    with pytest.raises(service.ServiceError, match="没能启动"):
        service.install(str(exe), config, system="Windows", home=home, run=ran.append, python=py,
                        started=lambda log, since: False)
    assert not path.exists() and ran[-1] == ["schtasks", "/Delete", "/TN", service.TASK, "/F"]


def test_windows_launcher_is_the_pythonw_of_the_running_env(tmp_path):
    """uv tool 在 Windows 上把 matrix-connect.exe 复制进 ~/.local/bin，旁边没有 pythonw：
    用运行 setup 的 Python 旁边的 pythonw（它的环境里装着 matrix），不找 PATH 上别的 Python；uvx 的临时环境不用。"""
    bin_exe = tmp_path / ".local" / "bin" / "matrix-connect.exe"
    tool = tmp_path / "AppData" / "Roaming" / "uv" / "tools" / "matrix" / "Scripts"
    for d in (bin_exe.parent, tool):
        d.mkdir(parents=True)
    bin_exe.write_text("")
    (tool / "pythonw.exe").write_text("")
    assert service.windows_launcher(str(bin_exe), str(tool / "python.exe")) == tool / "pythonw.exe"

    with pytest.raises(service.ServiceError, match="pythonw"):  # 两处都没有：不装
        service.windows_launcher(str(bin_exe), str(tmp_path / "elsewhere" / "python.exe"))

    cache = tmp_path / "AppData" / "Local" / "uv" / "cache" / "archive-v0" / "x" / "Scripts"
    cache.mkdir(parents=True)
    (cache / "pythonw.exe").write_text("")
    with pytest.raises(service.ServiceError):
        service.windows_launcher(str(bin_exe), str(cache / "python.exe"))


def test_service_knows_which_install_it_runs(tmp_path):
    """升级换了安装位置（包改名后 uv tool 装进新目录）：能看出装着的服务还在跑旧的那份。"""
    home = tmp_path / "home"
    for system in ("Darwin", "Linux"):
        assert service.runs_from("/new/bin/matrix-connect", system=system, home=home) is None
        service.install("/old/bin/matrix-connect", tmp_path / "config.json", system=system, home=home,
                        run=lambda cmd: None, env_path="/usr/bin")
        assert service.runs_from("/old/bin/matrix-connect", system=system, home=home) is True
        assert service.runs_from("/new/bin/matrix-connect", system=system, home=home) is False


def test_install_reuses_the_existing_pairing(tmp_path, monkeypatch, capsys):
    """`matrix-connect install` 按已有配置重新装后台服务，不重新配对（设备与绑在它上面的 Agent 不变）。"""
    installed = []
    monkeypatch.setattr("matrix.connector.main._install_service", installed.append)
    monkeypatch.setattr(pairing, "pair", lambda *a, **k: pytest.fail("不应重新配对"))
    with pytest.raises(SystemExit) as e:
        main(["install", "--config", str(tmp_path / "none.json")])
    assert e.value.code == 2 and "setup" in capsys.readouterr().err and installed == []
    path = tmp_path / "config.json"
    cfg.save(cfg.Config(server="http://127.0.0.1:8765", token="t", device_id="dev-1"), path)
    main(["install", "--config", str(path)])
    assert installed == [path]


def test_install_waits_for_the_log(tmp_path):
    """确认任务起来了靠连接器启动时写的日志：之前的旧日志不算。"""
    log = tmp_path / "matrix-connect.log"
    log.write_text("旧")
    assert not service._started(log, time.time() + 5, timeout=0.6)
    assert service._started(log, time.time() - 5, timeout=0.6)
    assert not service._started(tmp_path / "none.log", 0, timeout=0.6)


def test_ephemeral_install_is_detected():
    """uvx 的临时环境会被清理：不能把它写进开机自启。"""
    assert service.is_ephemeral("/Users/x/.cache/uv/archive-v0/abc/bin/matrix-connect")
    assert not service.is_ephemeral("/Users/x/.local/bin/matrix-connect")


# ================= 命令行 =================
def test_status_never_prints_the_token(tmp_path, capsys):
    path = tmp_path / "config.json"
    cfg.save(cfg.Config(server="http://127.0.0.1:8765", token="s3cret-token", device_id="dev-1",
                        device_name="笔记本", email="alice@x.io"), path)
    main(["status", "--config", str(path)])
    out = capsys.readouterr().out
    assert "笔记本" in out and "alice@x.io" in out and "s3cret-token" not in out


def test_run_without_config_explains_setup(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        main(["run", "--config", str(tmp_path / "none.json")])
    assert e.value.code == 2 and "setup" in capsys.readouterr().err


def test_run_log_captures_output(tmp_path, monkeypatch):
    """后台服务（Windows 上的 pythonw）没有 stdout / stderr：`--log` 把输出写进文件，太大时滚动一份。"""
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    log = tmp_path / "logs" / "matrix-connect.log"
    log.parent.mkdir()
    log.write_text("旧" * 10)
    monkeypatch.setattr("matrix.connector.main.LOG_MAX_BYTES", 10)
    with pytest.raises(SystemExit):
        main(["run", "--config", str(tmp_path / "none.json"), "--log", str(log)])
    sys.stderr.close()
    assert "启动" in log.read_text(encoding="utf-8") and "setup" in log.read_text(encoding="utf-8")
    assert (tmp_path / "logs" / "matrix-connect.log.1").read_text() == "旧" * 10
