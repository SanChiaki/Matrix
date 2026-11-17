"""管理操作的审计日志（docs/control-plane.md 第 8 节，C0）：后台的改动记一行、只追加；不记密钥与内容；
只有管理员看得到；个人的操作（自己的 IM 机器人）不进审计。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from matrix.audit import ACTIONS, AuditLog
from matrix.server import create_app
from tests.test_im import make  # noqa: F401 —— fixture
from tests.test_users import users

FLASH = {"id": "flash", "label": "DeepSeek Flash", "provider": "ds", "model": "deepseek-flash"}
PROVIDER = {"id": "ds", "name": "DeepSeek", "api": "openai-completions", "base_url": "https://api.deepseek.com"}


def test_admin_changes_are_audited_without_secrets(make, tmp_path: Path):  # noqa: F811
    net, b = make()
    admin_id, bob_id = users(net, "admin@x.io", "bob@x.io")
    app = create_app(net, b)
    admin, bob = TestClient(app), TestClient(app)
    admin.post("/api/auth/login", json={"email": "admin@x.io", "password": "password-123"})
    bob.post("/api/auth/login", json={"email": "bob@x.io", "password": "password-123"})
    key, im_secret = "sk-very-secret-key", "im-secret-token"

    rev = admin.get("/api/admin/models").json()["rev"]
    assert admin.put("/api/admin/models", json={"models": [FLASH], "default": "flash", "rev": rev,
                                                "providers": [{**PROVIDER, "api_key": key}]}).status_code == 200
    assert admin.post(f"/api/admin/users/{bob_id}/reset").status_code == 200
    assert admin.put("/api/admin/plugins/policy", json={"remote": "public"}).status_code == 200
    cid = admin.post("/api/admin/im", json={"kind": "fake", "values": {"bot": "b1", "token": im_secret}}).json()["channel"]["id"]
    assert admin.patch(f"/api/me/im/{cid}", json={"values": {"token": im_secret + "2"}}).status_code == 200
    assert admin.delete(f"/api/me/im/{cid}").status_code == 200
    bob.post("/api/me/im", json={"kind": "fake", "values": {"bot": "b2", "token": "x"}})     # 个人的：不进审计

    rows = admin.get("/api/admin/audit").json()["entries"]
    assert [r["action"] for r in rows] == ["im.delete", "im.update", "im.create", "plugins.policy", "user.reset_link",
                                           "models.save"]
    assert all(r["actor"] == admin_id and r["actor_email"] == "admin@x.io" and r["at"] for r in rows)
    assert rows[4]["target"] == bob_id and rows[0]["target"] == cid
    assert rows[5]["detail"] == {"models": 1, "providers": ["ds"], "keys_set": ["ds"]}
    raw = (tmp_path / "data" / "audit.jsonl").read_text(encoding="utf-8")
    assert key not in raw and im_secret not in raw                          # 不记密钥
    assert bob.get("/api/admin/audit").status_code == 404                   # 只给管理员


def test_audit_log_only_appends(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("u1", "a@x.io", "plugins.policy")
    first = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    log.record("u1", "a@x.io", "user.reset_link", "u2")
    assert (tmp_path / "audit.jsonl").read_text(encoding="utf-8").startswith(first)   # 前面的不动
    (tmp_path / "audit.jsonl").write_text(first + "坏行\n" + json.dumps({"action": "x"}) + "\n", encoding="utf-8")
    assert [r["action"] for r in log.recent()] == ["x", "plugins.policy"]
    assert all(v for v in ACTIONS.values())
