// 管理后台：整个网络的使用情况（/api/admin/usage）。只有计数与类别，不含内容。
const $ = (s, el = document) => el.querySelector(s);
const svgNS = "http://www.w3.org/2000/svg";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const S = { days: 30, data: null, info: null };

const ERRORS = { malformed_tool_call: "工具参数格式错误", empty_response: "空回复", rate_limited: "被限流", timeout: "超时",
  provider_connection: "连不上模型服务", provider_server: "模型服务出错", stopped: "人终止了这一轮",
  runtime_error: "运行时错误", device_offline: "设备不在线" };
const VENUES = { project: "项目群聊", session: "项目会话", dm: "私聊", home: "助理对话", task: "委托", other: "其他" };

const num = (n) => Number(n || 0).toLocaleString("zh-CN");
function compact(n) {
  n = Number(n || 0);
  const a = Math.abs(n);
  if (a >= 1e9) return `${+(n / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${+(n / 1e6).toFixed(1)}M`;
  if (a >= 1e4) return `${+(n / 1e3).toFixed(1)}K`;
  return num(n);
}
const pct = (a, b) => (b ? `${+(a / b * 100).toFixed(1)}%` : "—");
function dur(ms) {
  if (!ms) return "—";
  const s = ms / 1000;
  if (s < 60) return `${+s.toFixed(1)} 秒`;
  return `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`;
}
function when(ts) {
  if (!ts) return '<span class="muted">—</span>';
  const d = new Date(ts * 1000), diff = Date.now() / 1000 - ts;
  const full = d.toLocaleString("zh-CN", { hour12: false });
  let s;
  if (diff < 60) s = "刚刚";
  else if (diff < 3600) s = `${Math.floor(diff / 60)} 分钟前`;
  else if (diff < 86400) s = `${Math.floor(diff / 3600)} 小时前`;
  else if (diff < 86400 * 30) s = `${Math.floor(diff / 86400)} 天前`;
  else s = d.toLocaleDateString("zh-CN");
  return `<time datetime="${d.toISOString()}" title="${esc(full)}">${s}</time>`;
}
const shortDay = (day) => day.slice(5).replace("-", "/");
const runtimeName = (r) => r.where === "device"
  ? `端侧 · ${S.info?.harnesses?.[r.runtime] || r.runtime || "未知"}` : `云侧 · ${r.runtime || "默认"}`;

async function api(path) {
  const r = await fetch(path, { signal: AbortSignal.timeout(30000) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.detail || `HTTP ${r.status}`), { status: r.status });
  return data;
}

function deny(title, text) {
  $("#dash").hidden = true;
  $("#denied").hidden = false;
  $("#denied-title").textContent = title;
  $("#denied-text").textContent = text;
}

// ---------- 数字 ----------
function tile(label, value, sub = "") {
  return `<div class="tile"><span class="tile-label">${esc(label)}</span><b>${value}</b>${sub ? `<span class="tile-sub">${sub}</span>` : ""}</div>`;
}

function renderTiles(d) {
  const t = d.totals;
  $("#tiles-range").innerHTML = [
    tile("运行轮次", compact(t.runs), `失败 ${num(t.failed)}（${pct(t.failed, t.runs)}）`),
    tile("token", compact(t.total), `输入 ${compact(t.input)} · 输出 ${compact(t.output)}${t.cached ? ` · 缓存命中 ${compact(t.cached)}` : ""}`),
    tile("活跃用户", num(t.active_users), `共 ${num(t.users)} 人`),
    tile("人发的消息", compact(t.messages), `登记的用户任务 ${num(t.requests)}`),
    tile("委托", compact(t.tasks), `工具调用 ${compact(t.tools)}`),
    tile("连接器调用", compact(t.connector_calls), t.connector_calls ? `失败 ${num(t.connector_failed)}` : ""),
  ].join("");
  $("#tiles-now").innerHTML = [
    tile("用户", num(t.users), `近 7 天活跃 ${num(t.active_users_7d)}`),
    tile("项目", num(t.projects), `共享 ${num(t.shared_projects)}`),
    tile("Agent", num(t.agents), `端侧 ${num(t.device_agents)} · 正在运行 ${num(t.running)}`),
    tile("设备", num(t.devices), `在线 ${num(t.devices_online)}`),
    tile("我的连接器", num(t.connections), `项目插件 ${num(t.plugins)}`),
    tile("待审核提议", num(t.pending_proposals)),
  ].join("");
  const since = d.recording_since;
  $("#since-note").textContent = since && since > d.range.since
    ? ` 运行轮次与 token 从 ${new Date(since * 1000).toLocaleDateString("zh-CN")} 开始记录，之前的没有。` : "";
}

