// 管理后台：整个网络的使用情况（/api/admin/usage，只有计数与类别，不含内容）与云侧模型目录（/api/admin/models）。
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

async function api(path, opts = {}) {
  if (opts.body) opts = { ...opts, body: JSON.stringify(opts.body), headers: { "Content-Type": "application/json" } };
  const r = await fetch(path, { ...opts, signal: AbortSignal.timeout(30000) });
  const data = await r.json().catch(() => ({}));
  const detail = Array.isArray(data.detail) ? data.detail.map((x) => x.msg).join("；") : data.detail;
  if (!r.ok) throw Object.assign(new Error(detail || `HTTP ${r.status}`), { status: r.status });
  return data;
}

function deny(title, text) {
  $("#dash").hidden = true;
  $("#models").hidden = true;
  $("#plugins").hidden = true;
  $(".admin-tabs").hidden = true;
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
    `<tbody>${rows.map((r, i) => `<tr>${cols.map((c) => `<td${c.num ? ' class="num"' : ""}>${c.html(r, i)}</td>`).join("")}</tr>`).join("")}</tbody>`;
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
  render();
}

// ---------- 模型目录 ----------
const M = { data: null, edit: null, editProvider: null };
const API_NAMES = { "openai-completions": "OpenAI Chat Completions", "openai-responses": "OpenAI Responses",
  "anthropic-messages": "Anthropic Messages" };
const ourProvider = (id) => M.data?.providers.find((p) => p.id === id);
const modelText = (m) => `${ourProvider(m.provider)?.name || m.provider} / ${m.model}${m.reasoning_effort ? ` · ${m.reasoning_effort}` : ""}`;

async function loadCatalog() {
  try { M.data = await api("/api/admin/models"); }
  catch (e) { $("#m-error").textContent = e.message; return; }
  renderCatalog();
}

function renderCatalog() {
  const d = M.data;
  const rt = d.runtime_default ? modelText(d.runtime_default) : "";
  $("#m-status").innerHTML = (d.active
    ? `<span class="pill on">已启用</span><span>云侧 Agent 只能选下面的模型。</span>`
    : `<span class="pill off">未启用</span><span>云侧 Agent 现在可以自由填写模型。添加第一个模型即启用：之后只能选这里的，`
      + "已有 Agent 自己填的模型和目录里的对得上就算选了它，对不上的改用默认模型。</span>")
    + (d.error ? `<div class="warn-box">目录文件 <code>${esc(d.path)}</code> 有错（${esc(d.error)}），`
      + `${d.active ? "现在用的是上一份能用的" : "现在只能用运行时默认"}。在这里保存会整份覆盖它。</div>` : "");
  table("#t-presets", [
    { label: "名称", html: (m) => `<b>${esc(m.label)}</b>${m.id === d.default ? ' <span class="tag">默认</span>' : ""}`
      + (m.description ? `<div class="muted">${esc(m.description)}</div>` : "") },
    { label: "模型", html: (m) => `<code>${esc(modelText(m))}</code>`
      + (ourProvider(m.provider) && !ourProvider(m.provider).has_key ? ' <span class="key-state missing">供应商没设密钥</span>' : "") },
    { label: "标识", html: (m) => `<code>${esc(m.id)}</code>` },
    { label: "在用的 Agent", num: true, html: (m) => num(m.agents) },
    { label: "", html: (m) => `<span class="row-actions"><button type="button" class="link" data-edit="${esc(m.id)}">编辑</button>`
      + `<button type="button" class="link danger" data-del="${esc(m.id)}">删除</button></span>` },
  ], d.models, d.active ? "目录是空的：云侧 Agent 只能用运行时默认的模型" : "还没有模型");
  table("#t-providers", [
    { label: "名称", html: (p) => `<b>${esc(p.name)}</b> <code>${esc(p.id)}</code>` },
    { label: "协议", html: (p) => esc(API_NAMES[p.api] || p.api) },
    { label: "地址", html: (p) => `<code>${esc(p.base_url)}</code>` },
    { label: "密钥", html: (p) => p.has_key ? '<span class="key-state">已设置</span>' : '<span class="key-state missing">未设置</span>' },
    { label: "模型", num: true, html: (p) => num(d.models.filter((m) => m.provider === p.id).length) },
    { label: "", html: (p) => `<span class="row-actions"><button type="button" class="link" data-pedit="${esc(p.id)}">编辑</button>`
      + `<button type="button" class="link danger" data-pdel="${esc(p.id)}">删除</button></span>` },
  ], d.providers, "还没有：模型都用 dsh 里已配置的供应商");
  const sel = $("#m-default");
  sel.innerHTML = `<option value="">运行时默认${rt ? `（${esc(rt)}）` : ""}</option>`
    + d.models.map((m) => `<option value="${esc(m.id)}" ${m.id === d.default ? "selected" : ""}>${esc(m.label)}</option>`).join("");
  sel.disabled = !d.models.length;
  $("#m-default-note").textContent = `${num(d.default_agents)} 个云侧 Agent 用默认模型`;
  $("#m-unlisted-box").hidden = !d.unlisted.length;
  $("#m-unlisted-hint").textContent = d.active
    ? "这些 Agent 选的模型不在目录里（启用目录前自己填的，或选的已经删掉），这一轮起改用默认模型。加回目录后它们自动用回原来的。"
    : "这些是 Agent 现在自己填的模型。启用目录前先加进来，它们就会继续用原来的模型，否则改用默认。";
  table("#t-unlisted", [
    { label: "模型", html: (r) => r.provider ? `<code>${esc(modelText(r))}</code>` : `已删除的 <code>${esc(r.preset)}</code>` },
    { label: "Agent", num: true, html: (r) => num(r.agents) },
    { label: "", html: (r, i) => `<button type="button" class="link" data-adopt="${i}">加入目录</button>` },
  ], d.unlisted);
}

