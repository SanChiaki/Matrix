"""演示用的“已有看板系统”：一个公开的 HTTP API，不设任何限制（代替你们现有的看板系统）。

    uv run python examples/dashboard-system/board_api.py --port 8791 --store .board-system.json

- `POST /api/dashboards` `{title, spec}` → 建草稿，返回 `{id, title, status, url, draft_url}`
- `PUT /api/dashboards/<id>` `{title?, spec?}` → 改草稿（已发布的版本不变，要再发布才更新）
- `POST /api/dashboards/<id>/publish` → 把草稿发布出去，任何人打开 `url` 都能看到
- `GET /api/dashboards`、`GET /api/dashboards/<id>` → 查看
- `GET /d/<id>` 看已发布的版本，`GET /d/<id>?draft=1` 看草稿：给人看的页面

`spec` 是 JSON 字符串：`{scope, as_of, kpis: [{label, value, format}], charts: [{type: bar|line, title, format, bars|points}],
tables: [{title, columns: [{key, label}], rows: [...]}], notes}`。`--public-base` 是返回给人的链接前缀
（默认 `http://board.localhost:<端口>`：浏览器把 *.localhost 解析到本机，域名又能被连接器目录认出）。只监听 127.0.0.1。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class Boards:
    def __init__(self, store: Path | None, public_base: str):
        self.store, self.public_base, self.lock = store, public_base.rstrip("/"), threading.Lock()
        self.items: dict[str, dict[str, Any]] = {}
        if store and store.is_file():
            self.items = json.loads(store.read_text(encoding="utf-8"))

    def _save(self) -> None:
        if self.store:
            self.store.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")

    def view(self, d: dict[str, Any]) -> dict[str, Any]:
        return {"id": d["id"], "title": d["title"], "status": d["status"], "version": d["version"],
                "updated_at": d["updated_at"], "published_at": d.get("published_at"),
                "url": f"{self.public_base}/d/{d['id']}", "draft_url": f"{self.public_base}/d/{d['id']}?draft=1"}

    def create(self, title: str, spec: str) -> dict[str, Any]:
        _check_spec(spec)
        with self.lock:
            bid = str(len(self.items) + 1)
            d = {"id": bid, "title": title or "未命名看板", "draft": spec, "published": None, "status": "草稿",
                 "version": 0, "updated_at": time.time()}
            self.items[bid] = d
            self._save()
            return self.view(d)

    def update(self, bid: str, title: str | None, spec: str | None) -> dict[str, Any]:
        with self.lock:
            d = self._get(bid)
            if spec is not None:
                _check_spec(spec)
                d["draft"] = spec
            if title:
                d["title"] = title
            d["updated_at"] = time.time()
            if d["published"] is not None and d["draft"] != d["published"]:
                d["status"] = "已发布（草稿有改动）"
            self._save()
            return self.view(d)

    def publish(self, bid: str) -> dict[str, Any]:
        with self.lock:
            d = self._get(bid)
            d["published"], d["status"], d["version"] = d["draft"], "已发布", d["version"] + 1
            d["published_at"] = d["updated_at"] = time.time()
            self._save()
            return self.view(d)

    def _get(self, bid: str) -> dict[str, Any]:
        d = self.items.get(str(bid))
        if d is None:
            raise KeyError(f"没有看板 {bid}")
        return d


def _check_spec(spec: str) -> None:
    try:
        v = json.loads(spec)
    except (TypeError, ValueError) as e:
        raise ValueError(f"spec 要是 JSON 字符串：{e}") from e
    if not isinstance(v, dict):
        raise ValueError("spec 要是 JSON 对象")


def _fmt(v: Any, f: str | None) -> str:
    if v is None:
        return "—"
    if f == "pct":
        return f"{float(v) * 100:.1f}%".replace(".0%", "%")
    if f == "days":
        return f"{v} 天"
    return f"{v:,}" if isinstance(v, (int, float)) else str(v)


def render(d: dict[str, Any], draft: bool) -> str:
    """给人看的页面：标题、关键指标、条形（CSS）、表格。草稿页顶上写明是草稿。"""
    raw = d["draft"] if draft else d["published"]
    e = html.escape
    if raw is None:
        body = "<p class='muted'>这个看板还没有发布。</p>"
        spec: dict[str, Any] = {}
    else:
        spec = json.loads(raw)
        kpis = "".join(f"<div class='kpi'><div class='l'>{e(str(k.get('label', '')))}</div>"
                       f"<div class='v'>{e(_fmt(k.get('value'), k.get('format')))}</div></div>" for k in spec.get("kpis", []))
        charts = []
        for c in spec.get("charts", []):
            items = c.get("bars") or [{"label": p.get("x"), "value": p.get("y")} for p in c.get("points", [])]
            top = max([abs(float(i.get("value") or 0)) for i in items] + [1e-9])
            rows = "".join(f"<div class='bar'><span class='bl'>{e(str(i.get('label')))}</span>"
                           f"<span class='track'><span class='fill' style='width:{abs(float(i.get('value') or 0)) / top * 100:.1f}%'></span></span>"
                           f"<span class='bv'>{e(_fmt(i.get('value'), c.get('format')))}</span></div>" for i in items)
            charts.append(f"<section class='panel'><h2>{e(str(c.get('title', '')))}</h2>{rows}</section>")
        tables = []
        for t in spec.get("tables", []):
            cols = t.get("columns", [])
            head = "".join(f"<th>{e(str(col.get('label', '')))}</th>" for col in cols)
            body_rows = "".join("<tr>" + "".join(f"<td>{e(str(r.get(col.get('key'), '')))}</td>" for col in cols) + "</tr>"
                                for r in t.get("rows", []))
            tables.append(f"<section class='panel'><h2>{e(str(t.get('title', '')))}</h2><table><tr>{head}</tr>{body_rows}</table></section>")
        body = (f"<div class='kpis'>{kpis}</div><div class='grid'>{''.join(charts)}</div>{''.join(tables)}"
                + (f"<p class='muted'>{e(str(spec.get('notes')))}</p>" if spec.get("notes") else ""))
    meta = " · ".join(x for x in (spec.get("scope") and f"范围：{spec['scope']}", spec.get("as_of") and f"数据截至 {spec['as_of']}",
                                  "草稿（未发布）" if draft else f"已发布 v{d['version']}") if x)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(d['title'])} · 看板系统</title><style>
body{{margin:0;padding:24px;font:14px/1.5 system-ui,-apple-system,"PingFang SC",sans-serif;background:#f6f6f4;color:#111}}
.sys{{font-size:12px;color:#777;margin-bottom:6px}} h1{{margin:0 0 4px;font-size:20px}} .meta{{color:#555;margin-bottom:16px}}
.draft{{background:#fff4d6;border:1px solid #f0d58a;padding:6px 10px;border-radius:6px;margin-bottom:12px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:12px}}
.kpi,.panel{{background:#fff;border:1px solid #e3e2dc;border-radius:10px;padding:12px 14px}} .kpi .l{{color:#555;font-size:12.5px}} .kpi .v{{font-size:24px;font-weight:600}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px;margin-bottom:12px}} .panel h2{{font-size:13.5px;margin:0 0 8px}}
.bar{{display:grid;grid-template-columns:72px 1fr 64px;gap:8px;align-items:center;margin:4px 0;font-size:12.5px}} .bl{{color:#555;text-align:right}}
.track{{background:#eef2f8;border-radius:4px;height:14px}} .fill{{display:block;height:14px;background:#2a78d6;border-radius:0 4px 4px 0}} .bv{{color:#333}}
table{{border-collapse:collapse;width:100%;font-size:12.5px}} th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee}} th{{color:#555;font-weight:500}}
.muted{{color:#777}}</style></head><body><div class="sys">看板系统（演示）· 公开，任何人都能打开</div>
<h1>{e(d['title'])}</h1><div class="meta">{e(meta)}</div>{"<div class='draft'>这是草稿，还没有发布。</div>" if draft else ""}{body}</body></html>"""


