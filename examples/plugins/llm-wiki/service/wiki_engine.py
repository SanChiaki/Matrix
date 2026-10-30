"""LLM Wiki 引擎：给 Agent 用的无模型 wiki 存储（stdio MCP，仅标准库）。

模式参照 Karpathy 的 LLM Wiki：原始资料不可变（raw/），wiki 由 Agent 维护（wiki/），index.md 是目录、log.md 只追加。
引擎只做存储、检索与确定性检查，综合、改写、判断都由调用它的 Agent 做（工作流在插件的技能里）。
Matrix 按项目启动一个进程，工作目录就是这个项目的数据目录：

    raw/<主题>/<日期>-<名字>.md     原始资料，写进去就不再改
    wiki/<主题>/<名字>.md           Agent 维护的页面
    wiki/index.md                  目录：每页一行（链接、摘要、更新日期），按主题分组
    wiki/log.md                    操作日志，只追加
    history/<页面路径>/<时间>.md     页面被覆盖前的版本

用法：python wiki_engine.py --root <数据目录>
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(sys.argv[sys.argv.index("--root") + 1] if "--root" in sys.argv else ".").resolve()
RAW, WIKI, HISTORY = ROOT / "raw", ROOT / "wiki", ROOT / "history"
INDEX, LOG = WIKI / "index.md", WIKI / "log.md"
SLUG = re.compile(r"^[a-z0-9一-鿿][a-z0-9一-鿿-]{0,59}$")
PAGE_LIMIT = 60_000
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*%?(?![\w])")


def today() -> str:
    return time.strftime("%Y-%m-%d")


def slug(v: object, what: str) -> str:
    s = str(v or "").strip().lower()
    if not SLUG.match(s):
        raise ValueError(f"{what} 无效：{v!r}（小写字母、数字、中文、连字符，最长 60）")
    return s


def inside(base: Path, rel: str) -> Path:
    p = (ROOT / str(rel or "").strip().lstrip("/")).resolve()
    if p != base and base not in p.parents:
        raise ValueError(f"路径要在 {base.relative_to(ROOT).as_posix()}/ 下：{rel!r}")
    return p


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def init() -> None:
    for d in (RAW, WIKI, HISTORY):
        d.mkdir(parents=True, exist_ok=True)
    if not INDEX.exists():
        INDEX.write_text("# 项目 Wiki 目录\n\n还没有页面。用 wiki_add_source 放进原始资料，再按技能 wiki-ingest 编进来。\n", encoding="utf-8")
    if not LOG.exists():
        LOG.write_text("# Wiki 日志\n", encoding="utf-8")


def log(op: str, title: str, detail: str = "") -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n## [{today()}] {op} | {title}\n" + (f"{detail.strip()}\n" if detail.strip() else ""))


def pages() -> list[Path]:
    return sorted(p for p in WIKI.rglob("*.md") if p not in (INDEX, LOG))


def title_of(p: Path) -> str:
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return p.stem


# ---- 目录：按主题分组，每页一行 ----
def read_index() -> dict[str, dict[str, str]]:
    """{页面相对 wiki/ 的路径: {topic, title, summary, updated}}"""
    out: dict[str, dict[str, str]] = {}
    topic = ""
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            topic = line[3:].strip()
        m = re.match(r"^- \[(.+?)\]\((.+?)\) — (.*?)（更新 (\d{4}-\d{2}-\d{2})）\s*$", line)
        if m:
            out[m.group(2)] = {"topic": topic, "title": m.group(1), "summary": m.group(3), "updated": m.group(4)}
    return out


def write_index(entries: dict[str, dict[str, str]]) -> None:
    lines = ["# 项目 Wiki 目录", ""]
    if not entries:
        lines.append("还没有页面。")
    for topic in sorted({e["topic"] for e in entries.values()}):
        lines += [f"## {topic}", ""]
        for path, e in sorted(entries.items(), key=lambda kv: kv[1]["title"]):
            if e["topic"] == topic:
                lines.append(f"- [{e['title']}]({path}) — {e['summary']}（更新 {e['updated']}）")
        lines.append("")
    INDEX.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# ---- 工具 ----
def t_index(a: dict) -> str:
    return INDEX.read_text(encoding="utf-8")


def t_list(a: dict) -> str:
    prefix = str(a.get("prefix") or "")
    rows = [f"- {rel(p)}：{title_of(p)}" for p in pages() if rel(p).startswith(prefix)]
    raws = [f"- {rel(p)}" for p in sorted(RAW.rglob("*.md")) if rel(p).startswith(prefix)]
    return "\n".join(["## 页面", *(rows or ["（没有）"]), "", "## 原始资料", *(raws or ["（没有）"])])


def t_read(a: dict) -> str:
    p = inside(ROOT, str(a.get("path", "")))
    if not p.is_file() or not (RAW in p.parents or WIKI in p.parents):
        raise ValueError(f"没有这个文件：{a.get('path')!r}（用 wiki_list 看有哪些）")
    if a.get("version"):
        hp = HISTORY / rel(p) / f"{a['version']}.md"
        if not hp.is_file():
            raise ValueError(f"这一页没有版本 {a['version']}")
        return hp.read_text(encoding="utf-8")
    return p.read_text(encoding="utf-8")


def t_search(a: dict) -> str:
    q = str(a.get("query") or "").strip()
    if not q:
        raise ValueError("query 不能为空")
    terms = [t.lower() for t in q.split() if t]
    scope = str(a.get("scope") or "wiki")
    limit = max(1, min(int(a.get("limit") or 20), 100))
    roots = {"wiki": [WIKI], "raw": [RAW], "all": [WIKI, RAW]}.get(scope, [WIKI])
    hits: list[tuple[int, str]] = []
    for base in roots:
        for p in sorted(base.rglob("*.md")):
            if p == LOG:
                continue
            text = p.read_text(encoding="utf-8")
            low = text.lower()
            score = sum(low.count(t) for t in terms)
            if not score or not all(t in low for t in terms):
                continue
            lines = [ln.strip() for ln in text.splitlines() if any(t in ln.lower() for t in terms)][:3]
            hits.append((score, f"- {rel(p)}（{title_of(p)}）\n" + "\n".join(f"  > {ln[:200]}" for ln in lines)))
    hits.sort(key=lambda h: -h[0])
    return "\n".join(h for _, h in hits[:limit]) or f"wiki 里没有找到“{q}”（搜过 {scope}）。"


def t_add_source(a: dict) -> str:
    topic = slug(a.get("topic"), "topic")
    name = slug(a.get("slug"), "slug")
    content = str(a.get("content") or "").strip()
    if not content:
        raise ValueError("content 不能为空：原始资料的全文")
    if len(content) > PAGE_LIMIT:
        raise ValueError(f"原始资料最长 {PAGE_LIMIT} 个字符")
    pub = str(a.get("published") or "").strip()
    stem = f"{pub}-{name}" if re.match(r"^\d{4}-\d{2}-\d{2}$", pub) else name
    p, n = RAW / topic / f"{stem}.md", 2
    while p.exists():  # 原始资料不可变：同名另起一份
        p, n = RAW / topic / f"{stem}-{n}.md", n + 1
    p.parent.mkdir(parents=True, exist_ok=True)
    head = [f"# {str(a.get('title') or name).strip()}", "",
            f"> 出处：{str(a.get('source') or '未注明').strip()}；收集：{today()}；发布：{pub or '未知'}", ""]
    p.write_text("\n".join(head) + content + "\n", encoding="utf-8")
    return f"已存入原始资料 {rel(p)}（之后不能再改）。下一步按 wiki-ingest 分诊、编进页面。"


def t_write(a: dict) -> str:
    path = str(a.get("path") or "").strip()
    m = re.match(r"^(?:wiki/)?([^/]+)/([^/]+?)(?:\.md)?$", path)
    if not m:
        raise ValueError("path 写成 <主题>/<名字>，如 product/pricing")
    topic, name = slug(m.group(1), "主题"), slug(m.group(2), "页面名")
    content = str(a.get("content") or "").strip()
    summary = " ".join(str(a.get("summary") or "").split())
    op = str(a.get("op") or "update")
    if op not in ("ingest", "update", "archive"):
        raise ValueError("op 只能是 ingest / update / archive")
    if not content.startswith("# "):
        raise ValueError("content 要以一级标题开头（# 标题）")
    if not summary or len(summary) > 120:
        raise ValueError("summary 是目录里的一句话摘要，1–120 个字")
    if len(content) > PAGE_LIMIT:
        raise ValueError(f"一页最长 {PAGE_LIMIT} 个字符")
    p = WIKI / topic / f"{name}.md"
    if op == "archive" and p.exists():
        raise ValueError("归档页总是新的一页，不覆盖已有页面：换个名字")
    if p.exists():  # 覆盖前留一版
        h = HISTORY / rel(p) / f"{time.strftime('%Y%m%d-%H%M%S')}.md"
        h.parent.mkdir(parents=True, exist_ok=True)
        h.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content + "\n", encoding="utf-8")
    entries = read_index()
    key = f"{topic}/{name}.md"
    entries[key] = {"topic": topic, "title": title_of(p), "updated": today(),
                    "summary": ("[归档] " if op == "archive" else "") + summary}
    write_index(entries)
    log(op, title_of(p), str(a.get("log") or f"页面：{key}"))
    return f"已写入 wiki/{key}，目录与日志已更新。"


def t_log(a: dict) -> str:
    op = str(a.get("op") or "").strip()
    if op not in ("ingest", "query", "lint"):
        raise ValueError("op 只能是 ingest / query / lint")
    title = " ".join(str(a.get("title") or "").split())
    if not title:
        raise ValueError("title 不能为空")
    log(op, title, str(a.get("detail") or ""))
    return "已记进 wiki/log.md。"


def t_history(a: dict) -> str:
    p = inside(WIKI, str(a.get("path", "")))
    d = HISTORY / rel(p)
    vs = sorted((x.stem for x in d.glob("*.md")), reverse=True) if d.is_dir() else []
    return "\n".join(f"- {v}" for v in vs) or "这一页还没有旧版本。"


def t_lint(a: dict) -> str:
    """确定性体检：目录与页面一致、内部链接、原始资料引用、数字能在原始资料里找到。只报告，不修改。"""
    issues: list[str] = []
    entries = read_index()
    for path in entries:
        if not (WIKI / path).is_file():
            issues.append(f"- 目录：`{path}` 在目录里，但页面不存在")
    for p in pages():
        r = p.relative_to(WIKI).as_posix()
        text = p.read_text(encoding="utf-8")
        if r not in entries:
            issues.append(f"- 孤儿页：`wiki/{r}` 不在目录里")
        raws: list[Path] = []
        meta = next((ln for ln in text.splitlines() if ln.startswith("> ") and "原始资料" in ln), "")
        archived = entries.get(r, {}).get("summary", "").startswith("[归档]")
        if not meta and not archived:
            issues.append(f"- 缺出处：`wiki/{r}` 没有“原始资料”一行")
        for _, href in LINK.findall(text):
            if href.startswith(("http://", "https://", "#")):
                continue
            path = href.split("#")[0]
            # wiki/…、raw/… 开头的是相对数据根目录的写法（Agent 在对话里引用页面就这么写），其余相对当前页面
            target = (ROOT / path if path.startswith(("wiki/", "raw/")) else p.parent / path).resolve()
            if not target.is_file():
                issues.append(f"- 坏链接：`wiki/{r}` → `{href}`")
            elif RAW in target.parents and meta and href in meta:
                raws.append(target)
        if raws:  # 数字、百分比要原样出现在它引用的原始资料里
            source = "\n".join(x.read_text(encoding="utf-8") for x in raws)
            body = "\n".join(ln for ln in text.splitlines() if not ln.startswith("> "))
            body = LINK.sub(lambda m: m.group(1), body)
            for n in sorted(set(NUMBER.findall(body))):
                if len(n.rstrip("%")) >= 2 and n not in source:
                    issues.append(f"- 数字对不上：`wiki/{r}` 里的 {n} 在它引用的原始资料里找不到")
    head = f"体检了 {len(pages())} 页、{len(list(RAW.rglob('*.md')))} 份原始资料："
    return head + ("\n" + "\n".join(issues) if issues else "没有发现问题。")


TOOLS = {
    "wiki_index": (t_index, "读 wiki 目录（index.md）：每页一行，按主题分组。回答问题前先读它。", {}, []),
    "wiki_list": (t_list, "列出全部页面与原始资料的路径。", {"prefix": "可选：只列这个前缀开头的"}, []),
    "wiki_read": (t_read, "读一个页面或原始资料（path：如 wiki/product/pricing.md、raw/market/2026-10-01-report.md；version：可选，旧版本）。",
                  {"path": "文件路径", "version": "可选：wiki_history 列出的版本"}, ["path"]),
    "wiki_search": (t_search, "全文搜索（query：空格分开的词，全部命中才算；scope：wiki（默认）/ raw / all；limit）。",
                    {"query": "关键词", "scope": "wiki / raw / all", "limit": "最多几条"}, ["query"]),
    "wiki_history": (t_history, "列出一个页面的旧版本。", {"path": "wiki/ 下的页面路径"}, ["path"]),
    "wiki_lint": (t_lint, "体检：目录与页面是否一致、坏链接、缺出处、数字能否在引用的原始资料里原样找到。只报告。", {}, []),
    "wiki_add_source": (t_add_source, "存入一份原始资料（不可变，存进去就不能改）：topic、slug、title、content（全文），可选 source（出处）、published（YYYY-MM-DD）。",
                        {"topic": "主题，如 market", "slug": "文件名，如 q3-report", "title": "标题", "content": "全文",
                         "source": "出处（网址、文件名或作者）", "published": "发布日期 YYYY-MM-DD"},
                        ["topic", "slug", "title", "content"]),
    "wiki_write": (t_write, "新建或覆盖一个页面并更新目录与日志（path：<主题>/<名字>；content：整页 markdown，以 # 标题开头；summary：目录里的一句话；op：ingest / update / archive；log：可选，写进日志的说明）。覆盖前自动留旧版本。",
                   {"path": "<主题>/<名字>", "content": "整页 markdown", "summary": "一句话摘要", "op": "ingest / update / archive",
                    "log": "可选：日志说明"}, ["path", "content", "summary"]),
    "wiki_log": (t_log, "只往日志里记一笔（op：ingest / query / lint；title；detail）。用于“没有新内容”的资料或体检结果。",
                 {"op": "ingest / query / lint", "title": "标题", "detail": "说明"}, ["op", "title"]),
}


def schema(params: dict, required: list) -> dict:
    props = {k: {"type": "integer" if k == "limit" else "string", "description": v} for k, v in params.items()}
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


READ_ONLY = {"wiki_index", "wiki_list", "wiki_read", "wiki_search", "wiki_history", "wiki_lint"}


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    init()
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue  # 通知
        if method == "initialize":
            result: dict = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                            "serverInfo": {"name": "matrix-llm-wiki", "version": "0.1.0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": n, "description": d, "inputSchema": schema(p, r),
                                 "annotations": {"readOnlyHint": n in READ_ONLY}}
                                for n, (_, d, p, r) in TOOLS.items()]}
        elif method == "tools/call":
            p = msg.get("params") or {}
            fn = TOOLS.get(str(p.get("name")), (None,))[0]
            try:
                if fn is None:
                    raise ValueError(f"没有工具 {p.get('name')}")
                text, err = fn(p.get("arguments") or {}), False
            except (ValueError, OSError) as e:
                text, err = str(e), True
            result = {"content": [{"type": "text", "text": text}], "isError": err}
        elif method == "ping":
            result = {}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid,
                                         "error": {"code": -32601, "message": f"unknown method {method}"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
