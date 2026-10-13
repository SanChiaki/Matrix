"""确定性脚本场景：用于自动化测试与离线演练（--runtime scripted）。

对话 Agent（有网络工具时）：list_members → 并行委托群组成员 → 把竞争格局共享给市场研究 → 再委托一次 → 整合回复。
被委托的 Agent：直接交付一段结果文本。没有网络工具（不在群组中）时，对话 Agent 只能亲自回答。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from .runtime import ScriptCall


def _name(prompt: str) -> str:
    m = re.search(r"\*\*(.+?)\*\*", prompt)
    return m.group(1) if m else ""


_SESSION_AGENT: dict[str, str] = {}


def scenario_handler(call: ScriptCall) -> str:
    if call.turn == 1:
        _SESSION_AGENT[call.session_id] = _name(call.prompt)
    name = _SESSION_AGENT.get(call.session_id, "")
    is_task = "你收到一份委托" in call.prompt

    if is_task:
        call.use("web_search", {"queries": [name]}, "3 results")
        shared = "你可见的共享知识" in call.prompt
        if name == "Competitive Intelligence Agent":
            return "通用编排平台拥挤；跨组织授权与审计是空白。\n\n- 竞品 A：工作流编排\n- 竞品 B：多 Agent 框架\n\n来源：https://example.com/landscape"
        return f"{name} 结论" + ("（已参考共享的竞争格局）" if shared else "") + "\n\n正文。\n\n来源：https://example.com/source"

    if not call.tool_names:
        return f"{name}：我不在群组中，无法委托，只能基于自身能力回答。"

    roster = call.tool("list_members")
    targets = [n for n in ("Market Research Agent", "Technical Analysis Agent", "Competitive Intelligence Agent")
               if n in roster]
    asks = {
        "Market Research Agent": dict(mission="评估 AI Agent 协作平台的市场规模与需求趋势",
                                      constraints=["只看近两年数据"], expected_outcome="市场分析摘要"),
        # 故意多给 shell.exec：执行者自身没有，网络会剔除
        "Technical Analysis Agent": dict(mission="评估多 Agent 委托网络的技术可行性",
                                         authority=["web.research", "shell.exec"], expected_outcome="可行性评估"),
        "Competitive Intelligence Agent": dict(mission="梳理竞品与差异化空间", authority=["web.research"],
                                               expected_outcome="竞品清单与空白点"),
    }
    with ThreadPoolExecutor(len(targets) or 1) as pool:
        results = dict(zip(targets, pool.map(lambda n: call.tool("delegate", agent=n, **asks[n]), targets)))

    # 越界尝试：委托群组外的 Agent 会被拒绝
    call.tool("delegate", agent="Legal Compliance Agent", mission="评估合规风险", expected_outcome="风险清单")

    ci = results.get("Competitive Intelligence Agent", "")
    key = re.search(r"`(task-[0-9a-f]{8}:result)`", ci)
    if key and "Market Research Agent" in targets:
        call.tool("delegate", agent="Market Research Agent", mission="验证『企业跨团队 Agent 授权网络』细分市场",
                  expected_outcome="细分市场结论", share=[key.group(1)])
    return "机会成立：面向企业的跨团队 Agent 授权网络，差异化在授权链与上下文隔离。\n\n" + \
        "\n".join(f"- {n}：{r.splitlines()[0]}" for n, r in results.items())
