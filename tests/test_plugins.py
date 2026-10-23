"""项目插件（docs/project-plugins.md）：清单与包、安装与升级、记录、沙箱页面与桥、确认后才发的消息、
Agent 的记录工具、触发（外部回调、定时、记录变化）。"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from pathlib import Path

import pytest

from matrix import plugins as pluginfs
from matrix.engine import Network, NetworkError
from matrix.models import MCP_PREFIX, Message, SKILLS
from matrix.runtime import ScriptCall, ScriptedRuntime
from tests.test_users import client, joint, users

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "plugins.json"


def manifest(**over) -> dict:
    m = {
        "name": "deals", "version": "1.0.0", "title": "商机", "description": "商机推进",
        "tabs": [{"key": "board", "label": "看板", "entry": "page/index.html"}],
        "tables": {"leads": {"title": "线索", "fields": [
            {"key": "company", "type": "text", "required": True},
            {"key": "stage", "type": "select", "options": ["线索", "沟通", "报价"]},
            {"key": "owner_agent", "type": "agent"},
            {"key": "amount", "type": "number"}]}},
        "page_permissions": ["records.read", "records.write", "archive.read", "chat.post"],
        "agent_tools": ["records.read", "records.write"],
        "triggers": [
            {"key": "hook", "type": "webhook", "table": "leads", "notify": {"to": "Sales", "text": "新线索：{company}"}},
            {"key": "quote", "type": "record", "table": "leads", "when": {"field": "stage", "equals": "报价"},
             "notify": {"to": "Sales", "text": "{company} 进入报价"}},
            {"key": "weekly", "type": "schedule", "cron": "0 9 * * 1", "notify": {"to": "Sales", "text": "周报"}}],
    }
    m.update(over)
    return m


def package(m: dict | None = None, extra: dict[str, str] | None = None, top: str = "deals/") -> list[tuple[str, bytes]]:
    files = {"plugin.json": json.dumps(m or manifest(), ensure_ascii=False), "page/index.html": "<p>hi</p>",
             "page/app.js": "1", "skills/deal-intake/SKILL.md": '---\nname: "deal-intake"\ndescription: "怎么跟进商机"\n---\n做法',
             **(extra or {})}
    return [(top + k, v.encode()) for k, v in files.items()]


def zipped(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p, b in entries:
            z.writestr(p, b)
    return buf.getvalue()


def setup(tmp_path: Path, handler=None, **kw):
    rt = ScriptedRuntime(handler or (lambda call: "ok"))
    net = Network(rt, tmp_path, seed=False, **kw)
    host, = users(net, "host@x.io")
    net.create_agent("小助", "助理", [], [], actor=host)  # 第一个 Agent 是助理，Sales 才能被移出
    sales = net.create_agent("Sales", "跟进销售", [], ["delegate", "file.write"], actor=host)
    g = net.create_project("渠道", "华东渠道", [sales.id], actor=host)
    return net, rt, host, sales, g


# ================= 清单与包 =================
@pytest.mark.parametrize("bad, why", [
    ({"name": "Bad Name"}, "插件名"),
    ({"version": "v1"}, "version"),
    ({"tabs": [{"key": "x", "label": "x", "entry": "plugin.json"}]}, "page/"),
    ({"tabs": [{"key": "x", "label": "x", "entry": "page/../plugin.json"}]}, "page/"),
    ({"page_permissions": ["records.read", "vault.read"]}, "不认识"),
    ({"tables": {}, "triggers": [], "agent_tools": []}, "没有表"),
    ({"triggers": [{"key": "t", "type": "schedule", "cron": "* * * * *", "notify": {"to": "a", "text": "b"}}]}, "10 分钟"),
    ({"triggers": [{"key": "t", "type": "record", "table": "nope", "notify": {"to": "a", "text": "b"}}]}, "不在清单"),
    ({"triggers": [{"key": "t", "type": "record", "table": "leads", "when": {"field": "x", "equals": "y"},
                    "notify": {"to": "a", "text": "b"}}]}, "不在表"),
])
def test_manifest_validation(bad, why):
    with pytest.raises(pluginfs.PluginError, match=why):
        pluginfs.parse_manifest(manifest(**bad))


def test_package_rules():
    files = pluginfs.package_files(package())  # 多一层顶层目录可以
    assert pluginfs.check_package(files).name == "deals"
    with pytest.raises(pluginfs.PluginError, match="非法"):
        pluginfs.package_files(package(extra={"../evil.js": "x"}, top=""))
    with pytest.raises(pluginfs.PluginError, match="不在包里"):
        pluginfs.check_package(pluginfs.package_files([p for p in package() if not p[0].endswith("index.html")]))
    with pytest.raises(pluginfs.PluginError, match="name 要和目录名一致"):
        pluginfs.check_package(pluginfs.package_files(package(extra={"skills/x/SKILL.md": '---\nname: "y"\ndescription: "d"\n---\n'})))
    assert pluginfs.check_package(pluginfs.dir_files(EXAMPLE.parent / "plugins" / "sales-pipeline")).name == "sales-pipeline"


def test_cron():
    t = time.struct_time((2026, 10, 26, 9, 0, 0, 0, 299, -1))  # 周一 09:00
    assert pluginfs.cron_due("0 9 * * 1", t) and not pluginfs.cron_due("0 9 * * 2", t)
    assert pluginfs.cron_due("*/10 * * * *", t) and pluginfs.cron_due("0 9 26 * 3", t)  # 日、周都限定时取“或”
    with pytest.raises(pluginfs.PluginError):
        pluginfs.parse_cron("0 9 * *")


# ================= 安装、升级、卸载 =================
def test_install_from_catalog_and_upload_is_host_only(tmp_path: Path):
    """不变量 13、17：插件是项目档案的一部分，只有主持人装；联合项目的参与者看得到装了什么、申请了什么。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False,
                  plugins=pluginfs.PluginCatalog(EXAMPLE))
    host, guest, other = users(net, "h@x.io", "g@x.io", "o@x.io")
    g, chat = joint(net, host, "g@x.io", guest)
    assert [p["name"] for p in net.plugin_catalog()] == ["sales-pipeline"]
    with pytest.raises(NetworkError):
        net.install_plugin(g.id, "sales-pipeline", actor=guest)
    rev = g.archive_rev
    p = net.install_plugin(g.id, "sales-pipeline", actor=host)
    assert p["version"] == "1.0.0" and p["source"] == "catalog" and g.archive_rev == rev + 1
    assert not any(s["enabled"] for s in p["trigger_state"].values())  # 触发默认不开
    assert any("安装了插件「销售漏斗」" in m.text and "以看页面的人的名义" in m.text for m in chat.messages if m.role == "event")
    log = (tmp_path / "projects" / g.id / "log.jsonl").read_text(encoding="utf-8")
    assert '"plugin.install"' in log
    assert [x["name"] for x in net.project_detail(g.id, actor=guest)["plugin_list"]] == ["sales-pipeline"]
    with pytest.raises(NetworkError, match="unknown"):
        net.project_detail(g.id, actor=other)
    with pytest.raises(NetworkError):
        net.upload_plugin(g.id, package(), actor=guest)
    assert net.upload_plugin(g.id, package(), actor=host)["source"] == "upload"
    net.uninstall_plugin(g.id, "deals", actor=host)
    assert "deals" not in g.plugins and not (tmp_path / "projects" / g.id / "plugins" / "deals").exists()


