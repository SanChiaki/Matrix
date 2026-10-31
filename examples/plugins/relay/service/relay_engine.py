"""「接力分派」插件服务：route 挂点的建议器（stdio MCP，仅标准库，不调模型）。

网络在项目群聊里有新消息时调 route_suggest，传入消息、成员（名字、描述、能力）与插件申请的建议种类，这里只返回建议：
- claim：人发的、没 @ 任何人的消息，按成员能力关键词在消息里出现的次数选一个认领人（Multica 式的分派）；
- wake：消息里写了“需要 X”“交给 X”“请 X”，X 是某个成员的名字或能力，就请他看一下（Raft 式的接力，但只叫被点到的）。
预算、链、谁能认领都由网络把关（docs/plugin-hooks.md 第 8 节）；这里不记状态。
"""

from __future__ import annotations

import json
import re
import sys

HANDOFF = re.compile(r"(?:需要|交给|请)\s*([^\s，。、,.!！？?：:（）()]{1,8})")


def _matches(token: str, member: dict) -> bool:
    caps = [str(c) for c in member.get("capabilities") or []]
    return token == member.get("name") or any(token == c or token.startswith(c) or c.startswith(token) for c in caps)


def route_suggest(a: dict) -> dict:
    msg = a.get("message") or {}
    members = [m for m in a.get("members") or [] if isinstance(m, dict) and m.get("name")]
    may = set(a.get("may") or [])
    text = str(msg.get("text") or "")
    author = str(msg.get("author") or "")
    out: dict = {"claim": None, "wake": []}
    if "claim" in may and msg.get("author_kind") == "user" and not msg.get("mentions"):
        scored = []
        for m in members:
            hits = [c for c in m.get("capabilities") or [] if c and str(c) in text]
            if hits:
                scored.append((len(hits), m["name"], hits))
        if scored:
            scored.sort(key=lambda x: -x[0])
            n, name, hits = scored[0]
            out["claim"] = {"agent": name, "reason": f"消息里提到「{'」「'.join(map(str, hits[:3]))}」，是 {name} 的能力"}
    if "wake" in may:
        seen = set()
        for token in HANDOFF.findall(text):
            for m in members:
                if m["name"] != author and m["name"] not in seen and _matches(token, m):
                    seen.add(m["name"])
                    out["wake"].append({"agent": m["name"], "reason": f"消息里写了「{token}」"})
    return out


def explain(a: dict) -> str:
    return ("认领：人发的、没 @ 任何人的消息，按成员能力关键词出现次数选一个人。"
            "叫下一环：消息里写“需要 X / 交给 X / 请 X”，X 是成员名字或能力。预算与去重由网络把关。")


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    schema = {"type": "object", "properties": {"message": {"type": "object"}, "members": {"type": "array"},
                                               "may": {"type": "array"}}, "additionalProperties": True}
    tools = [{"name": "route_suggest", "description": "给群聊里的一条新消息建议认领人与下一环（只给建议）。",
              "inputSchema": schema, "annotations": {"readOnlyHint": True}},
             {"name": "explain", "description": "说明分派规则。", "inputSchema": {"type": "object", "properties": {}},
              "annotations": {"readOnlyHint": True}}]
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue
        p = msg.get("params") or {}
        if method == "initialize":
            result: dict = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                            "serverInfo": {"name": "matrix-relay", "version": "0.1.0"}}
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "tools/call":
            name, args = p.get("name"), p.get("arguments") or {}
            if name == "route_suggest":
                text, err = json.dumps(route_suggest(args), ensure_ascii=False), False
            elif name == "explain":
                text, err = explain(args), False
            else:
                text, err = f"没有工具 {name}", True
            result = {"content": [{"type": "text", "text": text}], "isError": err}
        elif method == "ping":
            result = {}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
