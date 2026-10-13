// Agent Network UI：网络图 / Agent 管理 / 群组管理 / 对话管理。状态来自 /api/network，事件流驱动刷新。
import { renderMarkdown } from "./md.js";

const $ = (s, el = document) => el.querySelector(s);
const svgNS = "http://www.w3.org/2000/svg";
const S = { snap: null, view: "network", agentSel: null, groupSel: null, convId: null, conv: null, sel: null, running: new Map() };
const STATUS_CN = { running: "执行中", completed: "已完成", failed: "失败", rejected: "已拒绝", idle: "空闲", busy: "运行中", thinking: "思考中" };
const GROUP_COLORS = ["#b48cff", "#3fbf7f", "#f2a541", "#e5534b", "#4cc9f0", "#ff7eb6"];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pill = (st) => `<span class="pill status ${esc(st)}">${esc(STATUS_CN[st] || st)}</span>`;
const initial = (name) => esc([...String(name || "?").replace(/ Agent$/, "").trim()][0] || "?").toUpperCase();
const avatar = (name, cls = "") => `<span class="avatar ${cls}" aria-hidden="true">${initial(name)}</span>`;
const placeholder = (text) => `<div class="placeholder"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M5 7.5h14M5 12h9M5 16.5h6"/></svg>${text}</div>`;
const tags = (xs, cls = "") => (xs || []).map((x) => `<span class="tag ${cls}">${esc(x)}</span>`).join("") || '<span class="muted">无</span>';
const agent = (id) => S.snap?.agents.find((a) => a.id === id);
const group = (id) => S.snap?.groups.find((g) => g.id === id);
const aname = (id) => agent(id)?.name || id || "（未知）";
const gcolor = (id) => GROUP_COLORS[Math.max(0, S.snap.groups.findIndex((g) => g.id === id)) % GROUP_COLORS.length];

async function api(path, opts = {}) {
  if (opts.body && typeof opts.body !== "string") {
    opts = { ...opts, body: JSON.stringify(opts.body), headers: { "Content-Type": "application/json" } };
  }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = data.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("；") : `${r.status}`);
  }
  return data;
}

async function init() {
  const info = await api("/api/info");
  $("#runtime").textContent = `Runtime · ${info.runtime}`;
  // 走 URL 而不是直接 show()：否则切回对话页时 URL 还停在 "#network"，
  // 刷新后会被路由回网络图。
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => { location.hash = b.dataset.view; }));
  $("#agent-new").addEventListener("click", () => { S.agentSel = null; renderAgentEditor(true); renderAgents(); });
  $("#group-new").addEventListener("click", () => { S.groupSel = null; renderGroupEditor(true); renderGroups(); });
  $("#conv-form").addEventListener("submit", createConv);
  $("#conv-agent").addEventListener("change", fillConvGroups);
  $("#conv-group").addEventListener("change", groupHint);
  $("#conv-new").addEventListener("click", () => { location.hash = "chats"; });
  $("#msg-form").addEventListener("submit", sendMsg);
  $("#msg").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("#msg-form").requestSubmit();
  });
  window.addEventListener("resize", () => S.view === "network" && renderGraph());
  window.addEventListener("hashchange", route);
  await reload();
  route();
  connect();
}

function route() {
  const hash = location.hash.slice(1);
  const PLAIN = ["network", "agents", "groups", "chats"];
  if (hash.startsWith("conv-")) {
    // 先定下 convId 再切视图：hashchange 事件可能晚于哈希变化抵达，
    // 若让 show() 在这里改 URL，会与后续事件撞车并回退到别的视图。
    S.convId = hash;
    show("chats");
    if (S.conv?.id !== hash) openConv(hash);
  } else {
    // 切到普通视图时清掉选中的对话，否则 URL 与画面会不一致。
    S.convId = null; S.conv = null;
    show(PLAIN.includes(hash) ? hash : "network");
  }
}

function show(view) {
  S.view = view;
  // “对话”不再是导航 tab：对话列表常驻左栏，#chats 即新建对话页。
  document.querySelectorAll(".tab").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === view)));
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== `view-${view}`; });
  if (view !== "chats" || !S.convId) history.replaceState(null, "", `#${view}`);
  render();
}

async function reload() {
  S.snap = await api("/api/network");
  if (S.convId) S.conv = await api(`/api/conversations/${S.convId}`).catch(() => null);
  render();
}

function render() {
  if (!S.snap) return;
  const busy = S.snap.agents.filter((a) => a.status === "busy").length;
  if (!S.linkLost) $("#live").textContent = busy ? `${busy} 个 Agent 运行中` : "全部空闲";
  $("#live").classList.toggle("busy", busy > 0);
  renderConvList();
  ({ network: renderGraph, agents: renderAgentsView, groups: renderGroupsView, chats: renderChats })[S.view]();
}