def test_upgrade_needs_confirmation_when_schema_shrinks(tmp_path: Path):
    net, _, host, sales, g = setup(tmp_path)
    net.upload_plugin(g.id, package(), actor=host)
    rec = net.write_plugin_record(g.id, "deals", "leads", {"company": "启明", "amount": 5}, actor=host)
    m = manifest(version="1.1.0")
    m["tables"]["leads"]["fields"] = [f for f in m["tables"]["leads"]["fields"] if f["key"] != "amount"]
    with pytest.raises(NetworkError, match="升级会改变.*删除字段 leads.amount"):
        net.upload_plugin(g.id, package(m), actor=host)
    assert g.plugins["deals"]["version"] == "1.0.0"
    net.upload_plugin(g.id, package(m), confirm=True, actor=host)
    assert g.plugins["deals"]["version"] == "1.1.0"
    assert net.plugin_records(g.id, "deals", "leads", actor=host)[0]["id"] == rec["id"]  # 旧数据还在
    net.uninstall_plugin(g.id, "deals", actor=host)
    net.upload_plugin(g.id, package(), actor=host)
    assert len(net.plugin_records(g.id, "deals", "leads", actor=host)) == 1  # 卸载保留记录


# ================= 记录 =================
def test_records_validated_against_manifest_and_logged(tmp_path: Path):
    """不变量 17：记录写入只经人的操作与工具，按清单校验，变化进 log.jsonl；不重推整份档案。"""
    net, _, host, sales, g = setup(tmp_path)
    net.upload_plugin(g.id, package(), actor=host)
    rev = g.archive_rev
    for bad, why in [({"stage": "沟通"}, "必填"), ({"company": "A", "stage": "瞎写"}, "只能是"),
                     ({"company": "A", "boss": "x"}, "没有字段"), ({"company": "A", "amount": "很多"}, "数字"),
                     ({"company": "A", "owner_agent": "不认识的人"}, "不是项目成员")]:
        with pytest.raises(NetworkError, match=why):
            net.write_plugin_record(g.id, "deals", "leads", bad, actor=host)
    r = net.write_plugin_record(g.id, "deals", "leads", {"company": "启明", "owner_agent": "Sales", "amount": "12"},
                                actor=host)
    assert r["fields"] == {"company": "启明", "owner_agent": sales.id, "amount": 12} and r["created_by"] == {"user": host}
    r2 = net.write_plugin_record(g.id, "deals", "leads", {"stage": "沟通"}, r["id"], actor=host)
    assert r2["fields"]["company"] == "启明" and r2["fields"]["stage"] == "沟通"  # 修改只校验给出的字段
    assert g.archive_rev == rev and g.plugins["deals"]["data_rev"] == 2
    log = (tmp_path / "projects" / g.id / "log.jsonl").read_text(encoding="utf-8")
    assert log.count('"plugin.record.') == 2
    net.delete_plugin_record(g.id, "deals", "leads", r["id"], actor=host)
    assert '"plugin.record.delete"' in (tmp_path / "projects" / g.id / "log.jsonl").read_text(encoding="utf-8")


