"""agentnet 的 stdio MCP 服务：由 DSH 的 dsh-mcp-client 拉起，把网络工具调用转发给网络。

只依赖标准库，任何支持 MCP 的 Agent 运行时都能用它接入网络。
调用方身份不由模型提供：网络在启动运行时时把执行上下文令牌写入 AGENTNET_TOKEN。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("AGENTNET_TOOL_URL", "")
TOKEN = os.environ.get("AGENTNET_TOKEN", "")
TIMEOUT = float(os.environ.get("AGENTNET_TOOL_TIMEOUT", "1800"))
PROTO = "2025-06-18"

TOOLS = [
    {
        "name": "list_members",
        "description": "列出你所在群组的其他成员：名称、描述、能力、权限、当前状态。委托前先用它发现谁能做。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "delegate",
        "description": ("把一个子任务委托给群组内的某个成员。立即返回任务 id，不等待结果；对方交付或失败时，"
                        "网络会把结果作为通知在新的一轮里送给你。只能委托 list_members 返回的成员。"
                        "对方的实际权限 = 对方自身权限 ∩ 你在 authority 里授予的权限；你的约束会被继承。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "执行成员的名称（来自 list_members）"},
                "mission": {"type": "string", "description": "目标：为什么做、做什么"},
                "authority": {"type": "array", "items": {"type": "string"},
                              "description": "授予的权限，只能收窄对方自身权限；省略表示不额外收窄"},
                "constraints": {"type": "string", "description": "不能做什么，每行一条"},
                "expected_outcome": {"type": "string", "description": "需要交回什么、以什么形式交付"},
                "share": {"type": "array", "items": {"type": "string"},
                          "description": "显式共享给对方的知识 key（只能是你可见的知识）"},
            },
            "required": ["agent", "mission", "expected_outcome"],
            "additionalProperties": False,
        },
    },
    {
        "name": "share_knowledge",
        "description": "把一条你可见的知识显式共享给群组内的某个成员（对方此后在本对话发起的委托中可见）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "知识 key"},
                "agent": {"type": "string", "description": "接收成员名称"},
            },
            "required": ["key", "agent"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_tasks",
        "description": "列出你发起的委托及其状态（running / in_review / completed / failed / rejected）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "accept_task",
        "description": "验收一个处于 in_review 的委托：接受交付，任务完成，结果沉淀为知识。",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "委托的任务 id"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_changes",
        "description": "退回一个处于 in_review 的委托：执行者会在原会话里看到你的意见并重新交付。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "委托的任务 id"},
                "feedback": {"type": "string", "description": "要修改什么、为什么"},
            },
            "required": ["task_id", "feedback"],
            "additionalProperties": False,
        },
    },
    {
        "name": "retry_task",
        "description": "续接一个失败的委托：执行者在原会话里接着做，已完成的部分不会丢。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "委托的任务 id"},
                "note": {"type": "string", "description": "可选：给执行者的补充说明"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_agent",
        "description": ("群组里没有合适的成员时，提议新建一个 Agent。网络不会直接创建：人审核（可修改）后批准，"
                        "新 Agent 加入当前群组；结果会作为通知送给你。提议的权限不能超出你当前的有效权限。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "新 Agent 的名称"},
                "description": {"type": "string", "description": "它做什么；其他 Agent 会据此判断能否委托"},
                "capabilities": {"type": "array", "items": {"type": "string"}, "description": "能力标签"},
                "authority": {"type": "array", "items": {"type": "string"},
                              "description": "建议的自身权限，只能是你有效权限的子集"},
                "reason": {"type": "string", "description": "为什么现有成员不够"},
            },
            "required": ["name", "description", "reason"],
            "additionalProperties": False,
        },
    },
]


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def call_network(name: str, args: dict) -> tuple[str, bool]:
    body = json.dumps({"token": TOKEN, "tool": name, "args": args}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        return str(data.get("text", "")), bool(data.get("is_error"))
    except urllib.error.HTTPError as e:
        return f"网络拒绝了调用（HTTP {e.code}）：{e.read().decode(errors='replace')[:400]}", True
    except Exception as e:  # noqa: BLE001
        return f"无法连接网络：{e!r}", True


def main() -> None:
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
                # 只实现 2025-06-18 语义；更新的客户端会回落到这个版本，更旧的则按其请求回应。
                "protocolVersion": min((msg.get("params") or {}).get("protocolVersion") or PROTO, PROTO),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agentnet", "version": "0.2.0"},
            }})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            p = msg.get("params") or {}
            text, is_error = call_network(str(p.get("name")), p.get("arguments") or {})
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