// 刷新节流：结构变化（Agent、群组、对话、委托状态）重拉整个快照；
// 运行过程（run.progress）只重拉当前打开的对话或委托，不碰其它视图。
const throttle = (fn, ms) => {
  let t = null, again = false;
  return () => {
    if (t) { again = true; return; }
    fn();
    t = setTimeout(() => { t = null; if (again) { again = false; fn(); } }, ms);
  };
};
const schedule = throttle(() => reload().catch(() => {}), 250);
const scheduleConv = throttle(() => refreshConv(), 250);
const scheduleTask = throttle(() => renderTask(), 400);

function onEvent(ev) {
  S.seq = Math.max(S.seq || 0, ev.seq);
  const d = ev.data;
  if (ev.type === "task.created" || ev.type === "task.updated") {
    if (d.task.status === "running") S.running.set(d.task.id, d.task); else S.running.delete(d.task.id);
  }
  if (ev.type === "run.progress") {
    if (S.view === "chats" && d.conversation_id === S.convId) scheduleConv();
    if (d.context === "task" && d.context_id === S.taskSel) scheduleTask();
    return;
  }
  if (ev.type === "tool.call" || ev.type === "runtime.invoke") return;  // 已由 run.progress 覆盖
  schedule();
  if (S.taskSel && ev.type === "task.updated" && d.task.id === S.taskSel) scheduleTask();
}

function connect() {
  const es = new EventSource(`/api/events?since=${S.snap.seq}`);
  es.onmessage = (m) => onEvent(JSON.parse(m.data));
  es.onopen = () => setLink(true);
  // EventSource 会自动重连并带上 Last-Event-ID；断开期间可能漏掉状态，重连后整体对齐一次。
  es.onerror = () => { setLink(false); };
  es.addEventListener("open", () => { if (S.linkLost) { S.linkLost = false; schedule(); } });
}

function setLink(ok) {
  if (!ok) S.linkLost = true;
  $("#live").classList.toggle("offline", !ok);
  if (!ok) $("#live").textContent = "连接中断，正在重连…";
  else render();
}