function slug(text, taken, fallback) {
  const base = String(text).toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[^a-z0-9]+|-+$/g, "").slice(0, 56) || fallback;
  let id = base, n = 2;
  while (taken.some((x) => x.id === id)) id = `${base}-${n++}`;
  return id;
}

// 整份保存：没改的部分取当前的。供应商的密钥只在 keys 里（这次新填的），不回传已存的
async function saveCatalog({ models = M.data.models, dflt = M.data.default, providers = M.data.providers, keys = {} } = {}) {
  const body = { default: dflt || null, rev: M.data.rev,
    models: models.map((m) => ({ id: m.id, label: m.label || "", provider: m.provider, model: m.model,
      reasoning_effort: m.reasoning_effort || null, description: m.description || "",
      context_window: m.context_window || null, max_tokens: m.max_tokens || null, image: !!m.image })),
    providers: providers.map((p) => ({ id: p.id, name: p.name, api: p.api, base_url: p.base_url,
      api_key: keys[p.id] || null })) };
  M.data = await api("/api/admin/models", { method: "PUT", body });
  renderCatalog();
}

const DSH = "__dsh";  // 模型表单里“dsh 里已配置的供应商”

function syncModelForm() {
  const f = $("#m-form"), ours = f.source.value !== DSH;
  $("#mf-provider-box").hidden = ours;
  f.provider.required = !ours;
  for (const el of f.querySelectorAll("[data-ours]")) el.hidden = !ours;
  $("#mf-hint").textContent = (ours
    ? "模型名照供应商文档写。推理强度用 dsh 的名字（off、minimal、low、medium、high、xhigh、max），原样交给供应商。"
    : "供应商名与模型要和 dsh 里配置的一致，服务器上的 dsh 要有对应的凭据。") + "标识保存后不能改：Agent 按它记住选了哪个。";
}

function openForm(m = null, prefill = {}) {
  M.edit = m;
  const f = $("#m-form");
  f.reset();
  const v = m || prefill;
  f.source.innerHTML = M.data.providers.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("")
    + `<option value="${DSH}">dsh 里已配置的供应商</option>`;
  f.source.value = ourProvider(v.provider) ? v.provider : (v.provider || !M.data.providers.length ? DSH : M.data.providers[0].id);
  for (const k of ["label", "model", "reasoning_effort", "description", "context_window", "max_tokens"]) f[k].value = v[k] || "";
  f.provider.value = f.source.value === DSH ? v.provider || "" : "";
  f.image.checked = !!v.image;
  f.pid.value = v.id || "";
  f.pid.readOnly = !!m;
  syncModelForm();
  $("#m-form-title").textContent = m ? `编辑 ${m.label}` : "添加模型";
  $("#mf-error").textContent = "";
  $("#m-dialog").showModal();
  f.label.focus();
}