# ================= 沙箱页面与桥 =================
def test_plugin_frame_is_sandboxed_and_token_bound(tmp_path: Path):
    """不变量 20：插件页面带 sandbox CSP（不透明来源、不能联网）、不需要也不带登录凭证；
    帧令牌只取这个插件 page/ 下的静态文件，过期、卸载、换版本、看的人看不到项目后一律 404。"""
    from fastapi.testclient import TestClient
    from matrix.server import create_app
    net, _, host, sales, g = setup(tmp_path)
    users(net, "o@x.io")
    net.upload_plugin(g.id, package(), actor=host)
    c = client(net, "host@x.io")
    src = c.post(f"/api/projects/{g.id}/plugins/deals/frame", json={"tab": "board"}).json()["src"]
    assert src.startswith("/plugin-frame/") and src.endswith("/page/index.html")
    anon = TestClient(create_app(net))  # 不带 cookie 也能取：令牌就是授权，页面也就拿不到 cookie
    r = anon.get(src)
    assert r.status_code == 200 and r.text == "<p>hi</p>"
    csp = r.headers["content-security-policy"]
    assert csp.startswith("sandbox allow-scripts allow-forms;") and "allow-same-origin" not in csp
    assert "form-action 'none'" in csp  # 表单的 submit 事件照常，但哪里都提交不出去
    assert "connect-src 'none'" in csp and "frame-ancestors 'self'" in csp and r.headers["referrer-policy"] == "no-referrer"
    token = src.split("/")[2]
    assert anon.get(f"/plugin-frame/{token}/page/app.js").status_code == 200
    assert anon.get(f"/plugin-frame/{token}/_matrix/sdk.js").status_code == 200
    for bad in ("plugin.json", "skills/deal-intake/SKILL.md", "page/../plugin.json", "page/nope.js"):
        assert anon.get(f"/plugin-frame/{token}/{bad}").status_code == 404, bad
    assert anon.get(f"/plugin-frame/{token[:-2]}xx/page/index.html").status_code == 404  # 改过的令牌
    assert client(net, "o@x.io").post(f"/api/projects/{g.id}/plugins/deals/frame", json={"tab": "board"}).status_code == 404
    net.upload_plugin(g.id, package(manifest(version="1.0.1")), actor=host)  # 换了版本：旧令牌作废
    assert anon.get(src).status_code == 404
    from matrix import engine
    old, engine.FRAME_TTL = engine.FRAME_TTL, -1
    try:
        src2 = net.plugin_frame(g.id, "deals", "board", actor=host)
        assert anon.get(f"/plugin-frame/{src2}").status_code == 404  # 过期
    finally:
        engine.FRAME_TTL = old


