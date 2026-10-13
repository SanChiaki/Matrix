"""提示词构造：对话（人 ↔ Agent）与委托任务（Agent ↔ Agent）。

Agent 不再输出结构化协议：委托通过网络工具（MCP）完成，最终回复就是交付内容。
"""

from __future__ import annotations

from .models import AUTHORITIES, MCP_PREFIX, AgentNode, Conversation, Group, KnowledgeItem, Task


def authority_lines(authority: list[str]) -> str:
    if not authority:
        return "- （无工具权限：只能基于已提供的信息推理）"
    return "\n".join(f"- `{a}`：{AUTHORITIES.get(a, a)}" for a in authority)


def _identity(agent: AgentNode) -> list[str]:
    parts = [
        f"你是 Agent Network 中的一个节点：**{agent.name}**（id: {agent.id}）。",
        f"你的能力：{', '.join(agent.capabilities) or '（未声明）'}。{agent.description}",
    ]
    return parts


def _network_tools(group: Group, can_delegate: bool) -> list[str]:
    if not can_delegate:
        return ["", "## 协作", "- 你在这里没有委托能力，亲自完成即可。"]
    return [
        "", f"## 协作：你所在的群组「{group.name}」",
        f"- 用 `{MCP_PREFIX}list_members` 查看群组成员及其能力；只能委托这些成员，网络不会为你创建新 Agent。",
        f"- 用 `{MCP_PREFIX}delegate` 委托：写清 mission、expected_outcome（交回什么、以什么形式），"
        "可选 authority（只能收窄对方自身权限）、constraints（不能做什么，每行一条）、share（共享给对方的知识 key）。工具会阻塞直到对方交回结果。",
        "- 多个相互独立的委托可以在同一轮里并行调用。",
        f"- 用 `{MCP_PREFIX}share_knowledge` 把你可见的知识显式共享给某个成员；默认任何知识都不会自动共享。",
        "- 不属于你自身能力的部分交给合适的成员，不要硬做；结果返回后由你判断是否继续委托。",
        "- 不要使用 subagent / workflow 之类的工具，协作只通过网络工具进行。",
    ]


def _knowledge(items: list[KnowledgeItem]) -> list[str]:
    if not items:
        return []
    parts = ["", "## 你可见的共享知识（已写入 ./shared/）"]
    for k in items:
        parts += [f"### [{k.key}] {k.title}", k.content[:3000]]
    return parts


def build_conversation_prompt(agent: AgentNode, conv: Conversation, group: Group | None, can_delegate: bool,
                              knowledge: list[KnowledgeItem], text: str, first: bool) -> str:
    if not first:
        return "\n".join([*_knowledge(knowledge), "", "## 用户", text]) if knowledge else text
    parts = [
        *_identity(agent), "",
        "你正在与一位人类用户持续对话。这是一个长期会话：后续消息会在同一会话里继续。",
        "", "## 你的权限 —— 运行时只启用了这些工具，越权调用会被记录为违规",
        authority_lines(agent.authority),
    ]
    if group is not None:
        parts += _network_tools(group, can_delegate)
    parts += [*_knowledge(knowledge), "",
              "回复直接面向用户，结论要有依据，信息缺口要标注。", "", "## 用户", text]
    return "\n".join(parts)


def build_task_prompt(task: Task, agent: AgentNode, delegator: AgentNode, group: Group, can_delegate: bool,
                      knowledge: list[KnowledgeItem]) -> str:
    d = task.delegation
    parts = [
        *_identity(agent), "",
        "你收到一份委托。在授权范围内自主完成目标，最终回复就是交付给委托方的结果。",
        "", "## 委托",
        f"- 委托方：{delegator.name}",
        f"- 任务 id：{task.id}",
        f"- 目标（Mission）：{d.mission}",
        f"- 输出要求（Expected Outcome）：{d.expected_outcome}",
        "", "## 实际权限（你自身权限 ∩ 委托授予）—— 运行时只启用了这些工具",
        authority_lines(task.granted_authority),
        "", "## 约束（Constraints，累加自整条委托链）",
        *(f"- {c}" for c in task.effective_constraints or ["（无）"]),
        *_network_tools(group, can_delegate),
        *_knowledge(knowledge),
        "", "## 交付",
        "- 第一行写一句话结论，然后是正文（精炼，800 字以内），最后列出证据来源（URL / 文件 / 子任务 id）。",
        "- 工具调用控制在 6 次以内；无法确认的内容要标注。",
    ]
    return "\n".join(parts)


def format_task_result(task: Task, agent_name: str) -> str:
    """delegate 工具返回给委托方的文本。"""
    head = [f"任务 {task.id} → {agent_name}：{task.status.value}",
            f"实际权限：{', '.join(task.granted_authority) or '无'}"
            + (f"；未授予：{', '.join(task.denied_authority)}" if task.denied_authority else "")]
    if task.violations:
        head.append("越权记录：" + "；".join(task.violations))
    if task.error:
        return "\n".join(head + [f"失败原因：{task.error}"])
    r = task.result or {}
    if r.get("knowledge_key"):
        head.append(f"结果已沉淀为知识 `{r['knowledge_key']}`，可在后续委托的 share 中引用。")
    return "\n".join(head + ["", str(r.get("report", ""))[:6000]])