// ---------- 柱状图 ----------
// 刻度取整：步长是 1 / 2 / 5 × 10ⁿ（计数不出现小数），最多 4 格
function niceScale(v) {
  const raw = Math.max(1, v) / 4, p = 10 ** Math.floor(Math.log10(raw)), m = raw / p;
  const step = Math.max(1, (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p);
  return { step, max: step * Math.max(1, Math.ceil(v / step)) };
}

function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(svgNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  parent?.appendChild(e);
  return e;
}

// 数据端 4px 圆角、基线方角的柱子
function bar(x, y, w, h, r = 4) {
  r = Math.min(r, h, w / 2);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

// series：[{key, cls}]，自下而上叠加；tipOf(day) → 提示的 HTML
function columns(box, days, series, tipOf) {
  box.innerHTML = "";
  const W = Math.max(280, box.clientWidth), H = 200, padL = 40, padR = 8, padT = 10, padB = 24;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H }, box);
  const { step, max } = niceScale(Math.max(0, ...days.map((d) => series.reduce((s, x) => s + (d[x.key] || 0), 0))));
  const y = (v) => padT + (H - padT - padB) * (1 - v / max);
  for (let v = 0; v <= max; v += step) {
    const yy = Math.round(y(v)) + .5, i = v;
    el("line", { x1: padL, x2: W - padR, y1: yy, y2: yy, class: i ? "grid" : "axis" }, svg);
    el("text", { x: padL - 6, y: yy + 4, class: "tick", "text-anchor": "end" }, svg).textContent = compact(v);
  }
  const band = (W - padL - padR) / days.length, bw = Math.max(2, Math.min(24, band - 2));
  const every = Math.ceil(days.length / Math.max(2, Math.floor((W - padL) / 56)));
  days.forEach((d, i) => {
    const x = padL + band * i + (band - bw) / 2;
    let base = 0;
    const parts = series.filter((s) => d[s.key] > 0);
    parts.forEach((s, j) => {
      const top = base + d[s.key];
      const y0 = y(base) - (j ? 2 : 0), y1 = y(top);  // 叠加段之间留 2px 表面间隙
      if (y0 - y1 > 0.5) {
        el("path", { d: j === parts.length - 1 ? bar(x, y1, bw, y0 - y1) : `M${x},${y1}h${bw}V${y0}H${x}Z`, class: `mark ${s.cls}` }, svg);
      }
      base = top;
    });
    if ((days.length - 1 - i) % every === 0) {
      const cx = x + bw / 2, edge = cx > W - 20;  // 贴右边的日期右对齐，不被裁掉
      el("text", { x: edge ? W - 1 : cx, y: H - 6, class: "tick", "text-anchor": edge ? "end" : "middle" }, svg).textContent = shortDay(d.day);
    }
    const hit = el("rect", { x: padL + band * i, y: padT, width: band, height: H - padT - padB, class: "hit" }, svg);
    hit.addEventListener("pointerenter", (ev) => showTip(ev, tipOf(d), hit));
    hit.addEventListener("pointermove", (ev) => moveTip(ev));
    hit.addEventListener("pointerleave", hideTip);
  });
}

function showTip(ev, html, hit) {
  const tip = $("#tip");
  tip.innerHTML = html;
  tip.hidden = false;
  hit.classList.add("on");
  S.hit = hit;
  moveTip(ev);
}
function moveTip(ev) {
  const tip = $("#tip"), r = tip.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY - r.height - 10;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
  if (y < 8) y = ev.clientY + 16;
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}
function hideTip() {
  $("#tip").hidden = true;
  S.hit?.classList.remove("on");
}

function renderCharts(d) {
  const days = d.daily.map((x) => ({ ...x, ok: x.runs - x.failed }));
  columns($("#chart-runs"), days, [{ key: "ok", cls: "ok" }, { key: "failed", cls: "fail" }], (x) =>
    `<b>${esc(x.day)}</b><div><i class="key ok"></i>成功 ${num(x.ok)}</div><div><i class="key fail"></i>失败 ${num(x.failed)}</div>` +
    `<div class="muted">活跃用户 ${num(x.active)} · 人发的消息 ${num(x.messages)}</div>`);
  columns($("#chart-tokens"), days, [{ key: "total", cls: "ok" }], (x) =>
    `<b>${esc(x.day)}</b><div>${num(x.total)} token</div><div class="muted">输入 ${compact(x.input)} · 输出 ${compact(x.output)}</div>`);
}

