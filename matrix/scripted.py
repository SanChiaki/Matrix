"""确定性脚本场景：用于自动化测试与离线演练（--runtime scripted）。

群聊里被 @ 的成员接任务。被 @ 的若是 Product Strategy Agent（规划者），它分几轮推进：
1. 被 @：open_request 登记为用户任务（看板）→ list_members → 逐个委托市场 / 技术 / 竞争 → 越界尝试委托项目外成员（被拒绝）。
2. 每收到一份交付：验收（accept_task）；竞争格局一到，就把它共享给市场研究再委托一次。
3. 所有委托都有了结果：整合回复，deliver_request 交付任务。
被 @ 的若是专项成员（市场 / 技术 / 竞争），它自己回答，不转手。
被委托的 Agent：直接交付一段结果文本。私聊里没有网络工具，只能亲自回答。
"""

from __future__ import annotations

import json
import re
import time

from .models import MCP_PREFIX, RELEASES
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


def _home(call: ScriptCall) -> str:
    """助理对话：问“有什么要我处理的”就按总览回答；“在 X 项目里 …”起草；“每天 N 点 …”“N 分钟后 …”建定时；定时到点就汇报。"""
    asked = re.findall(r"\*\*用户[^*]*\*\*（@ 你）：(.+)", call.prompt)
    todo = re.search(r"### 等你的所有者处理\n((?:- .+\n?)+)", call.prompt)
    if not asked:
        if "到点了" in call.prompt:
            return f"早报：\n{todo.group(1).strip()}" if todo else "没有新情况"
        return "（没有新消息）"
    text = asked[-1].strip()
    if m := re.search(r"在「?([^」\s]+?)」?(?:项目)?里(?:说|发)?[：:，,]?\s*(.+)", text):
        return call.tool("draft_message", project=m.group(1), text=m.group(2))
    if m := re.search(r"每天\s*(\d{1,2})\s*点", text):
        return call.tool("schedule", title="每日提醒", instruction=text, cron=f"0 {int(m.group(1))} * * *")
    if m := re.search(r"(\d+)\s*分钟后", text):
        return call.tool("schedule", title="提醒", instruction=text, after_minutes=m.group(1))
    if "定时" in text:
        return call.tool("list_schedules")
    if todo:
        return f"等你处理的事：\n{todo.group(1).strip()}"
    return "目前没有等你处理的事。"


# ================= 交付看板（examples/delivery-dashboard，docs/project-publishing.md） =================
# 项目里的“看板 Agent”有插件自带的技能 delivery-dashboard：用所有者授权的交付数据连接器查数，生成看板文件，
# publish_file 发布进档案，plugin_write 在“看板”表里登记。订阅了看板发布物的人问起来，助理按快照回答。
DASH_PLUGIN = "delivery-dashboard"
REGION_SLUG = {"华东": "huadong", "华南": "huanan", "华北": "huabei"}


def _asked(prompt: str) -> str:
    asked = re.findall(r"\*\*用户[^*]*\*\*（@ 你）：(.+)", prompt)
    return asked[-1].strip() if asked else ""


