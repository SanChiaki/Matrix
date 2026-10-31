"""主持人自己装插件：沙箱托管、按地址装远程服务、部署方策略、后台目录、上架申请、Agent 提议（docs/plugin-install.md）。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import socket
import subprocess
import threading
import zipfile
from pathlib import Path

import pytest

from matrix import plugin_sources as sources, plugins as pluginfs, sandbox
from matrix.engine import NeedsConfirm, Network, NetworkError
from matrix.models import MCP_PREFIX
from matrix.plugin_check import load
from matrix.plugin_service import RemoteService, ServiceError, check_requirements
from matrix.runtime import ScriptCall, ScriptedRuntime
from matrix.scripted import scenario_handler
from tests.test_plugin_services import MATERIALS, WIKI, _ctx, call
from tests.test_users import users

ROOT = Path(__file__).resolve().parents[1]
TEAM = ROOT / "examples" / "plugins" / "teammates"
needs_sandbox = pytest.mark.skipif(not sandbox.available()[0], reason="这台机器上没有可用的 bubblewrap 沙箱")

PROBE = """import json, os, socket, sys
for line in sys.stdin:
    m = json.loads(line)
    if m.get("id") is None:
        continue
    r = {}
    if m["method"] == "tools/list":
        r = {"tools": [{"name": "probe", "inputSchema": {"type": "object"}}]}
    if m["method"] == "tools/call":
        a, out = m["params"]["arguments"], {}
        try:
            open(a["secret"]).read()
            out["read"] = "yes"
        except OSError as e:
            out["read"] = type(e).__name__
        try:
            socket.create_connection(("127.0.0.1", int(a["port"])), timeout=2).close()
            out["net"] = "yes"
        except OSError as e:
            out["net"] = type(e).__name__
        with open("note.txt", "w") as f:
            f.write("x")
        out["write"] = os.path.exists("note.txt")
        try:
            import mx_probe_dep
            out["dep"] = mx_probe_dep.VALUE
        except ImportError:
            out["dep"] = None
        r = {"content": [{"type": "text", "text": json.dumps(out)}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
"""


def probe_package(**service) -> list[tuple[str, bytes]]:
    m = {"name": "probe", "version": "1.0.0", "title": "探针", "description": "沙箱测试",
         "service": {"command": "python", "args": ["{plugin}/service/probe.py"], "tools": {"probe": "read"}, **service}}
    return [("probe/plugin.json", json.dumps(m).encode()), ("probe/service/probe.py", PROBE.encode())]


def make_net(tmp_path: Path, handler=scenario_handler, catalog: Path | None = None):
    net = Network(ScriptedRuntime(handler), tmp_path / "data", seed=False,
                  plugins=pluginfs.PluginCatalog(catalog) if catalog else None)
    host, guest = users(net, "host@x.io", "guest@x.io")
    net.ensure_assistant(host)
    keeper = net.create_agent("知识管家", "维护项目 Wiki", ["整理资料"], ["file.write"], actor=host)
    g = net.create_project("白板定价", "定价调研", [keeper.id], actor=host)
    return net, host, guest, keeper, g


def listener() -> tuple[socket.socket, int]:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s, s.getsockname()[1]


# ================= 托管代码：只进沙箱 =================
def test_hosted_upload_needs_a_sandbox(tmp_path: Path, monkeypatch):
    """不变量 24：主持人上传的托管插件只在沙箱里跑；部署方没有沙箱（或关掉了）时拒绝，告诉他改远程服务或提交上架。"""
    monkeypatch.setenv("MATRIX_PLUGIN_SANDBOX", "off")
    net, host, _, _, g = make_net(tmp_path)
    with pytest.raises(NetworkError, match="没有可用的沙箱.*提交上架"):
        net.upload_plugin(g.id, probe_package(), confirm=True, actor=host)
    assert "probe" not in g.plugins


def test_hosted_upload_policy_and_network_refused(tmp_path: Path):
    """不变量 24：要联网的托管插件、不是 python 的命令、部署方关掉了自己装托管插件，都不能装。"""
    net, host, _, _, g = make_net(tmp_path)
    with pytest.raises(NetworkError, match="不能联网|沙箱"):
        net.upload_plugin(g.id, probe_package(network=True), confirm=True, actor=host)
    net.admin_save_plugin_policy(host, {"hosted_upload": False})
    with pytest.raises(NetworkError, match="不允许主持人自己装带托管服务"):
        net.upload_plugin(g.id, probe_package(), confirm=True, actor=host)


@needs_sandbox
def test_uploaded_plugin_runs_in_sandbox(tmp_path: Path):
    """不变量 24：上传的托管插件先要主持人看过确认卡（代码在沙箱里、不能联网、会交给它什么）；装上后它的进程
    读不到 Matrix 数据目录里的东西、连不上网络，只能写自己的数据目录。"""
    net, host, _, keeper, g = make_net(tmp_path)
    with pytest.raises(NeedsConfirm) as e:
        net.upload_plugin(g.id, probe_package(), actor=host)
    assert any("沙箱" in x and "不能联网" in x for x in e.value.items) and "参数会交给插件代码" in str(e.value)
    with pytest.raises(NeedsConfirm, match="不一样了"):  # 确认的不是这一份
        net.upload_plugin(g.id, probe_package(), confirm=True, actor=host, digest="0" * 64)
    out = net.upload_plugin(g.id, probe_package(), confirm=True, actor=host, digest=e.value.digest)
    assert out["runs"] == "sandbox" and out["source"] == "upload" and "secret" not in out
    secret = net.store.root / "secret.txt"
    secret.write_text("vault", encoding="utf-8")
    srv, port = listener()
    try:
        text, err = call(net, _ctx(net, keeper, g), "probe__probe", secret=str(secret), port=port)
    finally:
        srv.close()
    assert not err, text
    r = json.loads(text)
    assert r["read"] != "yes" and r["net"] != "yes" and r["write"] is True
    assert (net.store.plugin_data_dir(g.id, "probe") / "service" / "note.txt").is_file()
    assert "沙箱里运行，不能联网" in net.project_chat(g.id).messages[-1].text  # 安装记进群聊，所有人看得到


# ================= 远程服务：填地址安装 =================
@pytest.fixture
def wiki_server(tmp_path: Path):
    from importlib import util
    spec = util.spec_from_file_location("wiki_engine", WIKI / "service" / "wiki_engine.py")
    engine = util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    pkg = tmp_path / "author" / "llm-wiki"
    shutil.copytree(WIKI, pkg)
    secrets = tmp_path / "author" / "secrets.json"
    secrets.write_text("{}", encoding="utf-8")
    server = engine.make_http_server(0, tmp_path / "author" / "data", engine.secrets_file(secrets), pkg)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, secrets, pkg
    server.shutdown()


def test_policy_checks_hosts():
    p = sources.InstallPolicy()
    assert sources.check_remote(p, "https://plugins.example.com/x.zip", resolve=False) == "plugins.example.com"
    with pytest.raises(sources.SourceError, match="https"):
        sources.check_remote(p, "http://plugins.example.com/x.zip", resolve=False)
    with pytest.raises(sources.SourceError, match="内网|白名单"):
        sources.check_remote(p, "https://10.1.2.3/x.zip")
    p = sources.parse_policy({"remote": "listed", "allow": ["*.corp.example", "10.0.0.0/8"], "deny": ["bad.corp.example"]})
    assert sources.check_remote(p, "http://wiki.corp.example/x.zip", resolve=False) == "wiki.corp.example"
    assert sources.check_remote(p, "http://10.1.2.3/x.zip") == "10.1.2.3"
    with pytest.raises(sources.SourceError, match="禁止"):
        sources.check_remote(p, "https://bad.corp.example/x.zip", resolve=False)
    with pytest.raises(sources.SourceError, match="不在部署方允许"):
        sources.check_remote(p, "https://plugins.example.com/x.zip", resolve=False)
    with pytest.raises(sources.SourceError, match="关掉"):
        sources.check_remote(sources.InstallPolicy(remote="off"), "https://plugins.example.com/x.zip", resolve=False)
    with pytest.raises(sources.SourceError):
        sources.parse_policy({"remote": "everything"})


def test_remote_plugin_installed_from_url(tmp_path: Path, wiki_server, monkeypatch):
    """不变量 24：主持人填地址装插件作者自己运行的服务。部署方策略不放行时装不上；放行后先看确认卡（数据发到哪个主机、
    发什么），确认后网络生成这个安装自己的签名密钥（只给一次），作者配上后服务按项目切分数据；
    调用者只带成员 id、不带邮箱；重置密钥后旧的立即失效；部署方关掉远程插件后立即停用。"""
    base, secrets, pkg = wiki_server
    net, host, guest, keeper, g = make_net(tmp_path)
    url = base + "/matrix-plugin.zip"
    with pytest.raises(NetworkError, match="内网|https|白名单"):
        net.install_plugin_url(g.id, url, actor=host)
    with pytest.raises(NetworkError):
        net.admin_save_plugin_policy(guest, {"allow": ["127.0.0.1"]})  # 只有管理员能改策略
    net.admin_save_plugin_policy(host, {"allow": ["127.0.0.1"]})
    with pytest.raises(NetworkError, match="unknown project"):
        net.install_plugin_url(g.id, url, actor=guest)
    with pytest.raises(NeedsConfirm) as e:
        net.install_plugin_url(g.id, url, actor=host)
    told = "；".join(e.value.items)
    assert "插件服务在 127.0.0.1" in told and "签名密钥" in told
    out = net.install_plugin_url(g.id, url, confirm=True, actor=host, digest=e.value.digest)
    assert out["runs"] == "remote" and out["source"] == "url" and out["url"] == url and out["has_secret"]
    secret = out["secret"]
    assert secret.startswith("ps_") and "ps_" not in json.dumps(net.project_plugins(g))
    assert "插件服务在 127.0.0.1" in net.project_chat(g.id).messages[-1].text

    chat = net.project_chat(g.id, actor=host)
    for f in sorted(MATERIALS.glob("*.md")):
        net.publish_file(g.id, f.name, f.read_bytes(), actor=host)
    with pytest.raises(NetworkError, match="401|签名|密钥"):  # 作者还没配密钥：服务拒绝
        net.plugin_bridge(g.id, "llm-wiki", "service.call", {"tool": "index"}, actor=host)

    secrets.write_text(json.dumps({g.id: secret}), encoding="utf-8")
    net.services.stop(g.id, "llm-wiki")
    net.send_message(chat.id, "@知识管家 把项目资料收进 wiki", actor=host, background=False)
    assert "已把 2 份资料编进 wiki" in chat.messages[-1].text
    assert (tmp_path / "author" / "data" / g.id / "wiki" / "research" / "market-report.md").is_file()

    seen: list[dict] = []
    real = RemoteService.call
    monkeypatch.setattr(RemoteService, "call", lambda self, n, a, timeout=60, meta=None: (seen.append(meta), real(self, n, a, timeout, meta))[1])
    net.plugin_bridge(g.id, "llm-wiki", "service.call", {"tool": "index"}, actor=host)
    assert seen[-1]["matrix/caller"] == {"kind": "user", "id": host}  # 第三方那里只有成员 id

    assert net.refresh_plugin(g.id, "llm-wiki", actor=host)["unchanged"]
    m = json.loads((pkg / "plugin.json").read_text(encoding="utf-8"))
    m["version"] = "0.3.0"
    (pkg / "plugin.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(NeedsConfirm):
        net.refresh_plugin(g.id, "llm-wiki", actor=host)

    new = net.reset_plugin_secret(g.id, "llm-wiki", actor=host)
    assert new != secret
    with pytest.raises(NetworkError, match="签名不对"):
        net.plugin_bridge(g.id, "llm-wiki", "service.call", {"tool": "index"}, actor=host)
    secrets.write_text(json.dumps({g.id: new}), encoding="utf-8")
    assert net.plugin_bridge(g.id, "llm-wiki", "service.call", {"tool": "index"}, actor=host)

    net.admin_save_plugin_policy(host, {"remote": "off"})
    with pytest.raises(NetworkError, match="关掉了远程插件"):
        net.plugin_bridge(g.id, "llm-wiki", "service.call", {"tool": "index"}, actor=host)
    net.uninstall_plugin(g.id, "llm-wiki", actor=host)
    assert net._plugin_secret(g.id, "llm-wiki") is None  # 卸载连密钥一起删


def test_fetch_package_service_must_live_on_the_same_host(tmp_path: Path):
    """确认卡上写的主机就是数据会去的地方：插件地址和服务地址不在同一个主机上的不装。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    manifest = {"name": "elsewhere", "version": "1.0.0", "description": "x",
                "service": {"url": "https://collector.example.com/mcp", "tools": {"x": "read"}}}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            raw = json.dumps(manifest).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        p = sources.InstallPolicy(allow=("127.0.0.1",))
        with pytest.raises(sources.SourceError, match="同一个主机"):
            sources.fetch_package(p, f"http://127.0.0.1:{srv.server_address[1]}/plugin.json")
    finally:
        srv.shutdown()


# ================= 后台目录与上架申请 =================
def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x.io", "-c", "commit.gpgsign=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="没有 git")
def test_admin_adds_plugins_from_git_without_restart(tmp_path: Path):
    """管理员从 git 仓库把插件加进目录（记提交号与指纹），不用重启；项目里装的是那一版的拷贝，仓库更新后
    管理员重新拉取，主持人在插件卡上看到升级；拿出目录不影响已经装上的。不是管理员按不存在处理。"""
    repo = tmp_path / "repo"
    shutil.copytree(TEAM, repo / "plugins" / "teammates")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    net, host, guest, _, g = make_net(tmp_path)
    assert net.plugin_catalog() == []
    with pytest.raises(NetworkError, match="unknown admin"):
        net.admin_catalog_git(guest, str(repo), "", "plugins/teammates")
    with pytest.raises(NetworkError, match="没有 plugin.json"):
        net.admin_catalog_git(host, str(repo), "", "plugins")
    view = net.admin_catalog_git(host, str(repo), "", "plugins/teammates")
    row = next(x for x in view["catalog"] if x["name"] == "teammates")
    assert row["origin"] == "managed" and len(row["info"]["source"]["commit"]) == 40 and row["info"]["added_by"] == "host@x.io"
    assert [p["name"] for p in net.plugin_catalog()] == ["teammates"]
    out = net.install_plugin(g.id, "teammates", actor=host)  # 目录里的：不出确认卡
    assert out["source"] == "catalog" and out["runs"] in ("sandbox", "host")

    m = json.loads((repo / "plugins" / "teammates" / "plugin.json").read_text(encoding="utf-8"))
    m["version"] = "9.9.9"
    (repo / "plugins" / "teammates" / "plugin.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "bump")
    net.admin_catalog_refresh(host, "teammates")
    assert next(p for p in net.project_plugins(g) if p["name"] == "teammates")["update"] == "9.9.9"
    net.admin_catalog_remove(host, "teammates")
    assert net.plugin_catalog() == [] and "teammates" in g.plugins
    with pytest.raises(sources.SourceError, match="拉取仓库失败"):
        sources.git_package("ext::sh -c touch% /tmp/pwned")  # 只许 https 与本机路径
    assert not Path("/tmp/pwned").exists()


def test_submissions_reviewed_by_admin(tmp_path: Path):
    """主持人把插件包提交上架，管理员看过（文件、体检）后批准就进目录，所有项目都能装；拒绝写理由。
    提交的人只看得到自己的申请。"""
    net, host, guest, _, g = make_net(tmp_path)
    entries = [(f"llm-wiki/{p.as_posix()}", b) for p, b in load(WIKI).items()]
    sub = net.submit_plugin(entries, "我们组的知识库", actor=guest)
    assert sub["status"] == "pending" and sub["name"] == "llm-wiki" and "service/wiki_engine.py" in sub["files"]
    assert [s["id"] for s in net.my_plugin_submissions(guest)] == [sub["id"]] and net.my_plugin_submissions(host) == []
    assert "class Wiki" in net.admin_submission_file(host, sub["id"], "service/wiki_engine.py")
    with pytest.raises(NetworkError):
        net.admin_decide_submission(guest, sub["id"], True)
    view = net.admin_decide_submission(host, sub["id"], True, "看过了")
    assert next(x for x in view["catalog"] if x["name"] == "llm-wiki")["info"]["source"] == \
        {"kind": "submission", "id": sub["id"], "by": "guest@x.io"}
    assert net.my_plugin_submissions(guest)[0]["status"] == "approved"
    net.install_plugin(g.id, "llm-wiki", actor=host)
    with pytest.raises(NetworkError, match="处理过"):
        net.admin_decide_submission(host, sub["id"], False)
    sub2 = net.submit_plugin(probe_package(), "", actor=guest)
    net.admin_decide_submission(host, sub2["id"], False, "要联网的部分说明不清")
    assert net.my_plugin_submissions(guest)[0]["review_note"] == "要联网的部分说明不清"
    for _ in range(sources.PENDING_PER_USER):
        net.submit_plugin(probe_package(), "", actor=guest)
    with pytest.raises(NetworkError, match="待审核"):
        net.submit_plugin(probe_package(), "", actor=guest)


# ================= Agent 提议 =================
def test_agent_proposes_a_plugin_and_host_installs(tmp_path: Path):
    """Agent 只能提议装插件；主持人看过后批准，网络以主持人的身份安装那一份，结果通知提议的 Agent。"""
    prompts: list[str] = []

    def handler(c: ScriptCall) -> str:
        prompts.append(c.prompt)
        if c.prompt.rstrip().endswith("装 alpha"):
            return c.tool("propose_plugin", name="teammates", reason="大家分任务需要一个任务板")
        if c.prompt.rstrip().endswith("装 beta"):
            return c.tool("propose_plugin", name="nope", reason="x")
        return "好的"
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"plugins": [str(TEAM)]}), encoding="utf-8")
    net, host, guest, keeper, g = make_net(tmp_path, handler, catalog)
    chat = net.project_chat(g.id, actor=host)
    net.send_message(chat.id, "@知识管家 装 beta", actor=host, background=False)
    assert "目录里有：teammates" in chat.messages[-1].text
    net.send_message(chat.id, "@知识管家 装 alpha", actor=host, background=False)
    p = next(x for x in net.store.proposals.values() if x.kind == "plugin")
    assert p.status == "pending" and p.owner_id == host and p.plugin["catalog"] and "teammates" not in g.plugins
    with pytest.raises(NetworkError):
        net.approve_proposal(p.id, actor=host)  # 不是新建 Agent
    net.invite(g.id, "guest@x.io", actor=host)
    net.respond_invitation(g.id, True, actor=guest)
    with pytest.raises(NetworkError):
        net.install_proposed_plugin(p.id, actor=guest)  # 只有主持人
    net.install_proposed_plugin(p.id, "装上了", actor=host)
    assert "teammates" in g.plugins and g.plugins["teammates"]["by_user"] == host and p.status == "approved"
    net.wait_quiet(timeout=10)
    assert any("你提议的插件「队友协作（Raft 式）」已装进项目" in x for x in prompts)


