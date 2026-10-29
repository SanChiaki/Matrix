"""演示用的“交付数据”远程 MCP 服务（Streamable HTTP，只用标准库）。

代替现有的项目交付数据系统：一组确定性生成的交付项目（区域、阶段、计划 / 预计完成、延期、项目经理），
三个查数工具。**数据权限跟着人走**：每个人用自己的令牌授权，令牌决定能看哪些区域——
`demo-huadong` 只看华东，`demo-huanan` 只看华南，`demo-all` 看全部；别的令牌 401。

    uv run python examples/delivery-dashboard/delivery_data_mcp.py --port 8790

只监听 127.0.0.1。部署方在 connectors.json 里把它登记成连接器（见同目录的 connectors.json）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

REGIONS = ("华东", "华南", "华北")
STAGES = ("立项", "实施", "上线", "验收", "结项")
TOKENS = {"demo-huadong": ("华东",), "demo-huanan": ("华南",), "demo-all": REGIONS}
CLIENTS = ("申城银行", "钱江制造", "鹏城物流", "羊城零售", "京华能源", "津门港务", "金陵医药", "姑苏电子",
           "南海电网", "燕山钢铁", "西子文旅", "深南科技", "海河水务", "滨江地产", "珠江航运", "蓟门教育")
PMS = ("王敏", "李航", "张悦", "陈涛", "刘洋", "赵琳", "孙哲", "周婷")
SYSTEMS = ("核心系统升级", "数据中台", "供应链协同", "客服平台", "风控引擎", "门店数字化", "运维监控", "报表平台")


def _projects(today: dt.date) -> list[dict[str, Any]]:
    """确定性的交付项目：同一天查到的一样，日子往前走，预计完成与延期跟着变一点（每天刷新看得出变化）。"""
    rnd = random.Random(20261028)
    out = []
    for i in range(24):
        region = REGIONS[i % 3]
        stage = STAGES[rnd.randrange(len(STAGES))]
        planned = today + dt.timedelta(days=rnd.randrange(-20, 90))
        drift = rnd.choice((0, 0, 0, 0, 3, 7, 12, 21))
        wobble = (today.toordinal() + i) % 4 if drift else 0  # 每天有一点变化
        delay = drift + wobble if stage != "结项" else 0
        out.append({
            "id": f"DP-{1001 + i}", "name": f"{CLIENTS[i % len(CLIENTS)]} · {SYSTEMS[rnd.randrange(len(SYSTEMS))]}",
            "client": CLIENTS[i % len(CLIENTS)], "region": region, "stage": stage, "pm": PMS[rnd.randrange(len(PMS))],
            "planned_end": planned.isoformat(), "forecast_end": (planned + dt.timedelta(days=delay)).isoformat(),
            "delay_days": delay, "contract_wan": rnd.randrange(80, 900),
            "health": "红" if delay >= 14 else "黄" if delay >= 5 else "绿",
            "accepted_this_week": stage in ("验收", "结项") and rnd.random() < 0.4,
        })
    return out


def _weekly(today: dt.date, regions: tuple[str, ...], current: float) -> list[dict[str, Any]]:
    """近 8 周的按期率：最后一周就是现在的按期率，前 7 周在它附近确定性地起伏（同一天查到的一样）。"""
    out = []
    for k in range(7, -1, -1):
        week = (today - dt.timedelta(weeks=k)).isocalendar().week
        wobble = ((week * 3 + k * 5 + sum(len(r) for r in regions)) % 7 - 3) * 0.02
        rate = current if k == 0 else current + 0.06 + wobble
        out.append({"week": f"W{week:02d}", "on_time_rate": round(min(0.97, max(0.4, rate)), 3)})
    return out


class Data:
    def __init__(self, scope: tuple[str, ...]):
        self.scope = scope
        self.today = dt.date.today()

    def _regions(self, region: str | None) -> tuple[str, ...]:
        if not region:
            return self.scope
        if region not in REGIONS:
            raise ValueError(f"没有区域 {region}（可选 {'、'.join(REGIONS)}）")
        if region not in self.scope:
            raise PermissionError(f"你的账号没有 {region} 的数据权限（能看：{'、'.join(self.scope)}）")
        return (region,)

    def list_projects(self, region: str | None = None, stage: str | None = None) -> list[dict[str, Any]]:
        regions = self._regions(region)
        return [p for p in _projects(self.today) if p["region"] in regions and (not stage or p["stage"] == stage)]

    def project_detail(self, project_id: str) -> dict[str, Any]:
        p = next((x for x in _projects(self.today) if x["id"] == project_id), None)
        if p is None or p["region"] not in self.scope:
            raise LookupError(f"找不到项目 {project_id}，或你没有它的数据权限")
        start = dt.date.fromisoformat(p["planned_end"]) - dt.timedelta(days=120)
        names = ("需求确认", "方案评审", "开发完成", "联调测试", "上线", "验收")
        miles = [{"name": n, "planned": (start + dt.timedelta(days=24 * k)).isoformat(),
                  "done": STAGES.index(p["stage"]) * 1.4 > k} for k, n in enumerate(names)]
        return {**p, "milestones": miles}

    def delivery_summary(self, region: str | None = None) -> dict[str, Any]:
        regions = self._regions(region)
        ps = [p for p in _projects(self.today) if p["region"] in regions]
        live = [p for p in ps if p["stage"] != "结项"]
        delayed = [p for p in live if p["delay_days"] > 0]
        rate = round(1 - len(delayed) / len(live), 3) if live else 1.0
        return {
            "scope": "、".join(regions), "as_of": self.today.isoformat(),
            "active_projects": len(live),
            "on_time_rate": rate,
            "delayed_projects": len(delayed),
            "avg_delay_days": round(sum(p["delay_days"] for p in delayed) / len(delayed), 1) if delayed else 0.0,
            "accepted_this_week": sum(1 for p in ps if p["accepted_this_week"]),
            "contract_wan_in_flight": sum(p["contract_wan"] for p in live),
            "by_stage": [{"stage": s, "count": sum(1 for p in ps if p["stage"] == s)} for s in STAGES],
            "by_region": [{"region": r, "active": sum(1 for p in live if p["region"] == r),
                           "delayed": sum(1 for p in delayed if p["region"] == r)} for r in regions],
            "weekly_on_time": _weekly(self.today, regions, rate),
            "at_risk": [{k: p[k] for k in ("id", "name", "region", "stage", "pm", "delay_days", "health", "forecast_end")}
                        for p in sorted(delayed, key=lambda p: -p["delay_days"])[:8]],
        }


TOOLS = [
    {"name": "delivery_summary", "description": "交付总览：在建项目数、按期率、延期项目、平均延期天数、本周验收、各阶段项目数、近 8 周按期率、延期风险项目。按你账号的数据权限过滤。",
     "inputSchema": {"type": "object", "properties": {"region": {"type": "string", "description": "可选：华东 / 华南 / 华北；不填是你能看的全部"}}}},
    {"name": "list_projects", "description": "交付项目清单（可按区域、阶段筛选）：计划 / 预计完成、延期天数、健康度、项目经理。",
     "inputSchema": {"type": "object", "properties": {"region": {"type": "string"}, "stage": {"type": "string", "description": "立项 / 实施 / 上线 / 验收 / 结项"}}}},
    {"name": "project_detail", "description": "一个交付项目的详情与里程碑。",
     "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"]}},
]


class Handler(BaseHTTPRequestHandler):
    sessions: set[str] = set()

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静一点：只记调用
        pass

    def _send(self, code: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/mcp":
            return self._send(404, {"error": "not found"})
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        scope = TOKENS.get(token)
        if scope is None:
            return self._send(401, {"error": "令牌无效：演示令牌是 demo-huadong / demo-huanan / demo-all"})
        try:
            msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "not json"})
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # 通知
            return self._send(202)
        if method == "initialize":
            sid = secrets.token_hex(8)
            self.sessions.add(sid)
            return self._send(200, {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "delivery-data-demo", "version": "1.0.0"}}},
                {"Mcp-Session-Id": sid})
        if method == "tools/list":
            return self._send(200, {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        if method == "tools/call":
            params = msg.get("params") or {}
            name, args = params.get("name"), params.get("arguments") or {}
            data = Data(scope)
            try:
                fn = {"delivery_summary": data.delivery_summary, "list_projects": data.list_projects,
                      "project_detail": data.project_detail}[name]
                result = fn(**{k: v for k, v in args.items() if isinstance(v, str) and v})
                text, err = json.dumps(result, ensure_ascii=False), False
            except KeyError:
                text, err = f"没有工具 {name}", True
            except (ValueError, PermissionError, LookupError, TypeError) as e:
                text, err = str(e), True
            print(f"[delivery-data] {token} {name} {args} -> {'错误' if err else 'ok'}", flush=True)
            return self._send(200, {"jsonrpc": "2.0", "id": mid,
                                    "result": {"content": [{"type": "text", "text": text}], "isError": err}})
        return self._send(200, {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"没有方法 {method}"}})


def serve(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server


def main() -> None:
    ap = argparse.ArgumentParser(description="演示用的交付数据 MCP 服务")
    ap.add_argument("--port", type=int, default=8790)
    args = ap.parse_args()
    server = serve(args.port)
    print(f"交付数据（演示）：http://127.0.0.1:{args.port}/mcp  令牌 demo-huadong / demo-huanan / demo-all", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