def dashboard_spec(summary: dict, title: str) -> dict:
    """把 delivery_summary 的结果写成看板文件（格式见技能 delivery-dashboard）。"""
    weekly = summary.get("weekly_on_time") or []
    prev = weekly[-2]["on_time_rate"] if len(weekly) >= 2 else None
    return {
        "title": title, "scope": summary.get("scope", ""), "as_of": summary.get("as_of", ""),
        "generated_at": time.time(), "source": "交付数据 · delivery_summary",
        "kpis": [
            {"label": "在建项目", "value": summary.get("active_projects", 0), "format": "int"},
            {"label": "按期率", "value": summary.get("on_time_rate", 0), "format": "pct",
             **({"delta": round(summary.get("on_time_rate", 0) - prev, 3), "delta_label": "较上周", "good": "up"}
                if prev is not None else {})},
            {"label": "延期项目", "value": summary.get("delayed_projects", 0), "format": "int"},
            {"label": "平均延期", "value": summary.get("avg_delay_days", 0), "format": "days"},
            {"label": "本周验收", "value": summary.get("accepted_this_week", 0), "format": "int"},
        ],
        "charts": [
            {"type": "line", "title": "近 8 周按期率", "format": "pct",
             "points": [{"x": w["week"], "y": w["on_time_rate"]} for w in weekly]},
            {"type": "bar", "title": "各阶段项目数", "format": "int",
             "bars": [{"label": b["stage"], "value": b["count"]} for b in summary.get("by_stage") or []]},
        ],
        "tables": [{"title": "延期风险项目",
                    "columns": [{"key": "name", "label": "项目"}, {"key": "stage", "label": "阶段"},
                                {"key": "pm", "label": "项目经理"},
                                {"key": "delay_days", "label": "延期（天）", "align": "right"},
                                {"key": "forecast_end", "label": "预计完成"},
                                {"key": "health", "label": "健康度", "kind": "health"}],
                    "rows": summary.get("at_risk") or []}],
        "notes": "按期率 = 在建项目里预计完成不晚于计划完成的比例；健康度：延期 ≥14 天红、5–13 天黄。",
    }


def _dash_records(call: ScriptCall) -> list[tuple[str, dict]]:
    text = call.tool("plugin_records", plugin=DASH_PLUGIN, table="dashboards", limit=50)
    return [(m.group(1), json.loads(m.group(2))) for m in re.finditer(r"^- (\S+?)：(\{.*\})$", text, re.M)]


def _make_dashboard(call: ScriptCall, region: str | None, title: str, rid: str | None, refresh: str) -> str:
    """查数 → 写文件 → 发布 → 登记。返回给人的一段结论；查不到时返回原因。"""
    tool = "delivery-data__delivery_summary"
    if tool not in call.connector_tools():
        call.tool("suggest_connector", connector="交付数据", reason="做交付看板要查交付数据")
        return "我还用不了交付数据：请我的所有者在“连接器”里授权「交付数据」并勾给我，再叫我一次。"
    raw = call.connector(tool, **({"region": region} if region else {}))
    try:
        summary = json.loads(raw)
    except ValueError:
        return f"交付数据没查成：{raw}"
    fname = f"{REGION_SLUG.get(region or '', 'all')}-weekly.json"
    (call.cwd / fname).write_text(json.dumps(dashboard_spec(summary, title), ensure_ascii=False, indent=1), encoding="utf-8")
    call.tool("publish_file", path=fname)
    fields = {"title": title, "file": fname, "scope": summary.get("scope", ""), "owner_agent": call.agent_id}
    if rid:
        call.tool("plugin_write", plugin=DASH_PLUGIN, table="dashboards", record_id=rid, fields=json.dumps({"file": fname}))
    else:
        call.tool("plugin_write", plugin=DASH_PLUGIN, table="dashboards",
                  fields=json.dumps({**fields, "status": "草稿", "refresh": refresh}, ensure_ascii=False))
    worst = (summary.get("at_risk") or [{}])[0]
    return (f"「{title}」已生成（`{fname}`，数据截至 {summary.get('as_of')}）：{summary.get('scope')} 在建 "
            f"{summary.get('active_projects')} 个，按期率 {summary.get('on_time_rate', 0) * 100:.1f}%，延期 "
            f"{summary.get('delayed_projects')} 个" + (f"，延期最多的是 {worst.get('name')}（{worst.get('delay_days')} 天）" if worst else "")
            + "。在项目页“交付看板”标签里看；要给区域负责人看，请主持人在“发布”里发布成交付件。")