def test_bridge_is_limited_to_requested_caps_and_viewer_visibility(tmp_path: Path):
    """不变量 20：桥的能力 = 插件申请的 ∩ 看的人本身有的；读范围不超过全体参与者可见的内容。越权留痕。"""
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    host, guest, other = users(net, "h@x.io", "g@x.io", "o@x.io")
    g, chat = joint(net, host, "g@x.io", guest)
    net.upload_plugin(g.id, package(manifest(page_permissions=["records.read", "chat.read", "requests.read"])), actor=host)
    ctx = net.plugin_bridge(g.id, "deals", "context", {}, actor=guest)
    assert ctx["viewer"]["is_host"] is False and ctx["permissions"] == ["records.read", "chat.read", "requests.read"]
    with pytest.raises(NetworkError, match="没有申请"):
        net.plugin_bridge(g.id, "deals", "records.create", {"table": "leads", "fields": {"company": "x"}}, actor=guest)
    with pytest.raises(NetworkError, match="没有申请"):
        net.plugin_bridge(g.id, "deals", "archive.read", {}, actor=guest)
    assert '"plugin.violation"' in (tmp_path / "projects" / g.id / "log.jsonl").read_text(encoding="utf-8")
    with pytest.raises(NetworkError, match="unknown"):
        net.plugin_bridge(g.id, "deals", "context", {}, actor=other)
    with pytest.raises(NetworkError, match="桥没有"):
        net.plugin_bridge(g.id, "deals", "vault.read", {}, actor=guest)
    with net._lock:  # 只给主持人看的消息（如席位失败说明）不经桥给出去
        chat.messages.append(Message(id="msg-private", role="system", text="只给主持人", audience=[host]))
        chat.messages.append(Message(id="msg-secret", role="agent", text="给主持人的", audience=[host]))
    texts = [m["text"] for m in net.plugin_bridge(g.id, "deals", "chat.read", {}, actor=guest)]
    assert "只给主持人" not in texts and "给主持人的" not in texts
    c = client(net, "g@x.io")
    r = c.post(f"/api/projects/{g.id}/plugins/deals/bridge", json={"op": "records.list", "args": {"table": "leads"}})
    assert r.status_code == 200 and r.json() == {"result": []}


def test_chat_post_only_drafts_until_viewer_confirms(tmp_path: Path):
    """不变量 20：以看的人名义发群聊，桥只生成草稿；只有这个人（经父页面的确认卡）才能发出，发一次就作废。"""
    net = Network(ScriptedRuntime(lambda call: "收到"), tmp_path, seed=False)
    host, guest = users(net, "h@x.io", "g@x.io")
    sales = net.create_agent("Sales", "s", [], [], actor=host)
    g, chat = joint(net, host, "g@x.io", guest, [sales.id])
    net.upload_plugin(g.id, package(), actor=host)
    n = len(chat.messages)
    d = net.plugin_bridge(g.id, "deals", "chat.post", {"text": "@Sales 看一下报价"}, actor=host)
    assert d["mentions"] == ["Sales"] and d["joint"] is True and len(chat.messages) == n  # 什么都没发
    with pytest.raises(NetworkError):
        net.send_plugin_draft(d["id"], actor=guest)  # 别人确认不了
    from matrix.engine import BRIDGE_OPS
    assert not any("send" in op for op in BRIDGE_OPS)  # 桥里没有“发送”：插件页面自己发不出去
    msg = net.send_plugin_draft(d["id"], actor=host)
    assert msg.user_id == host and msg.via == "商机" and msg.mentions == [sales.id]
    with pytest.raises(NetworkError):
        net.send_plugin_draft(d["id"], actor=host)
    net.wait_quiet()


