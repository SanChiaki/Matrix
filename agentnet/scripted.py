"""确定性脚本场景：用于自动化测试与离线演练（--runtime scripted）。

群聊里被 @ 的成员接任务。被 @ 的若是 Product Strategy Agent（规划者），它分几轮推进：
1. 被 @：open_request 登记为用户任务（看板）→ list_members → 逐个委托市场 / 技术 / 竞争 → 越界尝试委托项目外成员（被拒绝）。
2. 每收到一份交付：验收（accept_task）；竞争格局一到，就把它共享给市场研究再委托一次。
3. 所有委托都有了结果：整合回复，deliver_request 交付任务。
被 @ 的若是专项成员（市场 / 技术 / 竞争），它自己回答，不转手。
被委托的 Agent：直接交付一段结果文本。私聊里没有网络工具，只能亲自回答。
"""

from __future__ import annotations

import re

from .runtime import ScriptCall

TARGETS = ("Market Research Agent", "Technical Analysis Agent", "Competitive Intelligence Agent")
ASKS = {
    "Market Research Agent": dict(mission="评估 AI Agent 协作平台的市场规模与需求趋势",
                                  constraints="只看近两年数据", expected_outcome="市场分析摘要"),
    # 故意多给 shell.exec：执行者自身没有，网络会剔除
    "Technical Analysis Agent": dict(mission="评估多 Agent 委托网络的技术可行性",
                                     authority=["web.research", "shell.exec"], expected_outcome="可行性评估"),
    "Competitive Intelligence Agent": dict(mission="梳理竞品与差异化空间", authority=["web.research"],
                                           expected_outcome="竞品清单与空白点"),
}


def _name(prompt: str) -> str:
    m = re.search(r"\*\*(.+?)\*\*", prompt)
    return m.group(1) if m else ""


_SESSION: dict[str, dict] = {}  # 会话 id → 这个会话的 Agent 名与对话进度


def scenario_handler(call: ScriptCall) -> str:
    if call.turn == 1 or call.session_id not in _SESSION:
        _SESSION[call.session_id] = {"name": _name(call.prompt), "results": {}, "pending": set()}
    st = _SESSION[call.session_id]
    name = st["name"]

    if "你收到一份委托" in call.prompt or (call.turn > 1 and "委托方要求修改" in call.prompt):
        call.use("web_search", {"queries": [name]}, "3 results")
        shared = "你可见的共享知识" in call.prompt
        if name == "Competitive Intelligence Agent":
            return "通用编排平台拥挤；跨组织授权与审计是空白。\n\n- 竞品 A：工作流编排\n- 竞品 B：多 Agent 框架\n\n来源：https://example.com/landscape"
        return f"{name} 结论" + ("（已参考共享的竞争格局）" if shared else "") + "\n\n正文。\n\n来源：https://example.com/source"

    if "（@ 你）" in call.prompt and name in TARGETS:
        # 被直接 @ 的专项成员：自己做，不转手
        call.use("web_search", {"queries": [name]}, "3 results")
        return f"{name} 直接回答：" + ("已参考前面的讨论。" if "**Product Strategy Agent**" in call.prompt else "正文。")

    if not call.can_delegate:
        return f"{name}：我在这里不能委托，只能基于自身能力回答。"

    if "（@ 你）" in call.prompt and not st["pending"] and not st["results"]:
        r = call.tool("open_request", title="研究一个新的 AI 产品机会", expected_outcome="机会判断与依据")
        st["request"] = m.group(1) if (m := re.search(r"(req-[0-9a-f]{8})", r)) else None
        roster = call.tool("list_members")
        for n in (n for n in TARGETS if n in roster):
            tid = re.search(r"任务 (task-[0-9a-f]{8})", call.tool("delegate", agent=n, **ASKS[n])).group(1)
            st["pending"].add(tid)
        # 越界尝试：委托项目外的 Agent 会被拒绝
        call.tool("delegate", agent="Legal Compliance Agent", mission="评估合规风险", expected_outcome="风险清单")
        return f"已委托 {len(st['pending'])} 个成员，等它们交付。"

    for block in call.prompt.split("## 网络通知：")[1:]:
        m = re.match(r"(.+?) 交付了任务 (task-[0-9a-f]{8})", block)
        if not m:
            continue
        who, tid = m.groups()
        call.tool("accept_task", task_id=tid)
        st["pending"].discard(tid)
        report = block.split("\n\n", 2)[1] if block.count("\n\n") >= 2 else ""
        st["results"].setdefault(who, report.splitlines()[0] if report else "")
        if who == "Competitive Intelligence Agent" and "Market Research Agent" in TARGETS and not st.get("followup"):
            # 把竞争格局显式共享给市场研究，再委托一次
            st["followup"] = True
            key = re.search(r"结果知识 `(task-[0-9a-f]{8}:result)`", block).group(1)
            r = call.tool("delegate", agent="Market Research Agent", mission="验证『企业跨团队 Agent 授权网络』细分市场",
                          expected_outcome="细分市场结论", share=[key])
            st["pending"].add(re.search(r"任务 (task-[0-9a-f]{8})", r).group(1))

    if st["pending"]:
        return f"已验收一部分，还在等 {len(st['pending'])} 个委托。"
    if st.get("request"):
        call.tool("deliver_request", request_id=st.pop("request"), summary="机会判断：面向企业的跨团队 Agent 授权网络")
    return "机会成立：面向企业的跨团队 Agent 授权网络，差异化在授权链与上下文隔离。\n\n" + \
        "\n".join(f"- {n}：{r}" for n, r in st["results"].items())
