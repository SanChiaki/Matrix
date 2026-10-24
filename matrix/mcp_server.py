"""matrix 的 stdio MCP 服务：由 DSH 的 dsh-mcp-client 拉起，把网络工具调用转发给网络。

只依赖标准库，任何支持 MCP 的 Agent 运行时都能用它接入网络。
调用方身份不由模型提供：网络在启动运行时时把执行上下文令牌写入 MATRIX_TOKEN。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("MATRIX_TOOL_URL", "")
TOKEN = os.environ.get("MATRIX_TOKEN", "")
TIMEOUT = float(os.environ.get("MATRIX_TOOL_TIMEOUT", "1800"))
ALLOWED = [n for n in os.environ.get("MATRIX_TOOLS", "").split(",") if n]  # 这次执行能用的工具；空 = 全部
PROTO = "2025-06-18"
# 回调的是本机网络（云侧服务或端侧连接器的本机桥）：不走系统 / 环境里配置的代理
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

TOOLS = [
    {
        "name": "list_members",
        "description": "列出你所在项目的其他成员：名称、描述、能力、权限、当前状态。委托前先用它发现谁能做。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "delegate",
        "description": ("把一个子任务委托给项目内的某个成员。立即返回任务 id，不等待结果；对方交付或失败时，"
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
                "attachments": {"type": "string",
                                "description": "转交给对方的附件：你工作目录里 attachments/ 下的相对路径，每行一个"},
            },
            "required": ["agent", "mission", "expected_outcome"],
            "additionalProperties": False,
        },
    },
    {
        "name": "share_knowledge",
        "description": "把一条你可见的知识显式共享给项目内的某个成员（对方此后在本对话发起的委托中可见）。",
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
        "description": ("项目里没有合适的成员时，提议新建一个 Agent。网络不会直接创建：人审核（可修改）后批准，"
                        "新 Agent 加入当前项目；结果会作为通知送给你。提议的权限不能超出你当前的有效权限。"),
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
    # ---- 项目档案：项目成员都能用，不需要 delegate。联合项目里写进去的内容对所有参与方可见 ----
    {
        "name": "record_decision",
        "description": ("把项目里定下来的一件事记进项目档案。项目所有成员此后每一轮都会看到仍然有效的决定。"
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
        "description": "列出项目档案里的决定。默认只列仍然有效的。",
        "inputSchema": {
            "type": "object",
            "properties": {"include_inactive": {"type": "string", "description": "写 true 时包含已取代 / 撤销的"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "publish_file",
        "description": ("把当前工作目录里的一个文件发布进项目档案（同名覆盖）。项目所有成员此后都能在 ./project/ 读到。"
                        "只发布与工作有关、验收过的内容。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作目录里的相对路径；是目录时打成一个 zip 发布"},
                "name": {"type": "string", "description": "可选：在档案里的文件名"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_brief",
        "description": "建议修改项目纲要（目标、范围、成功标准、当前阶段）。写完整的新纲要；主持人采纳后才生效。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "完整的新纲要"},
                "reason": {"type": "string", "description": "为什么要改"},
            },
            "required": ["text", "reason"],
            "additionalProperties": False,
        },
    },
    # ---- 插件记录：项目里装了把记录工具开给 Agent 的插件时才有（档案类，不需要 delegate） ----
    {
        "name": "plugin_records",
        "description": "查项目插件里的记录。插件名、表名与字段见推送的项目档案。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "plugin": {"type": "string", "description": "插件名，如 sales-pipeline"},
                "table": {"type": "string", "description": "表名，如 leads"},
                "query": {"type": "string", "description": "可选：关键词"},
                "where": {"type": "string", "description": "可选：JSON 对象字符串，按字段精确匹配，如 {\"stage\": \"沟通\"}"},
                "limit": {"type": "integer", "description": "可选：最多几条，默认 20，最多 100"},
            },
            "required": ["plugin", "table"],
            "additionalProperties": False,
        },
    },
    {
        "name": "plugin_write",
        "description": "新增或修改项目插件里的一条记录。只能写清单里的字段，网络按清单校验。写进去项目里所有人都看得到。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "plugin": {"type": "string", "description": "插件名"},
                "table": {"type": "string", "description": "表名"},
                "record_id": {"type": "string", "description": "修改时填记录 id；不填就是新增"},
                "fields": {"type": "string", "description": "JSON 对象字符串：要写的字段，如 {\"stage\": \"报价\"}"},
            },
            "required": ["plugin", "table", "fields"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_skill",
        "description": ("把一个值得反复用的做法提议为项目技能。主持人批准后进项目档案，项目里的成员此后都能用；"
                        "批准前不生效。技能只是做法说明，不授予任何权限。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名：小写英文、数字与连字符，如 weekly-report"},
                "description": {"type": "string", "description": "一句话：什么时候用它"},
                "body": {"type": "string", "description": "正文：怎么做（步骤、标准、注意事项）"},
                "reason": {"type": "string", "description": "为什么值得沉淀"},
            },
            "required": ["name", "description", "body", "reason"],
            "additionalProperties": False,
        },
    },
    # ---- 能力：缺连接器时提醒所有者（不授权、不安装）----
    {
        "name": "suggest_connector",
        "description": ("需要某个外部服务却没有对应的连接器时，提醒你的所有者去授权。只是提醒：不会授权、安装或开启任何东西，"
                        "这一轮继续用现有的能力。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "connector": {"type": "string", "description": "需要哪个服务，如 飞书文档、GitHub"},
                "reason": {"type": "string", "description": "拿它来做什么"},
            },
            "required": ["connector", "reason"],
            "additionalProperties": False,
        },
    },
    # ---- 项目会话与助理对话：助理读项目现状（按主人的可见性过滤）----
    {
        "name": "read_chat",
        "description": "读项目群聊的消息：按关键词找，或看最近的若干条。只读，看到的范围和你的所有者一样。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "助理对话里必填：项目名；项目会话里不用写"},
                "query": {"type": "string", "description": "可选：关键词，只返回含它的消息"},
                "limit": {"type": "string", "description": "可选：最多几条（1–50，默认 20），取最近的"},
            },
            "additionalProperties": False,
        },
    },
    # ---- 私有目录里没挂上的笔记（不变量 22）：只在这一轮有没挂上的笔记的云侧运行里出现 ----
    {
        "name": "open_note",
        "description": "读你私有目录里一篇没有挂上的笔记（写在别的场合）。读过以后，你的回复要先给所有者确认才会发出。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "私有目录说明里列出的文件名（相对 agent-home/ 的路径）"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_request",
        "description": "读项目里一个用户任务的详情：原始要求、状态、交付说明、为它发起的委托与结果。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "任务 id（req-…）"},
                "project": {"type": "string", "description": "可选：项目名（不写就按任务所在的项目）"},
            },
            "required": ["request_id"],
            "additionalProperties": False,
        },
    },
    # ---- 助理对话：起草（主人确认后以主人的名义发出）与主人的定时 ----
    {
        "name": "draft_message",
        "description": "替所有者起草一条发进某个项目群聊的消息。不会直接发出：所有者确认（可以改）后以所有者的名义发出。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "项目名：所有者参与的项目"},
                "text": {"type": "string", "description": "消息全文；要唤醒某个成员就写 @成员名"},
            },
            "required": ["project", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "schedule",
        "description": "按所有者的话建一个定时：到点时网络在你们的私聊里唤醒你。cron 与 after_minutes 二选一。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "定时叫什么（如：早报）"},
                "instruction": {"type": "string", "description": "到点时要你做什么，写成交给你自己的话"},
                "cron": {"type": "string", "description": "重复的：五段 cron（分 时 日 月 周，本地时间），如 0 9 * * 1-5"},
                "after_minutes": {"type": "string", "description": "一次性的：过多少分钟（至少 10）"},
            },
            "required": ["title", "instruction"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_schedules",
        "description": "列出所有者的定时。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "cancel_schedule",
        "description": "按所有者的话取消一个定时。",
        "inputSchema": {
            "type": "object",
            "properties": {"schedule_id": {"type": "string", "description": "定时 id（sched-…）"}},
            "required": ["schedule_id"],
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
        with _OPENER.open(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        return str(data.get("text", "")), bool(data.get("is_error"))
    except urllib.error.HTTPError as e:
        return f"网络拒绝了调用（HTTP {e.code}）：{e.read().decode(errors='replace')[:400]}", True
    except Exception as e:  # noqa: BLE001
        return f"无法连接网络：{e!r}", True


def main() -> None:
    # MCP stdio 是 UTF-8、按换行分消息；Windows 上管道默认用区域编码（如 GBK），换行写成 CRLF
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
                # 只实现 2025-06-18 语义；更新的客户端会回落到这个版本，更旧的则按其请求回应。
                "protocolVersion": min((msg.get("params") or {}).get("protocolVersion") or PROTO, PROTO),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "matrix", "version": "0.2.0"},
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
