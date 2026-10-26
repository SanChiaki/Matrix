"""用量台账与管理后台的统计（docs/reference/admin.md）。

每次调用运行时，网络往 `<data>/usage.jsonl` 追加一行：谁的 Agent、在哪种场合、云侧还是端侧、用的模型、成没成、
耗时、工具调用次数与运行时报告的 token。不写进 state.json（只追加，不随状态一起反复重写）。

管理后台只看用量，不看内容：台账里不记提示词、回复、工具参数，统计结果里不出现项目名、Agent 名、消息正文，
只有计数、类别与账号邮箱。这里全是纯函数，引擎负责收集状态、调用它们。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

FILE = "usage.jsonl"
DAY_S = 86400
MAX_DAYS = 90

# 各运行时报告 token 的字段名不一样（input_tokens / prompt_tokens / inputTokens ...）：归一化后按名字认
_INPUT = {"inputtokens", "prompttokens", "input", "prompt"}
_OUTPUT = {"outputtokens", "completiontokens", "output", "completion"}
_TOTAL = {"totaltokens", "total"}


def _norm(key: str) -> str:
    return re.sub(r"[^a-z]", "", key.lower())


def tokens(usage: dict[str, Any]) -> dict[str, int]:
    """运行时报告的用量 → {input, output, cached, total}。缓存命中单独列出，不另加进合计；
    认不出的字段忽略（原始字段仍在台账里，以后可以重新解读）。"""
    out = {"input": 0, "output": 0, "cached": 0, "total": 0}
    total = 0
    for k, v in (usage or {}).items():
        if not isinstance(v, int) or isinstance(v, bool):
            continue
        n = _norm(k)
        if n in _INPUT:
            out["input"] += v
        elif n in _OUTPUT:
            out["output"] += v
        elif n in _TOTAL:
            total += v
        elif "cache" in n and ("read" in n or "hit" in n):
            out["cached"] += v
    out["total"] = total or out["input"] + out["output"]
    return out


class UsageLog:
    """用量台账：一行一次运行时调用，只追加。"""

    def __init__(self, root: Path):
        self.path = root / FILE
        self._lock = threading.Lock()

    def record(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def read(self, since: float = 0.0) -> list[dict[str, Any]]:
        """since 以来的记录；坏行（写到一半断电等）跳过。"""
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        with self._lock, self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and float(e.get("ts") or 0) >= since:
                    out.append(e)
        return out

    def first_ts(self) -> float | None:
        """台账从什么时候开始记（这个功能上线之前的运行没有记录）。"""
        if not self.path.is_file():
            return None
        with self._lock, self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    return float(json.loads(line)["ts"])
                except (ValueError, KeyError, TypeError):
                    continue
        return None


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def days_back(now: float, days: int) -> tuple[float, list[str]]:
    """最近 days 天（含今天）的起点（当地零点）与每一天的标签。"""
    t = time.localtime(now)
    midnight = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    since = midnight - (days - 1) * DAY_S
    labels = [day_of(since + i * DAY_S + 3600 * 3) for i in range(days)]  # 偏几个小时，避开夏令时切换
    return since, labels


def _runs_bucket() -> dict[str, Any]:
    return {"runs": 0, "failed": 0, "input": 0, "output": 0, "cached": 0, "total": 0, "tools": 0, "ms": 0}


def _add(b: dict[str, Any], e: dict[str, Any], tk: dict[str, int]) -> None:
    b["runs"] += 1
    b["failed"] += 0 if e.get("ok") else 1
    for k in ("input", "output", "cached", "total"):
        b[k] += tk[k]
    b["tools"] += int(e.get("tools") or 0)
    b["ms"] += int(e.get("ms") or 0)


def _finish(b: dict[str, Any]) -> dict[str, Any]:
    ms = b.pop("ms")
    return {**b, "avg_ms": ms // b["runs"] if b["runs"] else 0}


def summarize(runs: Iterable[dict[str, Any]], *, people: list[dict[str, Any]], messages: list[tuple[str | None, float]],
              requests: list[tuple[str | None, float]], connector_calls: list[tuple[str | None, float, bool]],
              counts: dict[str, int], now: float, days: int, recording_since: float | None) -> dict[str, Any]:
    """管理后台的统计。people：[{id, email, created_at, admin, projects, agents, last_login}]；
    messages / requests：(用户 id, 时间)；connector_calls：(授权人, 时间, 成没成)；counts：当前状态的总数。"""
    since, labels = days_back(now, days)
    daily = {d: {**_runs_bucket(), "messages": 0, "requests": 0, "active": set()} for d in labels}
    total = _runs_bucket()
    by_user: dict[str | None, dict[str, Any]] = defaultdict(_runs_bucket)
    by_model: dict[str, dict[str, Any]] = defaultdict(_runs_bucket)
    by_runtime: dict[tuple[str, str], dict[str, Any]] = defaultdict(_runs_bucket)
    by_venue: dict[str, dict[str, Any]] = defaultdict(_runs_bucket)
    errors: dict[str, int] = defaultdict(int)
    last: dict[str | None, float] = defaultdict(float)
    for e in runs:
        ts = float(e.get("ts") or 0)
        if ts < since:
            continue
        tk = tokens(e.get("usage") or {})
        uid = e.get("user")
        _add(total, e, tk)
        _add(by_user[uid], e, tk)
        _add(by_model[e.get("model") or "默认"], e, tk)
        _add(by_runtime[(e.get("where") or "cloud", e.get("runtime") or "")], e, tk)
        _add(by_venue[e.get("venue") or "other"], e, tk)
        if not e.get("ok"):
            errors[e.get("error") or "runtime_error"] += 1
        d = daily.get(day_of(ts))
        if d is not None:
            _add(d, e, tk)
            if uid:
                d["active"].add(uid)
        last[uid] = max(last[uid], ts)
    msgs: dict[str | None, int] = defaultdict(int)
    for uid, ts in messages:
        if ts < since:
            continue
        msgs[uid] += 1
        last[uid] = max(last[uid], ts)
        d = daily.get(day_of(ts))
        if d is not None:
            d["messages"] += 1
            if uid:
                d["active"].add(uid)
    reqs: dict[str | None, int] = defaultdict(int)
    for uid, ts in requests:
        if ts >= since:
            reqs[uid] += 1
            d = daily.get(day_of(ts))
            if d is not None:
                d["requests"] += 1
    conn: dict[str | None, int] = defaultdict(int)
    conn_failed = 0
    for uid, ts, ok in connector_calls:
        if ts >= since:
            conn[uid] += 1
            conn_failed += 0 if ok else 1

    week = now - 7 * DAY_S
    users = []
    for p in people:
        uid = p["id"]
        seen = max(last.get(uid, 0.0), float(p.get("last_login") or 0))
        users.append({**p, "last_active": seen or None, "messages": msgs.get(uid, 0), "requests": reqs.get(uid, 0),
                      "connector_calls": conn.get(uid, 0), **_finish(dict(by_user.get(uid) or _runs_bucket()))})
    users.sort(key=lambda u: (-u["total"], -u["runs"], -(u["last_active"] or 0)))
    active_ids = set().union(*(d["active"] for d in daily.values())) if daily else set()
    return {
        "range": {"days": days, "since": since, "until": now},
        "recording_since": recording_since,
        "totals": {**counts, **_finish(dict(total)),
                   "messages": sum(msgs.values()), "requests": sum(reqs.values()),
                   "connector_calls": sum(conn.values()), "connector_failed": conn_failed,
                   "active_users": len(active_ids & {p["id"] for p in people}),
                   "active_users_7d": sum(1 for u in users if (u["last_active"] or 0) >= week)},
        "daily": [{"day": k, **_finish({kk: vv for kk, vv in v.items() if kk not in ("active",)}),
                   "active": len(v["active"])} for k, v in daily.items()],
        "users": users,
        "models": sorted(({"model": k, **_finish(dict(v))} for k, v in by_model.items()), key=lambda x: -x["runs"]),
        "runtimes": sorted(({"where": k[0], "runtime": k[1], **_finish(dict(v))} for k, v in by_runtime.items()),
                           key=lambda x: -x["runs"]),
        "venues": sorted(({"venue": k, **_finish(dict(v))} for k, v in by_venue.items()), key=lambda x: -x["runs"]),
        "errors": sorted(({"error": k, "count": v} for k, v in errors.items()), key=lambda x: -x["count"]),
    }