def _dashboard(call: ScriptCall) -> str | None:
    """项目里的看板 Agent：做看板、刷新、待发布时写说明。不是这些情形返回 None。"""
    if "delivery-dashboard" not in call.skills or MCP_PREFIX + "plugin_write" not in call.tool_names:
        return None
    text = _asked(call.prompt)
    if "标为待发布" in text:  # 记录触发：代表启用它的人 @ 过来
        m = re.search(r"「(.+?)」标为待发布", text)
        return (f"「{m.group(1) if m else '看板'}」核对过了：口径按 delivery-data-query，数字与交付数据一致。"
                "请主持人在项目页“发布”里把看板文件发布成交付件（带上“交付看板”视图），读者填区域负责人，开“跟随档案”。")
    if "刷新看板" in text:  # 人点“请 … 刷新”，或定时触发叫它刷新“刷新”是每天的看板
        daily = "每天的看板" in text
        done = []
        for rid, f in _dash_records(call):
            if (f.get("refresh") != "每天") if daily else (f.get("title", "") not in text and "全部" not in text):
                continue
            scope = str(f.get("scope", ""))
            region = scope if scope in REGION_SLUG else None
            done.append(_make_dashboard(call, region, f.get("title", "交付看板"), rid, f.get("refresh", "手动")))
        return "\n\n".join(done) or "没有要刷新的看板。"
    if text and "看板" in text:
        region = next((r for r in REGION_SLUG if r in text), None)
        title = f"{region or '全部区域'}交付周报"
        r = call.tool("open_request", title=f"做「{title}」看板", expected_outcome="项目页里能看的看板，附结论")
        req = m.group(1) if (m := re.search(r"(req-[0-9a-f]{8})", r)) else None
        reply = _make_dashboard(call, region, title, None, "每天" if "每天" in text else "手动")
        if req and "已生成" in reply:
            call.tool("deliver_request", request_id=req, summary=reply)
        return reply
    return None


# ================= 接入已有的看板系统（examples/dashboard-system） =================
# 看板系统经 MCP 包装层登记成连接器 boards：Agent 建草稿、把链接加进资料、有人说发布才发布（网络先给所有者确认）。
BOARD = "boards__"


def _board_system(call: ScriptCall) -> str | None:
    text = _asked(call.prompt)
    if not text or "看板" not in text or MCP_PREFIX + "add_link" not in call.tool_names:
        return None
    tools = call.connector_tools()
    if BOARD + "create_dashboard" not in tools:
        return None
    region = next((r for r in REGION_SLUG if r in text), None)

    def summary() -> dict | str:
        if "delivery-data__delivery_summary" not in tools:
            return "我还用不了交付数据：请我的所有者授权「交付数据」并勾给我。"
        raw = call.connector("delivery-data__delivery_summary", **({"region": region} if region else {}))
        try:
            return json.loads(raw)
        except ValueError:
            return f"交付数据没查成：{raw}"

    def spec(data: dict) -> str:
        sp = dashboard_spec(data, "")
        return json.dumps({k: sp[k] for k in ("scope", "as_of", "kpis", "charts", "tables", "notes")}, ensure_ascii=False)

    def boards() -> list[dict]:
        try:
            return json.loads(call.connector(BOARD + "list_dashboards"))
        except ValueError:
            return []

    def pick() -> dict | None:
        items = boards()
        return next((b for b in reversed(items) if b["title"] in text), None) or (items[-1] if items else None)

    if "发布" in text:
        b = pick()
        if b is None:
            return "看板系统里还没有我建的看板。"
        raw = call.connector(BOARD + "publish_dashboard", id=b["id"])
        try:
            done = json.loads(raw)
        except ValueError:
            return f"没有发布「{b['title']}」：{raw}"
        call.tool("add_link", name=done["title"], url=done["url"], note=f"已发布 v{done['version']}（看板系统是公开的）")
        return f"「{done['title']}」已发布 v{done['version']}：{done['url']}（公开，任何人都能打开）。项目资料里的链接已更新。"
    if any(w in text for w in ("更新", "刷新", "改")):
        b = pick()
        if b is None:
            return "看板系统里还没有我建的看板。"
        data = summary()
        if isinstance(data, str):
            return data
        d = json.loads(call.connector(BOARD + "update_dashboard", id=b["id"], spec=spec(data)))
        return f"「{d['title']}」的草稿已更新（数据截至 {data.get('as_of')}），预览：{d['draft_url']}。已发布的版本没变，要发布就说一声。"
    data = summary()
    if isinstance(data, str):
        return data
    title = f"{region or '全部区域'}交付周报"
    r = call.tool("open_request", title=f"在看板系统里做「{title}」", expected_outcome="看板草稿与链接")
    req = m.group(1) if (m := re.search(r"(req-[0-9a-f]{8})", r)) else None
    d = json.loads(call.connector(BOARD + "create_dashboard", title=title, spec=spec(data)))
    call.tool("add_link", name=d["title"], url=d["url"], note=f"草稿，预览：{d['draft_url']}")
    reply = (f"「{title}」的草稿建好了（数据截至 {data.get('as_of')}：按期率 {data.get('on_time_rate', 0) * 100:.1f}%，"
             f"延期 {data.get('delayed_projects')} 个），预览：{d['draft_url']}。链接已加进项目资料。"
             "要发布就告诉我；看板系统是公开的，发布前会请我的所有者确认。")
    if req:
        call.tool("deliver_request", request_id=req, summary=reply)
    return reply


