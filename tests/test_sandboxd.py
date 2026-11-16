"""沙箱管理器 matrix-sandboxd（docker 后端，阶段 1b）：不起容器，用假的 docker 执行器。

请求只认执行单元 id、所有者、Agent、代数、配额、启动令牌、逻辑工作目录；任意镜像、挂载、特权、命令一律拒绝。
`docker run` 参数是纯函数，逐项核对安全选项；运行时不在时失败关闭；标签与列举；路径前缀校验；Unix socket 上的接口。"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from matrix.sandboxd import (DockerConfig, Manager, RequestError, Unavailable, init_argv, names, run_argv, serve,
                             validate_ensure)
from matrix.sandboxes import DockerSandboxes, SandboxError, SandboxHost, SandboxHub, SandboxSpec

TOKEN = "t" * 32


def body(**kw):
    b = {"unit_id": "seat:conv-1/agent-1", "owner_id": "user-1", "agent_id": "agent-1", "generation": 3,
         "boot_token": TOKEN, "workdir": "/data/conversations/conv-1/agent-1"}
    b.update(kw)
    return b


@pytest.mark.parametrize("bad", [
    {"image": "evil:latest"}, {"mounts": ["/:/host"]}, {"privileged": True}, {"command": ["sh"]},
    {"network": "host"}, {"runtime": "runc"}, {"env": {"X": "1"}}, {"cap_add": ["SYS_ADMIN"]},
])
def test_requests_with_anything_else_are_refused(bad):
    with pytest.raises(RequestError):
        validate_ensure(body(**bad), DockerConfig())


@pytest.mark.parametrize("wd", [
    "/etc", "/data/state.json", "/data/conversations/", "/data/conversations/../agents/x", "/data/conversations/c/./a",
    "relative/path", "/data/conversations/c,dst=/host", "/data/conversations/c a", "/state", "/tmp/x",
    "/data/conversations/c\n/a", "/data/tasks/t=1",
])
def test_workdir_must_sit_under_allowed_prefix(wd):
    with pytest.raises(RequestError):
        validate_ensure(body(workdir=wd), DockerConfig())


def test_quota_and_ids_are_bounded():
    conf = DockerConfig()
    for bad in ({"quota": {"memory_mb": 10**9}}, {"quota": {"cpus": 99}}, {"quota": {"pids": 0}},
                {"quota": {"swap": 1}}, {"unit_id": "seat:../x"}, {"unit_id": "box:1"}, {"agent_id": "a b"},
                {"boot_token": "short"}, {"generation": "3"}, {"generation": True}):
        with pytest.raises(RequestError):
            validate_ensure(body(**bad), conf)
    got = validate_ensure(body(quota={"memory_mb": 1024}), conf)
    assert got["quota"] == {"cpus": 2.0, "memory_mb": 1024, "pids": 512}


def test_config_refuses_host_network_and_secrets():
    for bad in ({"network": "host"}, {"runtime": "kata"}, {"env": {"DEEPSEEK_API_KEY": "x"}},
                {"workdir_prefixes": ["/"]}, {"workdir_prefixes": ["/data"]}):
        with pytest.raises(ValueError):
            DockerConfig(**bad)


def test_run_argv_security_options_runsc():
    """runsc（默认）：非 root、去掉全部特权、no-new-privileges、只读根、有上限的 /tmp、独立 IPC、自己的网络、配额、
    只有这个单元的两个卷（工作目录挂在原来的绝对路径上）；不放开 seccomp / systempaths，不挂宿主目录与 docker.sock。"""
    conf = DockerConfig()
    req = validate_ensure(body(), conf)
    a = run_argv(req, conf, 100.0)
    s = " ".join(a)
    pairs = list(zip(a, a[1:]))
    assert ("--runtime", "runsc") in pairs and ("--user", "1000:1000") in pairs and ("--cap-drop", "ALL") in pairs
    assert ("--security-opt", "no-new-privileges") in pairs and "--read-only" in a and ("--ipc", "private") in pairs
    assert any(x.startswith("/tmp:") and "size=512m" in x for x in a)
    assert ("--network", "matrix-sandbox") in pairs and "--privileged" not in a and "host" not in a
    assert ("--memory", "2048m") in pairs and ("--memory-swap", "2048m") in pairs and ("--pids-limit", "512") in pairs
    assert ("--cpus", "2") in pairs
    work, state = names("seat:conv-1/agent-1")[1:]
    mounts = [y for x, y in pairs if x == "--mount"]
    assert mounts == [f"type=volume,src={work},dst=/data/conversations/conv-1/agent-1",
                      f"type=volume,src={state},dst=/state"]
    assert "-v" not in a and "bind" not in s and "docker.sock" not in s
    assert "seccomp=unconfined" not in s and "systempaths=unconfined" not in s
    assert ("--label", "dev.matrix.unit=seat:conv-1/agent-1") in pairs and ("--label", "dev.matrix.generation=3") in pairs
    assert a[a.index(conf.image) + 1:a.index(conf.image) + 4] == ["python", "-m", "matrix.connector.sandbox"]
    assert ("--env", f"MATRIX_BOOT_TOKEN={TOKEN}") in pairs


def test_insecure_runc_opens_exactly_two_options():
    """insecure-runc：runc 里内层 bwrap 要放开 seccomp 与 /proc 遮挡，不多不少，其余安全选项照旧。"""
    conf = DockerConfig(runtime="insecure-runc")
    a = run_argv(validate_ensure(body(), conf), conf, 0)
    opts = [y for x, y in zip(a, a[1:]) if x == "--security-opt"]
    assert sorted(opts) == ["no-new-privileges", "seccomp=unconfined", "systempaths=unconfined"]
    assert ("--runtime", "runc") in list(zip(a, a[1:])) and ("--cap-drop", "ALL") in list(zip(a, a[1:]))
    assert not conf.production
    i = init_argv(conf, "v")
    assert ("--cap-add", "CHOWN") in list(zip(i, i[1:])) and ("--network", "none") in list(zip(i, i[1:]))


class FakeDocker:
    """记下每条 docker 命令，按场景回结果。"""

    def __init__(self, runtimes=("runc", "runsc")):
        self.calls: list[list[str]] = []
        self.runtimes = runtimes
        self.containers: dict[str, dict] = {}
        self.volumes: set[str] = set()

    def __call__(self, argv):
        self.calls.append(argv)
        cmd = argv[1:]
        if cmd[:1] == ["info"]:
            return 0, json.dumps({r: {} for r in self.runtimes}), ""
        if cmd[:2] == ["container", "inspect"]:
            c = self.containers.get(cmd[2])
            return (0, json.dumps([c]), "") if c else (1, "", "No such container")
        if cmd[:2] == ["volume", "inspect"]:
            return (0, "[]", "") if cmd[2] in self.volumes else (1, "", "no such volume")
        if cmd[:2] == ["volume", "create"]:
            self.volumes.add(cmd[-1])
            return 0, cmd[-1], ""
        if cmd[:2] == ["volume", "rm"]:
            self.volumes -= set(cmd[3:])
            return 0, "", ""
        if cmd[:1] == ["rm"]:
            return (0, "", "") if self.containers.pop(cmd[-1], None) else (1, "", "No such container")
        if cmd[:2] == ["run", "--rm"]:
            return 0, "", ""
        if cmd[:2] == ["run", "-d"]:
            name = cmd[cmd.index("--name") + 1]
            labels = dict(x.split("=", 1) for x, prev in zip(cmd[1:], cmd) if prev == "--label")
            self.containers[name] = {"Config": {"Labels": labels}, "State": {"Running": True}}
            return 0, "id", ""
        if cmd[:2] == ["volume", "ls"]:
            return 0, "", ""
        if cmd[:2] == ["ps", "-a"]:
            rows = [f"{c['Config']['Labels']['dev.matrix.unit']}\t{'running' if c['State']['Running'] else 'exited'}\t"
                    f"{c['Config']['Labels']['dev.matrix.generation']}\t{c['Config']['Labels']['dev.matrix.started']}"
                    for c in self.containers.values()]
            return 0, "\n".join(rows), ""
        return 1, "", f"unexpected {cmd}"


def test_manager_ensure_is_idempotent_and_labels():
    d = FakeDocker()
    m = Manager(DockerConfig(), run=d, now=lambda: 50.0)
    st = m.ensure(body())
    assert st["state"] == "running" and st["generation"] == 3
    runs = [c for c in d.calls if c[1:3] == ["run", "-d"]]
    assert len(runs) == 1 and len([c for c in d.calls if c[1:3] == ["run", "--rm"]]) == 1  # 新卷要交给沙箱用户
    m.ensure(body())                                 # 同一代数在跑：不重建
    assert len([c for c in d.calls if c[1:3] == ["run", "-d"]]) == 1
    m.ensure(body(generation=4))                     # 新的一代：旧容器先删，卷留着、不再初始化
    assert len([c for c in d.calls if c[1:3] == ["run", "-d"]]) == 2
    assert len([c for c in d.calls if c[1:3] == ["run", "--rm"]]) == 1
    assert m.list() == [{"unit_id": "seat:conv-1/agent-1", "state": "running", "generation": 4, "started_at": 50.0}]
    m.stop("seat:conv-1/agent-1")
    assert m.status("seat:conv-1/agent-1")["state"] == "stopped"     # 卷还在
    m.destroy("seat:conv-1/agent-1")
    assert m.status("seat:conv-1/agent-1")["state"] == "absent"


def test_missing_runtime_fails_closed():
    """配置的运行时不在：拒绝（ISOLATION），不退回 runc，也不起容器。"""
    d = FakeDocker(runtimes=("runc",))
    m = Manager(DockerConfig(), run=d)
    with pytest.raises(Unavailable, match="ISOLATION"):
        m.ensure(body())
    assert not [c for c in d.calls if c[1:2] == ["run"]]
    rep = m.report()
    assert rep["isolation"] == "none" and not rep["production"]
    rep = Manager(DockerConfig(runtime="insecure-runc"), run=FakeDocker(runtimes=("runc",))).report()
    assert rep["isolation"] == "sandbox" and not rep["production"] and rep["note"].startswith("未达到生产隔离")


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket")
def test_unix_socket_api_and_control_plane_client(tmp_path: Path):
    """控制面经 Unix socket 调管理器：合法请求照办，带了镜像的请求被拒（400），运行时不在时是 ISOLATION。"""
    d = FakeDocker()
    sock = str(tmp_path / "sbx.sock")
    srv = serve(Manager(DockerConfig(), run=d), sock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        prov = DockerSandboxes(sock)
        prov.bind(SandboxHost(hub=SandboxHub(), connect_url="", root=tmp_path,
                              workdir_of=lambda u: Path("/data/conversations/conv-1/agent-1")))
        st = prov.ensure(SandboxSpec("seat:conv-1/agent-1", "user-1", "agent-1", 1, TOKEN))
        assert st.state == "running" and prov.status("seat:conv-1/agent-1").generation == 1
        assert [s.unit_id for s in prov.list()] == ["seat:conv-1/agent-1"]
        assert prov.report()["production"]
        with pytest.raises(SandboxError, match="拒绝"):
            prov._call("POST", "/ensure", body(image="evil"))
        bad = DockerSandboxes(sock)
        bad.bind(SandboxHost(hub=SandboxHub(), connect_url="", root=tmp_path, workdir_of=lambda u: Path("/etc")))
        with pytest.raises(SandboxError):
            bad.ensure(SandboxSpec("seat:conv-1/agent-1", "user-1", "agent-1", 2, TOKEN))
        prov.destroy("seat:conv-1/agent-1")
        assert prov.status("seat:conv-1/agent-1").state == "absent"
        d.runtimes = ("runc",)
        srv.RequestHandlerClass.manager._runtime_ok = None
        with pytest.raises(SandboxError, match="ISOLATION"):
            prov.ensure(SandboxSpec("seat:conv-1/agent-1", "user-1", "agent-1", 3, TOKEN))
    finally:
        srv.shutdown()
        srv.server_close()
    assert DockerSandboxes(str(tmp_path / "nope.sock")).report()["isolation"] == "none"
