"""云端连接器的 stdio MCP 桥：由 DSH 的 dsh-mcp-client 拉起，把连接器工具的列出与调用转给网络。

只依赖标准库。运行时拿到的只有执行上下文令牌（MATRIX_TOKEN）：这一轮能用哪些连接器、带什么凭证，
都由网络按令牌认定并代为调用（docs/connectors-and-skills.md 4.2），凭证不经过这个进程。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("MATRIX_CONNECTOR_URL", "").rstrip("/")
TOKEN = os.environ.get("MATRIX_TOKEN", "")
TIMEOUT = float(os.environ.get("MATRIX_TOOL_TIMEOUT", "600"))
PROTO = "2025-06-18"
# 回调的是本机网络：不走系统 / 环境里配置的代理
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(f"{URL}/{path}", data=json.dumps({"token": TOKEN, **body}).encode(),
                                 headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def list_tools() -> list[dict]:
    try:
        return list(post("list", {}).get("tools") or [])
    except Exception:  # noqa: BLE001 —— 列不出来就是这一轮没有连接器工具，不让整轮失败
        return []


def call(name: str, args: dict) -> tuple[str, bool]:
    try:
        data = post("call", {"tool": name, "args": args})
        return str(data.get("text", "")), bool(data.get("is_error"))
    except urllib.error.HTTPError as e:
        return f"网络拒绝了调用（HTTP {e.code}）：{e.read().decode(errors='replace')[:400]}", True
    except Exception as e:  # noqa: BLE001
        return f"无法连接网络：{e!r}", True


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": min((msg.get("params") or {}).get("protocolVersion") or PROTO, PROTO),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "matrix-connectors", "version": "0.1.0"},
            }})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": list_tools()}})
        elif method == "tools/call":
            p = msg.get("params") or {}
            text, is_error = call(str(p.get("name")), p.get("arguments") or {})
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