def _from_release(call: ScriptCall) -> str | None:
    """订阅了看板发布物的人问交付情况：只按挂上的快照回答，写明版本与快照时间。没有挂上就说明。"""
    text = _asked(call.prompt)
    if not text or not re.search(r"看板|按期|延期|交付|准时|周报", text):
        return None
    root = call.cwd / RELEASES
    boards = sorted(root.glob("*/*.json")) if root.is_dir() else []
    if not boards:
        if "这一轮没有挂上" in call.prompt:
            return "订阅的看板这一轮没有挂上（这里有不在读者名单上的人），我不能在这里复述它的内容。"
        return None
    spec = json.loads(boards[0].read_text(encoding="utf-8"))
    head = re.search(r"交付件「(.+?)」(v\d+)，快照于 ([\d\- :]+)", call.prompt)
    k = {x["label"]: x for x in spec.get("kpis", [])}
    rate = k.get("按期率", {}).get("value", 0)
    risk = spec.get("tables", [{}])[0].get("rows", [])[:3]
    return (f"按订阅的「{head.group(1) if head else spec.get('title')}」{head.group(2) if head else ''}"
            f"（快照于 {head.group(3) if head else spec.get('as_of')}，数据截至 {spec.get('as_of')}）："
            f"{spec.get('scope')} 在建 {k.get('在建项目', {}).get('value')} 个，按期率 {rate * 100:.1f}%，"
            f"延期 {k.get('延期项目', {}).get('value')} 个，平均延期 {k.get('平均延期', {}).get('value')} 天。"
            + ("风险最高的：" + "；".join(f"{r['name']}（延期 {r['delay_days']} 天，{r['pm']}）" for r in risk) + "。" if risk else ""))


# ================= 项目 Wiki（examples/plugins/llm-wiki，docs/plugin-hooks.md） =================
# 项目里装了「项目 Wiki」插件：成员 Agent 有插件技能 wiki-*，插件服务的工具按原名 llm-wiki__* 出现；每轮推来的提示词里有 wiki 目录（context 挂点），
# 工作目录里有 wiki 的只读副本（mount 挂点）。人说“收进 wiki”就把项目文件编成页面；提问就查 wiki 带引用作答；
# 用户任务验收后网络代验收的人 @ 它写回（after 挂点）。
WIKI = "llm-wiki"


def _wiki_call(call: ScriptCall, tool: str, **args: object) -> str:
    return call.tool(f"{WIKI}__{tool}", **args)


def _wiki_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", name.lower().rsplit(".", 1)[0]).strip("-")[:50] or "note"


def _wiki_page(title: str, source: str, raw: str, lines: list[str], related: list[str] = ()) -> str:
    body = [f"# {title}", "", f"> 来源：{source}；原始资料：[{title}](../../{raw})；更新：{time.strftime('%Y-%m-%d')}", ""]
    body += lines + ([""] + ["## 相关", *related] if related else [])
    return "\n".join(body)