async function submitForm(ev) {
  ev.preventDefault();
  const f = ev.target, v = (k) => f[k].value.trim(), ours = f.source.value !== DSH;
  const int = (k) => (ours && v(k) ? Number(v(k)) : null);
  const m = { id: M.edit?.id || v("pid") || slug(v("model"), M.data.models, "model"), label: v("label"),
    provider: ours ? f.source.value : v("provider"), model: v("model"), reasoning_effort: v("reasoning_effort"),
    description: v("description"), context_window: int("context_window"), max_tokens: int("max_tokens"),
    image: ours && f.image.checked };
  if (!M.edit && M.data.models.some((x) => x.id === m.id)) { $("#mf-error").textContent = `标识 ${m.id} 已经有了`; return; }
  const models = M.edit ? M.data.models.map((x) => (x.id === M.edit.id ? m : x)) : [...M.data.models, m];
  $("#mf-submit").disabled = true;
  try { await saveCatalog({ models }); $("#m-dialog").close(); }
  catch (e) { $("#mf-error").textContent = e.message; if (e.status === 409 && /别处/.test(e.message)) loadCatalog(); }
  finally { $("#mf-submit").disabled = false; }
}

function openProvider(p = null) {
  M.editProvider = p;
  const f = $("#p-form");
  f.reset();
  for (const k of ["name", "base_url"]) f[k].value = p?.[k] || "";
  f.api.value = p?.api || "openai-completions";
  f.pid.value = p?.id || "";
  f.pid.readOnly = !!p;
  f.api_key.required = !p?.has_key;
  f.api_key.placeholder = p?.has_key ? "已设置，留空不改" : "必填：dsh 没有密钥不会发请求";
  $("#p-form-title").textContent = p ? `编辑 ${p.name}` : "添加供应商";
  $("#pf-error").textContent = "";
  $("#p-dialog").showModal();
  f.name.focus();
}

async function submitProvider(ev) {
  ev.preventDefault();
  const f = ev.target, v = (k) => f[k].value.trim();
  const p = { id: M.editProvider?.id || v("pid") || slug(v("name"), M.data.providers, "provider"), name: v("name"),
    api: f.api.value, base_url: v("base_url") };
  if (!M.editProvider && M.data.providers.some((x) => x.id === p.id)) { $("#pf-error").textContent = `标识 ${p.id} 已经有了`; return; }
  if (!M.data.active && !confirm("保存供应商会启用模型目录：之后云侧 Agent 只能选目录里的模型，自己填的模型对不上的改用默认。"
    + "接着把要用的模型加进来。继续？")) return;
  const providers = M.editProvider ? M.data.providers.map((x) => (x.id === p.id ? p : x)) : [...M.data.providers, p];
  $("#pf-submit").disabled = true;
  try {
    await saveCatalog({ providers, keys: v("api_key") ? { [p.id]: v("api_key") } : {} });
    f.api_key.value = "";
    $("#p-dialog").close();
  } catch (e) { $("#pf-error").textContent = e.message; if (e.status === 409 && /别处/.test(e.message)) loadCatalog(); }
  finally { $("#pf-submit").disabled = false; }
}