# ================= Agent 这一侧 =================
def test_agent_record_tools_and_no_trigger_loops(tmp_path: Path):
    """不变量 6、20：插件把记录工具开给 Agent 时才挂上；Agent 写记录留痕为 Agent，但不引发“记录变化”触发
    （Agent 之间不能经插件互相唤醒）；人改成同样的值才触发。打法排在项目技能之后、档案推送里有表结构。"""
    seen: dict = {}

    def handler(call: ScriptCall) -> str:
        seen.setdefault("tools", call.tool_names)
        seen["prompt"] = call.prompt
        seen["skills"] = call.skills
        if "改成报价" in call.prompt:
            rid = re.search(r"rec-[0-9a-f]{8}", call.prompt).group()
            seen["write"] = call.tool("plugin_write", plugin="deals", table="leads", record_id=rid,
                                      fields='{"stage": "报价"}')
            seen["read"] = call.tool("plugin_records", plugin="deals", table="leads", where='{"stage": "报价"}')
        return "好的"
    net, rt, host, sales, g = setup(tmp_path, handler)
    chat = net.project_chat(g.id, actor=host)
    net.send_message(chat.id, "@Sales 你好", actor=host, background=False)
    assert MCP_PREFIX + "plugin_write" not in seen.pop("tools")  # 没装插件：没有记录工具
    net.upload_plugin(g.id, package(), actor=host)
    net.set_plugin_trigger(g.id, "deals", "quote", True, actor=host)
    rec = net.write_plugin_record(g.id, "deals", "leads", {"company": "启明", "stage": "沟通"}, actor=host)
    net.send_message(chat.id, f"@Sales 把 {rec['id']} 改成报价", actor=host, background=False)
    assert MCP_PREFIX + "plugin_write" in rt.calls[-1].tool_names
    assert "已修改记录" in seen["write"] and "启明" in seen["read"]
    assert "表 `leads`（线索）字段" in seen["prompt"] and "deal-intake" in seen["skills"]
    r = net.plugin_records(g.id, "deals", "leads", actor=host)[0]
    assert r["fields"]["stage"] == "报价" and r["updated_by"] == {"agent": sales.id}
    assert not any(m.via for m in chat.messages)  # Agent 改的：没有触发
    net.write_plugin_record(g.id, "deals", "leads", {"stage": "沟通"}, r["id"], actor=host)
    net.write_plugin_record(g.id, "deals", "leads", {"stage": "报价"}, r["id"], actor=host)
    net.wait_quiet()
    fired = [m for m in chat.messages if m.via == "商机"]
    assert len(fired) == 1 and fired[0].text == "@Sales 启明 进入报价" and fired[0].user_id == host


def test_plugin_skills_rank_after_project_skills(tmp_path: Path):
    net, rt, host, sales, g = setup(tmp_path, lambda call: (call.cwd / SKILLS / "deal-intake" / "SKILL.md").read_text("utf-8"))
    net.upload_plugin(g.id, package(), actor=host)
    net.save_project_skill(g.id, "deal-intake", "项目自己的版本", "项目做法", actor=host)
    chat = net.project_chat(g.id, actor=host)
    net.send_message(chat.id, "@Sales 开始", actor=host, background=False)
    assert "项目做法" in chat.messages[-1].text