def _wiki(call: ScriptCall) -> str | None:
    if "wiki-ingest" not in call.skills or f"{MCP_PREFIX}{WIKI}__write" not in call.tool_names:
        return None
    text = _asked(call.prompt)
    if not text:
        return None
    if "已验收" in text and "写回" in text:  # after 挂点：代验收的人 @ 过来
        title = m.group(1) if (m := re.search(r"「(.+?)」已验收", text)) else "已验收的结论"
        summary = m.group(1).strip() if (m := re.search(r"交付说明：(.+)$", text)) else ""
        if not summary:
            _wiki_call(call, "log", op="ingest", title=f"no material: {title}", detail="交付说明为空")
            return f"「{title}」的交付没有可写回的结论，只记了一笔日志。"
        slug = _wiki_slug(re.sub(r"[^a-z0-9]+", "-", title.lower())) if re.search(r"[a-z0-9]", title.lower()) else "answer"
        r = _wiki_call(call, "add_source", topic="decisions", slug=f"{slug}-{int(time.time()) % 100000}",
                       title=title, content=summary, source=f"「{title}」的交付，{time.strftime('%Y-%m-%d')} 验收")
        raw = re.search(r"(raw/\S+?\.md)", r).group(1)
        page = re.search(r"raw/decisions/(\S+?)\.md", r).group(1)
        _wiki_call(call, "write", path=f"decisions/{page}", op="ingest", summary=f"已验收：{title}"[:100],
                   content=_wiki_page(title, f"「{title}」的交付（已验收）", raw, [summary]))
        return f"已把「{title}」验收过的结论写回 wiki：新页 `wiki/decisions/{page}.md`，目录和日志已更新。"
    if "收进 wiki" in text or "编进 wiki" in text:
        files = sorted(p for p in (call.cwd / "project").glob("*.md")) if (call.cwd / "project").is_dir() else []
        named = [p for p in files if p.name in text]
        files = named or files
        if not files:
            return "项目文件里没有可以收进 wiki 的 markdown 资料。"
        r = call.tool("open_request", title="把项目资料编进 Wiki", expected_outcome="wiki 里能查到的页面")
        req = m.group(1) if (m := re.search(r"(req-[0-9a-f]{8})", r)) else None
        done = []
        for f in files:
            content = f.read_text(encoding="utf-8")
            title = next((ln[2:].strip() for ln in content.splitlines() if ln.startswith("# ")), f.stem)
            slug = _wiki_slug(f.name)
            raw = re.search(r"(raw/\S+?\.md)", _wiki_call(call, "add_source", topic="research", slug=slug,
                                                            title=title, content=content, source=f"项目文件 {f.name}")).group(1)
            facts = [ln for ln in content.splitlines() if ln.lstrip().startswith(("- ", "* "))][:8]
            _wiki_call(call, "write", path=f"research/{slug}", op="ingest", summary=f"{title}的要点"[:100],
                       content=_wiki_page(title, f"项目文件 {f.name}", raw, ["要点（原文摘录）：", "", *facts]))
            done.append(f"`wiki/research/{slug}.md`（{title}）")
        lint = _wiki_call(call, "lint")
        reply = f"已把 {len(done)} 份资料编进 wiki：" + "、".join(done) + f"。体检：{lint.splitlines()[0]}" + \
            ("没有发现问题。" if "没有发现问题" in lint else "有问题待看：\n" + "\n".join(lint.splitlines()[1:]))
        if req:
            call.tool("deliver_request", request_id=req, summary=reply)
        return reply
    if "？" in text or "?" in text:
        has_index = "项目 Wiki 目录" in call.prompt  # context 挂点把目录推来了
        q = re.sub(r"^@\S+\s*", "", text).strip("？? ")
        terms = [t for t in ("定价", "价格", "访谈", "痛点", "竞品", "渠道") if t in q] or [q[:6]]
        hits = _wiki_call(call, "search", query=terms[0])
        paths = re.findall(r"^- (wiki/\S+?\.md)（(.+?)）", hits, re.M)
        if not paths:
            return f"wiki 里没有找到（搜过“{terms[0]}”）。" + ("" if has_index else "（这一轮也没有收到 wiki 目录）")
        path, title = paths[0]
        page = _wiki_call(call, "read", path=path)
        facts = [ln for ln in page.splitlines() if ln.lstrip().startswith("- ") and any(t in ln for t in terms)] or \
            [ln for ln in page.splitlines() if ln.lstrip().startswith("- ")][:3]
        r = call.tool("open_request", title=q[:40], expected_outcome="带出处的回答")
        req = m.group(1) if (m := re.search(r"(req-[0-9a-f]{8})", r)) else None
        answer = "根据项目 Wiki：" + "；".join(f.strip("- ").strip() for f in facts[:3]) + f"。出处：[{title}]({path})"
        if req:
            call.tool("deliver_request", request_id=req, summary=answer)
        return answer + ("" if has_index else "\n\n（注意：这一轮没有收到 wiki 目录。）")
    return None