// ================= 网络图 =================
function el(tag, attrs = {}, parent) {
  const n = document.createElementNS(svgNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}

function layout(svg) {
  const W = svg.clientWidth || 800, H = svg.clientHeight || 520;
  const agents = S.snap.agents, pos = {};
  pos.human = { x: 80, y: H / 2 - 16 };
  // Agent 排在面向“人”的椭圆弧上（-105°~105°），人到每个 Agent 的连线都不会穿过别的节点
  const cx = W * 0.38, cy = H / 2 - 16, rx = Math.max(160, W * 0.5), ry = Math.max(120, (H - 190) / 2);
  const n = agents.length, span = (Math.PI * 7) / 12;
  agents.forEach((a, i) => {
    const ang = n === 1 ? 0 : -span + (2 * span * i) / (n - 1);
    pos[a.id] = { x: cx + rx * Math.cos(ang), y: cy + ry * Math.sin(ang) };
  });
  return pos;
}

// 二次曲线，两端按节点半径收缩，箭头落在节点边缘而不是被节点盖住
function curve(p, q, bend = 0.12, trim = 26) {
  const mx = (p.x + q.x) / 2, my = (p.y + q.y) / 2, dx = q.x - p.x, dy = q.y - p.y;
  const cx = mx - dy * bend, cy = my + dx * bend;
  const cut = (a, c) => { const vx = c.x - a.x, vy = c.y - a.y, l = Math.hypot(vx, vy) || 1;
    return { x: a.x + (vx / l) * trim, y: a.y + (vy / l) * trim }; };
  const s = cut(p, { x: cx, y: cy }), e = cut(q, { x: cx, y: cy });
  return { d: `M${s.x},${s.y} Q${cx},${cy} ${e.x},${e.y}`, lx: (mx + cx) / 2, ly: (my + cy) / 2 };
}

function renderGraph() {
  const svg = $("#graph");
  svg.innerHTML = "";
  const pos = layout(svg);
  const defs = el("defs", {}, svg);
  [["arrow", "#4a4f60"], ["arrow-active", "#8b93ff"], ["arrow-sel", "#ffffff"]].forEach(([id, c]) => {
    const m = el("marker", { id, viewBox: "0 0 10 10", refX: 10, refY: 5, markerWidth: 6, markerHeight: 6,
      markerUnits: "userSpaceOnUse", orient: "auto-start-reverse" }, defs);
    el("path", { d: "M0,0 L10,5 L0,10 z", fill: c }, m);
  });
  const edges = el("g", {}, svg), nodes = el("g", {}, svg);

  // 人 → 可对话的 Agent（授权边）；对话次数决定是否实线
  const convCount = {};
  S.snap.conversations.forEach((c) => { convCount[c.agent_id] = (convCount[c.agent_id] || 0) + 1; });
  S.snap.agents.filter((a) => a.chat_enabled).forEach((a) => {
    const c = curve(pos.human, pos[a.id], 0.05);
    const p = el("path", { d: c.d, class: "edge conv", "stroke-width": convCount[a.id] ? 2.5 : 1.5 }, edges);
    el("title", {}, p).textContent = `人 可与 ${a.name} 对话${convCount[a.id] ? `（${convCount[a.id]} 个对话）` : ""}`;
  });

  // 长期委托关系
  S.snap.relations.forEach((r) => {
    if (!pos[r.from] || !pos[r.to]) return;
    const key = `rel:${r.from}:${r.to}`, sel = S.sel === key;
    const c = curve(pos[r.from], pos[r.to]);
    const p = el("path", { d: c.d, class: `edge rel${sel ? " selected" : ""}`, "stroke-width": Math.min(8, 1.5 + r.count),
      "marker-end": `url(#${sel ? "arrow-sel" : "arrow"})`, tabindex: 0, role: "button",
      "aria-label": `${aname(r.from)} 委托 ${aname(r.to)} ${r.count} 次` }, edges);
    p.addEventListener("click", () => selectGraph(key));
    p.addEventListener("keydown", (e) => e.key === "Enter" && selectGraph(key));
    const t = el("text", { x: c.lx, y: c.ly, class: "edge-label" }, edges);
    t.textContent = `×${r.count}`;
  });

  // 活跃委托叠加
  S.snap.active_tasks.forEach((t) => {
    if (!pos[t.delegator_id] || !pos[t.delegatee_id]) return;
    const c = curve(pos[t.delegator_id], pos[t.delegatee_id], 0.2);
    const p = el("path", { d: c.d, class: "edge active", "stroke-width": 3, "marker-end": "url(#arrow-active)" }, edges);
    el("title", {}, p).textContent = `正在委托：${t.delegation.mission}`;
  });

  // 节点
  const human = el("g", { class: "node human", transform: `translate(${pos.human.x},${pos.human.y})` }, nodes);
  el("circle", { r: 20, class: "core" }, human);
  el("text", { class: "glyph" }, human).textContent = "人";
  el("text", { y: 40 }, human).textContent = "你";
  S.snap.agents.forEach((a) => {
    const key = `agent:${a.id}`;
    const g = el("g", { class: `node agent${a.status === "busy" ? " busy" : ""}${S.sel === key ? " selected" : ""}`,
      transform: `translate(${pos[a.id].x},${pos[a.id].y})`, tabindex: 0, role: "button",
      "aria-label": `${a.name}，${STATUS_CN[a.status]}` }, nodes);
    el("circle", { r: 34, class: "halo" }, g);
    S.snap.groups.filter((gr) => gr.member_ids.includes(a.id)).forEach((gr, i) =>
      el("circle", { r: 23 + i * 4, fill: "none", stroke: gcolor(gr.id), "stroke-width": 1.5, opacity: 0.7 }, g));
    el("circle", { r: 18, class: "core" }, g);
    el("text", { class: "glyph" }, g).textContent = initial(a.name);
    el("text", { y: 46 }, g).textContent = a.name.replace(/ Agent$/, "");
    el("text", { y: 60, class: "sub" }, g).textContent = a.authority.join(" · ") || "无权限";
    g.addEventListener("click", () => selectGraph(key));
    g.addEventListener("keydown", (e) => e.key === "Enter" && selectGraph(key));
  });
  renderNetDetail();
}

function selectGraph(key) { S.sel = S.sel === key ? null : key; renderGraph(); }

function renderNetDetail() {
  const box = $("#net-detail");
  const [kind, a, b] = (S.sel || "").split(":");
  if (kind === "agent" && agent(a)) {
    const x = agent(a);
    const out = S.snap.relations.filter((r) => r.from === a), inn = S.snap.relations.filter((r) => r.to === a);
    const relList = (rs, key) => rs.length ? `<ul class="rel-list">${rs.map((r) =>
      `<li><span>${esc(aname(r[key]))}</span><span class="muted">×${r.count}</span></li>`).join("")}</ul>` : "—";
    box.innerHTML = `<div class="card-head">${avatar(x.name)}<h2 class="panel-title" style="margin:0">${esc(x.name)}</h2>${pill(x.status)}</div>
      <p class="muted" style="margin-top:12px">${esc(x.description)}</p>
      <dl><dt>能力</dt><dd>${tags(x.capabilities)}</dd><dt>自身权限</dt><dd>${tags(x.authority, "ok")}</dd>
      <dt>可对话</dt><dd>${x.chat_enabled ? "是" : "否"}</dd>
      <dt>群组</dt><dd>${tags(S.snap.groups.filter((g) => g.member_ids.includes(a)).map((g) => g.name))}</dd>
      <dt>委托出去</dt><dd>${relList(out, "to")}</dd>
      <dt>被委托</dt><dd>${relList(inn, "from")}</dd></dl>`;
  } else if (kind === "rel") {
    const r = S.snap.relations.find((r) => r.from === a && r.to === b);
    if (!r) { box.innerHTML = ""; return; }
    box.innerHTML = `<h2 class="panel-title">${esc(aname(a))} <span class="muted">→</span> ${esc(aname(b))}</h2>
      <p class="muted">长期委托关系</p>
      <div class="stats"><div class="stat"><b>${r.count}</b><span>委托次数</span></div><div class="stat"><b>${r.completed}</b><span>已完成</span></div>
      <div class="stat"><b>${r.failed}</b><span>失败 / 拒绝</span></div><div class="stat"><b>${r.running}</b><span>进行中</span></div></div>
      <dl><dt>群组</dt><dd>${tags(r.groups.map((g) => group(g)?.name || "（已删除）"))}</dd>
      <dt>最近</dt><dd>${new Date(r.last_at * 1000).toLocaleString()}</dd></dl>`;
  } else {
    box.innerHTML = `<h2 class="panel-title">概览</h2><p class="muted">点击节点或连线查看详情。</p>
      <div class="stats"><div class="stat"><b>${S.snap.agents.length}</b><span>Agent</span></div><div class="stat"><b>${S.snap.groups.length}</b><span>群组</span></div>
      <div class="stat"><b>${S.snap.conversations.length}</b><span>对话</span></div><div class="stat"><b>${S.snap.relations.length}</b><span>委托关系</span></div>
      <div class="stat"><b>${S.snap.active_tasks.length}</b><span>进行中委托</span></div></div>
      <p class="muted">连线粗细代表历史委托次数，流动的虚线是正在进行的委托，节点外圈颜色对应所属群组。</p>`;
  }
}

// ================= Agent 管理 =================
function renderAgentsView() { renderAgents(); renderAgentEditor(); }

function renderAgents() {
  $("#agent-list").innerHTML = S.snap.agents.map((a) => `
    <li class="${S.agentSel === a.id ? "selected" : ""}"><button type="button" data-id="${esc(a.id)}">
      <div class="card-head">${avatar(a.name)}<span class="name">${esc(a.name)}</span>${pill(a.status)}</div>
      <p class="card-desc">${esc(a.description)}</p>
      <div class="card-meta">${tags(a.authority, "ok")}</div>
      <div class="card-foot"><span>${a.chat_enabled ? "可直接对话" : "不开放对话"}</span>
        <span>${esc(S.snap.groups.filter((g) => g.member_ids.includes(a.id)).map((g) => g.name).join("、") || "未加入群组")}</span></div>
    </button></li>`).join("") || '<li class="empty">还没有 Agent。</li>';
  $("#agent-list").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => { S.agentSel = b.dataset.id; renderAgents(); renderAgentEditor(); }));
}