function bindCatalog() {
  $("#m-add").addEventListener("click", () => openForm());
  $("#p-add").addEventListener("click", () => openProvider());
  $("#mf-cancel").addEventListener("click", () => $("#m-dialog").close());
  $("#pf-cancel").addEventListener("click", () => $("#p-dialog").close());
  $("#m-form").addEventListener("submit", submitForm);
  $("#p-form").addEventListener("submit", submitProvider);
  $("#mf-source").addEventListener("change", syncModelForm);
  $("#m-default").addEventListener("change", async (ev) => {
    try { await saveCatalog({ dflt: ev.target.value }); }
    catch (e) { $("#m-error").textContent = e.message; loadCatalog(); }
  });
  $("#models").addEventListener("click", async (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    const d = M.data;
    $("#m-error").textContent = "";
    if (b.dataset.edit) openForm(d.models.find((m) => m.id === b.dataset.edit));
    if (b.dataset.pedit) openProvider(d.providers.find((p) => p.id === b.dataset.pedit));
    if (b.dataset.adopt) {
      const r = d.unlisted[Number(b.dataset.adopt)];
      openForm(null, r.provider ? { ...r, label: r.model } : { id: r.preset });
    }
    if (b.dataset.del) {
      const m = d.models.find((x) => x.id === b.dataset.del);
      const users = m.agents ? `${m.agents} 个 Agent 在用它，删掉后这些 Agent 改用默认模型。` : "";
      if (!confirm(`删除 ${m.label}？${users}`)) return;
      try { await saveCatalog({ models: d.models.filter((x) => x.id !== m.id), dflt: d.default === m.id ? null : d.default }); }
      catch (e) { $("#m-error").textContent = e.message; loadCatalog(); }
    }
    if (b.dataset.pdel) {
      const p = d.providers.find((x) => x.id === b.dataset.pdel);
      const using = d.models.filter((m) => m.provider === p.id);
      if (using.length) {
        $("#m-error").textContent = `${p.name} 还有 ${using.length} 个模型在用（${using.map((m) => m.label).join("、")}），先删掉或改用别的供应商。`;
        return;
      }
      if (!confirm(`删除供应商 ${p.name}？保存的密钥一起删掉。`)) return;
      try { await saveCatalog({ providers: d.providers.filter((x) => x.id !== p.id) }); }
      catch (e) { $("#m-error").textContent = e.message; loadCatalog(); }
    }
  });
}

// ---------- 插件（/api/admin/plugins） ----------
const P = { data: null };
const PL_SOURCE = (x) => x.origin === "file" ? '<span class="muted">plugins.json</span>'
  : x.info.source?.kind === "git" ? `git <code>${esc(x.info.source.url)}</code>${x.info.source.path ? ` <code>${esc(x.info.source.path)}</code>` : ""}`
    + ` <span class="muted">@${esc((x.info.source.commit || "").slice(0, 7))}</span>`
  : x.info.source?.kind === "submission" ? `上架申请 · ${esc(x.info.source.by || "")}` : "上传";
const PL_SERVICE = (x) => !x.service ? '<span class="muted">无</span>'
  : x.service.hosted ? `托管${x.service.network ? " · 要联网" : ""}${x.service.requirements ? " · 有依赖" : ""}` : `远程 <code>${esc(x.service.host || "")}</code>`;

async function loadPlugins() {
  $("#pl-error").textContent = "";
  try { P.data = await api("/api/admin/plugins"); renderPlugins(); } catch (e) { $("#pl-error").textContent = e.message; }
  loadBlueprints();
}

async function loadBlueprints() {
  try {
    const list = (await api("/api/blueprints")).filter((b) => b.source === "catalog");
    table("#t-blueprints", [
      { label: "蓝图", html: (b) => `<b>${esc(b.title)}</b> <span class="muted">${esc(b.version)} · 第 ${b.rev} 版</span><div class="muted">${esc(b.description)}</div>` },
      { label: "岗位", html: (b) => esc(b.roles.join("、")) },
      { label: "用过", num: true, html: (b) => num(b.uses) },
      { label: "", html: (b) => b.status === "active" ? `<button type="button" class="link danger" data-bp-withdraw="${esc(b.id)}">撤回</button>` : '<span class="muted">已撤回</span>' },
    ], list, "目录里还没有蓝图");
  } catch (e) { $("#pl-error").textContent = e.message; }
}