# ================= 队友协作（Raft 式，examples/plugins/teammates） =================
# 群聊里人发的每条消息都送给频道成员（不用 @），成员自己判断要不要接：不是自己的事只回“（略过）”。
# 工作项在插件的任务板上，先认领再动手；做完交 in_review，人在群聊里说“#N 完成”。
TEAM = "teammates"
_TEAM_NAMES: dict[str, str] = {}  # Agent id → 名字：续接的会话里提示词不再从名字开头


def _team_said(prompt: str) -> str:
    said = re.findall(r"^\*\*用户[^*]*\*\*(?:（@[^）]*）)?：(.+)$", prompt, re.M)
    if not said:
        said = re.findall(r"^> (.+)$", prompt, re.M)  # route 送来的消息原文
    return said[-1].strip() if said else ""


def _team(call: ScriptCall) -> str | None:
    if "teammate-protocol" not in call.skills or f"{MCP_PREFIX}{TEAM}__tasks" not in call.tool_names:
        return None
    name = _name(call.prompt)
    if name in ("前端", "后端", "测试"):
        _TEAM_NAMES[call.agent_id] = name
    name = _TEAM_NAMES.get(call.agent_id, "")
    text = _team_said(call.prompt)
    t = lambda tool, **a: call.tool(f"{TEAM}__{tool}", **a)  # noqa: E731
    if "做完了吗" in text and "（@ 你）" in call.prompt:  # 人点名问进度：把自己进行中的任务交待验收
        board = t("tasks", mine=True, status="in_progress")
        ids = re.findall(r"^#(\d+)", board, re.M)
        for n in ids:
            t("task_update", id=int(n), status="in_review", note=f"{name}这部分做完了（演示），请验收")
        return f"做完了：{'、'.join('#' + n for n in ids)} 已交待验收。" if ids else "我手上没有进行中的任务。"
    if "建任务" in text:
        board = t("tasks", status="todo")
        if name == "后端" and "验证码" in text:
            n = re.search(r"^#(\d+) .*验证码", board, re.M).group(1)
            r = t("task_claim", id=int(n))
            return f"我来做 #{n}：验证码的生成和校验接口。" if "认领成功" in r else "（略过）"
        if name == "前端" and "登录页" in text:
            r = t("task_create", title="登录页的验证码输入框（配合后端的接口）", claim=True)
            n = re.search(r"#(\d+)", r).group(1)
            return f"前端部分我开了 #{n} 并认领：登录页的验证码输入框，接后端的接口。"
        return "（略过）"
    return "（略过）"


def scenario_handler(call: ScriptCall) -> str:
    if (reply := _from_release(call)) is not None:
        return reply
    if (reply := _dashboard(call)) is not None:
        return reply
    if (reply := _wiki(call)) is not None:
        return reply
    if (reply := _team(call)) is not None:
        return reply
    if (reply := _board_system(call)) is not None:
        return reply
    if MCP_PREFIX + "draft_message" in call.tool_names:
        return _home(call)
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