function authorityBoxes(selected) {
  return Object.entries(S.snap.authorities).map(([k, v]) =>
    `<label><input type="checkbox" name="authority" value="${esc(k)}" ${selected.includes(k) ? "checked" : ""}>
      <span class="opt"><code>${esc(k)}</code><small>${esc(v)}</small></span></label>`).join("");
}

function renderAgentEditor(isNew = false) {
  const box = $("#agent-editor"), a = agent(S.agentSel);
  if (!a && !isNew) { box.innerHTML = placeholder("选择一个 Agent 编辑，或新建。"); return; }
  const x = a || { name: "", description: "", capabilities: [], authority: ["web.research"], chat_enabled: true };
  box.innerHTML = `<h2 class="panel-title">${a ? esc(a.name) : "新建 Agent"}</h2>
    <p class="muted">${a ? "修改后点击保存生效。" : "定义它能做什么，以及最多能被授予哪些权限。"}</p>
    <form class="editor" id="agent-form">
      <div class="field"><label for="af-name">名称</label><input id="af-name" name="name" required minlength="2" value="${esc(x.name)}"></div>
      <div class="field"><label for="af-desc">描述</label>
        <textarea id="af-desc" name="description" rows="3" required minlength="2">${esc(x.description)}</textarea>
        <p class="hint">会出现在 list_members 里，供其他 Agent 判断能不能委托。</p></div>
      <div class="field"><label for="af-caps">能力</label><input id="af-caps" name="capabilities" value="${esc(x.capabilities.join(", "))}">
        <p class="hint">逗号分隔。</p></div>
      <fieldset class="checks"><legend>自身权限 · 委托只能在此之内再收窄</legend>${authorityBoxes(x.authority)}</fieldset>
      <label class="switch"><input type="checkbox" name="chat_enabled" ${x.chat_enabled ? "checked" : ""}> 允许人直接与它对话</label>
      <p class="error" role="alert"></p>
      <div class="actions"><button type="submit">${a ? "保存" : "创建"}</button>
        ${a ? '<button type="button" class="ghost" id="af-chat">与它对话</button><button type="button" class="danger" id="af-del">删除</button>' : ""}</div>
    </form>`;
  const f = $("#agent-form");
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      name: f.name.value.trim(), description: f.description.value.trim(),
      capabilities: f.capabilities.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
      authority: [...f.querySelectorAll('[name="authority"]:checked')].map((c) => c.value),
      chat_enabled: f.chat_enabled.checked,
    };
    try {
      const r = await api(a ? `/api/agents/${a.id}` : "/api/agents", { method: a ? "PATCH" : "POST", body });
      S.agentSel = r.id; await reload();
    } catch (err) { $(".error", f).textContent = err.message; }
  });
  $("#af-del")?.addEventListener("click", async () => {
    if (!confirm(`删除 ${a.name}？它会从所有群组中移除。`)) return;
    try { await api(`/api/agents/${a.id}`, { method: "DELETE" }); S.agentSel = null; await reload(); }
    catch (err) { $(".error", f).textContent = err.message; }
  });
  $("#af-chat")?.addEventListener("click", () => { S.startAgent = a.id; location.hash = "chats"; });
}

