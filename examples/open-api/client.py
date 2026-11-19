"""业务系统对接 Matrix 的示例客户端（docs/open-api-integration.md）：只用 Python 标准库。

    MATRIX_URL=http://127.0.0.1:8765 MATRIX_TOKEN=mat_… python examples/open-api/client.py agents
    python examples/open-api/client.py build  "报销流程" <入口 Agent id> [别的成员 id …]   # 建项目并发布服务
    python examples/open-api/client.py chat   <服务 id> "我要报销 300 元" [session_id]
    python examples/open-api/client.py usage  <项目 id> <服务 id>
    python examples/open-api/client.py delete <项目 id>                                   # 只能删经这个应用建的项目

令牌是管理员（业务系统账号）或用户（代表个人）在 Matrix 里签的，只显示一次。不要把它写进代码仓库。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

BASE = os.environ.get("MATRIX_URL", "http://127.0.0.1:8765").rstrip("/")
TOKEN = os.environ.get("MATRIX_TOKEN", "")


class MatrixError(Exception):
    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(f"HTTP {status}: {body.get('detail')}")
        self.status, self.body = status, body


def _open(method: str, path: str, body: Any = None, raw: bytes | None = None, timeout: float = 60):
    data = raw if raw is not None else (json.dumps(body, ensure_ascii=False).encode() if body is not None else None)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if raw is None and body is not None:
        headers["Content-Type"] = "application/json"
    path = urllib.parse.quote(path, safe="/")  # 资料路径可以有中文：按 URL 编码
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except ValueError:
            detail = {"detail": e.reason}
        raise MatrixError(e.code, detail) from None


def call(method: str, path: str, body: Any = None, raw: bytes | None = None) -> Any:
    with _open(method, path, body, raw) as r:
        return json.loads(r.read().decode("utf-8"))


def call_confirmed(method: str, path: str, body: dict[str, Any]) -> Any:
    """要确认的操作（用模板、装插件）：第一次返回 409 和要确认的事，看过后带上 digest 再来一次。"""
    try:
        return call(method, path, body)
    except MatrixError as e:
        if e.status != 409 or "digest" not in e.body:
            raise
        print("要确认的事：", *e.body.get("confirm", []), sep="\n  - ")
        return call(method, path, {**body, "digest": e.body["digest"]})


def chat(service_id: str, message: str, session_id: str | None = None) -> Iterator[tuple[str, dict[str, Any]]]:
    """调用服务：读 SSE，逐个给出 (事件, 数据)。第一个总是 session。"""
    r = _open("POST", f"/svc/agents/{service_id}/chat", {"message": message, "session_id": session_id}, timeout=960)
    with r:
        event, data = "message", []
        for raw in r:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
            elif not line and data:
                yield event, json.loads("\n".join(data))
                event, data = "message", []


def main(argv: list[str]) -> None:
    if not TOKEN:
        sys.exit("先设置 MATRIX_TOKEN（在 Matrix 里签的应用令牌）")
    cmd = argv[0] if argv else "agents"
    if cmd == "agents":
        for a in call("GET", "/open/v1/agents"):
            print(f"{a['id']}  {a['name']}  [{a['kind']}]  {a['description']}")
    elif cmd == "build":
        name, entry, *others = argv[1:]
        p = call("POST", "/open/v1/projects", {"name": name, "brief": f"{name}：由业务系统建", "members": [entry, *others]})
        call("PUT", f"/open/v1/projects/{p['id']}/files/公开/说明.md", raw="这里放给服务读的资料。".encode())
        s = call("POST", f"/open/v1/projects/{p['id']}/services",
                 {"name": "intake", "title": f"{name}服务", "description": "业务系统发布的服务", "entry": entry,
                  "staff": others,                        # 入口能委托谁：不写就是项目里全部 Agent（含负责人等人的助理）
                  "authority": ["delegate", "web.research"],  # 服务授予的权限：不写就是没有权限（入口不能委托）
                  "scope": ["公开"], "inbox": "收件", "public": True})
        print(json.dumps({"project": p["id"], "service": s["id"]}, ensure_ascii=False))
    elif cmd == "chat":
        sid, msg = argv[1], argv[2]
        for ev, data in chat(sid, msg, argv[3] if len(argv) > 3 else None):
            print(ev, json.dumps(data, ensure_ascii=False))
    elif cmd == "delete":
        print(json.dumps(call("DELETE", f"/open/v1/projects/{argv[1]}"), ensure_ascii=False))
    elif cmd == "usage":
        print(json.dumps(call("GET", f"/open/v1/projects/{argv[1]}/services/{argv[2]}/usage"), ensure_ascii=False, indent=1))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except MatrixError as e:
        sys.exit(str(e))
