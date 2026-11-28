"""跨平台的文件（docs/files-across-platforms.md）：各平台收发文件的方式不同，差异只在部署方的目录与驱动里消化——
fetch_file 只从部署方登记的来源拉（凭证不给模型、只发给第一跳的主机、每一跳都在 hosts 里），拉进这一轮的暂存区再 send_file；
平台目录的 files.in 决定这一轮的文件怎么交给平台（短期链接 / 平台的 file_id / 平台来取），files.source 接住平台在流里报的文件，
export_file 按 files.export 把 Matrix 的文件交给平台上的工具。短期链接只在签它的那一轮里有效。"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from matrix import file_sources as fsrc
from matrix.engine import ExecContext, Network
from matrix.file_sources import SourceCatalog, SourceError, parse_sources
from matrix.remote_agents import PlatformCatalog, PlatformError, parse_platforms
from matrix.runtime import ScriptedRuntime
from matrix.server import create_app
from tests.test_users import users

sys.path.insert(0, str(Path(__file__).parent.parent / "examples" / "remote-agent"))
from demo_platform import API_KEY, DemoPlatform, report  # noqa: E402


class Env:
    """演示平台 + Matrix。`files` 是平台目录里 demo 平台的 files 配置；来源目录里登记了演示平台的文件库（带平台凭证）。"""

    def __init__(self, tmp_path: Path, files: dict[str, Any] | None = None, extra_sources: list[dict] | None = None,
                 extra_platforms: list[dict] | None = None):
        self.data = data = tmp_path / "data"
        data.mkdir()
        self.demo = DemoPlatform(mcp_post=self._mcp_post, file_post=self._file_post, file_get=self._file_get)
        self.plats = plats = tmp_path / "agent-platforms.json"
        plats.write_text(json.dumps({"platforms": [
            {"id": "demo", "name": "演示平台", "url": self.demo.url, "bind_wait_s": 3, **({"files": files} if files else {})},
            *[{"url": self.demo.url, "bind_wait_s": 3, **p} for p in (extra_platforms or [])],
        ]}), encoding="utf-8")
        self.srcs = srcs = tmp_path / "file-sources.json"
        srcs.write_text(json.dumps({"sources": [
            {"id": "demo-files", "name": "演示平台的文件库", "url": self.demo.url + "/files/{ref}/content",
             "auth": "platform", "platform": "demo", "hint": "平台的文件 id（pf-…）"},
            *(extra_sources or []),
        ]}), encoding="utf-8")
        self.net = Network(ScriptedRuntime(lambda call: "云侧"), data, seed=False, platforms=PlatformCatalog(plats),
                           file_sources=SourceCatalog(srcs))
        self.client = TestClient(create_app(self.net))

    def _mcp_post(self, url: str, headers: dict[str, str], body: dict) -> dict:
        r = self.client.post("/mcp/remote", json=body, headers=headers)
        return r.json() if r.content else {}

    def _file_post(self, url: str, headers: dict[str, str], data: bytes) -> tuple[int, dict]:
        r = self.client.post(url.removeprefix("http://matrix"), content=data, headers=headers)
        return r.status_code, r.json()

    def _file_get(self, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        r = self.client.get(url.removeprefix("http://matrix"), headers=headers)
        return r.status_code, r.content

    def connect_mcp(self, agent_id: str, actor: str, platform_agent: str = "expense") -> str:
        k = self.net.issue_remote_key(agent_id, actor=actor)
        self.demo.mcp[platform_agent] = {"url": "http://matrix" + k["path"], "headers": {k["header"]: k["value"]}}
        return k["key"]

    def close(self) -> None:
        self.demo.close()


@pytest.fixture
def make_env(tmp_path: Path):
    made: list[Env] = []

    def make(files: dict[str, Any] | None = None, extra_sources: list[dict] | None = None,
             extra_platforms: list[dict] | None = None) -> Env:
        e = Env(tmp_path, files, extra_sources, extra_platforms)
        made.append(e)
        return e
    yield make
    for e in made:
        e.close()


class FileHost:
    """一个第三方的文件暂存：/f 给文件，/r 重定向到 localhost:同一端口/f，/big 超过 1 MB，/away 重定向到别的主机。"""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []   # (路径, Authorization)
        self.headers: list[dict[str, str]] = []  # 每个请求的全部请求头
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                outer.seen.append((self.path, self.headers.get("Authorization", "")))
                outer.headers.append(dict(self.headers))
                port = self.server.server_address[1]
                if self.path == "/r":
                    self.send_response(302)
                    self.send_header("Location", f"http://localhost:{port}/f")
                    self.end_headers()
                    return
                if self.path == "/away":
                    self.send_response(302)
                    self.send_header("Location", "http://elsewhere.invalid/f")
                    self.end_headers()
                    return
                body = b"x" * 1_500_000 if self.path == "/big" else "季度报表".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%E6%8A%A5%E8%A1%A8.csv")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:  # /big：客户端看到 Content-Length 超过上限就断开，不读内容
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def host():
    h = FileHost()
    yield h
    h.close()


def _ctx(dm_id: str, agent_id: str, kind: str = "seat") -> ExecContext:
    return ExecContext(kind=kind, id=f"{dm_id}/{agent_id}", agent_id=agent_id, conversation_id=dm_id, project_id=None,
                       authority=[], constraints=[], depth=0, chain=[agent_id])


def test_source_catalog_only_takes_what_the_deployer_wrote():
    """来源只来自部署方目录：地址模板要含 {ref}，整条链接的来源要写 hosts，带凭证的要写平台；ref 按来源的格式收。"""
    with pytest.raises(SourceError, match=r"\{ref\}"):
        parse_sources({"sources": [{"id": "a", "url": "https://x.example/files"}]})
    with pytest.raises(SourceError, match="hosts"):
        parse_sources({"sources": [{"id": "a", "url": "{ref}"}]})
    with pytest.raises(SourceError, match="platform"):
        parse_sources({"sources": [{"id": "a", "url": "https://x.example/{ref}", "auth": "platform"}]})
    with pytest.raises(SourceError, match="auth"):
        parse_sources({"sources": [{"id": "a", "url": "https://x.example/{ref}", "auth": "cookie"}]})
    with pytest.raises(SourceError, match="重复"):
        parse_sources({"sources": [{"id": "a", "url": "https://x.example/{ref}"}, {"id": "a", "url": "https://y/{ref}"}]})
    ids, links = parse_sources({"sources": [
        {"id": "ids", "url": "https://files.example/api/{ref}/raw"},
        {"id": "links", "url": "{ref}", "hosts": ["oss.example"], "max_mb": 500}]})
    assert ids.hosts == ("files.example",) and links.max_mb == 20  # 不超过附件上限
    assert ids.resolve("file-1") == "https://files.example/api/file-1/raw"
    with pytest.raises(SourceError, match="格式"):
        ids.resolve("../../admin")  # 模板来源只收一个 id，路径穿不出去
    with pytest.raises(SourceError, match="格式"):
        links.resolve("javascript:alert(1)")
    assert ids.usable_by(None) and not parse_sources({"sources": [
        {"id": "p", "url": "https://x.example/{ref}", "auth": "platform", "platform": "corp"}]})[0].usable_by("other")


def test_fetch_stays_on_listed_hosts_and_drops_credentials_on_redirect(host: FileHost):
    """每一跳都要在 hosts 里；凭证只发给第一跳的主机，重定向到别的主机（预签名链接常见）时不带；超过上限不收。"""
    src, = parse_sources({"sources": [{"id": "oss", "url": "{ref}", "hosts": ["127.0.0.1", "localhost"], "max_mb": 1}]})
    data, mime, name = fsrc.fetch(src, f"http://127.0.0.1:{host.port}/f", {"Authorization": "Bearer s3cret"})
    assert data == "季度报表".encode("utf-8") and mime == "text/csv" and name == "报表.csv"
    fsrc.fetch(src, f"http://127.0.0.1:{host.port}/r", {"Authorization": "Bearer s3cret"})
    assert host.seen[-2] == ("/r", "Bearer s3cret") and host.seen[-1] == ("/f", "")
    with pytest.raises(SourceError, match="白名单"):
        fsrc.fetch(src, f"http://127.0.0.1:{host.port}/away")
    with pytest.raises(SourceError, match="上限"):
        fsrc.fetch(src, f"http://127.0.0.1:{host.port}/big")
    only_ip, = parse_sources({"sources": [{"id": "x", "url": "{ref}", "hosts": ["10.9.9.9"]}]})
    with pytest.raises(SourceError, match="白名单"):
        fsrc.fetch(only_ip, f"http://127.0.0.1:{host.port}/f")


def test_fetch_file_pulls_a_platform_file_into_staging_and_sends_it(make_env):
    """平台回答里只给自己的文件 id：模型调 fetch_file(source, ref)，Matrix 带平台凭证去取（模型看不到凭证），
    拉进这一轮的暂存区，再 send_file 附在回复上；一轮结束暂存删掉。"""
    env = make_env()
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    env.connect_mcp(a.id, maya)
    dm = net.direct_chat(a.id, actor=maya)
    net.send_message(dm.id, "拉文件「demo-files」", actor=maya, background=False)
    reply = dm.messages[-1]
    assert [x["name"] for x in reply.attachments] == ["报销助手底稿.md"], reply.text
    f, _ = net.attachment_file(dm.id, reply.attachments[0]["sha"], actor=maya)
    assert f.read_bytes() == report("expense")
    assert env.demo.file_gets[-1]["Authorization"] == f"Bearer {API_KEY}"
    prompt = env.demo.sessions[dm.seats[a.id].dsh_session_id][-1]
    assert "## 外部文件来源" in prompt and "`demo-files`：演示平台的文件库" in prompt and API_KEY not in prompt
    assert not any((net.store.root / "staging").iterdir())
    # 不在目录里的来源、不合格式的 ref：不发请求
    live = _ctx(dm.id, a.id)
    n = len(env.demo.file_gets)
    with pytest.raises(Exception, match="没有能用的来源"):
        net.fetch_into_staging(live, "nope", "pf-1")
    with pytest.raises(Exception, match="格式"):
        net.fetch_into_staging(live, "demo-files", "../agents/expense")
    assert len(env.demo.file_gets) == n


def test_fetch_file_is_offered_only_where_it_can_be_used(make_env, host: FileHost):
    """fetch_file 只在有这个 Agent 能用的来源、且这一轮发得出去时出现：带平台凭证的来源只给那个平台的 Agent，
    云侧 Agent 只看得到不带凭证的来源；拉来的文件同样用 send_file(file_id) 发。"""
    env = make_env(extra_sources=[{"id": "oss", "name": "对象存储", "url": "{ref}", "hosts": ["127.0.0.1"]}])
    net = env.net
    maya, = users(net, "maya@x.io")
    cloud = net.create_agent("云侧助手", "d", [], [], actor=maya)
    dm = net.direct_chat(cloud.id, actor=maya)
    ctx = _ctx(dm.id, cloud.id)
    assert net._file_tools(ctx, cloud) == ["fetch_file"]
    assert [s.id for s in net._sources_for(cloud)] == ["oss"]  # 平台的文件库要平台凭证：云侧 Agent 用不了
    ctx.file_tools = ["fetch_file"]
    net._contexts["t-cloud"] = ctx
    with pytest.raises(Exception, match="没有能用的来源"):
        net.fetch_into_staging(ctx, "demo-files", "pf-1")
    text, bad = net.handle_tool("t-cloud", "fetch_file", {"source": "oss", "ref": f"http://127.0.0.1:{host.port}/f"})
    assert not bad and "报表.csv" in text
    fid = next(iter(ctx.staged))
    text, bad = net.handle_tool("t-cloud", "send_file", {"file_id": fid})
    assert not bad and [x["name"] for x in ctx.sent] == ["报表.csv"]
    net._contexts.pop("t-cloud")
    net._drop_staged(ctx)
    # 委托任务里没有 send_file 也没有 delegate：拉来了也发不出去，不给
    task = _ctx(dm.id, cloud.id, kind="task")
    assert net._file_tools(task, cloud) == []
    text, bad = net.handle_tool("t-none", "fetch_file", {})
    assert bad


def test_files_reported_in_the_stream_are_fetched_and_attached_even_without_mcp(make_env):
    """files.source：平台在流里报 file {ref, name}，Matrix 按那个来源取回，照 send_file 附在这一轮的回复上——
    基础接入（没配 Matrix 的 MCP）也行。没配 files.source 的平台报的文件不取。"""
    env = make_env(files={"source": "demo-files"})
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    dm = net.direct_chat(a.id, actor=maya)
    net.send_message(dm.id, "报文件", actor=maya, background=False)
    reply = dm.messages[-1]
    assert [x["name"] for x in reply.attachments] == ["报销助手报表.md"], reply.text
    f, _ = net.attachment_file(dm.id, reply.attachments[0]["sha"], actor=maya)
    assert f.read_bytes() == report("expense")
    assert "Matrix 会自动取回" in env.demo.sessions[dm.seats[a.id].dsh_session_id][-1]


def test_files_in_url_hands_short_links_valid_for_that_round(make_env):
    """files.in = url：这一轮拿得到的文件随对话请求附上 Matrix 签的短期链接，平台不用带请求头就能取；一轮结束链接作废。"""
    env = make_env(files={"in": "url", "matrix_url": "http://matrix"})
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)  # 基础接入就能收文件
    dm = net.direct_chat(a.id, actor=maya)
    meta = net.upload_attachment(dm.id, "发票.pdf", b"%PDF-1.4 invoice", "application/pdf", actor=maya)
    net.send_message(dm.id, "看看这张发票", attachments=[meta], actor=maya, background=False)
    got, = env.demo.received_last
    assert got["code"] == 200 and got["data"] == b"%PDF-1.4 invoice" and got["sha256"] == meta["sha"]
    assert got["url"].startswith("http://matrix/mcp/remote/links/lnk_")
    assert "随消息收到：发票.pdf" in dm.messages[-1].text
    prompt = env.demo.sessions[dm.seats[a.id].dsh_session_id][-1]
    assert "已经随这条消息交给你的平台" in prompt and "GET /mcp/remote/files/" not in prompt
    assert env.client.get(got["url"].removeprefix("http://matrix")).status_code == 404
    assert env.client.get("/mcp/remote/links/lnk_guess").status_code == 404


def test_files_in_upload_puts_files_into_the_platform_once(make_env):
    """files.in = upload：先传到平台自己的文件接口，对话请求里附平台的 file_id；同一份内容不重复传。"""
    env = make_env(files={"in": "upload"})
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    dm = net.direct_chat(a.id, actor=maya)
    meta = net.upload_attachment(dm.id, "发票.pdf", b"%PDF-1.4 invoice", "application/pdf", actor=maya)
    net.send_message(dm.id, "看看", attachments=[meta], actor=maya, background=False)
    got, = env.demo.received_last
    assert got["file_id"] in env.demo.files and got["data"] == b"%PDF-1.4 invoice" and "url" not in got
    net.send_message(dm.id, "再看看", actor=maya, background=False)
    again, = env.demo.received_last
    assert again["file_id"] == got["file_id"] and len(env.demo.files) == 1


def test_export_file_gives_the_platform_a_link_for_its_own_tools(make_env):
    """export_file：平台默认来取（pull），但平台上的工具只认链接时，按 files.export 签一条这一轮有效的短期链接。"""
    env = make_env(files={"matrix_url": "http://matrix"})  # 写了 matrix_url，export 默认就是 url
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    env.connect_mcp(a.id, maya)
    dm = net.direct_chat(a.id, actor=maya)
    meta = net.upload_attachment(dm.id, "数据.csv", b"a,b\n1,2\n", "text/csv", actor=maya)
    net.send_message(dm.id, "导出", attachments=[meta], actor=maya, background=False)
    assert env.demo.downloads[-1] == ("expense", 200, b"a,b\n1,2\n")
    assert "短期下载链接" in dm.messages[-1].text
    ctx = _ctx(dm.id, a.id)
    assert "export_file" in net._file_tools(ctx, a)
    text, bad = net._tool_export_file(ctx, {"file": "0" * 64})
    assert bad and "拿不到" in text


def test_platform_files_config_is_validated():
    """平台目录的 files：短期链接要写 Matrix 的地址；方式只能是认得的那几种。"""
    with pytest.raises(PlatformError, match="matrix_url"):
        parse_platforms({"platforms": [{"id": "x", "url": "http://x", "files": {"in": "url"}}]})
    with pytest.raises(PlatformError, match="files.in"):
        parse_platforms({"platforms": [{"id": "x", "url": "http://x", "files": {"in": "ftp"}}]})
    with pytest.raises(PlatformError, match="files.export"):
        parse_platforms({"platforms": [{"id": "x", "url": "http://x", "files": {"export": "mail"}}]})
    p, = parse_platforms({"platforms": [{"id": "x", "url": "http://x"}]})
    assert (p.files_in, p.files_export, p.files_sources) == ("pull", None, ())  # 不写 files：和原来一样
    assert (p.files_repeat, p.files_resend, p.files_upload_ttl_s) == ("skip", "all", 0)
    q, = parse_platforms({"platforms": [{"id": "x", "url": "http://x", "files": {"matrix_url": "https://m.example/"}}]})
    assert q.files_export == "url" and q.matrix_url == "https://m.example"


def test_a_driver_without_file_support_falls_back_to_pull(make_env):
    """外部驱动没声明 files_in 时，files.in 照旧按 pull：在提示词里列出文件，平台带请求头来取。"""
    env = make_env(files={"in": "upload"})
    net = env.net
    from matrix.remote_agents import StandardDriver

    class Old(StandardDriver):
        files_in = ()
    net._remote_driver = lambda name: Old()
    net.remote_runtime._driver = net._remote_driver
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    dm = net.direct_chat(a.id, actor=maya)
    meta = net.upload_attachment(dm.id, "发票.pdf", b"%PDF", "application/pdf", actor=maya)
    net.send_message(dm.id, "看看", attachments=[meta], actor=maya, background=False)
    assert not env.demo.received_last and not env.demo.files
    assert f"GET /mcp/remote/files/{meta['sha']}" in env.demo.sessions[dm.seats[a.id].dsh_session_id][-1]
    assert any(e.type == "remote.files_fallback" for e in net.bus.since(0))


# ================= 一个来源给几个平台用、一个平台用几个来源 =================
def _live(net: Network, dm_id: str, agent_id: str, token: str) -> ExecContext:
    ctx = _ctx(dm_id, agent_id)
    net._contexts[token] = ctx
    return ctx


def test_one_source_serves_several_platforms_each_with_its_own_credentials(make_env, host: FileHost):
    """来源的 for 写几个平台：auth = platform 时每个 Agent 带自己所在平台的凭证（请求头按那个平台的写法），
    谁也拿不到别的平台的；for 里没有的平台、云侧 Agent 都用不了。"""
    env = make_env(
        extra_sources=[{"id": "oss", "name": "共享暂存", "url": "{ref}", "hosts": ["127.0.0.1"],
                        "auth": "platform", "for": ["demo", "demo2"]}],
        extra_platforms=[{"id": "demo2", "name": "第二个平台", "auth": {"header": "X-Platform-Key", "template": "Token {value}"}},
                         {"id": "other", "name": "没登记的平台"}])
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    b = net.add_remote_agent("demo2", "travel", "key-two", name="第二个平台的 Agent", actor=maya)
    c = net.add_remote_agent("other", "expense", "key-three", name="别的平台的 Agent", actor=maya)
    cloud = net.create_agent("云侧助手", "d", [], [], actor=maya)
    assert [x.id for x in net._sources_for(a)] == ["demo-files", "oss"]
    assert [x.id for x in net._sources_for(b)] == ["oss"]  # demo-files 只给 demo
    assert net._sources_for(c) == [] and net._sources_for(cloud) == []
    url = f"http://127.0.0.1:{host.port}/f"
    for agent, token in ((a, "t-a"), (b, "t-b")):
        dm = net.direct_chat(agent.id, actor=maya)
        net.fetch_into_staging(_live(net, dm.id, agent.id, token), "oss", url)
    sent = [(h.get("Authorization"), h.get("X-Platform-Key")) for h in host.headers]
    assert sent == [(f"Bearer {API_KEY}", None), (None, "Token key-two")]
    dm = net.direct_chat(c.id, actor=maya)
    with pytest.raises(Exception, match="没有能用的来源"):
        net.fetch_into_staging(_live(net, dm.id, c.id, "t-c"), "oss", url)


def test_secret_source_carries_the_deployers_key_only(make_env, host: FileHost, monkeypatch: pytest.MonkeyPatch):
    """auth = secret：带部署方给这个来源存的密钥（保管库，命令行从标准输入写入），没存就不取。"""
    import io

    from matrix import server
    env = make_env(extra_sources=[{"id": "dms", "name": "文档中心", "url": "{ref}", "hosts": ["127.0.0.1"],
                                   "auth": "secret", "header": "X-Api-Key", "template": "{value}", "for": ["cloud"]}])
    net = env.net
    maya, = users(net, "maya@x.io")
    cloud = net.create_agent("云侧助手", "d", [], [], actor=maya)
    dm = net.direct_chat(cloud.id, actor=maya)
    ctx = _live(net, dm.id, cloud.id, "t-cloud")
    url = f"http://127.0.0.1:{host.port}/f"
    with pytest.raises(Exception, match="file-source-secret dms"):
        net.fetch_into_staging(ctx, "dms", url)
    assert host.headers == []  # 没有密钥就不发请求
    monkeypatch.setattr("sys.stdin", io.StringIO("dms-secret-value\n"))
    server.file_source_secret(["dms", "--data", str(env.data)])
    net.fetch_into_staging(ctx, "dms", url)
    assert host.headers[-1].get("X-Api-Key") == "dms-secret-value" and not host.headers[-1].get("Authorization")
    assert "dms-secret-value" not in (env.data / "state.json").read_text(encoding="utf-8")


def test_a_platform_with_several_sources_picks_one_by_the_ref_format(make_env, host: FileHost):
    """files.sources 写几个来源：平台报的文件按 ref 格式选，恰好匹配一个才取（平台文件 id 与第三方链接各走各的）；
    部署方没把正则写开、两个来源都匹配时不猜，启动检查也会报出来。"""
    env = make_env(files={"sources": ["demo-files", "oss"]},
                   extra_sources=[{"id": "oss", "name": "共享暂存", "url": "{ref}", "hosts": ["127.0.0.1"]}])
    net = env.net
    assert net.file_config_problems() == []
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    dm = net.direct_chat(a.id, actor=maya)
    net.send_message(dm.id, f"报文件，再报链接「http://127.0.0.1:{host.port}/f」", actor=maya, background=False)
    assert sorted(x["name"] for x in dm.messages[-1].attachments) == ["报表.csv", "报销助手报表.md"]

    env.srcs.write_text(json.dumps({"sources": [
        {"id": "demo-files", "url": env.demo.url + "/files/{ref}/content", "auth": "platform", "for": ["demo"]},
        {"id": "mirror", "url": env.demo.url + "/mirror/{ref}"}]}), encoding="utf-8")
    import os
    os.utime(env.srcs, (1, 1))  # 换了内容的目录：按修改时间重新读
    env.plats.write_text(env.plats.read_text(encoding="utf-8").replace('"oss"', '"mirror"'), encoding="utf-8")
    os.utime(env.plats, (1, 1))
    assert any("ref 格式一样" in p for p in net.file_config_problems())
    net.send_message(dm.id, "报文件", actor=maya, background=False)
    assert not dm.messages[-1].attachments
    ev = [e.data for e in net.bus.since(0) if e.type == "remote.file"][-1]
    assert ev["outcome"] == "failed" and "同时符合几个文件来源" in ev["note"]


def test_platform_files_config_names_unknown_or_unusable_sources(make_env):
    """启动检查：files.sources 里写了来源目录里没有的、或没给这个平台用的来源。"""
    env = make_env(files={"sources": ["nope", "oss"]},
                   extra_sources=[{"id": "oss", "url": "{ref}", "hosts": ["x.example"], "auth": "platform", "for": ["elsewhere"]}])
    probs = env.net.file_config_problems()
    assert any("nope" in p and "没有" in p for p in probs) and any("oss" in p and "没给这个平台用" in p for p in probs)


# ================= 多轮对话反复提到同一个文件 =================
def test_a_file_fetched_before_is_reused_when_mentioned_again(make_env):
    """同一个对话里取过的（来源, ref）再取时直接用已有的那份：不再访问来源（平台的临时链接过期了也不怕）；
    fresh=true 或来源 cache=false 时重新取；别的对话里的不算（每个对话各记各的，可见性不放宽）。"""
    env = make_env(extra_sources=[{"id": "live", "name": "会变的文件", "url": "{ref}", "hosts": ["127.0.0.1"], "cache": False}])
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    env.connect_mcp(a.id, maya)
    dm = net.direct_chat(a.id, actor=maya)
    net.send_message(dm.id, "拉文件「demo-files」", actor=maya, background=False)
    first = dm.messages[-1].attachments[0]
    fid = next(iter(env.demo.files))
    assert dm.file_refs[f"src:demo-files:{fid}"] == first["sha"]
    n = len(env.demo.file_gets)
    del env.demo.files[fid]  # 平台那边已经删了 / 链接过期了
    ctx = _live(net, dm.id, a.id, "t-2")
    out = net.fetch_into_staging(ctx, "demo-files", fid)
    assert out["cached"] and out["sha256"] == first["sha"] and len(env.demo.file_gets) == n
    with pytest.raises(Exception, match="404"):
        net.fetch_into_staging(ctx, "demo-files", fid, fresh=True)  # 一定要最新的：去平台取，取不到就是取不到
    other = net.direct_chat(net.add_remote_agent("demo", "travel", API_KEY, actor=maya).id, actor=maya)
    with pytest.raises(Exception, match="404"):
        net.fetch_into_staging(_live(net, other.id, a.id, "t-3"), "demo-files", fid)
    # 旧的暂存 file_id 也认：下一轮 delegate 的 attachments / send_file 写它，就是当时发出去的那份
    old = next(k[4:] for k in dm.file_refs if k.startswith("stg:"))
    assert net._pass_attachments(ctx, old) == [first]
    assert "file_refs" not in net.conversation_view(dm, maya)  # 只持久化，不进接口返回


def test_platform_reporting_the_same_file_every_round_attaches_it_once(make_env):
    """平台每一轮都把会话里的文件报一遍：files.repeat = skip（默认）时已经发过的不再附，也不再取；attach 时照附，但不重新取。"""
    env = make_env(files={"sources": ["demo-files"]})
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    dm = net.direct_chat(a.id, actor=maya)
    net.send_message(dm.id, "报文件", actor=maya, background=False)
    assert [x["name"] for x in dm.messages[-1].attachments] == ["报销助手报表.md"]
    n = len(env.demo.file_gets)
    net.send_message(dm.id, "报文件", actor=maya, background=False)
    assert not dm.messages[-1].attachments and len(env.demo.file_gets) == n
    ev = [e.data for e in net.bus.since(0) if e.type == "remote.file"][-1]
    assert ev["outcome"] == "skipped" and "之前已经发过" in ev["note"]
    import os
    env.plats.write_text(env.plats.read_text(encoding="utf-8").replace('"sources"', '"repeat": "attach", "sources"'),
                         encoding="utf-8")
    os.utime(env.plats, (1, 1))
    net.send_message(dm.id, "报文件", actor=maya, background=False)
    assert [x["name"] for x in dm.messages[-1].attachments] == ["报销助手报表.md"] and len(env.demo.file_gets) == n


def test_uploaded_platform_file_ids_survive_a_restart_and_expire(make_env):
    """files.in = upload：传过的平台 file_id 记在对话上（重启后也认），过了 files.upload_ttl_s 才重传。"""
    env = make_env(files={"in": "upload", "upload_ttl_s": 3600})
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    dm = net.direct_chat(a.id, actor=maya)
    meta = net.upload_attachment(dm.id, "发票.pdf", b"%PDF-1.4 invoice", "application/pdf", actor=maya)
    net.send_message(dm.id, "看看", attachments=[meta], actor=maya, background=False)
    again = Network(ScriptedRuntime(lambda call: ""), env.data, seed=False, platforms=PlatformCatalog(env.plats),
                    file_sources=SourceCatalog(env.srcs))
    ups = again.store.conversations[dm.id].remote_files[a.id]["uploads"]
    assert list(ups) == [meta["sha"]] and ups[meta["sha"]][0] in env.demo.files
    net.send_message(dm.id, "再看看", actor=maya, background=False)
    assert len(env.demo.files) == 1
    dm.remote_files[a.id]["uploads"][meta["sha"]][1] -= 7200  # 过期了
    net.send_message(dm.id, "第三次", actor=maya, background=False)
    assert len(env.demo.files) == 2 and env.demo.received_last[0]["file_id"] != ups[meta["sha"]][0]


def test_resend_new_hands_only_new_files_within_the_same_platform_session(make_env):
    """files.resend = new：同一个平台会话里只交上一轮之后新出现的文件；会话 id 变了（新会话）就再交全部。"""
    env = make_env(files={"in": "url", "matrix_url": "http://matrix", "resend": "new"})
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    dm = net.direct_chat(a.id, actor=maya)
    first = net.upload_attachment(dm.id, "一.txt", b"one", "text/plain", actor=maya)
    net.send_message(dm.id, "第一份", attachments=[first], actor=maya, background=False)
    assert [f["name"] for f in env.demo.received_last] == ["一.txt"]
    second = net.upload_attachment(dm.id, "二.txt", b"two", "text/plain", actor=maya)
    net.send_message(dm.id, "第二份", attachments=[second], actor=maya, background=False)
    assert [f["name"] for f in env.demo.received_last] == ["二.txt"]
    prompt = env.demo.sessions[dm.seats[a.id].dsh_session_id][-1]
    assert "一.txt" in prompt and "之前已经交给过你的平台" in prompt  # 提示词里照样列出全部
    dm.seats[a.id].dsh_session_id = None  # 平台会话换了
    third = net.upload_attachment(dm.id, "三.txt", b"three", "text/plain", actor=maya)
    net.send_message(dm.id, "第三份", attachments=[third], actor=maya, background=False)
    assert sorted(f["name"] for f in env.demo.received_last) == ["一.txt", "三.txt", "二.txt"]


def test_file_sources_section_says_what_this_round_can_do_with_the_file():
    """提示词里的“外部文件来源”按这一轮的工具写：委托任务里发的随交付交回；没有 send_file 时只能转交。"""
    from matrix.protocol import format_file_sources
    srcs = [{"id": "oss", "name": "对象存储", "hint": "临时链接"}]
    assert "发给对话里的人" in format_file_sources(srcs)
    assert "随交付交回" in format_file_sources(srcs, task=True)
    no_send = format_file_sources(srcs, send=False, task=True)
    assert "这里没有 send_file" in no_send and "send_file 发" not in no_send


# ================= 上下文里出现的文件 id：就地标注 =================
def test_file_ids_in_later_context_are_annotated_with_the_matrix_copy(make_env):
    """外部平台 Agent 在回复里写了平台的文件 id、Matrix 按 file 事件把它取回附上之后，别的成员被 @ 时，
    上下文里的这个 id 就地标注成对话里的那份附件（云侧 / 端侧就是 attachments/ 下的路径），不用再按 id 取一次。"""
    env = make_env(files={"sources": ["demo-files"]})
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    cloud = net.create_agent("分析员", "d", [], [], actor=maya)
    g = net.create_project("报销", "", [a.id, cloud.id], actor=maya)
    chat = net.project_chat(g.id, actor=maya)
    net.send_message(chat.id, f"@{a.name} 报文件", actor=maya, background=False)
    att, = chat.messages[-1].attachments
    fid = env.demo.reported["expense"]
    assert fid in chat.messages[-1].text
    seen: list[str] = []
    net.runtime.handler = lambda call: seen.append(call.prompt) or "看过了"
    net.send_message(chat.id, f"@{cloud.name} 看看上面那份报表", actor=maya, background=False)
    from matrix.models import attachment_rel
    assert f"{fid}（Matrix 已拿到：`{attachment_rel(att)}`）" in seen[-1]
    assert fid in chat.messages[-3].text and "Matrix 已拿到" not in chat.messages[-3].text  # 只在提示词里标，消息原文不变


def test_annotations_cover_upload_ids_and_respect_visibility(make_env):
    """传到平台得到的 file_id 也记下、也标注；只标这一轮本来就拿得到的附件，只标整个 id（pf-1 不标进 pf-12）；
    外部平台 Agent 看到的标注指向“文件”一节，不是它那里不存在的 attachments/ 路径。"""
    env = make_env(files={"in": "upload"})
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    dm = net.direct_chat(a.id, actor=maya)
    meta = net.upload_attachment(dm.id, "发票.pdf", b"%PDF-1.4 invoice", "application/pdf", actor=maya)
    net.send_message(dm.id, "看看", attachments=[meta], actor=maya, background=False)
    fid = env.demo.received_last[0]["file_id"]
    assert dm.file_refs[f"up:demo:{fid}"] == meta["sha"]
    ctx = _ctx(dm.id, a.id)
    out = net._annotate_file_ids(ctx, a, f"平台说 {fid} 处理完了；{fid}9 不是它")
    assert f"{fid}（Matrix 已拿到：附件 发票.pdf，sha256 {meta['sha']}" in out and f"{fid}9 不是它" in out
    cloud = net.create_agent("云侧助手", "d", [], [], actor=maya)
    other = net.direct_chat(cloud.id, actor=maya)
    assert net._annotate_file_ids(_ctx(other.id, cloud.id), cloud, f"{fid}") == fid  # 别的对话：没有这份附件，不标


def test_service_face_only_uses_sources_without_credentials(make_env):
    """不变量 27：公共 Agent 在服务面只为调用方做事、用调用方自己的授权；平台凭证（添加它的人的）、来源密钥（部署方的）
    都不是调用方的，服务面里这类来源一律不给，不带凭证的照常。"""
    env = make_env(extra_sources=[{"id": "open", "name": "公开暂存", "url": "{ref}", "hosts": ["127.0.0.1"]},
                                  {"id": "dms", "url": "{ref}", "hosts": ["127.0.0.1"], "auth": "secret", "for": ["*"]}])
    net = env.net
    maya, = users(net, "maya@x.io")
    a = net.add_remote_agent("demo", "expense", API_KEY, actor=maya)
    dm = net.direct_chat(a.id, actor=maya)
    ctx = _ctx(dm.id, a.id)
    assert [x.id for x in net._sources_for(a, ctx)] == ["demo-files", "open", "dms"]
    ctx.service = True
    assert [x.id for x in net._sources_for(a, ctx)] == ["open"]
    net._contexts["t-svc"] = ctx
    with pytest.raises(Exception, match="没有能用的来源"):
        net.fetch_into_staging(ctx, "demo-files", "pf-1")
