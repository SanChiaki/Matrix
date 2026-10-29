// 交付看板：渲染 Agent 生成的看板文件（JSON，格式见技能 delivery-dashboard）。
// 同一份代码两处用：项目里读“看板”表和项目档案；发布物里只读发布时的快照（context().mode === "release"）。
// 跑在 Matrix 的沙箱里：不能联网，读写都经 window.matrix（桥），按看页面的人的权限。
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const SVG = "http://www.w3.org/2000/svg";
let ctx, rows = [], members = [], files = [], sel = null;

const fail = (e) => { $("#err").textContent = e ? e.message || String(e) : ""; };
const fmt = (v, f) => {
  if (v == null || v === "") return "—";
  if (f === "pct") return `${(v * 100).toFixed(1).replace(/\.0$/, "")}%`;
  if (f === "days") return `${v} 天`;
  if (f === "wan") return `${Number(v).toLocaleString("zh-CN")} 万`;
  return typeof v === "number" ? v.toLocaleString("zh-CN") : String(v);
};
const when = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
const el = (tag, attrs = {}, parent) => {
  const n = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.append(n);
  return n;
};

// ---------- 提示框：数值在前、名称在后；内容一律 textContent ----------
function showTip(evt, value, label, line = false) {
  const tip = $("#tip");
  tip.replaceChildren();
  const b = document.createElement("b");
  if (line) { const k = document.createElement("i"); k.className = "key"; b.append(k); }
  b.append(document.createTextNode(value));
  const s = document.createElement("span");
  s.textContent = label;
  tip.append(b, s);
  tip.hidden = false;
  const r = evt.target.getBoundingClientRect?.() || { left: 0, top: 0, width: 0 };
  const x = evt.clientX ?? r.left + r.width / 2, y = evt.clientY ?? r.top;
  tip.style.left = `${Math.min(innerWidth - tip.offsetWidth - 8, x + 12)}px`;
  tip.style.top = `${Math.max(8, y - tip.offsetHeight - 10)}px`;
}
const hideTip = () => { $("#tip").hidden = true; };

// ---------- 刻度：整齐的数 ----------
function ticks(lo, hi, n = 4) {
  const span = hi - lo || Math.abs(hi) || 1, raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const out = [];
  for (let v = Math.floor(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toFixed(10));
  if (out[out.length - 1] < hi) out.push(+(out[out.length - 1] + step).toFixed(10));
  return out;
}

// ---------- 折线：单序列，标题说明是什么，不要图例；十字线跟随最近的点，末端标值 ----------
function lineChart(c) {
  const W = 520, H = 210, L = 44, R = 46, T = 10, B = 24;
  const pts = c.points || [];
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${c.title}：折线图，${pts.length} 个点` });
  if (!pts.length) return svg;
  const ys = pts.map((p) => p.y), lo0 = Math.min(...ys), hi0 = Math.max(...ys), pad = (hi0 - lo0) * 0.25 || 0.05;
  const tk = ticks(Math.max(0, lo0 - pad), hi0 + pad), lo = tk[0], hi = tk[tk.length - 1];
  const x = (i) => L + (pts.length === 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (pts.length - 1));
  const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo || 1));
  for (const t of tk) {
    el("line", { x1: L, x2: W - R, y1: y(t), y2: y(t), class: t === lo ? "base" : "gridline" }, svg);
    el("text", { x: L - 6, y: y(t) + 4, "text-anchor": "end" }, svg).textContent = fmt(t, c.format);
  }
  pts.forEach((p, i) => { if (pts.length <= 12 || i % 2 === 0) el("text", { x: x(i), y: H - 6, "text-anchor": "middle" }, svg).textContent = p.x; });
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p.y)}`).join("");
  el("path", { d: `${d}L${x(pts.length - 1)},${y(lo)}L${x(0)},${y(lo)}Z`, class: "area" }, svg);
  el("path", { d, class: "line" }, svg);
  const last = pts.length - 1;
  el("circle", { cx: x(last), cy: y(pts[last].y), r: 4, class: "dot" }, svg);
  el("text", { x: x(last) + 8, y: y(pts[last].y) + 4, class: "val" }, svg).textContent = fmt(pts[last].y, c.format);
  const cross = el("line", { y1: T, y2: H - B, class: "cross", visibility: "hidden" }, svg);
  const hover = el("circle", { r: 4, class: "dot", visibility: "hidden" }, svg);
  const colW = pts.length > 1 ? (W - L - R) / (pts.length - 1) : W - L - R;
  pts.forEach((p, i) => {
    const hit = el("rect", { x: x(i) - colW / 2, y: T, width: colW, height: H - T - B, class: "hit", tabindex: 0,
                             "aria-label": `${p.x}：${fmt(p.y, c.format)}` }, svg);
    const on = (e) => {
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      hover.setAttribute("cx", x(i)); hover.setAttribute("cy", y(p.y)); hover.setAttribute("visibility", "visible");
      showTip(e, fmt(p.y, c.format), `${p.x} · ${c.title}`, true);
    };
    const off = () => { cross.setAttribute("visibility", "hidden"); hover.setAttribute("visibility", "hidden"); hideTip(); };
    hit.addEventListener("pointermove", on); hit.addEventListener("focus", on);
    hit.addEventListener("pointerleave", off); hit.addEventListener("blur", off);
  });
  return svg;
}