// ================= 群组管理 =================
function renderGroupsView() { renderGroups(); renderGroupEditor(); }

function renderGroups() {
  $("#group-list").innerHTML = S.snap.groups.map((g) => `
    <li class="${S.groupSel === g.id ? "selected" : ""}"><button type="button" data-id="${esc(g.id)}">
      <div class="card-head"><span class="swatch" style="background:${gcolor(g.id)}"></span><span class="name">${esc(g.name)}</span><span class="pill">${g.member_ids.length} 名成员</span></div>
      <p class="card-desc">${esc(g.description || "没有描述")}</p>
      <div class="card-meta">${tags(g.member_ids.map(aname))}</div>
    </button></li>`).join("") || '<li class="empty">还没有群组。</li>';
  $("#group-list").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => { S.groupSel = b.dataset.id; renderGroups(); renderGroupEditor(); }));
}

function renderGroupEditor(isNew = false) {
  const box = $("#group-editor"), g = group(S.groupSel);
  if (!g && !isNew) { box.innerHTML = placeholder("选择一个群组编辑成员，或新建。"); return; }
  const x = g || { name: "", description: "", member_ids: [] };
  box.innerHTML = `<h2 class="panel-title">${g ? `<span class="swatch" style="background:${gcolor(g.id)}"></span>${esc(g.name)}` : "新建群组"}</h2>
    <p class="muted">委托只能在群组成员之间进行。</p>
    <form class="editor" id="group-form">
      <div class="field"><label for="gf-name">名称</label><input id="gf-name" name="name" required value="${esc(x.name)}"></div>
      <div class="field"><label for="gf-desc">描述</label><textarea id="gf-desc" name="description" rows="2">${esc(x.description)}</textarea></div>
      <fieldset class="checks"><legend>成员</legend>
        ${S.snap.agents.map((a) => `<label><input type="checkbox" name="member" value="${esc(a.id)}" ${x.member_ids.includes(a.id) ? "checked" : ""}>
          <span class="opt">${esc(a.name)}<small>${esc(a.authority.join(" · ") || "无权限")}</small></span></label>`).join("")}
      </fieldset>
      <p class="error" role="alert"></p>
      <div class="actions"><button type="submit">${g ? "保存" : "创建"}</button>
        ${g ? '<button type="button" class="danger" id="gf-del">删除群组</button>' : ""}</div>
    </form>`;
  const f = $("#group-form");
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { name: f.name.value.trim(), description: f.description.value.trim(),
      member_ids: [...f.querySelectorAll('[name="member"]:checked')].map((c) => c.value) };
    try {
      const r = await api(g ? `/api/groups/${g.id}` : "/api/groups", { method: g ? "PATCH" : "POST", body });
      S.groupSel = r.id; await reload();
    } catch (err) { $(".error", f).textContent = err.message; }
  });
  $("#gf-del")?.addEventListener("click", async () => {
    if (!confirm(`删除群组 ${g.name}？已有对话保留，但不再能委托。`)) return;
    try { await api(`/api/groups/${g.id}`, { method: "DELETE" }); S.groupSel = null; await reload(); }
    catch (err) { $(".error", f).textContent = err.message; }
  });
}