def test_http_confirm_carries_items_and_digest(tmp_path: Path):
    """接口：要确认时 409 带一条条要确认的事和包的指纹；带回指纹才装。"""
    from fastapi.testclient import TestClient
    from matrix.server import create_app
    if not sandbox.available()[0]:
        pytest.skip("没有沙箱")
    net, host, _, _, g = make_net(tmp_path)
    c = TestClient(create_app(net))
    assert c.post("/api/auth/login", json={"email": "host@x.io", "password": "password-123"}).status_code == 200
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, b in probe_package():
            z.writestr(path, b)
    r = c.post(f"/api/projects/{g.id}/plugins/upload", content=buf.getvalue(), headers={"Content-Type": "application/zip"})
    assert r.status_code == 409 and r.json()["confirm"] and len(r.json()["digest"]) == 64
    r = c.post(f"/api/projects/{g.id}/plugins/upload?confirm=true&digest={r.json()['digest']}", content=buf.getvalue(),
               headers={"Content-Type": "application/zip"})
    assert r.status_code == 200 and r.json()["runs"] == "sandbox"


# ================= 依赖锁文件 =================
def _wheel(d: Path) -> Path:
    files = {"mx_probe_dep/__init__.py": b"VALUE = 'from-wheel'\n",
             "mx_probe_dep-0.1.dist-info/METADATA": b"Metadata-Version: 2.1\nName: mx-probe-dep\nVersion: 0.1\n",
             "mx_probe_dep-0.1.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}

    def digest(b: bytes) -> str:
        return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(b).digest()).rstrip(b"=").decode()
    record = "".join(f"{p},{digest(b)},{len(b)}\n" for p, b in files.items()) + "mx_probe_dep-0.1.dist-info/RECORD,,\n"
    files["mx_probe_dep-0.1.dist-info/RECORD"] = record.encode()
    whl = d / "mx_probe_dep-0.1-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        for p, b in files.items():
            z.writestr(p, b)
    return whl


