"""业务系统对接演示（examples/business-system）：模拟的报销系统经真实 HTTP 把对接能力全走一遍（查、组、发、调、错误、清理），
报销单送审时流里看得到被委托方的进展与心跳，批量核对能中断。演示坏了这里会变红。"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn

import matrix.open_server as open_server
import matrix.scripted as scripted
from matrix import plugins as pluginfs
from matrix.engine import Network
from matrix.runtime import ScriptedRuntime
from matrix.server import create_app

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "business-system"


def _load(name: str, file: str):
    """按独一无二的模块名加载（别的示例也有 setup_demo.py，不能都叫这个名字）。"""
    spec = importlib.util.spec_from_file_location(name, EXAMPLE / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


biz_system = _load("biz_demo_system", "biz_system.py")
setup_demo = _load("biz_demo_setup", "setup_demo.py")


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def demo(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(scripted, "EXPENSE_PAUSE_S", 1.2)   # 合同审核中间不出声的那段：长过下面的心跳间隔
    monkeypatch.setattr(scripted, "EXPENSE_STEP_S", 0.3)
    monkeypatch.setattr(open_server, "SVC_HEARTBEAT_S", 0.5)
    data = tmp_path / "data"
    data.mkdir()
    mport, bport = _port(), _port()
    cfg = setup_demo.build(data, f"http://127.0.0.1:{mport}", quiet=True)
    net = Network(ScriptedRuntime(scripted.scenario_handler), data, seed=False,
                  plugins=pluginfs.PluginCatalog(setup_demo.PLUGINS))
    server = uvicorn.Server(uvicorn.Config(create_app(net), host="127.0.0.1", port=mport, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    biz = biz_system.serve(data / setup_demo.CONFIG, bport)
    threading.Thread(target=biz.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{bport}", net, cfg
    biz.shutdown()
    server.should_exit = True


def post(base: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def review(base: str, message: str, session_id: str | None = None) -> list[tuple[str, dict]]:
    req = urllib.request.Request(base + "/api/review", data=json.dumps({"message": message, "session_id": session_id}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return list(biz_system.parse_sse(iter(r)))


def test_every_capability_runs_against_a_real_matrix(demo):
    """对接能力页的每一步都真的调成：查、组（含 409 确认后带 digest 再来）、发、调（自己的、续接、别人的）、用量；
    错误处理那几步分别是 403 / 401 / 404；令牌不出现在给浏览器的数据里；清理后项目没了。"""
    base, net, cfg = demo
    with urllib.request.urlopen(base + "/api/state") as r:
        page = r.read().decode()
    assert cfg["token"] not in page and cfg["personal_token"] not in page
    steps = [s["id"] for s in json.loads(page)["steps"]]
    expect = {"err_scope": 403, "err_token": 401, "err_missing": 404}
    for sid in steps:
        r = post(base, f"/api/steps/{sid}")
        assert r["error"] is None, (sid, r["error"])
        statuses = [c["status"] for c in r["calls"]]
        if sid in expect:
            assert statuses == [expect[sid]], (sid, statuses)
        elif sid == "from_blueprint":
            assert statuses == [409, 200]                                 # 先看要确认的事，再带 digest
        else:
            assert all(s < 300 for s in statuses), (sid, statuses, r["calls"][-1]["response"])
        if sid == "svc_chat":
            evs = r["calls"][-1]["response"]
            assert evs[0]["event"] == "session" and evs[-1]["event"] == "done" and "建议直接通过" in evs[-1]["data"]["text"]
        if sid == "svc_resume":
            assert "报销制度" in r["calls"][-1]["response"][-1]["data"]["text"]   # 续接：同一个会话里问依据
        if sid == "svc_other":
            assert "600 元" in r["calls"][-1]["response"][-1]["data"]["text"]
        if sid == "project":
            g = net.store.projects[r["state"]["project"]]
            ann = next(u.id for u in net.store.users.values() if u.email == "ann@demo.local")
            assert ann in g.participants and g.via_app                     # 开放的 Agent 拉进来，所有者成为参与者
    assert not [g for g in net.store.projects.values() if g.via_app]      # 清理：经应用建的项目都删了


def test_claim_review_streams_delegatee_progress_and_can_be_interrupted(demo):
    """报销单送审，多 Agent 协同：入口分头交给票据核验、预算管家、合同审核，合同审核再转给法务顾问（第二层委托）；
    流里看得到每一份委托与状态、被委托方的话与工具、心跳、入口每收到一份时说还在等谁，最后的结论汇总各方。批量核对中途能中断。"""
    base, net, cfg = demo
    r = post(base, "/api/prepare")
    assert r["state"].get("service"), r
    evs = review(base, biz_system.CLAIMS[1]["message"])
    prog = [d for e, d in evs if e == "progress"]
    tasks = {(p["from"], p["agent"]) for p in prog if p["kind"] == "task"}
    assert tasks == {("财务助手", "票据核验"), ("财务助手", "预算管家"), ("财务助手", "合同审核"), ("合同审核", "法务顾问")}
    done = {(p["agent"]) for p in prog if p["kind"] == "task" and p["status"] == "completed"}
    assert done == {"票据核验", "预算管家", "合同审核", "法务顾问"}                     # 每一份都验收了
    assert any(p["kind"] == "tool" and p["agent"] == "法务顾问" and p["brief"] == "合同/合同模板.md" for p in prog)
    assert {p["agent"] for p in prog if p["kind"] == "text"} >= {"票据核验", "预算管家", "合同审核", "法务顾问"}
    assert any(p["kind"] == "heartbeat" and "法务顾问" in p["working"] for p in prog)
    assert any(e == "delta" and "还在等" in d["text"] for e, d in evs)                 # 入口收到一份说一声
    assert evs[-1][0] == "done" and all(n in evs[-1][1]["text"] for n in ("票据核验", "预算管家", "合同审核"))

    out: list = []
    t = threading.Thread(target=lambda: out.extend(review(base, biz_system.CLAIMS[2]["message"])))
    t.start()
    sid = None
    for _ in range(200):
        conv = next((c for c in net.store.conversations.values() if c.kind == "service" and c.status != "idle"), None)
        if conv is not None and any("第 2/" in s.get("text", "") for s in net._live.get(f"{conv.id}/{conv.agent_id}", [])):
            sid = conv.id
            break
        time.sleep(0.05)
    assert sid, "批量核对没开始"
    assert post(base, "/api/interrupt", {"session_id": sid})["status"] == 200
    t.join(20)
    assert out[-1][0] == "error" and "终止" in out[-1][1]["message"]


def test_chat_entry_talks_to_own_and_others_services(demo):
    """对话入口：没发布自己的服务时也能找别人的服务聊；自己的服务要先发布（409）；带 session_id 续接同一个服务会话。"""
    base, net, cfg = demo
    with urllib.request.urlopen(base + "/api/state") as r:
        targets = {t["key"]: t for t in json.loads(r.read())["targets"]}
    assert targets["own"]["service"] is None and targets["差旅咨询"]["service"] == cfg["services"]["差旅咨询"]
    evs = chat(base, "去上海出差住宿标准是多少？", "差旅咨询")
    assert evs[-1][0] == "done" and "600 元" in evs[-1][1]["text"]
    with pytest.raises(urllib.error.HTTPError) as e:
        chat(base, "BX-2001 打车 50 元", "own")
    assert e.value.code == 409
    post(base, "/api/prepare")
    first = chat(base, "BX-2001 打车 50 元", "own")
    sid = first[0][1]["session_id"]
    again = chat(base, "依据是什么？", "own", sid)
    assert again[0][1]["session_id"] == sid and "报销制度" in again[-1][1]["text"]


def chat(base: str, message: str, service: str, session_id: str | None = None) -> list[tuple[str, dict]]:
    req = urllib.request.Request(base + "/api/chat", method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"message": message, "service": service, "session_id": session_id}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return list(biz_system.parse_sse(iter(r)))