// ---------- 表格 ----------
function table(id, cols, rows, empty = "这段时间没有记录") {
  const t = $(id);
  if (!rows.length) { t.innerHTML = `<tbody><tr><td class="empty">${empty}</td></tr></tbody>`; return; }
  t.innerHTML = `<thead><tr>${cols.map((c) => `<th${c.num ? ' class="num"' : ""} scope="col">${esc(c.label)}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td${c.num ? ' class="num"' : ""}>${c.html(r)}</td>`).join("")}</tr>`).join("")}</tbody>`;
}
const runCols = [
  { label: "轮次", num: true, html: (r) => num(r.runs) },
  { label: "失败", num: true, html: (r) => r.failed ? `${num(r.failed)} <span class="muted">${pct(r.failed, r.runs)}</span>` : "0" },
  { label: "token", num: true, html: (r) => compact(r.total) },
  { label: "平均耗时", num: true, html: (r) => dur(r.avg_ms) },
];

function renderTables(d) {
  table("#t-users", [
    { label: "账号", html: (u) => `${esc(u.email)}${u.admin ? ' <span class="tag">管理员</span>' : ""}` },
    { label: "注册", html: (u) => when(u.created_at) },
    { label: "最近活跃", html: (u) => when(u.last_active) },
    { label: "项目", num: true, html: (u) => num(u.projects) },
    { label: "Agent", num: true, html: (u) => num(u.agents) },
    { label: "消息", num: true, html: (u) => num(u.messages) },
    { label: "用户任务", num: true, html: (u) => num(u.requests) },
    { label: "连接器调用", num: true, html: (u) => num(u.connector_calls) },
    ...runCols.slice(0, 3),
  ], d.users, "还没有用户");
  table("#t-models", [{ label: "模型", html: (r) => esc(r.model) }, ...runCols], d.models);
  table("#t-runtimes", [{ label: "位置", html: (r) => esc(runtimeName(r)) }, ...runCols], d.runtimes);
  table("#t-venues", [{ label: "场合", html: (r) => esc(VENUES[r.venue] || r.venue) }, ...runCols], d.venues);
  table("#t-errors", [
    { label: "原因", html: (r) => esc(ERRORS[r.error] || r.error) },
    { label: "次数", num: true, html: (r) => num(r.count) },
    { label: "占失败", num: true, html: (r) => pct(r.count, d.totals.failed) },
  ], d.errors, "这段时间没有失败");
  table("#t-daily", [
    { label: "日期", html: (r) => esc(r.day) },
    { label: "活跃用户", num: true, html: (r) => num(r.active) },
    { label: "消息", num: true, html: (r) => num(r.messages) },
    { label: "用户任务", num: true, html: (r) => num(r.requests) },
    ...runCols.slice(0, 2),
    { label: "输入", num: true, html: (r) => num(r.input) },
    { label: "输出", num: true, html: (r) => num(r.output) },
    { label: "token", num: true, html: (r) => num(r.total) },
  ], [...d.daily].reverse());
}

function render() {
  const d = S.data;
  if (!d) return;
  renderTiles(d);
  renderCharts(d);
  renderTables(d);
}

async function load() {
  try {
    S.data = await api(`/api/admin/usage?days=${S.days}`);
  } catch (e) {
    if (e.status === 401) return deny("请先登录", "登录后再打开管理后台。");
    if (e.status === 404) return deny("没有权限", "管理后台只给部署方指定的管理员。");
    return deny("没拿到数据", e.message);
  }
  $("#denied").hidden = true;
  $("#dash").hidden = false;
  render();
}

async function init() {
  const [info, me] = await Promise.all([api("/api/info").catch(() => null), api("/api/me").catch((e) => e)]);
  S.info = info;
  if (me instanceof Error) return deny(me.status === 401 ? "请先登录" : "没拿到数据",
    me.status === 401 ? "登录后再打开管理后台。" : me.message);
  $("#me").textContent = me.email;
  if (!me.admin) return deny("没有权限", "管理后台只给部署方指定的管理员。");
  for (const r of document.querySelectorAll('input[name="days"]')) {
    r.addEventListener("change", () => { S.days = Number(r.value); load(); });
  }
  let t;
  new ResizeObserver(() => { clearTimeout(t); t = setTimeout(() => S.data && renderCharts(S.data), 80); }).observe($(".charts"));
  await load();
}

init();