# ================= 触发 =================
def test_webhook_writes_record_and_speaks_for_the_enabler(tmp_path: Path):
    """不变量 10、20：回调令牌只存哈希、只显示一次；触发消息代表启用它的人，被 @ 的 Agent 据此登记的
    用户任务下发人是这个人；停用、未知令牌、超频都拒绝。"""
    def handler(call: ScriptCall) -> str:
        if "新线索" in call.prompt:
            call.tool("open_request", title="跟进启明", expected_outcome="判断与下一步")
        return "收到"
    net, rt, host, sales, g = setup(tmp_path, handler)
    net.upload_plugin(g.id, package(), actor=host)
    c = client(net, "host@x.io")
    path = c.post(f"/api/projects/{g.id}/plugins/deals/triggers/hook/hook").json()["path"]
    token = path.rsplit("/", 1)[1]
    assert token not in (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "hook_hash" not in json.dumps(net.project_detail(g.id, actor=host))
    assert c.post(path, json={"company": "启明"}).status_code == 409  # 还没启用
    net.set_plugin_trigger(g.id, "deals", "hook", True, actor=host)
    r = c.post(path, json={"company": "启明", "stage": "线索", "secret": "丢掉"})
    assert r.status_code == 200 and r.json()["record"].startswith("rec-")
    net.wait_quiet()
    rec = net.plugin_records(g.id, "deals", "leads", actor=host)[0]
    assert rec["fields"] == {"company": "启明", "stage": "线索"} and rec["created_by"]["trigger"] == "hook"
    chat = net.project_chat(g.id, actor=host)
    m = next(x for x in chat.messages if x.via)
    assert m.text == "@Sales 新线索：启明" and m.user_id == host and m.mentions == [sales.id]
    req = next(iter(net.store.requests.values()))
    assert req.requester_id == host and req.message_id == m.id
    assert c.post("/hooks/h_nope", json={}).status_code == 404
    assert c.post(path, content=b"not json").status_code == 400
    from matrix import engine
    old, engine.HOOK_RATE = engine.HOOK_RATE, 1
    try:
        assert c.post(path, json={"company": "B"}).status_code == 429
    finally:
        engine.HOOK_RATE = old
    old_path = path
    path = c.post(f"/api/projects/{g.id}/plugins/deals/triggers/hook/hook").json()["path"]
    assert c.post(old_path, json={"company": "C"}).status_code == 404  # 重置后旧地址失效
    net.wait_quiet()


def test_schedule_fires_once_per_minute_and_pauses_after_failures(tmp_path: Path):
    net, rt, host, sales, g = setup(tmp_path)
    net.upload_plugin(g.id, package(), actor=host)
    monday9 = time.mktime((2026, 10, 26, 9, 0, 5, 0, 0, -1))
    assert net.run_schedules(monday9) == 0  # 没启用
    net.set_plugin_trigger(g.id, "deals", "weekly", True, actor=host)
    assert net.run_schedules(monday9) == 1 and net.run_schedules(monday9 + 30) == 0
    assert net.run_schedules(monday9 + 3600) == 0
    net.wait_quiet()
    chat = net.project_chat(g.id, actor=host)
    assert [m.text for m in chat.messages if m.via] == ["@Sales 周报"]
    net.remove_member(g.id, sales.id, actor=host)  # 目标成员不在了：失败计数，3 次暂停
    for week in (1, 2, 3):
        net.run_schedules(monday9 + week * 7 * 86400)
    st = net.project_detail(g.id, actor=host)["plugin_list"][0]["trigger_state"]["weekly"]
    assert st["paused"] and st["failures"] == 3 and "不是项目成员" in st["error"]
    assert any("连续 3 次失败" in m.text for m in chat.messages if m.role == "event")
    assert net.run_schedules(monday9 + 4 * 7 * 86400) == 0


def test_disabled_plugin_is_inert_until_reenabled(tmp_path: Path):
    """不变量 20：停用的插件对外一律不可用——页面令牌、桥、记录、Agent 的记录工具与提示、打法、档案推送、
    触发都停；包、记录与触发设置保留，只有主持人能启停，重新启用即恢复。"""
    seen: dict = {}

    def handler(call: ScriptCall) -> str:
        seen["tools"], seen["prompt"], seen["skills"] = call.tool_names, call.prompt, call.skills
        return "好的"
    net, rt, host, sales, g = setup(tmp_path, handler)
    other, = users(net, "g@x.io")
    net.upload_plugin(g.id, package(), actor=host)
    rec = net.write_plugin_record(g.id, "deals", "leads", {"company": "启明"}, actor=host)
    net.set_plugin_trigger(g.id, "deals", "weekly", True, actor=host)
    token = net.reset_plugin_hook(g.id, "deals", "hook", actor=host)
    net.set_plugin_trigger(g.id, "deals", "hook", True, actor=host)
    src = net.plugin_frame(g.id, "deals", "board", actor=host)
    with pytest.raises(NetworkError):
        net.set_plugin_enabled(g.id, "deals", False, actor=other)
    rev = g.archive_rev
    p = net.set_plugin_enabled(g.id, "deals", False, actor=host)
    assert p["enabled"] is False and g.archive_rev == rev + 1
    assert p["trigger_state"]["weekly"]["enabled"]  # 设置原样保留
    chat = net.project_chat(g.id, actor=host)
    assert any("停用了插件「商机」" in m.text for m in chat.messages if m.role == "event")
    with pytest.raises(NetworkError, match="unknown frame"):
        net.plugin_frame_file(src.split("/")[0], "page/index.html")
    for call in (lambda: net.plugin_frame(g.id, "deals", "board", actor=host),
                 lambda: net.plugin_bridge(g.id, "deals", "context", {}, actor=host),
                 lambda: net.plugin_records(g.id, "deals", "leads", actor=host),
                 lambda: net.write_plugin_record(g.id, "deals", "leads", {"company": "B"}, actor=host),
                 lambda: net.plugin_webhook(token, {"company": "C"})):
        with pytest.raises(NetworkError, match="已停用"):
            call()
    assert net.run_schedules(time.mktime((2026, 10, 26, 9, 0, 5, 0, 0, -1))) == 0
    net.send_message(chat.id, "@Sales 看看", actor=host, background=False)
    assert MCP_PREFIX + "plugin_records" not in seen["tools"] and "plugin_records" not in seen["prompt"]
    assert "### 插件" not in seen["prompt"] and "deal-intake" not in seen["skills"]
    assert [m["name"] for m in net.project_detail(g.id, actor=host)["plugin_list"]] == ["deals"]  # 管理页还看得到
    net.upload_plugin(g.id, package(manifest(version="1.0.1")), actor=host)
    assert g.plugins["deals"]["disabled"]  # 升级不改变启停
    net.set_plugin_enabled(g.id, "deals", True, actor=host)
    assert net.plugin_records(g.id, "deals", "leads", actor=host)[0]["id"] == rec["id"]
    assert net.plugin_webhook(token, {"company": "C"})["record"]
    net.wait_quiet()


def test_plugin_toggle_route_is_host_only(tmp_path: Path):
    net = Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False)
    host, guest = users(net, "h@x.io", "g@x.io")
    g, _ = joint(net, host, "g@x.io", guest)
    net.upload_plugin(g.id, package(), actor=host)
    assert client(net, "g@x.io").put(f"/api/projects/{g.id}/plugins/deals", json={"enabled": False}).status_code == 409
    r = client(net, "h@x.io").put(f"/api/projects/{g.id}/plugins/deals", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert client(net, "g@x.io").post(f"/api/projects/{g.id}/plugins/deals/frame", json={"tab": "board"}).status_code == 409


# ================= 本地校验命令 matrix-plugin check =================
def test_check_command_passes_example_and_reports_errors(tmp_path: Path, capsys):
    from matrix import plugin_check
    assert plugin_check.main(["check", str(EXAMPLE.parent / "plugins" / "sales-pipeline"), "--strict"]) == 0
    out = capsys.readouterr().out
    assert "✓ sales-pipeline 1.0.0" in out and "提醒" not in out
    z = tmp_path / "deals.zip"
    z.write_bytes(zipped(package(manifest(version="v1"))))
    assert plugin_check.main(["check", str(z)]) == 1
    assert "装不上：version 无效" in capsys.readouterr().err
    assert plugin_check.main(["check", str(tmp_path / "nope")]) == 1


def test_check_command_warns_about_likely_mistakes(tmp_path: Path, capsys):
    from matrix import plugin_check
    m = manifest(page_permissions=["records.read", "members.read"])
    m["triggers"][0]["notify"]["text"] = "新线索：{company} {boss}"
    m["triggers"][2]["notify"]["text"] = "周报 {company}"
    files = pluginfs.package_files(package(m, extra={
        "page/index.html": '<script src="/_matrix/sdk.js"></script><link href="https://cdn.x.io/a.css" rel="stylesheet">',
        "page/app.js": 'await matrix.records.update("leads", id, {}); fetch("/api/me");',
        "app.css": "body{}"}))
    warns = plugin_check.lint(files, pluginfs.check_package(files))
    text = "\n".join(warns)
    assert "桥客户端的路径要写 '../_matrix/sdk.js'" in text
    assert "外部资源 https://cdn.x.io/a.css" in text and "用了 fetch" in text
    assert "页面用到了「写插件自己的表" in text and "没有 records.write" in text
    assert "申请了 members.read" in text
    assert "{boss} 不是表 leads 的字段" in text and "定时触发" in text
    assert "app.css 不在 page/ 下" in text
    d = tmp_path / "deals"
    for rel, b in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(b)
    assert plugin_check.main(["check", str(d)]) == 0 and plugin_check.main(["check", str(d), "--strict"]) == 1
    capsys.readouterr()
    nested = pluginfs.package_files(package(manifest(tabs=[{"key": "b", "label": "b", "entry": "page/a/b.html"}]),
                                            extra={"page/a/b.html": '<script src="../../_matrix/sdk.js"></script>'}))
    assert not any("桥客户端" in w for w in plugin_check.lint(nested, pluginfs.check_package(nested)))
