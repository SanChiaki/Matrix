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
ALLOWED = [n for n in os.environ.get("AGENTNET_TOOLS", "").split(",") if n]  # 这次执行能用的工具；空 = 全部
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
    # ---- 群组档案：群组成员都能用，不需要 delegate。联合群组里写进去的内容对所有参与方可见 ----
    {
        "name": "record_decision",
        "description": ("把群组里定下来的一件事记进群组档案。群组所有成员此后每一轮都会看到仍然有效的决定。"
                        "要推翻旧决定，写 supersedes 为旧决定的 id。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "决定的内容：一两句话"},
                "reason": {"type": "string", "description": "为什么这样定"},
                "supersedes": {"type": "string", "description": "可选：被取代的旧决定 id"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_decisions",
        "description": "列出群组档案里的决定。默认只列仍然有效的。",
        "inputSchema": {
            "type": "object",
            "properties": {"include_inactive": {"type": "string", "description": "写 true 时包含已取代 / 撤销的"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "publish_file",
        "description": ("把当前工作目录里的一个文件发布进群组档案（同名覆盖）。群组所有成员此后都能在 ./group/ 读到。"
                        "群组属于某个项目时，target=project 发布到项目文件，项目里所有群组此后都能在 ./project/ 读到。"
                        "只发布与工作有关、验收过的内容。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作目录里的相对路径；是目录时打成一个 zip 发布"},
                "name": {"type": "string", "description": "可选：在档案里的文件名"},
                "target": {"type": "string", "enum": ["group", "project"],
                           "description": "可选：group（默认，群组档案）或 project（群组所属项目的文件）"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "handoff",
        "description": ("项目里需要别的方面（另一个群组）接着做时，提交一次交接：写清要对方做什么、依据哪些项目文件、"
                        "交回什么。由人确认（可能修改）后，以人的名义发进对方群聊并 @ 对方成员；你不能直接跨群组委托。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "group": {"type": "string",
                          "description": "目标群组的名称（同一项目里的其他群组）；在联合群组里留空，由项目负责人分派"},
                "agent": {"type": "string", "description": "目标群组里接手的成员名称；在联合群组里留空"},
                "message": {"type": "string", "description": "交接内容：背景、要做什么、依据的文件（如 project/xxx）、交回什么"},
                "after": {"type": "string",
                          "description": "可选：要等哪些交接完成后再发出（交接 id，多个用逗号分隔），例如测试要等开发交付"},
                "replaces": {"type": "string",
                             "description": "可选：替换一个还没完成的、发给同一群组的交接（id）；人确认后旧的作废，等旧的交接改为等这条"},
            },
            "required": ["message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "review_handoff",
        "description": ("项目负责人确认、不采纳或转交人处理一条待确认的交接。确认后以项目所有者的名义发进对方群聊并 @ 对方成员；"
                        "超出网络设的边界时会自动转给人。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handoff_id": {"type": "string", "description": "交接 id"},
                "decision": {"type": "string", "enum": ["confirm", "decline", "escalate", "send_now"],
                             "description": "confirm 确认；decline 不采纳（提交者会被唤醒）；escalate 交给人；send_now 立即发出你确认过的排队交接"},
                "message": {"type": "string", "description": "可选：确认时改写的交接内容"},
                "group": {"type": "string", "description": "确认时交给哪个群组（联合群组提交的交接必须指定，只能是内部群组）"},
                "agent": {"type": "string", "description": "确认时接手的成员（目标群组里的名称；换了群组时必须指定）"},
                "after": {"type": "string", "description": "可选：确认时改依赖（交接 id，逗号分隔；空字符串表示不等）"},
                "replaces": {"type": "string", "description": "可选：确认时替换的、还没发出的交接 id"},
                "note": {"type": "string", "description": "不采纳或交给人时的理由"},
            },
            "required": ["handoff_id", "decision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "share_file",
        "description": ("项目负责人把项目文件共享给项目里的一个联合群组；那里的所有参与方此后都读得到（包括之后的新版本）。"
                        "写 handoff 时是交回那个联合群组提交的交接：共享并通知提交者。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "项目文件名（project/ 下的）；交回时可以写多个，逗号分隔"},
                "group": {"type": "string", "description": "联合群组的名称（写了 handoff 时不用写）"},
                "handoff": {"type": "string", "description": "可选：要交回的交接 id（联合群组提交的）"},
                "note": {"type": "string", "description": "可选：交回时给对方的说明（不要写项目内部的细节）"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "review_request",
        "description": ("项目负责人验收一个由它确认的交接引出的、已交付的任务：验收、退回（写明要改什么）或交给人。"
                        "验收后等它完成的交接会自动发出。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "任务 id（req-…）"},
                "decision": {"type": "string", "enum": ["accept", "changes", "escalate"],
                             "description": "accept 验收；changes 退回返工；escalate 交给人验收"},
                "feedback": {"type": "string", "description": "退回时要改什么；交给人时的理由"},
            },
            "required": ["request_id", "decision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_brief",
        "description": "建议修改群组 brief（目标、范围、成功标准、当前阶段）。写完整的新 brief；主持人采纳后才生效。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "完整的新 brief"},
                "reason": {"type": "string", "description": "为什么要改"},
            },
            "required": ["text", "reason"],
            "additionalProperties": False,
        },
    },
    # ---- 人交代的任务（看板）：群聊、私聊的席位都有 ----
    {
        "name": "open_request",
        "description": ("把人这一轮交代给你的事登记为任务，出现在他的看板上。只登记有交付目标的事（报告、结论、文件、改动）；"
                        "闲聊、问答、澄清直接回复，不要登记。来源消息由网络认定。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "任务标题：一句话说清要做什么"},
                "expected_outcome": {"type": "string", "description": "交付什么、以什么形式"},
            },
            "required": ["title", "expected_outcome"],
            "additionalProperties": False,
        },
    },
    {
        "name": "deliver_request",
        "description": "交付一个你登记的任务，由下发的人验收。你为它发起的委托都有结果之后才能交付。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "open_request 返回的任务 id"},
                "summary": {"type": "string", "description": "交付了什么、在哪里（文件路径、结论）"},
            },
            "required": ["request_id", "summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "accept_request",
        "description": "下发的人在本轮实际送达、@ 你的消息中明确表示通过后，代为验收你在当前对话已交付的任务。含糊、催促、继续修改不算，不得自行验收。",
        "inputSchema": {
            "type": "object",
            "properties": {"request_id": {"type": "string", "description": "你已交付、待验收的用户任务 id"}},
            "required": ["request_id"],
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
            # 只列出这次执行能用的工具；网络端仍会按执行上下文再检查一次
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [t for t in TOOLS if not ALLOWED or t["name"] in ALLOWED]}})
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