// ---------- 横向条形：单序列，条宽封顶，末端圆角、基线方角，值标在末端 ----------
function barChart(c) {
  const bars = c.bars || [], ROW = 30, BAR = 18, L = 64, R = 48, W = 520, H = Math.max(ROW, bars.length * ROW) + 6;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${c.title}：条形图，${bars.length} 项` });
  const max = Math.max(1, ...bars.map((b) => b.value)), w = (v) => ((W - L - R) * v) / max;
  el("line", { x1: L, x2: L, y1: 0, y2: bars.length * ROW, class: "base" }, svg);
  bars.forEach((b, i) => {
    const y0 = i * ROW + (ROW - BAR) / 2, bw = w(b.value), r = Math.min(4, bw / 2);
    el("text", { x: L - 8, y: y0 + BAR / 2 + 4, "text-anchor": "end" }, svg).textContent = b.label;
    const bar = el("path", { class: "bar", d: bw > 0 ? `M${L},${y0}h${bw - r}a${r},${r} 0 0 1 ${r},${r}v${BAR - 2 * r}a${r},${r} 0 0 1 -${r},${r}h-${bw - r}z` : "" }, svg);
    el("text", { x: L + bw + 6, y: y0 + BAR / 2 + 4, class: "val" }, svg).textContent = fmt(b.value, c.format);
    const hit = el("rect", { x: 0, y: i * ROW, width: W, height: ROW, class: "hit", tabindex: 0, "aria-label": `${b.label}：${fmt(b.value, c.format)}` }, svg);
    const on = (e) => { bar.classList.add("on"); showTip(e, fmt(b.value, c.format), `${b.label} · ${c.title}`); };
    const off = () => { bar.classList.remove("on"); hideTip(); };
    hit.addEventListener("pointermove", on); hit.addEventListener("focus", on);
    hit.addEventListener("pointerleave", off); hit.addEventListener("blur", off);
  });
  return svg;
}

// ---------- 表格（健康度配图标和文字，颜色不单独表意） ----------
const HEALTH = { "红": ["red", "!", "严重"], "黄": ["yellow", "▲", "关注"], "绿": ["green", "✓", "正常"] };
function cell(col, v) {
  if (col.kind === "health" && HEALTH[v]) { const [k, i, t] = HEALTH[v]; return `<span class="health ${k}"><i aria-hidden="true">${i}</i>${t}</span>`; }
  return esc(col.format ? fmt(v, col.format) : v);
}
function tableHtml(t) {
  return `<table><thead><tr>${t.columns.map((c) => `<th class="${c.align === "right" ? "num" : ""}">${esc(c.label)}</th>`).join("")}</tr></thead>
    <tbody>${(t.rows || []).map((r) => `<tr>${t.columns.map((c) => `<td class="${c.align === "right" ? "num" : ""}">${cell(c, r[c.key])}</td>`).join("")}</tr>`).join("")
      || `<tr><td colspan="${t.columns.length}">没有数据</td></tr>`}</tbody></table>`;
}
const chartData = (c) => c.type === "line"
  ? tableHtml({ columns: [{ key: "x", label: "周" }, { key: "y", label: c.title, align: "right", format: c.format }], rows: c.points })
  : tableHtml({ columns: [{ key: "label", label: "类别" }, { key: "value", label: c.title, align: "right", format: c.format }], rows: c.bars });

function render(spec, meta) {
  const board = $("#board");
  board.innerHTML = `<div class="head"><h1>${esc(spec.title || "看板")}</h1>
      <span class="meta">${[spec.scope && `范围：${esc(spec.scope)}`, spec.as_of && `数据截至 ${esc(spec.as_of)}`, meta].filter(Boolean).join(" · ")}</span></div>
    <section class="kpis" aria-label="关键指标">${(spec.kpis || []).map((k) => {
      const dir = k.delta == null ? "" : (k.delta > 0) === (k.good !== "down") ? "good" : k.delta === 0 ? "" : "bad";
      return `<div class="kpi"><div class="label">${esc(k.label)}</div><div class="value">${esc(fmt(k.value, k.format))}</div>
        ${k.delta != null ? `<div class="delta ${dir}">${k.delta > 0 ? "↑" : k.delta < 0 ? "↓" : "→"} ${esc(k.format === "pct" ? `${(Math.abs(k.delta) * 100).toFixed(1)} 个百分点` : fmt(Math.abs(k.delta), k.format))} ${esc(k.delta_label || "")}</div>` : ""}</div>`;
    }).join("")}</section>
    <section class="grid" id="charts"></section>
    ${(spec.tables || []).map((t) => `<section class="panel"><h2>${esc(t.title)}</h2>${tableHtml(t)}</section>`).join("")}
    ${spec.notes ? `<p class="notes">${esc(spec.notes)}</p>` : ""}
    ${spec.source ? `<p class="notes">来源：${esc(spec.source)}</p>` : ""}`;
  const charts = $("#charts");
  for (const c of spec.charts || []) {
    const p = document.createElement("section");
    p.className = "panel";
    const h = document.createElement("h2");
    h.textContent = c.title;
    p.append(h, c.type === "line" ? lineChart(c) : barChart(c));
    const d = document.createElement("details");
    d.className = "data";
    d.innerHTML = `<summary>查看数据</summary>${chartData(c)}`;
    p.append(d);
    charts.append(p);
  }
}

async function show(file, meta) {
  fail();
  if (!file) { $("#board").innerHTML = '<div class="empty">这个看板还没有文件。</div>'; return; }
  try { render(JSON.parse((await matrix.archive.file(file)).text), meta); }
  catch (e) { $("#board").innerHTML = ""; fail(new Error(`读不了看板文件 ${file}：${e.message}`)); }
}

// ---------- 项目里：看板表 + 档案 ----------
const memberName = (id) => members.find((m) => m.id === id)?.name || "";
async function loadProject() {
  rows = await matrix.records.list("dashboards", { limit: 200 });
  matrix.ui.badge(String(rows.filter((r) => r.fields.status === "待发布").length || ""));
  if (!rows.length) {
    $("#pick").innerHTML = ""; $("#acts").innerHTML = "";
    $("#board").innerHTML = `<div class="empty"><p>还没有看板。</p><p>在群聊里说：<code>@看板 Agent 做一个华东交付周报看板</code></p>
      <p>它会用它所有者授权的交付数据查数，生成看板文件，在这里登记。</p></div>`;
    return;
  }
  if (!rows.some((r) => r.id === sel)) sel = rows[0].id;
  $("#pick").innerHTML = rows.map((r) => `<button type="button" data-id="${esc(r.id)}" aria-pressed="${r.id === sel}">${esc(r.fields.title)}<span class="st">${esc(r.fields.status || "")}</span></button>`).join("");
  const r = rows.find((x) => x.id === sel), status = ctx.tables.find((t) => t.key === "dashboards").fields.find((f) => f.key === "status").options;
  const who = memberName(r.fields.owner_agent) || "看板 Agent";
  $("#acts").innerHTML = `<label>状态 <select id="status">${status.map((o) => `<option ${r.fields.status === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></label>
    <button type="button" id="refresh">请 ${esc(who)} 刷新</button>`;
  document.querySelectorAll("#pick [data-id]").forEach((b) => b.addEventListener("click", () => { sel = b.dataset.id; loadProject().catch(fail); }));
  $("#status").addEventListener("change", (e) => matrix.records.update("dashboards", r.id, { status: e.target.value }).then(loadProject).catch(fail));
  $("#refresh").addEventListener("click", () => matrix.chat.post(`@${who} 刷新看板「${r.fields.title}」`).catch(fail));
  const by = r.updated_by?.agent ? memberName(r.updated_by.agent) : "";
  await show(r.fields.file, [r.fields.refresh && r.fields.refresh !== "手动" ? `${r.fields.refresh}刷新` : "", by && `${by} 更新于 ${when(r.updated_at)}`].filter(Boolean).join(" · "));
}

// ---------- 发布物里：只读快照 ----------
async function loadRelease() {
  files = (await matrix.archive.read()).files.filter((f) => f.name.endsWith(".json"));
  const rel = ctx.release;
  if (!files.some((f) => f.name === sel)) sel = files[0]?.name;
  $("#pick").innerHTML = files.length > 1 ? files.map((f) => `<button type="button" data-f="${esc(f.name)}" aria-pressed="${f.name === sel}">${esc(f.name)}</button>`).join("") : "";
  $("#acts").innerHTML = "";
  document.querySelectorAll("#pick [data-f]").forEach((b) => b.addEventListener("click", () => { sel = b.dataset.f; loadRelease().catch(fail); }));
  await show(sel, `发布物「${rel.title}」v${rel.version} · 快照于 ${when(rel.snapshot_at)}`);
}

(async () => {
  try {
    ctx = await matrix.context();
    if (ctx.theme !== "light") document.documentElement.classList.add("dark");
    if (ctx.mode === "release") { await loadRelease(); return; }
    members = await matrix.members().catch(() => []);
    await loadProject();
    matrix.on((event) => { if (event === "records.changed") loadProject().catch(fail); });
  } catch (e) { fail(e); }
})();