// ================= 对话 =================
const plain = (s) => String(s || "").replace(/[#*_`>|~]+/g, "").replace(/\s+/g, " ").trim();

function renderConvList() {
  $("#conv-list").innerHTML = S.snap.conversations.map((c) => {
    const sel = S.view === "chats" && S.convId === c.id;
    return `<li class="${sel ? "selected" : ""}"><a href="#${esc(c.id)}"${sel ? ' aria-current="page"' : ""}>
      ${avatar(aname(c.agent_id), "sm")}
      <span class="c-body">
        <span class="t"><span class="n">${esc(aname(c.agent_id))}</span>${c.status === "thinking" ? '<span class="dot-live" title="运行中"></span>' : ""}</span>
        <span class="s">${esc(plain(c.last_message) || "还没有消息")}</span>
        <span class="g">${c.group_id ? `<span class="swatch" style="background:${gcolor(c.group_id)}"></span>${esc(group(c.group_id)?.name || "（已删除）")}` : "不在群组中"}</span>
      </span></a></li>`;
  }).join("") || '<li class="empty">还没有对话</li>';
  $("#conv-new").setAttribute("aria-pressed", String(S.view === "chats" && !S.convId));
}

function renderChats() {
  const start = !S.convId;
  $("#view-chats").classList.toggle("start", start);
  $("#conv-start").hidden = !start;
  $("#task-detail").hidden = start;
  if (start) fillConvAgents();
  else renderThread();
}

// 事件会频繁触发重绘：选项没变就不重建 <select>，否则用户正打开的下拉框会被关掉。
function fillConvAgents() {
  const sel = $("#conv-agent"), prev = sel.value;
  const opts = S.snap.agents.filter((a) => a.chat_enabled);
  if (S.startAgent) { sel.dataset.sig = ""; }
  const sig = opts.map((a) => a.id + a.name).join("|");
  if (sel.dataset.sig !== sig) {
    sel.innerHTML = opts.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
    const want = S.startAgent || prev;
    if (want && opts.some((a) => a.id === want)) sel.value = want;
    S.startAgent = null;
    sel.dataset.sig = sig;
  }
  fillConvGroups();
}

function fillConvGroups() {
  const aid = $("#conv-agent").value, sel = $("#conv-group"), prev = sel.value;
  const gs = S.snap.groups.filter((g) => g.member_ids.includes(aid));
  const sig = aid + ":" + gs.map((g) => g.id + g.name + g.member_ids.length).join("|");
  if (sel.dataset.sig !== sig) {
    sel.innerHTML = gs.map((g) => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join("")
      + '<option value="">不在群组中</option>';
    if (sel.dataset.agent === aid && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
    sel.dataset.agent = aid;
    sel.dataset.sig = sig;
  }
  groupHint();
}

function groupHint() {
  const a = agent($("#conv-agent").value), g = group($("#conv-group").value);
  $("#conv-group-hint").textContent = !a ? "没有开放直接对话的 Agent。"
    : !g ? "不在群组中：它只能自己完成，不能委托。"
    : !a.authority.includes("delegate") ? `${a.name} 没有 delegate 权限，在群组里也不能委托。`
    : `可委托：${g.member_ids.filter((m) => m !== a.id).map(aname).join("、") || "（群组里没有其他成员）"}`;
  $("#conv-form button[type=submit]").disabled = !a;
}

async function createConv(e) {
  e.preventDefault();
  try {
    const c = await api("/api/conversations", { method: "POST",
      body: { agent_id: $("#conv-agent").value, group_id: $("#conv-group").value || null } });
    await reload();
    location.hash = c.id;
    await openConv(c.id);
    $("#msg").focus();
  } catch (err) { alert(err.message); }
}

async function openConv(id) {
  if (S.convId !== id) S.taskSel = null;
  S.convId = id;
  history.replaceState(null, "", `#${id}`);
  S.conv = await api(`/api/conversations/${id}`).catch(() => ({ id, missing: true }));
  if (S.convId === id) render();
}

async function refreshConv() {
  const id = S.convId;
  if (!id || S.view !== "chats") return;
  const c = await api(`/api/conversations/${id}`).catch(() => null);
  if (c && S.convId === id) { S.conv = c; renderThread(); }
}

const TOOL_CN = { [`mcp__agentnet__delegate`]: "委托", [`mcp__agentnet__list_members`]: "查看成员",
  [`mcp__agentnet__share_knowledge`]: "共享知识", web_search: "搜索", web_fetch: "抓取网页", read: "读取",
  write: "写入", edit: "编辑", bash: "执行命令", glob: "查找文件", grep: "搜索文件", todo_write: "更新计划" };
const toolName = (t) => TOOL_CN[t] || t.replace(/^mcp__agentnet__/, "");
const time = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

// 过程：中间文本与工具调用。运行中实时展开；结束后折叠在回复上方。
function stepsHtml(steps, live) {
  if (!steps.length) return "";
  const rows = steps.map((s) => s.kind === "text"
    ? `<li class="step text"><div class="md">${renderMarkdown(s.text)}</div></li>`
    : `<li class="step tool ${esc(s.status)}"><span class="st" aria-label="${esc(STATUS_CN[s.status] || s.status)}"></span>
        <b>${esc(toolName(s.tool))}</b>${s.brief ? `<span class="brief">${esc(s.brief)}</span>` : ""}</li>`).join("");
  const tools = steps.filter((s) => s.kind === "tool").length;
  if (live) return `<ol class="steps live" aria-label="运行过程">${rows}</ol>`;
  return `<details class="steps-wrap"><summary>过程 · ${tools} 次工具调用</summary><ol class="steps">${rows}</ol></details>`;
}

function renderThread() {
  const c = S.conv;
  if (!c) return;
  if (c.missing) {
    $("#thread-head").innerHTML = "";
    $("#thread").innerHTML = `<li>${placeholder("这个对话不存在或已删除。")}</li>`;
    $("#msg-form").hidden = true; renderTask(); return;
  }
  $("#msg-form").hidden = false;
  const a = agent(c.agent_id), g = group(c.group_id);
  const canDelegate = g && a?.authority.includes("delegate");
  $("#thread-head").innerHTML = `${avatar(a?.name)}<div class="info"><h2 class="panel-title" style="margin:0">${esc(a?.name)} ${pill(c.status)}</h2>
    <div class="muted" title="DSH 会话 ${esc(c.dsh_session_id || "")}">${g ? `群组「${esc(g.name)}」 · ${canDelegate ? "可委托 " + esc(g.member_ids.filter((m) => m !== c.agent_id).map(aname).join("、")) : "无 delegate 权限，不能委托"}` : "不在群组中，不能委托"}</div></div>
    <button type="button" class="danger" id="conv-del">删除对话</button>`;
  $("#conv-del").addEventListener("click", async () => {
    if (!confirm("删除这个对话？")) return;
    try { await api(`/api/conversations/${c.id}`, { method: "DELETE" }); S.convId = null; S.conv = null; location.hash = "chats"; await reload(); }
    catch (err) { alert(err.message); }
  });

  const byParent = {};
  c.tasks.forEach((t) => (byParent[t.parent_task_id || "root"] ||= []).push(t));
  const treeOf = (ids) => {
    const out = [];
    const walk = (t, child) => { out.push(delegBtn(t, child)); (byParent[t.id] || []).forEach((x) => walk(x, true)); };
    c.tasks.filter((t) => ids.includes(t.id)).forEach((t) => walk(t, false));
    return out.join("");
  };
  const assigned = new Set(c.messages.flatMap((m) => m.task_ids));
  const pending = c.tasks.filter((t) => !t.parent_task_id && !assigned.has(t.id)).map((t) => t.id);
  const items = c.messages.map((m) => {
    const delegs = m.task_ids.length ? `<div class="delegs" aria-label="本轮委托">${treeOf(m.task_ids)}</div>` : "";
    if (m.role === "system") return `<li class="msg system"><div class="content">${stepsHtml(m.steps || [], false)}${delegs}<div class="body">${esc(m.text)}</div></div></li>`;
    if (m.role === "user") return `<li class="msg user"><div class="content"><div class="who"><b>你</b>${time(m.ts)}</div><div class="body">${esc(m.text)}</div></div></li>`;
    return `<li class="msg agent">${avatar(a?.name, "sm")}<div class="content">
      <div class="who"><b>${esc(a?.name)}</b>${time(m.ts)}</div>${stepsHtml(m.steps || [], false)}${delegs}<div class="body md">${renderMarkdown(m.text)}</div></div></li>`;
  });
  if (c.status === "thinking") {
    const live = c.live || [];
    items.push(`<li class="msg agent">${avatar(a?.name, "sm")}<div class="content"><div class="who"><b>${esc(a?.name)}</b>运行中</div>
      ${stepsHtml(live, true)}${pending.length ? `<div class="delegs">${treeOf(pending)}</div>` : ""}
      <span class="typing" aria-label="处理中"><i></i><i></i><i></i></span></div></li>`);
  }
  const th = $("#thread"), atBottom = th.scrollHeight - th.scrollTop - th.clientHeight < 80;
  // 保留用户手动展开的“过程”，重绘不把它们收起来
  const open = new Set([...th.querySelectorAll("details[open]")].map((d) => d.closest(".msg")?.dataset.i));
  th.innerHTML = items.join("") || `<li>${placeholder("发一条消息开始对话。在群组中，它会自己决定要不要把任务委托给群组成员。")}</li>`;
  [...th.children].forEach((li, i) => { li.dataset.i = i; if (open.has(String(i))) li.querySelector("details")?.setAttribute("open", ""); });
  if (atBottom) th.scrollTop = th.scrollHeight;
  th.querySelectorAll(".deleg").forEach((b) => b.addEventListener("click", () => {
    S.taskSel = b.dataset.id;
    th.querySelectorAll(".deleg").forEach((x) => x.classList.toggle("selected", x === b));
    renderTask();
  }));
  $("#msg-form button").disabled = c.status === "thinking";
  renderTask();
}

function delegBtn(t, child) {
  return `<button type="button" class="deleg${child ? " child" : ""}${S.taskSel === t.id ? " selected" : ""}" data-id="${esc(t.id)}">
    <span class="route"><span class="name">${esc(aname(t.delegator_id))}</span><span class="arrow">→</span><span class="name">${esc(t.delegatee_id ? aname(t.delegatee_id) : "（群组外）")}</span>${pill(t.status)}</span>
    <span class="mission">${esc(t.delegation.mission)}</span></button>`;
}

async function renderTask() {
  const box = $("#task-detail"), id = S.taskSel;
  if (!id) { box.innerHTML = placeholder("点击对话里的委托，查看授权、约束、实时过程与结果。"); return; }
  const t = await api(`/api/tasks/${id}`).catch(() => null);
  if (!t || S.taskSel !== id) return;
  const keep = box.scrollTop;
  const d = t.delegation;
  box.innerHTML = `<h2 class="panel-title">${esc(aname(t.delegator_id))} <span class="muted">→</span> ${esc(t.delegatee_id ? aname(t.delegatee_id) : "（群组外）")} ${pill(t.status)}</h2>
    <p class="muted"><code>${esc(t.id)}</code> · 深度 ${t.depth}</p>
    <dl>
    <dt>目标</dt><dd>${esc(d.mission)}</dd><dt>输出要求</dt><dd>${esc(d.expected_outcome)}</dd>
    <dt>请求权限</dt><dd>${tags(d.authority)}</dd>
    <dt>实际权限</dt><dd>${tags(t.granted_authority, "ok")} ${tags(t.denied_authority, "no")}</dd>
    <dt>约束</dt><dd>${(t.effective_constraints.map(esc).join("<br>")) || "—"}</dd>
    <dt>共享知识</dt><dd>${tags(d.share)}</dd>
    <dt>DSH 会话</dt><dd>${esc(t.dsh_session_id || "—")}</dd>
    ${t.violations.length ? `<dt>越权</dt><dd class="bad">${t.violations.map(esc).join("<br>")}</dd>` : ""}
    ${t.error ? `<dt>原因</dt><dd class="bad">${esc(t.error)}</dd>` : ""}</dl>
    ${t.status === "running" ? `<h3>实时过程</h3>${stepsHtml(t.live || [], true) || '<span class="typing" aria-label="处理中"><i></i><i></i><i></i></span>'}` : ""}
    ${t.result ? `<h3>交付结果</h3><div class="md report">${renderMarkdown(t.result.report)}</div>` : ""}
    ${t.evidence.length ? `<h3>工具调用证据（${t.evidence.length}）</h3><pre>${esc(t.evidence.map((e) => `${e.status} ${e.tool} ${e.input}`).join("\n"))}</pre>` : ""}
    <details><summary>执行者收到的提示词</summary><pre>${esc(t.prompt)}</pre></details>`;
  box.scrollTop = keep;
}

async function sendMsg(e) {
  e.preventDefault();
  const text = $("#msg").value.trim();
  if (!text || !S.convId) return;
  $("#msg-form button").disabled = true;
  try { await api(`/api/conversations/${S.convId}/messages`, { method: "POST", body: { text } }); $("#msg").value = ""; await reload(); }
  catch (err) { alert(err.message); $("#msg-form button").disabled = false; }
}

init().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<p class="error" role="alert">加载失败：${esc(e.message)}</p>`); });
