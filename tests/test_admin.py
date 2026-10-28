"""管理后台：用量台账、统计、谁是管理员、只看用量不看内容（docs/reference/admin.md）。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from matrix.engine import Network
from matrix.runtime import ScriptCall, ScriptedRuntime
from matrix.server import create_app
from matrix.usage import UsageLog, days_back, summarize, tokens

SECRET = "机密的收购计划 Bluebird"


def _talk(call: ScriptCall) -> str:
    """每轮报告一次用量；提示词里带“出错”的这一轮失败。"""
    call._emit({"type": "status", "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 50}})
    if "出错" in call.prompt:
        call.fail("RATE_LIMIT: too many requests")
    return "好的"


def _login(c: TestClient, email: str) -> dict:
    r = c.post("/api/auth/register", json={"email": email, "password": "password-123"})
    assert r.status_code == 200, r.text
    return c.get("/api/me").json()


def _say(net: Network, c: TestClient, conv_id: str, text: str) -> None:
    assert c.post(f"/api/conversations/{conv_id}/messages", json={"text": text}).status_code == 200
    net.wait_idle(conv_id, timeout=10)


def test_tokens_normalizes_runtime_fields():
    assert tokens({"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 6}) == \
        {"input": 10, "output": 4, "cached": 6, "total": 14}
    assert tokens({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "prompt_cache_hit_tokens": 3}) == \
        {"input": 10, "output": 5, "cached": 3, "total": 15}
    assert tokens({"inputTokens": 7, "outputTokens": 1, "flag": True, "x": "y"}) == \
        {"input": 7, "output": 1, "cached": 0, "total": 8}
    assert tokens({}) == {"input": 0, "output": 0, "cached": 0, "total": 0}


def test_usage_log_skips_broken_lines(tmp_path: Path):
    log = UsageLog(tmp_path)
    log.record({"ts": 100.0, "ok": True})
    with log.path.open("a", encoding="utf-8") as f:
        f.write('{"ts": 2\n')  # 写到一半断电
    log.record({"ts": 300.0, "ok": False})
    assert [e["ts"] for e in log.read()] == [100.0, 300.0]
    assert [e["ts"] for e in log.read(since=200)] == [300.0]
    assert log.first_ts() == 100.0


def test_summarize_buckets_by_local_day():
    now = time.time()
    since, labels = days_back(now, 7)
    assert len(labels) == 7 and labels[-1] == time.strftime("%Y-%m-%d", time.localtime(now))
    runs = [{"ts": now, "user": "u1", "ok": True, "model": "m", "usage": {"input_tokens": 5}},
            {"ts": now, "user": "u1", "ok": False, "error": "timeout", "usage": {}},
            {"ts": since - 10, "user": "u1", "ok": True, "usage": {"input_tokens": 999}}]  # 范围外
    d = summarize(runs, people=[{"id": "u1", "email": "a@x.io"}], messages=[("u1", now)], requests=[],
                  connector_calls=[("u1", now, False)], counts={}, now=now, days=7, recording_since=None)
    assert d["totals"]["runs"] == 2 and d["totals"]["failed"] == 1 and d["totals"]["total"] == 5
    assert d["totals"]["active_users"] == 1 and d["totals"]["connector_failed"] == 1
    assert d["daily"][-1]["runs"] == 2 and sum(x["runs"] for x in d["daily"]) == 2
    assert d["errors"] == [{"error": "timeout", "count": 1}]
    assert {m["model"] for m in d["models"]} == {"m", "默认"}


def test_admin_dashboard(tmp_path: Path):
    net = Network(ScriptedRuntime(_talk), tmp_path, seed=False)
    owner, partner = TestClient(create_app(net)), TestClient(create_app(net))
    me = _login(owner, "owner@example.com")
    other = _login(partner, "partner@example.com")
    assert me["admin"] is True and other["admin"] is False  # 没指定管理员：最早注册的那个人

    g = owner.post("/api/projects", json={"name": SECRET, "member_ids": []}).json()
    _say(net, owner, me["home_id"], SECRET)
    _say(net, owner, me["home_id"], "这一轮会出错")
    _say(net, partner, other["home_id"], "你好")

    assert owner.get("/admin").status_code == 200
    r = owner.get("/api/admin/usage", params={"days": 7})
    assert r.status_code == 200
    d = r.json()
    t = d["totals"]
    assert t["users"] == 2 and t["projects"] == 1 and t["messages"] == 3
    assert t["runs"] >= 3 and t["failed"] >= 1 and t["input"] == 120 * t["runs"] and t["cached"] == 50 * t["runs"]
    assert t["total"] == 150 * t["runs"] and t["active_users"] == 2
    assert len(d["daily"]) == 7 and d["daily"][-1]["runs"] == t["runs"]
    users = {u["email"]: u for u in d["users"]}
    assert users["owner@example.com"]["admin"] and users["owner@example.com"]["messages"] == 2
    assert users["owner@example.com"]["projects"] == 1 and users["partner@example.com"]["runs"] >= 1
    assert {v["venue"] for v in d["venues"]} == {"home"}
    assert {(x["where"], x["runtime"]) for x in d["runtimes"]} == {("cloud", "scripted")}
    assert d["errors"][0]["error"] == "rate_limited"
    assert d["recording_since"] is not None
    assert g["id"]

    # 只看用量，不看内容：项目名、消息正文、Agent 名都不出现在统计里，台账里也不记
    body = r.text
    assert SECRET not in body
    assert not any(a.name in body for a in net.store.agents.values())
    ledger = (tmp_path / "usage.jsonl").read_text(encoding="utf-8")
    assert SECRET not in ledger and "好的" not in ledger
    assert all(set(json.loads(x)) <= {"ts", "ms", "user", "agent", "project", "venue", "where", "runtime", "model",
                                      "ok", "error", "tools", "usage"} for x in ledger.splitlines())

    # 不是管理员：按不存在处理
    assert partner.get("/api/admin/usage").status_code == 404
    assert TestClient(create_app(net)).get("/api/admin/usage").status_code == 401
    assert owner.get("/api/admin/usage", params={"days": 365}).status_code == 422


def test_admins_named_by_deployer(tmp_path: Path):
    net = Network(ScriptedRuntime(_talk), tmp_path, seed=False, admins=[" Partner@Example.com ", ""])
    owner, partner = TestClient(create_app(net)), TestClient(create_app(net))
    assert _login(owner, "owner@example.com")["admin"] is False  # 指定了管理员：最早注册的人不再默认是
    assert _login(partner, "partner@example.com")["admin"] is True
    assert owner.get("/api/admin/usage").status_code == 404
    assert partner.get("/api/admin/usage").status_code == 200


def test_admin_usage_counts_connector_calls(tmp_path: Path):
    """连接器调用来自各连接的 calls.jsonl，按授权人算（只数次数与成败，不看调用内容）。"""
    from matrix.models import Connection
    net = Network(ScriptedRuntime(_talk), tmp_path, seed=False)
    c = TestClient(create_app(net))
    me = _login(c, "owner@example.com")
    conn = Connection(id="conn-1", owner_id=me["id"], connector_id="docs", name="文档")
    net.store.connections[conn.id] = conn
    log = tmp_path / "connections" / conn.id / "calls.jsonl"
    log.parent.mkdir(parents=True)
    now = time.time()
    log.write_text("\n".join([json.dumps({"ts": now, "ok": True, "tool": SECRET}),
                              json.dumps({"ts": now, "ok": False, "tool": "search"}),
                              json.dumps({"ts": now - 400 * 86400, "ok": True}),  # 范围外
                              "{broken"]) + "\n", encoding="utf-8")
    r = c.get("/api/admin/usage", params={"days": 7})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["totals"]["connector_calls"] == 2 and d["totals"]["connector_failed"] == 1
    assert d["users"][0]["connector_calls"] == 2 and SECRET not in r.text