def serve(port: int, store: Path | None = None, public_base: str | None = None) -> ThreadingHTTPServer:
    boards = Boards(store, public_base or f"http://board.localhost:{port}")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
            raw = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            v = json.loads(self.rfile.read(n) or b"{}")
            return v if isinstance(v, dict) else {}

        def _route(self, method: str) -> None:
            path, _, query = self.path.partition("?")
            try:
                if method == "GET" and (m := re.fullmatch(r"/d/(\w+)", path)):
                    return self._send(200, render(boards._get(m.group(1)), "draft=1" in query), "text/html")
                if method == "GET" and path == "/api/dashboards":
                    return self._send(200, [boards.view(d) for d in boards.items.values()])
                if method == "GET" and (m := re.fullmatch(r"/api/dashboards/(\w+)", path)):
                    d = boards._get(m.group(1))
                    return self._send(200, {**boards.view(d), "spec": d["draft"]})
                if method == "POST" and path == "/api/dashboards":
                    b = self._body()
                    return self._send(201, boards.create(str(b.get("title", "")), b.get("spec")))
                if method == "PUT" and (m := re.fullmatch(r"/api/dashboards/(\w+)", path)):
                    b = self._body()
                    return self._send(200, boards.update(m.group(1), b.get("title"), b.get("spec")))
                if method == "POST" and (m := re.fullmatch(r"/api/dashboards/(\w+)/publish", path)):
                    return self._send(200, boards.publish(m.group(1)))
                return self._send(404, {"error": "not found"})
            except KeyError as e:
                return self._send(404, {"error": str(e).strip("'")})
            except ValueError as e:
                return self._send(400, {"error": str(e)})

        def do_GET(self) -> None:  # noqa: N802
            self._route("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._route("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._route("PUT")

    return ThreadingHTTPServer(("127.0.0.1", port), H)


def main() -> None:
    ap = argparse.ArgumentParser(description="演示用的公开看板系统（HTTP API）")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--store", type=Path, default=None, help="把看板存进这个 JSON 文件（不给就只在内存里）")
    ap.add_argument("--public-base", default=None)
    args = ap.parse_args()
    server = serve(args.port, args.store, args.public_base)
    print(f"看板系统（演示）：http://127.0.0.1:{args.port}/api/dashboards ，页面 http://board.localhost:{args.port}/d/<id>", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