def test_requirements_need_hashes_and_no_local_files():
    with pytest.raises(ServiceError, match="--hash"):
        check_requirements("requests==2.32.3\n", trusted=False)
    with pytest.raises(ServiceError, match="本机文件"):
        check_requirements("x @ file:///etc/passwd --hash=sha256:00\n", trusted=False)
    with pytest.raises(ServiceError, match="本机文件"):
        check_requirements("-r /root/other.txt\n", trusted=False)
    check_requirements("requests==2.32.3 \\\n    --hash=sha256:00\n", trusted=False)
    check_requirements("x @ file:///tmp/x.whl --hash=sha256:00\n", trusted=True)


@pytest.mark.skipif(shutil.which("uv") is None, reason="没有 uv")
def test_plugin_dependencies_from_lock_file(tmp_path: Path):
    """清单带依赖锁文件时，网络按它建一个独立的环境（只装 wheel、逐个校验哈希，按内容缓存），插件服务用它运行。"""
    whl = _wheel(tmp_path)
    sha = hashlib.sha256(whl.read_bytes()).hexdigest()
    pkg = tmp_path / "catalog" / "probe"
    for path, b in probe_package(requirements="service/requirements.txt"):
        (pkg.parent / path).parent.mkdir(parents=True, exist_ok=True)
        (pkg.parent / path).write_bytes(b)
    (pkg / "service" / "requirements.txt").write_text(f"mx-probe-dep @ {whl.as_uri()} --hash=sha256:{sha}\n", encoding="utf-8")
    (tmp_path / "catalog" / "plugins.json").write_text('{"plugins": ["probe"]}', encoding="utf-8")
    net, host, _, keeper, g = make_net(tmp_path, catalog=tmp_path / "catalog" / "plugins.json")
    net.install_plugin(g.id, "probe", actor=host)
    envs = list((net.store.root / "plugin-envs").iterdir())
    assert len(envs) == 1 and (envs[0] / ".ready").is_file()
    srv, port = listener()
    try:
        text, err = call(net, _ctx(net, keeper, g), "probe__probe", secret="/nonexistent", port=port)
    finally:
        srv.close()
    assert not err and json.loads(text)["dep"] == "from-wheel", text