function renderPlugins() {
  const d = P.data, f = $("#pl-policy");
  f.remote.value = d.policy.remote;
  f.allow.value = d.policy.allow.join("\n");
  f.deny.value = d.policy.deny.join("\n");
  f.hosted_upload.checked = d.policy.hosted_upload;
  f.sandbox_catalog.checked = d.policy.sandbox_catalog;
  $("#pl-sandbox").textContent = d.sandbox.available ? "沙箱：可用（bubblewrap）。"
    : `沙箱：不可用（${d.sandbox.note}）。主持人上传的托管插件装不上，目录里的托管插件由本部署直接运行。`;
  $("#pl-cat-hint").innerHTML = "所有项目的主持人都能从这里安装。装进项目的是当时那一版的拷贝：这里更新后，主持人在插件卡上看到“升级”。"
    + (d.catalog_file ? ` 文件里的条目在 <code>${esc(d.catalog_file)}</code> 里改。` : "")
    + (d.catalog_error ? ` <span class="key-state missing">文件有错：${esc(d.catalog_error)}</span>` : "");
  table("#t-plugin-catalog", [
    { label: "插件", html: (x) => `<b>${esc(x.title)}</b> <code>${esc(x.name)}</code> <span class="muted">${esc(x.version)}</span>` },
    { label: "来源", html: PL_SOURCE },
    { label: "服务", html: PL_SERVICE },
    { label: "在用的项目", num: true, html: (x) => num(x.installs) },
    { label: "", html: (x) => x.origin === "file" ? "" : `<span class="row-actions">`
      + (x.info.source?.kind === "git" ? `<button type="button" class="link" data-pl-refresh="${esc(x.name)}">重新拉取</button>` : "")
      + `<button type="button" class="link danger" data-pl-remove="${esc(x.name)}">移出目录</button></span>` },
  ], d.catalog, "目录是空的：从 git 仓库添加、上传插件包，或者批准上架申请");
  const subs = [...d.submissions].sort((a, b) => (a.status === "pending" ? 0 : 1) - (b.status === "pending" ? 0 : 1));
  table("#t-plugin-subs", [
    { label: "插件", html: (x) => `<b>${esc(x.title)}</b> <code>${esc(x.name)}</code> <span class="muted">${esc(x.version)}</span>` },
    { label: "提交", html: (x) => `${esc(x.by || "")}<div class="muted">${when(x.created_at)}</div>` },
    { label: "说明与文件", html: (x) => `<div>${esc(x.note || "（没写说明）")}</div><div class="pl-sub-files">${x.files.map((p) =>
      `<button type="button" class="link" data-pl-file="${esc(x.id)}" data-path="${esc(p)}">${esc(p)}</button>`).join("")}</div>`
      + (x.lint?.length ? `<ul class="pl-lint">${x.lint.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "") },
    { label: "状态", html: (x) => x.status === "pending" ? '<span class="pill off">待审核</span>'
      : `<span class="pill ${x.status === "approved" ? "on" : "off"}">${x.status === "approved" ? "已上架" : "未通过"}</span>`
        + (x.review_note ? `<div class="muted">${esc(x.review_note)}</div>` : "") },
    { label: "", html: (x) => x.status !== "pending" ? "" : `<span class="row-actions"><button type="button" class="link" data-pl-ok="${esc(x.id)}">批准上架</button>`
      + `<button type="button" class="link danger" data-pl-no="${esc(x.id)}">拒绝</button></span>` },
  ], subs, "还没有上架申请");
  for (const td of document.querySelectorAll("#t-plugin-subs td:nth-child(3)")) td.classList.add("wrap");
}

function pickZip() {
  return new Promise((resolve) => {
    const inp = Object.assign(document.createElement("input"), { type: "file", accept: ".zip,application/zip" });
    inp.addEventListener("change", () => resolve(inp.files[0] || null));
    inp.click();
  });
}

function bindPlugins() {
  const lines = (t) => t.split(/\n+/).map((x) => x.trim()).filter(Boolean);
  $("#pl-policy").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    $("#plp-save").disabled = true;
    try {
      P.data = await api("/api/admin/plugins/policy", { method: "PUT", body: { remote: f.remote.value, allow: lines(f.allow.value),
        deny: lines(f.deny.value), hosted_upload: f.hosted_upload.checked, sandbox_catalog: f.sandbox_catalog.checked } });
      renderPlugins();
      $("#pl-error").textContent = "";
    } catch (e) { $("#pl-error").textContent = e.message; }
    finally { $("#plp-save").disabled = false; }
  });
  $("#pl-git").addEventListener("click", () => { $("#plg-error").textContent = ""; $("#pl-git-dialog").showModal(); $("#plg-url").focus(); });
  $("#plg-cancel").addEventListener("click", () => $("#pl-git-dialog").close());
  $("#pl-git-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    $("#plg-submit").disabled = true;
    try {
      P.data = await api("/api/admin/plugins/catalog/git", { method: "POST", body: { url: f.url.value.trim(), ref: f.ref.value.trim(), path: f.path.value.trim() } });
      renderPlugins();
      $("#pl-git-dialog").close();
    } catch (e) { $("#plg-error").textContent = e.message; }
    finally { $("#plg-submit").disabled = false; }
  });
  $("#bp-zip").addEventListener("click", async () => {
    const file = await pickZip();
    if (!file) return;
    try {
      const r = await fetch("/api/admin/blueprints", { method: "POST", body: file, headers: { "Content-Type": "application/zip" } });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
      loadBlueprints();
    } catch (e) { $("#pl-error").textContent = e.message; }
  });
  $("#t-blueprints").addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-bp-withdraw]");
    if (!b || !confirm("撤回后不能再用它建项目，已经建的项目不受影响。")) return;
    try { await api(`/api/blueprints/${b.dataset.bpWithdraw}/withdraw`, { method: "POST" }); loadBlueprints(); }
    catch (e) { $("#pl-error").textContent = e.message; }
  });
  $("#pl-zip").addEventListener("click", async () => {
    const file = await pickZip();
    if (!file) return;
    try {
      const r = await fetch("/api/admin/plugins/catalog/upload", { method: "POST", body: file, headers: { "Content-Type": "application/zip" } });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
      P.data = data;
      renderPlugins();
    } catch (e) { $("#pl-error").textContent = e.message; }
  });
  $("#plugins").addEventListener("click", async (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    $("#pl-error").textContent = "";
    try {
      if (b.dataset.plRefresh) P.data = await api(`/api/admin/plugins/catalog/${encodeURIComponent(b.dataset.plRefresh)}/refresh`, { method: "POST" });
      if (b.dataset.plRemove) {
        if (!confirm("移出目录？已经装进项目的照常可用，只是之后没有升级来源。")) return;
        P.data = await api(`/api/admin/plugins/catalog/${encodeURIComponent(b.dataset.plRemove)}`, { method: "DELETE" });
      }
      if (b.dataset.plFile) {
        const r = await api(`/api/admin/plugins/submissions/${b.dataset.plFile}/file?path=${encodeURIComponent(b.dataset.path)}`);
        $("#plf-title").textContent = r.path;
        $("#plf-body").textContent = r.text;
        $("#pl-file-dialog").showModal();
        return;
      }
      if (b.dataset.plOk || b.dataset.plNo) {
        const ok = Boolean(b.dataset.plOk);
        const note = prompt(ok ? "批准上架：给提交的人留一句（可空）" : "拒绝的理由（提交的人看得到）", "");
        if (note === null) return;
        P.data = await api(`/api/admin/plugins/submissions/${b.dataset.plOk || b.dataset.plNo}`, { method: "POST", body: { approve: ok, note } });
      }
      renderPlugins();
    } catch (e) { $("#pl-error").textContent = e.message; }
  });
}

// ---------- 标签页 ----------
function showTab() {
  const tab = ["#models", "#plugins"].includes(location.hash) ? location.hash.slice(1) : "usage";
  for (const a of document.querySelectorAll(".admin-tabs a")) a.setAttribute("aria-current", a.dataset.tab === tab ? "page" : "false");
  $("#dash").hidden = tab !== "usage";
  $("#models").hidden = tab !== "models";
  $("#plugins").hidden = tab !== "plugins";
  if (tab === "models") loadCatalog();
  else if (tab === "plugins") loadPlugins();
  else if (!S.data) load();
  else render();
}

async function init() {
  const [info, me] = await Promise.all([api("/api/info").catch(() => null), api("/api/me").catch((e) => e)]);
  S.info = info;
  if (me instanceof Error) return deny(me.status === 401 ? "请先登录" : "没拿到数据",
    me.status === 401 ? "登录后再打开管理后台。" : me.message);
  $("#me").textContent = me.email;
  if (!me.admin) return deny("没有权限", "管理后台只给部署方指定的管理员。");
  $(".admin-tabs").hidden = false;
  bindCatalog();
  bindPlugins();
  for (const r of document.querySelectorAll('input[name="days"]')) {
    r.addEventListener("change", () => { S.days = Number(r.value); load(); });
  }
  let t;
  new ResizeObserver(() => { clearTimeout(t); t = setTimeout(() => S.data && !$("#dash").hidden && renderCharts(S.data), 80); })
    .observe($(".charts"));
  addEventListener("hashchange", showTab);
  showTab();
}

init();
