"""提示词构造：对话（人 ↔ Agent）、委托任务（Agent ↔ Agent）、网络通知与失败说明。

Agent 不输出结构化协议：委托通过网络工具（MCP）完成，最终回复就是交付内容。
委托是异步的：delegate 立即返回，结果作为网络通知在新的一轮里送达。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Callable

from .models import (AGENT_HOME as HOME, ARCHIVE_TOOLS, ATTACHMENTS, AUTHORITIES, CONNECTOR_PREFIX, DECISIONS_PUSHED,
                     MCP_PREFIX, PLUGIN_TOOLS, PROJECT_FILES, SKILLS, ABILITY_TOOLS,
                     REQUEST_TOOLS, SESSION_TOOLS, AgentNode, Conversation, Decision, KnowledgeItem, Message, Project,
                     Proposal, Task, UserRequest, attachment_rel)


def authority_lines(authority: list[str]) -> str:
    if not authority:
        return "- （无工具权限：只能基于已提供的信息推理）"
    return "\n".join(f"- `{a}`：{AUTHORITIES.get(a, a)}" for a in authority)


def _identity(agent: AgentNode) -> list[str]:
    return [
        f"你是 Agent 协作网络 Matrix 中的一个节点：**{agent.name}**（id: {agent.id}）。",
        f"你的能力：{', '.join(agent.capabilities) or '（未声明）'}。{agent.description}",
    ]


def _home(writable: bool, mounted: bool = True) -> list[str]:
    if mounted:
        how = "可以读写" if writable else "只能读（你没有 file.write）"
        home = ["", "## 你的私有目录",
                f"- `./{HOME}/` 是只属于你的目录，跨对话、跨任务保留，你{how}。网络不会读取或替你整理它。",
                "- 想记住什么（经验、偏好、常用资料）由你自己决定；需要时自己去读。",
                "- 你可能同时在做别的工作：别的运行可能同时改动这里的文件，冲突时你的版本会另存为 `*.conflict-*` 副本。"]
    else:
        home = ["", "## 你的私有目录",
                "- 这里是联合项目，你的私有目录没有挂载（你的所有者可以为这个项目打开）。不要设法读取或复述它的内容。"]
    return home + [
            "", "## 工作区边界",
            "- 你的写操作只限于当前工作目录。需要写工作目录以外时，照沙箱拒绝后的提示带理由申请升权重试一次；"
            "申请会交给你的所有者审批。被拒绝就停下来说明，不要绕过。"]


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def format_attachments(items: list[dict]) -> list[str]:
    """附件只给一行路径（只读副本，在工作目录里），内容由模型用自己的工具去读。"""
    return [f"- 附件 `{attachment_rel(a)}`（{a.get('mime', '')}，{_size(int(a.get('size', 0)))}）" for a in items]


def _attachments_note() -> list[str]:
    return ["", "## 附件",
            f"- 人发来的附件以只读副本放在 `./{ATTACHMENTS}/` 下，消息里写明每个文件的路径；需要时用你自己的工具读。"
            "这里的改动不会保存。"]


def format_ledger(items: list[dict], hidden: int = 0) -> str:
    """工作台账：这个 Agent 此刻同时在做的其他工作。hidden：不在这个信任域里、不列细节的条目数。"""
    lines = ["## 你同时还在处理（工作台账）",
            "这些是你在别的会话里正在进行的工作。它们与这一轮互不相干：不要改动它们的产物，也不要把它们的结论当成这次的输入。"]
    for it in items:
        who = f"（为 {it['user']}）" if it.get("user") else ""
        if it["kind"] == "task":
            lines.append(f"- 委托 {it['context_id']}{who}：{it.get('for_agent', '')} 交给你的「{it.get('what', '')}」")
        else:
            lines.append(f"- {it.get('what', '对话')}{who}")
    if hidden:
        lines.append(f"- 另有 {hidden} 项不在这个项目里的工作（不在这里谈论）")
    return "\n".join(lines)


# 网络工具 → 提示词里的一行用法。键必须与 mcp_server.TOOLS 一致（测试保证）。
TOOL_GUIDE: dict[str, str] = {
    "list_members": "查看项目成员及其能力；只能委托这些成员。",
    "delegate": ("委托一个成员：写清 mission、expected_outcome（交回什么、以什么形式），可选 authority（只能收窄对方自身权限）、"
                 "constraints（不能做什么，每行一条）、share（共享给对方的知识 key）、"
                 f"attachments（转交给对方的附件：`{ATTACHMENTS}/` 下的路径，每行一个）。**立即返回任务 id，不等待结果**。"),
    "list_tasks": "查看你发起的委托及其状态。",
    "accept_task": "接受一个已交付（in_review）的委托。",
    "request_changes": "退回一个已交付的委托并写明要改什么，对方会在原会话里修改后重新交付。",
    "retry_task": "续接一个失败的委托，对方在原会话里接着做，已完成的部分不会丢。",
    "share_knowledge": "把你可见的知识显式共享给某个成员；默认任何知识都不会自动共享。",
    "propose_agent": "项目里没有合适的成员时，提议新建一个 Agent（权限不超出你的有效权限），由人审核；批准后才能委托它。",
    "record_decision": "把项目里定下来的事记进项目档案（内容、理由；可写 supersedes 取代旧决定）。所有成员此后都会看到。",
    "list_decisions": "查看项目档案里的全部决定（include_inactive 包含已取代 / 撤销的）。",
    "publish_file": ("把当前工作目录里的一个文件（或一个目录，打成 zip）发布进项目档案，所有成员此后在 `./project/` 读到"
                     "（只发布别人要用、已经定稿的交付物）。"),
    "propose_brief": "建议修改项目纲要（写完整的新纲要和理由），主持人决定。",
    "propose_skill": ("把一个值得反复用的做法提议为项目技能（name：小写英文与连字符，description：什么时候用，"
                      "body：怎么做，reason：为什么值得沉淀），主持人批准后项目里的成员才能用。"),
    "suggest_connector": ("需要某个外部服务（如飞书、GitHub、某个数据库）却没有对应的连接器时，提醒你的所有者"
                          "（connector：哪个服务，reason：拿来做什么）。只是提醒，不会授权或安装；提醒完用现有的能力继续。"),
    "read_chat": "读项目群聊的消息（query 按关键词找，limit 条数，默认最近 20 条）：了解群聊里交代过、讨论过、定下过什么。",
    "plugin_records": ("查项目插件里的记录（plugin：插件名，table：表名，query：关键词，where：JSON 字符串按字段精确匹配，"
                       "如 {\"stage\": \"沟通\"}，limit 条数）。只有项目档案里列出的插件与表。"),
    "plugin_write": ("新增或修改项目插件里的一条记录（plugin、table，record_id：修改时填，fields：JSON 字符串，"
                     "只写清单里的字段，如 {\"stage\": \"报价\"}）。写进去项目里所有人都看得到。"),
    "read_request": "读项目里一个用户任务的详情（request_id）：原始要求、状态、交付说明、为它发起的委托与结果。",
    "open_request": "人交代给你一件有交付目标的事时，先登记为任务（title、expected_outcome），它会出现在下发人的看板上。",
    "accept_request": "仅当下发的人在本轮实际送达、@ 你的消息中明确验收你已交付的任务时，按其指示验收（request_id）。不能自行验收、退回或取消。",
    "deliver_request": "任务做完后交付（request_id、summary：交付了什么、在哪里），由下发的人验收。",
}


def _network_tools(project: Project, can_delegate: bool, plugins: bool = False) -> list[str]:
    archive = [
        "", f"## 项目档案：「{project.name}」",
        *(f"- `{MCP_PREFIX}{name}`：{TOOL_GUIDE[name]}" for name in ARCHIVE_TOOLS),
        f"- `./{PROJECT_FILES}/` 是项目档案文件的只读副本，改动不会保存。",
        *((f"- `{MCP_PREFIX}{name}`：{TOOL_GUIDE[name]}" for name in PLUGIN_TOOLS) if plugins else ()),
    ]
    if project.kind == "joint":
        archive.append("- 这是联合项目：写进档案的内容、你在群聊里的回复，所有参与方（不同的人）都看得到。"
                       "只写和这个项目的工作有关的内容，不要带出你在别处知道的事。")
    if not can_delegate:
        return archive + ["", "## 协作", "- 你在这里没有委托能力，亲自完成即可。"]
    joint = ["- 联合项目里的成员属于不同的人：委托别人的成员，对方以它的所有者的名义、用它的所有者的资源执行，"
             "规则和内部项目相同。"] if project.kind == "joint" else []
    return archive + [
        "", f"## 协作：你所在的项目「{project.name}」",
        *(f"- `{MCP_PREFIX}{name}`：{text}" for name, text in TOOL_GUIDE.items()
          if name not in ARCHIVE_TOOLS + REQUEST_TOOLS + SESSION_TOOLS + ABILITY_TOOLS + PLUGIN_TOOLS),
        *joint,
        "",
        "委托是异步的：",
        "- 一次工具调用只发一个委托，发完一个再发下一个；不需要也不要在同一条消息里并行发多个。",
        "- 发完委托就可以结束这一轮：简短说明你委托了什么、在等什么。不要空等，也不要猜测结果。",
        "- 对方交付时，它工作目录里的产物会拷进你工作目录的 `deliveries/<任务 id>/`。",
        "- 成员交付或失败时，网络会在新的一轮里把结果作为通知送给你。届时逐个验收：满意就 accept_task，"
        "需要修改就 request_changes；这一轮结束时仍未处理的交付视为接受。",
        "- 先判断自己能不能做：在你能力范围内的直接做；只把确实需要别的能力的部分委托给合适的成员，不要为了分工而分工。",
        "- 与项目成员协作只能通过网络工具；你自己的 subagent 只是替你干活的帮手，"
        "不能代替委托，权限也不会超出你自己的权限。",
    ]


def _abilities() -> list[str]:
    return [
        "", "## 连接器与技能",
        f"- 连接器是你的所有者授权的外部服务。这一轮能用哪些会另外告诉你，它们的工具以 `{CONNECTOR_PREFIX}` 开头；"
        "没告诉你的就是没有，不要自己安装、配置或找凭证。",
        *(f"- `{MCP_PREFIX}{name}`：{TOOL_GUIDE[name]}" for name in ABILITY_TOOLS),
        "- 技能是可复用的做法说明，按需要使用。技能不授予任何权限：写着要用的工具或服务，你没有就是没有。",
    ]


def format_abilities(cloud: list[tuple[str, str]], device: list[str],
                     skills: list[tuple[str, str]]) -> tuple[str, str]:
    """本轮可用的连接器与（需要在提示词里列出的）技能。返回 (说明, 指纹)；都没有时指纹为空。
    调用方只在指纹变化时把说明放进这一轮。"""
    if not (cloud or device or skills):
        return "## 本轮可用的连接器与技能\n- 之前可用的连接器与技能，这一轮都没有了。", ""
    key = hashlib.sha1(json.dumps([cloud, device, skills], ensure_ascii=False).encode()).hexdigest()[:16]
    parts = ["## 本轮可用的连接器与技能"]
    if cloud:
        parts.append("- 连接器（你的所有者授权，网络代为调用）：" +
                     "、".join(f"{n}（工具以 `{CONNECTOR_PREFIX}{prefix}` 开头）" for n, prefix in cloud))
    if device:
        parts.append("- 连接器（你所在电脑上已经配好的服务）：" + "、".join(device))
    if not (cloud or device):
        parts.append("- 这一轮没有可用的连接器。")
    if skills:
        parts += [f"- 技能（正文在 `./{SKILLS}/<名字>/SKILL.md`，用之前先读）：",
                  *(f"  - {n}：{d}" for n, d in skills)]
    return "\n".join(parts), key


def _requests() -> list[str]:
    return [
        "", "## 人交代的任务（看板）",
        *(f"- `{MCP_PREFIX}{name}`：{TOOL_GUIDE[name]}" for name in REQUEST_TOOLS),
        "- 先判断这条消息是不是任务：有明确的交付目标（一份报告、一个结论、一组文件、一项改动）才是；"
        "闲聊、打招呼、简单问答、追问澄清都不是，直接回复，不要登记。",
        "- 是任务就在开始做之前 open_request；能在这一轮做完的，做完后 deliver_request。"
        "需要委托别人的，等委托都有了结果、你整合完再 deliver_request。",
        "- accept_request 只用于你在当前对话接下、已交付的任务：以下发人本轮最后一条送达且 @ 你的消息为依据，"
        "只有明确说通过、可以了、就这样、验收才调用；含糊、催促、继续改都不算。没有明确授权不要调用，"
        "不要把验收消息登记成新任务；不确定对应哪个任务时先澄清。网络只校验来源、轮次与席位，语义由你判断。",
        "- 一条消息只登记一次。下发的人退回时，在原有工作上修改，然后用同一个 request_id 重新交付。",
    ]


def format_open_requests(items: list[UserRequest]) -> str:
    lines = ["## 你在这个对话里接下、还没结束的任务"]
    for r in items:
        state = "进行中" if r.status == "in_progress" else "已交付，等验收"
        lines.append(f"- {r.id}（{state}）：{r.title} —— 交付：{r.expected_outcome}")
    return "\n".join(lines)


def format_request_changes(r: UserRequest, who: str) -> str:
    return "\n".join([f"## 网络通知：{who} 退回了 {r.id}（第 {r.revisions} 次），要求修改", f"任务：{r.title}",
                      f"修改意见：{r.feedback}", "", f"在原有工作上修改，然后用 deliver_request 重新交付（request_id: {r.id}）。"])


def _writing() -> list[str]:
    return ["", "## 写作",
            "- 长文档分节写：先写骨架，再逐节追加（edit），不要一次写入上万字。"]


def _knowledge(items: list[KnowledgeItem]) -> list[str]:
    if not items:
        return []
    parts = ["", "## 你可见的共享知识（已写入 ./shared/）"]
    for k in items:
        parts += [f"### [{k.key}] {k.title}", k.content[:3000]]
    return parts


def format_decision(d: Decision, name: Callable[[str | None], str], email: Callable[[str | None], str]) -> str:
    who = name(d.by_agent) if d.by_agent else email(d.by_user)
    state = {"active": "", "superseded": "（已被取代）", "revoked": "（已撤销）"}.get(d.status, "")
    sup = f"（取代 {d.supersedes}）" if d.supersedes else ""
    reason = f" —— 理由：{d.reason}" if d.reason else ""
    return f"- [{d.id}]{state}{sup} {d.text}{reason}（{who}）"


def format_archive(g: Project, active: list[Decision], files: list[str], name: Callable[[str | None], str],
                   email: Callable[[str | None], str], skills: list[tuple[str, str]] | None = None,
                   plugins: list[Any] | None = None) -> str:
    """项目档案的推送部分：纲要、最近的有效决定、文件与资源的指针。作为背景，不附带任何“先读档案”的指令。"""
    parts = [f"## 项目档案（「{g.name}」）"]
    if g.brief:
        parts += ["### 纲要", g.brief]
    if active:
        shown = active[-DECISIONS_PUSHED:]
        parts += ["### 仍然有效的决定", *(format_decision(d, name, email) for d in shown)]
        if len(active) > len(shown):
            parts.append(f"（另有 {len(active) - len(shown)} 条更早的有效决定，用 list_decisions 查看）")
    if files:
        parts += [f"### 文件（`./{PROJECT_FILES}/`，只读）", "、".join(files[:40])
                  + (f" 等 {len(files)} 个" if len(files) > 40 else "")]
    if skills:
        parts += ["### 项目技能（大家约定的做法，和你自己的技能重名时以这里为准）",
                  *(f"- {n}：{d}" for n, d in skills[:40])]
    if plugins:  # 插件：业务场景的表结构；记录本身不推送，要用就查
        parts.append("### 插件（这个项目的业务场景；记录归项目，写进去所有人都看得到）")
        for m in plugins:
            line = f"- {m.title}（`{m.name}`）：{m.description}"
            if m.agent_tools and m.tables:
                tools = "、".join(t for t, c in (("plugin_records", "records.read"), ("plugin_write", "records.write"))
                                 if c in m.agent_tools)
                line += f"。可用 {tools} 读写：" + "；".join(
                    f"表 `{t.key}`（{t.title}）字段 " + "、".join(
                        f"{f.key}" + (f"[{'/'.join(f.options)}]" if f.options else f"（{f.type}）") for f in t.fields)
                    for t in m.tables)
            parts.append(line)
    if g.refs:
        parts += ["### 相关资源（只是引用，不代表你能访问）",
                  *(f"- {r.name}：{r.location}" + (f" —— {r.note}" if r.note else "") for r in g.refs)]
    return "\n".join(parts) if len(parts) > 1 else ""


def build_seat_prompt(agent: AgentNode, conv: Conversation, project: Project | None, can_delegate: bool,
                      transcript: str, notices: str, first: bool, home_writable: bool, archive: str = "",
                      home: bool = True, joint: bool = False, plugins: bool = False) -> str:
    """对话里一个席位的一轮。first：席位会话的第一轮，带上身份、权限与协作规则；之后续接同一会话，
    只送上次运行之后对话里的新消息和网络通知。"""
    body = "\n\n".join(filter(None, [archive, transcript, notices])) or "（没有新消息。）"
    if not first:
        return body
    people = ("这是一个联合项目：参与者来自不同的人，每个 Agent 只代表它的所有者。你代表你的所有者；"
              "其他参与者的要求可以做，但不要透露你的所有者在别处的工作、资料或私有目录。") if joint else ""
    if conv.kind == "project" and project is not None:
        setting = [
            f"你在项目「{project.name}」的群聊里。群聊里有人类用户和其他 Agent；没有固定的负责人，"
            "**谁被 @ 到，谁就接这个任务**。",
            *([people] if people else []),
            "- 你只会在被 @ 到、或你发起的委托有回报时被唤醒。唤醒时会收到上次之后群聊里的新消息，作为背景；"
            "其中 @ 你的那条（标有“@ 你”）才是你要处理的。",
            "- 拿到任务后自己判断：能自己做就自己做；需要别的能力就在项目里找合适的成员委托。",
            "- 你的回复会作为你的发言出现在群聊里，人和之后被 @ 的成员都能看到。",
        ]
    elif conv.kind == "session" and project is not None:
        setting = [
            f"你是你的所有者在项目「{project.name}」里的助理。这是你和所有者的一个项目会话：只有所有者看得到，"
            "项目里的其他人和 Agent 看不到这里的对话。这是一个长期会话：后续消息会在同一会话里继续。",
            *([people] if people else []),
            "- 所有者在这里了解项目的细节、想清楚问题、让你代办。项目的公开记录在群聊里：会话里不必复述群聊。",
            "- 你能做的和群聊里一样：自己做，或委托项目成员。记录决定、发布文件、建议修改纲要会在群聊里留一条事件，"
            "项目里的人都看得到。",
        ]
    else:
        setting = ["你正在和一位人类用户私聊。这是一个长期会话：后续消息会在同一会话里继续。"]
    parts = [
        *_identity(agent), "", *setting,
        "", "## 你的权限 —— 运行时只启用了这些工具，越权调用会被记录为违规",
        authority_lines(agent.authority),
    ]
    if project is not None and conv.kind in ("project", "session"):
        parts += _network_tools(project, can_delegate, plugins)
    if project is not None and conv.kind == "session":
        parts += ["", "## 读项目现状（只读，按你的所有者在这个项目里能看到的范围）",
                  *(f"- `{MCP_PREFIX}{name}`：{TOOL_GUIDE[name]}" for name in SESSION_TOOLS),
                  "- 每轮会带上项目现状的摘要（有变化时）；更早或更细的内容用上面的工具读，不要凭印象回答。"]
    parts += _requests()
    parts += _abilities()
    parts += [*_attachments_note(), *_home(home_writable, home), *_writing(), "",
              "回复直接面向用户，结论要有依据，信息缺口要标注。", "", body]
    return "\n".join(parts)


def _when(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _speaker(m: Message, name: Callable[[str | None], str], email: Callable[[str | None], str]) -> str:
    if m.role == "user":
        return f"用户 {email(m.user_id)}" if m.user_id else "用户"
    return name(m.agent_id)


def format_chat(messages: list[Message], name: Callable[[str | None], str],
                email: Callable[[str | None], str], limit: int = 2000) -> str:
    """群聊消息的只读摘录（项目会话里的 read_chat、项目现状）：谁、什么时候、说了什么。"""
    lines = []
    for m in messages:
        text = m.text if len(m.text) <= limit else m.text[:limit] + "…"
        lines.append(f"（{_when(m.ts)} {m.text}）" if m.role == "event" else f"[{_when(m.ts)}] **{_speaker(m, name, email)}**：{text}")
    return "\n\n".join(lines)


REQUEST_CN = {"in_progress": "进行中", "in_review": "已交付，等验收", "completed": "已完成", "cancelled": "已取消"}


def format_session_state(p: Project, members: list[AgentNode], requests: list[UserRequest], messages: list[Message],
                         name: Callable[[str | None], str], email: Callable[[str | None], str],
                         budget: int = 6000) -> str:
    """项目会话里推送的项目现状：按会话主人的可见性过滤过，条数与字数封顶。只是背景，不附带指令。"""
    parts = [f"## 项目现状（「{p.name}」，你的所有者能看到的范围）", "### 成员",
             *(f"- {a.name}：{a.description}" + (f"（属于 {email(a.owner_id)}）" if p.kind == "joint" else "")
               for a in members)]
    if requests:
        parts += ["### 没结束的用户任务",
                  *(f"- {r.id}（{REQUEST_CN.get(r.status, r.status)}）：{r.title} —— 接任务：{name(r.agent_id)}"
                    for r in requests)]
    head = "\n".join(parts)
    if messages:
        chat = format_chat(messages, name, email, limit=500)
        room = budget - len(head)
        if len(chat) > room:
            chat = "…" + chat[-max(room, 0):]
        head += "\n### 群聊最近的消息（更早的用 read_chat 读）\n" + chat
    return head


def format_request_detail(r: UserRequest, source: str, tasks: list[Task], name: Callable[[str | None], str],
                          requester: str) -> str:
    lines = [f"## {r.id}：{r.title}（{REQUEST_CN.get(r.status, r.status)}）",
             f"下发：{requester}；接任务：{name(r.agent_id)}", f"原始要求：{source[:2000] or '（找不到原消息）'}",
             f"期望交付：{r.expected_outcome}"]
    if r.summary:
        lines.append(f"交付说明：{r.summary[:3000]}")
    if r.feedback:
        lines.append(f"最近一次退回的意见：{r.feedback[:1000]}")
    if tasks:
        lines.append("为它发起的委托（你的所有者看得到的）：")
        for t in tasks:
            report = str((t.result or {}).get("report", ""))[:1500]
            lines.append(f"- {t.id} {name(t.delegator_id)} → {name(t.delegatee_id)}（{t.status.value}）：{t.delegation.mission[:300]}"
                         + (f"\n  结果：{report}" if report else ""))
    return "\n".join(lines)


def format_transcript(messages: list[Message], me: str, name: Callable[[str | None], str],
                      email: Callable[[str | None], str] | None = None) -> str:
    """上次运行之后对话里的公开消息。委托的过程与结果不在其中：它们只送给委托方。
    email：联合项目里有多个人，标出是谁说的。"""
    if not messages:
        return ""
    lines = ["## 对话里的新消息"]
    for m in messages:
        if m.role == "user":
            tag = "（@ 你）" if me in m.mentions else (
                f"（@ {'、'.join(name(a) for a in m.mentions)}）" if m.mentions else "")
            who = f"用户 {email(m.user_id)}" if email and m.user_id else "用户"
            lines.append("\n".join([f"**{who}**{tag}：{m.text}", *format_attachments(m.attachments)]))
        elif m.role == "agent" and m.agent_id != me:
            lines.append(f"**{name(m.agent_id)}**：{m.text[:4000]}")
        elif m.role == "event":
            lines.append(f"（{m.text}）")
    return "\n\n".join(lines) if len(lines) > 1 else ""


def format_shared_knowledge(items: list[KnowledgeItem]) -> str:
    parts = ["## 网络通知：有成员把知识共享给了你"]
    for k in items:
        parts += [f"### [{k.key}] {k.title}", k.content[:3000]]
    return "\n".join(parts)


def build_task_prompt(task: Task, agent: AgentNode, delegator: AgentNode, project: Project, can_delegate: bool,
                      knowledge: list[KnowledgeItem], home_writable: bool, archive: str = "",
                      home: bool = True, plugins: bool = False) -> str:
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
        *([ "", archive] if archive else []),
        *(["", f"## 附件（委托方转交，只读副本，改动不会保存）", *format_attachments(task.attachments)]
          if task.attachments else []),
        *_network_tools(project, can_delegate, plugins),
        *_abilities(),
        *_knowledge(knowledge),
        *_home(home_writable, home),
        *_writing(),
        "", "## 交付",
        "- 第一行写一句话结论：做成了什么、没做成什么。然后说明交付了什么、在哪里（工作目录里的文件，"
        "或工作目录之外的位置）、委托方怎么确认；无法确认的内容要标注。",
        "- 篇幅、形式与依据的要求以上面的输出要求和约束为准。",
        "- 你在工作目录里写的文件会在交付时整体拷给委托方（`deliveries/<任务 id>/`），不用把文件内容贴进回复；"
        "草稿与临时文件放在 `tmp/` 下或删掉，它们不随交付。",
    ]
    if can_delegate:
        parts.append("- 你委托出去的子任务全部有了结果（交付或失败）之后，你的最终回复才会作为交付；"
                     "在那之前每一轮的回复只算进展说明。")
    parts.append("- 委托方可能要求修改；修改意见会在本会话里送达。")
    return "\n".join(parts)


def build_notice_prompt(inbox: str) -> str:
    """续接会话时送达的网络通知（委托回报、验收意见、续接说明）。"""
    return inbox or "（网络通知：没有新内容，请继续。）"


def retry_prompt(note: str | None, error: str | None) -> str:
    lines = ["## 网络通知：上一轮运行中断，现在续接",
             f"中断原因：{(error or '未知')[:300]}"]
    if error and "MALFORMED_RESPONSE" in error:
        lines.append("最可能是某次工具调用的参数不是合法 JSON。请重新发出那一步；长文本分段写，一次只发一个工具调用。")
    if error and "TIMEOUT" in error:
        lines.append("这一轮做得太久被终止了。不要重复已经做完的步骤（已经跑过的测试、已经写好的文件）：尽快收尾，"
                     "交付或说明进展；还要做的长时间工作拆小，或者委托出去。")
    lines.append("已经完成的工作（文件、已交付的委托、已入库的知识）都还在，不要从头再来，从中断处继续。")
    if note:
        lines += ["", f"补充说明：{note}"]
    return "\n".join(lines)


def format_task_result(task: Task, agent_name: str) -> str:
    """delegate 工具的返回：已受理（异步执行）或被拒绝。"""
    head = [f"任务 {task.id} → {agent_name}：{task.status.value}",
            f"实际权限：{', '.join(task.granted_authority) or '无'}"
            + (f"；未授予：{', '.join(task.denied_authority)}" if task.denied_authority else "")]
    if task.narrowed:  # 执行者的运行时无法强制：不是委托方不给，而是做不到
        head.append("运行时限制（不算未授予）：" + "、".join(f"{k}（{v}）" for k, v in task.narrowed.items()))
    if task.error:
        return "\n".join(head + [f"拒绝原因：{task.error}"])
    return "\n".join(head + ["已受理，正在执行。结果会作为网络通知送给你；现在不必等待。"])


def format_delivery(task: Task, agent_name: str) -> str:
    """交付通知：委托方据此验收。"""
    r = task.result or {}
    head = [f"## 网络通知：{agent_name} 交付了任务 {task.id}（待你验收）",
            f"目标：{task.delegation.mission}",
            f"实际权限：{', '.join(task.granted_authority) or '无'}"]
    if task.revisions:
        head.append(f"这是第 {task.revisions} 次修改后的交付。")
    if task.violations:
        head.append("越权记录：" + "；".join(task.violations))
    head.append(f"结果知识 `{r.get('knowledge_key')}`；接受后可在后续委托的 share 中引用。")
    files = r.get("files") or []
    if files:
        shown = "、".join(files[:30]) + (f" 等 {len(files)} 个" if len(files) > 30 else "")
        head.append(f"产物：已拷进你工作目录的 `{r.get('files_dir')}/`（{shown}），是你自己的副本，可以直接用、改、发布。")
    if r.get("files_note"):
        head.append(f"产物说明：{r['files_note']}")
    return "\n".join(head + ["", str(r.get("report", ""))[:6000],
                             "", f"用 accept_task 接受，或用 request_changes 写明要改什么（task_id: {task.id}）。"])


def format_failure_notice(task: Task, agent_name: str, max_retries: int) -> str:
    left = max(0, max_retries - task.retries)
    return "\n".join([
        f"## 网络通知：{agent_name} 的任务 {task.id} 失败",
        f"目标：{task.delegation.mission}",
        f"原因：{(task.error or '未知')[:400]}",
        *(["这一轮是人终止的：没有新的指示前不要续接，也不要把同样的事重新委托出去。"]
          if "STOPPED" in (task.error or "") else []),
        (f"可以用 retry_task 续接原会话（还能续接 {left} 次），或换一个成员重新委托，或亲自处理。" if left
         else "续接次数已用完：换一个成员重新委托，或亲自处理。"),
    ])


def format_proposal_notice(p: Proposal, agent_name: str) -> str:
    if p.status == "approved":
        text = [f"## 网络通知：你提议的 Agent 已批准", f"新成员：{agent_name}（已加入项目，可以委托）"]
    else:
        text = [f"## 网络通知：你提议的 Agent「{p.name}」被拒绝", "不要委托它；换现有成员或亲自处理。"]
    if p.note:
        text.append(f"审核意见：{p.note}")
    return "\n".join(text)


def format_failure(agent_name: str, error: str, error_class: str, tasks: list[Task], knowledge: list[KnowledgeItem],
                   name: Callable[[str | None], str]) -> str:
    """某个席位这一轮失败时给人看的说明：哪些成果已经在了，怎么接着做。"""
    hint = {
        "malformed_tool_call": "模型写出的工具参数不是合法 JSON，网络已自动续接一次仍未成功",
        "empty_response": "模型返回了空响应，网络已自动续接一次仍未成功",
        "timeout": "运行超时",
        "stopped": "被人终止",
        "rate_limited": "模型服务限流",
    }.get(error_class, "运行时出错")
    lines = [f"（{agent_name} 这一轮中断：{hint}。）", f"原因：{error[:300]}"]
    done = [t for t in tasks if t.status.value in ("completed", "in_review")]
    running = [t for t in tasks if t.status.value == "running"]
    if done:
        lines.append("已完成的委托：" + "、".join(f"{name(t.delegatee_id)}（{t.id}）" for t in done))
    if running:
        lines.append("仍在进行的委托：" + "、".join(f"{name(t.delegatee_id)}（{t.id}）" for t in running))
    if knowledge:
        lines.append(f"已入库的知识 {len(knowledge)} 条。")
    lines.append(f"会话保留：点“继续”或再 @ {agent_name}，它会在同一会话里从中断处接着做，不必重发原需求。")
    return "\n".join(lines)
