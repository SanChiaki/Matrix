"""云侧执行面阶段 0（docs/cloud-runtime.md 第 8 节）：dsh 外面的读隔离沙箱、每个执行单元一个 dsh 状态目录、旧会话迁移、
开关与降级。命令行拼装用纯函数测，不依赖真实 dsh 与 bwrap（三端可跑）；真实环境的检查在 matrix-dsh-check。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from matrix import sandbox
from matrix.engine import Network
from matrix.runtime import (DshRuntime, RunRequest, ScriptedRuntime, adopt_dsh_session, dsh_session_dirname,
                            dsh_session_files, install_root, isolation_mode, outer_argv, outer_ro)
from tests.test_connector import fake_bin
from tests.test_users import users

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="外层沙箱只在 Linux 上用（resource、bwrap）")


@pytest.fixture
def fake_bwrap(monkeypatch):
    monkeypatch.setattr(sandbox, "_bwrap", lambda: "/usr/bin/bwrap")


def _binds(cmd: list[str], flag: str) -> list[str]:
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == flag]


# ================= 开关 =================
def test_isolation_mode_parsing(monkeypatch):
    monkeypatch.delenv("MATRIX_CLOUD_ISOLATION", raising=False)
    monkeypatch.delenv("AGENTNET_CLOUD_ISOLATION", raising=False)
    assert isolation_mode() == ("preferred", "")  # 阶段 0 的默认
    monkeypatch.setenv("MATRIX_CLOUD_ISOLATION", "Required")
    assert isolation_mode() == ("required", "")
    assert isolation_mode("off") == ("off", "")
    mode, note = isolation_mode("strict")  # 写错了：按 required，不悄悄放开
    assert mode == "required" and "认不出" in note


# ================= 命令行拼装 =================
def test_install_root_follows_the_real_location(tmp_path: Path):
    pkg = tmp_path / "opt" / "dsh" / "node_modules" / "@deepseek-ai" / "dsh" / "lib"
    pkg.mkdir(parents=True)
    (pkg / "bin.js").write_text("", encoding="utf-8")
    assert install_root(str(pkg / "bin.js")) == (tmp_path / "opt" / "dsh" / "node_modules").resolve()
    node = tmp_path / "node22" / "bin"
    node.mkdir(parents=True)
    (node / "node").write_text("", encoding="utf-8")
    assert install_root(str(node / "node")) == (tmp_path / "node22").resolve()
    if os.name == "posix":
        assert install_root("/usr/bin/node") is None   # 系统目录整块挂着，不另挂
    assert install_root(None) is None


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上建符号链接要额外权限")
def test_install_root_resolves_npm_bin_links(tmp_path: Path):
    """npm 的 node_modules/.bin/dsh 是指向包里脚本的链接：按真实位置找安装目录（本机在 /opt/dsh，镜像里在 /usr 下）。"""
    lib = tmp_path / "dsh" / "node_modules" / "@deepseek-ai" / "dsh" / "lib"
    lib.mkdir(parents=True)
    (lib / "bin.js").write_text("", encoding="utf-8")
    bins = tmp_path / "dsh" / "node_modules" / ".bin"
    bins.mkdir()
    os.symlink("../@deepseek-ai/dsh/lib/bin.js", bins / "dsh")
    assert install_root(str(bins / "dsh")) == (tmp_path / "dsh" / "node_modules").resolve()


def test_outer_ro_has_code_runtimes_and_ca(tmp_path: Path):
    ca = tmp_path / "ca.crt"
    ca.write_text("x", encoding="utf-8")
    ro = outer_ro("/usr/bin/dsh", {"PATH": "", "NODE_EXTRA_CA_CERTS": str(ca), "SSL_CERT_FILE": str(tmp_path / "nope")})
    assert Path(sandbox.__file__).resolve().parent in ro   # Matrix 代码：网络工具的 MCP 服务与 dsh 插件
    assert all(p in ro for p in sandbox.python_dirs())
    assert ca in ro and tmp_path / "nope" not in ro


def test_outer_argv_only_mounts_this_unit(fake_bwrap, monkeypatch, tmp_path: Path):
    """读隔离：可写的只有这个执行单元的工作目录（原来的绝对路径）与它的 dsh 状态目录；共享网络；
    包含数据目录的只读目录不挂（那会把别人的东西带进来）。"""
    monkeypatch.setattr(sandbox, "_is_root", lambda: False)
    data = tmp_path / "data"
    cwd, state = data / "conversations" / "c1" / "a1", data / "dsh-state" / "seats" / "c1" / "a1"
    code = tmp_path / "app" / "matrix"
    code.mkdir(parents=True)
    cmd = outer_argv(["/opt/dsh/bin/dsh", "headless", "--patch", str(cwd / ".dsh-authority.yml")],
                     cwd=cwd, state=state, ro=[code, data, tmp_path])
    assert _binds(cmd, "--bind") == [str(cwd), str(state)]   # 绝对路径不变：dsh 的会话绑死了记录时的工作目录
    assert str(code) in _binds(cmd, "--ro-bind")
    assert str(data) not in _binds(cmd, "--ro-bind") and str(tmp_path) not in _binds(cmd, "--ro-bind")
    assert "--share-net" in cmd and "--unshare-all" in cmd and ["--cap-drop", "ALL"] == cmd[cmd.index("--cap-drop"):][:2]
    assert cmd[cmd.index("--chdir") + 1] == str(cwd)
    assert cmd[cmd.index("--") + 1:] == ["/opt/dsh/bin/dsh", "headless", "--patch", str(cwd / ".dsh-authority.yml")]


def test_nested_wrap_as_root_uses_its_own_user_namespace(fake_bwrap, monkeypatch, tmp_path: Path):
    """以 root 运行时去掉全部特权，内层的 bwrap 就建不了命名空间：改成显式的用户命名空间，特权只在里面有效。"""
    monkeypatch.setattr(sandbox, "_is_root", lambda: True)
    cmd = sandbox.wrap(["/bin/true"], ro=[], rw=[tmp_path], network=True, nested=True)
    assert "--unshare-user" in cmd and "--cap-drop" not in cmd
    cmd = sandbox.wrap(["/bin/true"], ro=[], rw=[tmp_path], network=True)   # 插件服务的沙箱不变
    assert "--unshare-user" not in cmd and "--cap-drop" in cmd


# ================= DshRuntime：HOME、外层沙箱、降级 =================
RECORDER = ("import os, pathlib\n"
            "pathlib.Path(os.environ['RECORD']).write_text(json.dumps({'argv': sys.argv[1:], 'home': os.environ.get('HOME'),"
            " 'tmp': os.environ.get('TMPDIR')}), encoding='utf-8')\n"
            "print(json.dumps({'type': 'final', 'text': 'ok'}))\n")


def _req(tmp_path: Path, state: Path | None) -> RunRequest:
    return RunRequest(agent_id="a", context_id="c", prompt="hi", cwd=tmp_path / "work", authority=["file.write"],
                      state_dir=state, env={"RECORD": str(tmp_path / "record.json")})


@linux_only
def test_hosted_run_is_wrapped_with_its_own_home(monkeypatch, tmp_path: Path):
    """外层可用时：bwrap 包住 dsh，HOME 是执行单元的状态目录，TMPDIR 指向外层里可写的 /tmp（否则 dsh 起不了 bash），
    覆盖层在工作目录里（读得到）。"""
    bwrap = fake_bin(tmp_path, "bwrap", RECORDER)
    monkeypatch.setattr(sandbox, "_bwrap", lambda: bwrap)
    monkeypatch.setattr(sandbox, "available", lambda kind="plugin": (True, "bubblewrap"))
    state = tmp_path / "state"
    rt = DshRuntime(fake_bin(tmp_path, "dsh", "print(json.dumps({'type':'final','text':'x'}))\n"), isolation="preferred")
    res = rt.run(_req(tmp_path, state))
    assert res.ok, res.error
    rec = json.loads((tmp_path / "record.json").read_text(encoding="utf-8"))
    argv = rec["argv"]
    assert rec["home"] == str(state) and rec["tmp"] == "/tmp" and state.is_dir()
    assert _binds([bwrap, *argv], "--bind") == [str((tmp_path / "work").resolve()), str(state.resolve())]
    patch = argv[argv.index("--patch") + 1]
    assert Path(patch).parent == tmp_path / "work"   # 覆盖层在工作目录里，外层里读得到
    caps = rt.capabilities()
    assert caps.isolation == "sandbox" and caps.approval == "sandbox-only"


@linux_only
def test_preferred_without_sandbox_runs_unwrapped_and_says_so(monkeypatch, tmp_path: Path):
    """preferred：外层不可用时照旧跑（本机开发、macOS、Windows），HOME 仍是执行单元自己的；能力报告写明没有读隔离。"""
    monkeypatch.setattr(sandbox, "available", lambda kind="plugin": (False, "没有 bwrap"))
    state = tmp_path / "state"
    rt = DshRuntime(fake_bin(tmp_path, "dsh", RECORDER), isolation="preferred")
    assert rt.run(_req(tmp_path, state)).ok
    rec = json.loads((tmp_path / "record.json").read_text(encoding="utf-8"))
    assert rec["argv"][0] == "headless" and rec["home"] == str(state)
    caps = rt.capabilities()
    assert caps.isolation == "none" and "没有读隔离" in caps.isolation_note and "没有 bwrap" in caps.isolation_note


def test_required_without_sandbox_refuses(monkeypatch, tmp_path: Path):
    """required：没有可用的外层沙箱就拒绝云侧运行，写明原因；dsh 根本不启动。"""
    monkeypatch.setattr(sandbox, "available", lambda kind="plugin": (False, "没有 bwrap"))
    rt = DshRuntime(fake_bin(tmp_path, "dsh", RECORDER), isolation="required")
    res = rt.run(_req(tmp_path, tmp_path / "state"))
    assert not res.ok and res.error.startswith("ISOLATION") and "没有 bwrap" in res.error
    assert res.error_class == "isolation_unavailable" and not res.retryable
    assert not (tmp_path / "record.json").exists()
    assert "拒绝" in rt.capabilities().isolation_note


def test_off_and_device_driver_never_wrap(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sandbox, "available", lambda kind="plugin": (True, "bubblewrap"))
    assert DshRuntime("dsh", isolation="off").isolation()["active"] is False
    device = DshRuntime("dsh", hosted=False)
    assert device.isolation()["active"] is False and device.capabilities().approval == "interactive"


# ================= 旧会话迁移 =================
def _legacy_session(home: Path, cwd: Path, sid: str) -> None:
    d = home / ".dsh" / "sessions" / dsh_session_dirname(cwd) / sid
    d.mkdir(parents=True)
    (d / "index.json").write_text("{}", encoding="utf-8")
    (d / "session.lock").write_text("123", encoding="utf-8")
    idx = home / ".dsh" / "storages" / "session_projcache" / "sessions" / f"{sid}.json"
    idx.parent.mkdir(parents=True)
    idx.write_text('{"cwd": 1}', encoding="utf-8")


def test_session_dirname_matches_dsh():
    assert dsh_session_dirname(Path("/srv/mdata/conversations/c1/a1")) == "--srv-mdata-conversations-c1-a1--"


def test_adopt_copies_both_places_without_lock(tmp_path: Path):
    legacy, home, cwd = tmp_path / "legacy", tmp_path / "unit", Path("/data/conversations/c1/a1")
    _legacy_session(legacy, cwd, "s-1")
    assert adopt_dsh_session(legacy, home, cwd, "s-1")
    d, idx = dsh_session_files(home, cwd, "s-1")
    assert d is not None and (d / "index.json").is_file() and not (d / "session.lock").exists()
    assert idx.read_text(encoding="utf-8") == '{"cwd": 1}'
    assert dsh_session_files(legacy, cwd, "s-1")[0] is not None  # 原处不删：回退时还能用
    assert adopt_dsh_session(legacy, home, cwd, "s-1")             # 已经搬过：直接成功
    assert not adopt_dsh_session(legacy, home, cwd, "s-missing")
    assert not adopt_dsh_session(legacy, home, cwd, "../x")        # 会话 id 只认正常的写法


def _seat_net(tmp_path: Path, handler=lambda call: "ok"):
    net = Network(ScriptedRuntime(handler), tmp_path / "data", seed=False)
    net.legacy_dsh_home = tmp_path / "legacy"
    alice = users(net, "alice@x.io")[0]
    a = net.create_agent("A", "a", [], ["file.write"], actor=alice)
    dm = net.direct_chat(a.id, actor=alice)
    return net, alice, a, dm


def test_cloud_runs_get_their_own_state_dir(tmp_path: Path):
    """不变量 7：每个执行单元一个 dsh 状态目录（这一轮 dsh 的 HOME），在数据目录下、不在任何工作目录里；端侧没有。"""
    net, alice, a, dm = _seat_net(tmp_path)
    net.send_message(dm.id, "hi", background=False, actor=alice)
    req = net.runtime.calls[-1]._req
    seat = net.store.conversations[dm.id].seats[a.id]
    assert req.state_dir == net.store.unit_state_dir("seat", f"{dm.id}/{a.id}") and req.state_dir.is_dir()
    assert req.state_dir.is_relative_to(net.store.root / "dsh-state")
    assert not req.state_dir.is_relative_to(net.store.root / seat.workdir)
    local = net.create_agent("L", "l", [], [], actor=alice)
    rt = ScriptedRuntime(lambda call: "ok")
    local.device_runtime = "dev-1"
    net.register_runtime("dev-1", rt)
    net.send_message(net.direct_chat(local.id, actor=alice).id, "hi", background=False, actor=alice)
    assert rt.calls[-1]._req.state_dir is None


def test_legacy_session_is_moved_in_and_resumed(tmp_path: Path):
    """不变量 7：升级前的席位记着一个会话：第一次在新方式下运行时把它从共用的 ~/.dsh 搬进自己的状态目录，照旧续接。"""
    net, alice, a, dm = _seat_net(tmp_path)
    net.send_message(dm.id, "hi", background=False, actor=alice)
    seat = net.store.conversations[dm.id].seats[a.id]
    sid = seat.dsh_session_id
    state = net.store.unit_state_dir("seat", f"{dm.id}/{a.id}")
    import shutil
    shutil.rmtree(state)                     # 回到升级前：没有状态目录，会话在共用的 ~/.dsh
    _legacy_session(net.legacy_dsh_home, (net.store.root / seat.workdir).resolve(), sid)
    net.send_message(dm.id, "again", background=False, actor=alice)
    call = net.runtime.calls[-1]
    assert call.resumed and seat.dsh_session_id == sid
    assert dsh_session_files(state, (net.store.root / seat.workdir).resolve(), sid)[0] is not None


def test_legacy_session_missing_starts_a_new_one(tmp_path: Path):
    """搬不了（共用 ~/.dsh 里已经没有这个会话）：清掉会话 id，走“没有会话”的流程，新会话的第一轮带上身份与规则。"""
    net, alice, a, dm = _seat_net(tmp_path)
    net.send_message(dm.id, "hi", background=False, actor=alice)
    seat = net.store.conversations[dm.id].seats[a.id]
    import shutil
    shutil.rmtree(net.store.unit_state_dir("seat", f"{dm.id}/{a.id}"))
    net.send_message(dm.id, "again", background=False, actor=alice)
    call = net.runtime.calls[-1]
    assert not call.resumed and "## 工作区边界" in call.prompt
    assert seat.dsh_session_id == call.session_id


def test_legacy_task_session_missing_resends_the_mission(tmp_path: Path):
    """委托：会话搬不了时重发完整委托说明。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path / "data", seed=False)
    net.legacy_dsh_home = tmp_path / "legacy"
    alice = users(net, "alice@x.io")[0]
    lead = net.create_agent("Lead", "l", [], ["delegate"], actor=alice)
    worker = net.create_agent("Worker", "w", [], [], actor=alice)
    g = net.create_project("G", "", [lead.id, worker.id], actor=alice)
    missions: list[str] = []

    def handler(call):
        if call.agent_id == lead.id and call.turn == 1:
            call.tool("delegate", agent="Worker", mission="写一份结论", expected_outcome="结论", authority=[])
        if call.agent_id == worker.id:
            missions.append(call.prompt)
        return "收到"

    net.runtime.handler = handler
    net.send_message(net.project_chat(g.id, actor=alice).id, "@Lead 安排", background=False, actor=alice)
    task = next(t for t in net.store.tasks.values() if t.delegatee_id == worker.id)
    import shutil
    shutil.rmtree(net.store.unit_state_dir("task", task.id))
    assert len(missions) == 1 and task.dsh_session_id
    net._task_round(task)   # 再跑一轮（续接的一轮本来只送通知）
    assert len(missions) == 2 and "你收到一份委托" in missions[-1] and "写一份结论" in missions[-1]


# ================= 看得到：能力报告与管理后台 =================
def test_isolation_is_visible_on_agent_page_and_admin(tmp_path: Path):
    """云侧运行有没有读隔离：Agent 页的能力报告与管理后台都看得到；审批一项是 sandbox-only。"""
    net = Network(DshRuntime(fake_bin(tmp_path, "dsh", "print('{}')\n"), isolation="off"), tmp_path / "data", seed=False)
    alice = users(net, "alice@x.io")[0]
    a = net.create_agent("A", "a", [], ["file.write"], actor=alice)
    caps = net.agent_view(a, alice)["runtime_capabilities"]
    assert caps["isolation"] == "none" and "MATRIX_CLOUD_ISOLATION=off" in caps["isolation_note"]
    assert caps["approval"] == "sandbox-only"
    iso = net.admin_models(alice)["isolation"]
    assert iso["mode"] == "off" and iso["active"] is False
    scripted = Network(ScriptedRuntime(lambda call: "ok"), tmp_path / "data2", seed=False)
    assert scripted.cloud_isolation()["active"] is False
