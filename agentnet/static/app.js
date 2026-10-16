// Matrix UI：网络图 / Agent 管理 / 群组管理 / 对话管理。状态来自 /api/network，事件流驱动刷新。
import { renderMarkdown } from "./md.js";

const $ = (s, el = document) => el.querySelector(s);
const svgNS = "http://www.w3.org/2000/svg";
const S = { snap: null, view: "network", agentSel: null, groupSel: null, convId: null, conv: null, sel: null, running: new Map() };
const STATUS_CN = { running: "执行中", in_review: "待验收", completed: "已完成", failed: "失败", rejected: "已拒绝",
  idle: "空闲", busy: "运行中", thinking: "思考中", waiting: "等委托", pending: "待审核", approved: "已批准" };
const modelLabel = (m) => m ? `${m.provider} / ${m.model}${m.reasoning_effort ? ` · ${m.reasoning_effort}` : ""}` : "";
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
  if (r.status === 401 && !path.startsWith("/api/auth/")) {  // 登录过期 / 退出：回到登录页
    showGate("login");
    throw new Error("请先登录");
  }
  if (!r.ok) {
    const d = data.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("；") : `${r.status}`);
  }
  return data;
}

// ---------- 登录与首个 Agent 引导 ----------
function showGate(mode) {
  S.gate = mode;
  $("#gate").hidden = false;
  document.body.classList.add("gated");
  $("#auth-form").hidden = mode === "onboard";
  $("#onboard-form").hidden = mode !== "onboard";
  if (mode === "onboard") {
    $("#ob-auth").innerHTML = "<legend>权限</legend>" + authorityBoxes(["web.research", "file.write"]);
    $("#ob-name").focus();
    return;
  }
  const reg = mode === "register";
  $("#auth-title").textContent = reg ? "注册" : "登录";
  $("#auth-sub").textContent = reg ? (S.info?.has_unclaimed ? "你是第一个注册的人，会接管这里已有的 Agent、群组和对话。"
    : "用邮箱和密码注册一个账号。") : "用邮箱和密码登录。";
  $("#auth-submit").textContent = reg ? "注册并登录" : "登录";
  $("#auth-switch-text").textContent = reg ? "已经有账号？" : "还没有账号？";
  $("#auth-switch").textContent = reg ? "登录" : "注册";
  $("#auth-pass").autocomplete = reg ? "new-password" : "current-password";
  $("#auth-error").textContent = "";
  $("#auth-email").focus();
}

function hideGate() {
  S.gate = null;
  $("#gate").hidden = true;
  document.body.classList.remove("gated");
}

async function submitAuth(e) {
  e.preventDefault();
  const body = { email: $("#auth-email").value, password: $("#auth-pass").value };
  try {
    await api(`/api/auth/${S.gate === "register" ? "register" : "login"}`, { method: "POST", body });
    $("#auth-pass").value = "";
    await enter();
  } catch (err) { $("#auth-error").textContent = err.message; }
}

async function submitOnboard(e) {
  e.preventDefault();
  const body = {
    name: $("#ob-name").value.trim(), description: $("#ob-desc").value.trim(), capabilities: [],
    authority: [...document.querySelectorAll('#ob-auth [name="authority"]:checked')].map((c) => c.value),
    visibility: $('#onboard-form [name="ob-vis"]:checked').value,
  };
  try {
    const a = await api("/api/agents", { method: "POST", body });
    hideGate();
    await reload();
    location.hash = `dm-${a.id}`;
  } catch (err) { $("#ob-error").textContent = err.message; }
}

// 登录后：拿到身份，没有自己的 Agent 就引导创建一个
async function enter() {
  S.me = await api("/api/me");
  $("#me-email").textContent = S.me.email;
  if (!S.started) { S.started = true; await start(); }
  else await reload();
  if (S.me.agent_count === 0 && !S.skipOnboard) showGate("onboard");
  else hideGate();
}

async function init() {
  S.info = await api("/api/info");
  $("#auth-form").addEventListener("submit", submitAuth);
  $("#auth-switch").addEventListener("click", () => showGate(S.gate === "register" ? "login" : "register"));
  $("#onboard-form").addEventListener("submit", submitOnboard);
  $("#ob-skip").addEventListener("click", () => { S.skipOnboard = true; hideGate(); });
  $("#logout").addEventListener("click", async () => {
    await api("/api/auth/logout", { method: "POST", body: {} }).catch(() => {});
    location.reload();
  });
  try { await enter(); }
  catch { showGate(S.info.has_users ? "login" : "register"); }
}

async function start() {
  const info = S.info;
  S.defaultModel = info.default_model;
  $("#runtime").textContent = `Runtime · ${info.runtime}`;
  if (info.default_model) $("#runtime").title = `未单独配置模型的 Agent 使用：${modelLabel(info.default_model)}`;
  $("#board-agent").addEventListener("change", () => { S.boardAgent = $("#board-agent").value; renderBoard(); });
  // 走 URL 而不是直接 show()：否则切回对话页时 URL 还停在 "#network"，
  // 刷新后会被路由回网络图。
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => { location.hash = b.dataset.view; }));
  $("#agent-new").addEventListener("click", () => { S.agentSel = null; renderAgentEditor(true); renderAgents(); });
  $("#group-new").addEventListener("click", () => { S.groupSel = null; renderGroupEditor(true); renderGroups(); });
  $("#group-quick").addEventListener("click", () => { S.groupSel = null; S.newGroup = true; location.hash = "groups"; });
  $("#tab-members").addEventListener("click", () => sideTab("members"));
  $("#tab-task").addEventListener("click", () => sideTab("task"));
  bindComposer();
  $("#msg-form").addEventListener("submit", sendMsg);
  window.addEventListener("resize", () => S.view === "network" && renderGraph());
  window.addEventListener("hashchange", route);
  await reload();
  route();
  connect();
}

function route() {
  const hash = location.hash.slice(1);
  const PLAIN = ["network", "board", "agents", "groups"];
  // 参照 Raft：#g-<群组> 打开群组的群聊，#dm-<Agent> 打开和它的私聊（第一次打开时创建）
  const m = hash.match(/^(g|dm)-(.+)$/);
  if (m) {
    const body = m[1] === "g" ? { group_id: m[2] } : { agent_id: m[2] };
    api("/api/conversations", { method: "POST", body })
      .then((c) => { location.replace(`#${c.id}`); })
      .catch((err) => { alert(err.message); location.replace("#network"); });
    return;
  }
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
  // 对话不是导航 tab：群聊与私聊常驻左栏，点哪个开哪个。
  document.querySelectorAll(".tab").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === view)));
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== `view-${view}`; });
  if (view !== "chats") history.replaceState(null, "", `#${view}`);
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
  const todo = (S.snap.approvals || []).length + (S.snap.proposals || []).filter((p) => p.status === "pending").length;
  $("#todo-count").hidden = !todo;
  $("#todo-count").textContent = todo;
  $("#todo-count").setAttribute("aria-label", `${todo} 件待处理`);
  ({ network: renderGraph, board: loadBoard, agents: renderAgentsView, groups: renderGroupsView, chats: renderChats })[S.view]();
}

// ---------- 看板（参照 Raft task board） ----------
const BOARD_COLS = [
  { key: "running", title: "执行中", match: (t) => t.status === "running" },
  { key: "in_review", title: "待验收", match: (t) => t.status === "in_review" },
  { key: "completed", title: "已完成", match: (t) => t.status === "completed" },
  { key: "failed", title: "失败 / 已拒绝", match: (t) => t.status === "failed" || t.status === "rejected" },
];

async function loadBoard() {
  try { S.board = await api("/api/board"); } catch { return; }
  renderBoard();
}

function renderBoard() {
  const b = S.board;
  if (!b || S.view !== "board") return;
  // 筛选：委托方或执行方是这个 Agent
  const ids = [...new Set(b.tasks.flatMap((t) => [t.delegator_id, t.delegatee_id]).filter(Boolean))];
  const sel = $("#board-agent"), want = S.boardAgent || "";
  sel.innerHTML = `<option value="">全部 Agent</option>` + ids.map((id) => `<option value="${esc(id)}">${esc(aname(id))}</option>`).join("");
  sel.value = ids.includes(want) ? want : "";
  const tasks = b.tasks.filter((t) => !sel.value || t.delegator_id === sel.value || t.delegatee_id === sel.value);

  // 待我处理：工作区外溢申请（我是 Agent 所有者）+ 我对话里的 Agent 提议
  const todo = [
    ...b.approvals.map((ap) => `<li class="todo-item approval">
      <div class="who">${avatar(aname(ap.agent_id), "sm")}<b>${esc(aname(ap.agent_id))}</b> 申请在工作目录以外操作 ${pill("pending")}
        <span class="muted">${time(ap.created_at)}</span></div>
      <p class="reason">${esc(ap.reason)}</p>
      <div class="actions"><button type="button" data-ap="${esc(ap.id)}" data-allow="1">允许这一次</button>
        <button type="button" class="ghost" data-ap="${esc(ap.id)}" data-allow="0">拒绝</button>
        <a href="#${esc(ap.conversation_id)}" class="muted">查看对话</a></div>
      <p class="hint">获批后只放行这一次调用（以完全访问权限运行），不是放行某个目录。</p></li>`),
    ...b.proposals.map((p) => `<li class="todo-item">${avatar(aname(p.proposer_id), "sm")}<span><b>${esc(aname(p.proposer_id))}</b> 提议新建
      「${esc(p.name)}」</span><a class="button sm" href="#${esc(p.conversation_id)}">去审核</a></li>`),
  ];
  $("#todo").hidden = !todo.length;
  $("#todo-list").innerHTML = todo.join("");
  $("#todo-list").querySelectorAll("[data-ap]").forEach((btn) => btn.addEventListener("click", async () => {
    btn.closest(".actions").querySelectorAll("button").forEach((x) => { x.disabled = true; });
    try { await api(`/api/approvals/${btn.dataset.ap}`, { method: "POST", body: { allow: btn.dataset.allow === "1" } }); }
    catch (err) { alert(err.message); }
    await loadBoard(); await reload();
  }));

  const card = (t) => `<li><button type="button" class="task-card ${S.taskSel === t.id ? "selected" : ""}" data-id="${esc(t.id)}">
      <span class="route"><span class="name">${esc(t.delegator_name)}</span><span aria-hidden="true">→</span><span class="name">${esc(t.delegatee_name)}</span></span>
      <span class="mission">${esc(t.delegation.mission)}</span>
      <span class="meta"><span>${esc(t.conversation_title)}</span><span>${time(t.created_at)}</span>
        ${t.revisions ? `<span>退回 ${t.revisions}</span>` : ""}${t.retries ? `<span>续接 ${t.retries}</span>` : ""}</span>
    </button></li>`;
  $("#board-cols").innerHTML = BOARD_COLS.map((col) => {
    const xs = tasks.filter(col.match);
    return `<section class="board-col ${col.key}" aria-label="${col.title}">
      <h3>${col.title}<span class="count">${xs.length}</span></h3>
      <ul>${xs.map(card).join("") || '<li class="empty">无</li>'}</ul></section>`;
  }).join("");
  $("#board-cols").querySelectorAll(".task-card").forEach((btn) => btn.addEventListener("click", () => {
    S.taskSel = btn.dataset.id; renderBoard(); renderTask($("#board-detail"));
  }));

  const busy = Object.entries(b.ledger || {});
  $("#busy").hidden = !busy.length;
  $("#busy-list").innerHTML = busy.map(([aid, items]) => `<li>${avatar(aname(aid), "sm")}<b>${esc(aname(aid))}</b>
    <span class="muted">同时在做 ${items.length} 件：</span>${items.map((it) => `<span class="tag">${esc(it.kind === "task" ? `委托：${it.what}` : it.what)}${it.user ? ` · ${esc(it.user)}` : ""}</span>`).join("")}</li>`).join("");
  if (S.taskSel && tasks.some((t) => t.id === S.taskSel)) renderTask($("#board-detail"));
  else $("#board-detail").innerHTML = placeholder("点一张任务卡片，查看授权、约束、过程与结果。");
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
    if (d.task.status === "running" || d.task.status === "in_review") S.running.set(d.task.id, d.task);
    else S.running.delete(d.task.id);
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

  // 人 → Agent：可对话（私聊）或在群聊里被 @ 过；参与的对话数决定粗细
  const convCount = {};
  S.snap.conversations.forEach((c) => Object.keys(c.seats || {}).forEach((a) => { convCount[a] = (convCount[a] || 0) + 1; }));
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
      <dt>模型</dt><dd>${esc(modelLabel(effectiveModel(x)) || "运行时默认")}${x.model ? "" : ' <span class="muted">（默认）</span>'}</dd>
      <dt>可对话</dt><dd>${x.chat_enabled ? "是" : "否"}</dd>
      ${x.created_by.startsWith("proposal:") ? `<dt>来源</dt><dd>由 Agent 提议、人批准创建</dd>` : ""}
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
      <div class="stat"><b>${S.snap.active_tasks.length}</b><span>进行中委托</span></div>
      <div class="stat"><b>${(S.snap.proposals || []).filter((p) => p.status === "pending").length}</b><span>待审核提议</span></div></div>
      <p class="muted">连线粗细代表历史委托次数，流动的虚线是正在进行的委托，节点外圈颜色对应所属群组。</p>`;
  }
}

// ================= Agent 管理 =================
function renderAgentsView() { renderAgents(); renderAgentEditor(); }

// 卡片上的“对话”：链接到 #g-<群组> / #dm-<Agent>，路由负责打开（私聊第一次打开时创建）
const chatBtn = (hash, label) => `<a class="button sm chat-btn" href="#${esc(hash)}" aria-label="${esc(label)}">
  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M2.5 4.5a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2H7l-3 2.5v-2.5h0a1.5 1.5 0 0 1-1.5-1.5z"/></svg>对话</a>`;

function renderAgents() {
  // 卡片主体选中编辑；底部“对话”按钮直接打开私聊。按钮不能嵌套，所以“对话”是卡片里的另一个按钮。
  $("#agent-list").innerHTML = S.snap.agents.map((a) => `
    <li class="${S.agentSel === a.id ? "selected" : ""}"><div class="card">
      <button type="button" class="card-main" data-id="${esc(a.id)}" aria-label="编辑 ${esc(a.name)}">
        <div class="card-head">${avatar(a.name)}<span class="name">${esc(a.name)}</span>${pill(a.status)}</div>
        <p class="card-desc">${esc(a.description)}</p>
        <div class="card-meta">${tags(a.authority, "ok")}${modelTag(a)}</div>
      </button>
      <div class="card-foot"><span>${a.mine ? `<span class="vis-tag ${esc(a.visibility)}">${VIS_CN[a.visibility]}</span>`
          : `<span class="vis-tag other">${esc(a.owner_email || "他人")} 的</span>`}</span>
        ${a.chat_enabled ? chatBtn(`dm-${a.id}`, `和 ${a.name} 私聊`) : '<span class="muted">不开放私聊</span>'}</div>
    </div></li>`).join("") || '<li class="empty">还没有 Agent。点“新建 Agent”创建一个。</li>';
  $("#agent-list").querySelectorAll(".card-main").forEach((b) =>
    b.addEventListener("click", () => { S.agentSel = b.dataset.id; renderAgents(); renderAgentEditor(); }));
}

function authorityBoxes(selected) {
  return Object.entries(S.snap.authorities).map(([k, v]) =>
    `<label><input type="checkbox" name="authority" value="${esc(k)}" ${selected.includes(k) ? "checked" : ""}>
      <span class="opt"><code>${esc(k)}</code><small>${esc(v)}</small></span></label>`).join("");
}

function readModel(f) {
  const provider = f.provider.value.trim(), model = f.model.value.trim(), effort = f.effort.value.trim();
  return provider && model ? { provider, model, reasoning_effort: effort || null } : null;
}

const effectiveModel = (a) => a.model || S.defaultModel;
const modelTag = (a) => {
  const m = effectiveModel(a);
  return m ? `<span class="tag model" title="${a.model ? "单独配置" : "运行时默认"}">${esc(m.model)}${a.model ? "" : " · 默认"}</span>` : "";
};

const VIS_CN = { private: "只有我", public: "所有人", shared: "指定的人" };

function visibilityFields(x) {
  const users = S.users || [];
  return `<fieldset class="vis"><legend>谁能用它</legend>
    <div class="seg">${Object.entries(VIS_CN).map(([k, v]) =>
      `<label><input type="radio" name="visibility" value="${k}" ${x.visibility === k ? "checked" : ""}> ${v}</label>`).join("")}</div>
    <div class="share-with" ${x.visibility === "shared" ? "" : "hidden"}>
      ${users.length ? users.map((u) => `<label class="switch"><input type="checkbox" name="shared_with" value="${esc(u.id)}"
        ${(x.shared_with || []).includes(u.id) ? "checked" : ""}> ${esc(u.email)}</label>`).join("") : '<p class="hint">还没有其他用户。</p>'}
    </div>
    <p class="hint">能用它的人可以私聊它、把它拉进自己的群；只有你能修改它、审批它在工作目录以外的操作。</p></fieldset>`;
}

function renderAgentReadonly(box, a) {
  box.innerHTML = `<h2 class="panel-title">${avatar(a.name)} ${esc(a.name)} ${pill(a.status)}</h2>
    <p class="muted">${esc(a.owner_email || "")} 的 Agent · ${VIS_CN[a.visibility] || ""}可用</p>
    <dl><dt>描述</dt><dd>${esc(a.description)}</dd><dt>能力</dt><dd>${tags(a.capabilities)}</dd>
    <dt>自身权限</dt><dd>${tags(a.authority, "ok")}</dd><dt>模型</dt><dd>${esc(modelLabel(effectiveModel(a)) || "运行时默认")}</dd></dl>
    <div class="actions">${a.chat_enabled ? chatBtn(`dm-${a.id}`, `和 ${a.name} 私聊`) : '<span class="muted">不开放私聊</span>'}</div>
    <p class="hint">只有所有者能修改它。</p>`;
}

function renderAgentEditor(isNew = false) {
  const box = $("#agent-editor"), a = agent(S.agentSel);
  if (!a && !isNew) { box.innerHTML = placeholder("选择一个 Agent 编辑，或新建。"); return; }
  if (a && !a.mine) { renderAgentReadonly(box, a); return; }
  if (!S.users) api("/api/users").then((u) => { S.users = u; renderAgentEditor(isNew); }).catch(() => {});
  const x = a || { name: "", description: "", capabilities: [], authority: ["web.research"], chat_enabled: true,
    visibility: "private", shared_with: [] };
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
      <fieldset class="model"><legend>模型</legend>
        <div class="model-fields">
          <div class="field"><label for="af-provider">Provider</label><input id="af-provider" name="provider" value="${esc(x.model?.provider || "")}" placeholder="${esc(S.defaultModel?.provider || "")}"></div>
          <div class="field"><label for="af-model">Model</label><input id="af-model" name="model" value="${esc(x.model?.model || "")}" placeholder="${esc(S.defaultModel?.model || "")}"></div>
          <div class="field"><label for="af-effort">推理强度</label><input id="af-effort" name="effort" value="${esc(x.model?.reasoning_effort || "")}" placeholder="默认"></div>
        </div>
        <p class="hint">留空则使用运行时默认模型${S.defaultModel ? `（${esc(modelLabel(S.defaultModel))}）` : ""}。provider 必须是运行时里已配置的。</p>
      </fieldset>
      ${visibilityFields(x)}
      <label class="switch"><input type="checkbox" name="chat_enabled" ${x.chat_enabled ? "checked" : ""}> 允许人和它私聊（群聊里随时可以 @ 它）</label>
      <p class="error" role="alert"></p>
      <div class="actions"><button type="submit">${a ? "保存" : "创建"}</button>
        ${a ? `${a.chat_enabled ? '<button type="button" class="ghost" id="af-chat">私聊</button>' : ""}<button type="button" class="danger" id="af-del">删除</button>` : ""}</div>
    </form>`;
  const f = $("#agent-form");
  f.querySelectorAll('[name="visibility"]').forEach((r) => r.addEventListener("change", () => {
    $(".share-with", f).hidden = f.querySelector('[name="visibility"]:checked').value !== "shared";
  }));
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      name: f.name.value.trim(), description: f.description.value.trim(),
      capabilities: f.capabilities.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
      authority: [...f.querySelectorAll('[name="authority"]:checked')].map((c) => c.value),
      chat_enabled: f.chat_enabled.checked,
      model: readModel(f),
      visibility: f.querySelector('[name="visibility"]:checked').value,
      shared_with: [...f.querySelectorAll('[name="shared_with"]:checked')].map((c) => c.value),
    };
    if ((f.provider.value.trim() !== "") !== (f.model.value.trim() !== "")) {
      $(".error", f).textContent = "Provider 与 Model 要么都填，要么都留空。"; return;
    }
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
  $("#af-chat")?.addEventListener("click", () => { location.hash = `dm-${a.id}`; });
}

// ================= 群组管理 =================
function renderGroupsView() {
  renderGroups();
  renderGroupEditor(S.newGroup);
  if (S.newGroup) { S.newGroup = false; $("#gf-name")?.focus(); }
}

function renderGroups() {
  $("#group-list").innerHTML = S.snap.groups.map((g) => `
    <li class="${S.groupSel === g.id ? "selected" : ""}"><div class="card">
      <button type="button" class="card-main" data-id="${esc(g.id)}" aria-label="编辑 ${esc(g.name)}">
        <div class="card-head"><span class="swatch" style="background:${gcolor(g.id)}"></span><span class="name">${esc(g.name)}</span><span class="pill">${g.member_ids.length} 名成员</span></div>
        <p class="card-desc">${esc(g.description || "没有描述")}</p>
        <div class="card-meta">${tags(g.member_ids.map(aname))}</div>
      </button>
      <div class="card-foot"><span></span>${chatBtn(`g-${g.id}`, `进入 ${g.name} 的群聊`)}</div>
    </div></li>`).join("") || '<li class="empty">还没有群组。</li>';
  $("#group-list").querySelectorAll(".card-main").forEach((b) =>
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
        ${g ? '<button type="button" class="ghost" id="gf-chat">进入群聊</button><button type="button" class="danger" id="gf-del">删除群组</button>' : ""}</div>
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
  $("#gf-chat")?.addEventListener("click", () => { location.hash = `g-${g.id}`; });
  $("#gf-del")?.addEventListener("click", async () => {
    if (!confirm(`删除群组 ${g.name}？已有对话保留，但不再能委托。`)) return;
    try { await api(`/api/groups/${g.id}`, { method: "DELETE" }); S.groupSel = null; await reload(); }
    catch (err) { $(".error", f).textContent = err.message; }
  });
}

// ================= 对话 =================
const plain = (s) => String(s || "").replace(/[#*_`>|~]+/g, "").replace(/\s+/g, " ").trim();

function renderConvList() {
  // 参照 Raft 的侧栏：群聊（每个群组一个）和私聊（每个可私聊的 Agent 一个），点进去就能聊。
  const convs = S.snap.conversations;
  const live = convs.filter((c) => !c.archived);
  const pending = new Set((S.snap.proposals || []).filter((p) => p.status === "pending").map((p) => p.conversation_id));
  const dot = (c) => !c ? "" : c.status === "thinking" ? '<span class="dot-live" title="运行中"></span>'
    : c.status === "waiting" ? '<span class="dot-live waiting" title="等委托回报"></span>'
    : c.blocked ? '<span class="dot-live blocked" title="有成员上一轮中断"></span>' : "";
  const item = (href, sel, icon, name, sub, c) => `<li class="${sel ? "selected" : ""}"><a href="#${esc(href)}"${sel ? ' aria-current="page"' : ""}>
      ${icon}<span class="c-body"><span class="t"><span class="n">${esc(name)}</span>${dot(c)}
        ${c && pending.has(c.id) ? '<span class="pill pending" title="有待审核的 Agent 提议">待审核</span>' : ""}</span>
        <span class="s">${esc(sub)}</span></span></a></li>`;
  const opened = (c) => S.view === "chats" && S.convId === c?.id;

  $("#group-chat-list").innerHTML = S.snap.groups.map((g) => {
    const c = live.find((x) => x.kind === "group" && x.group_id === g.id);
    const icon = `<span class="avatar sm group" aria-hidden="true" style="background:${gcolor(g.id)}">#</span>`;
    return item(c ? c.id : `g-${g.id}`, opened(c), icon, g.name,
      plain(c?.last_message) || `${g.member_ids.length} 名成员`, c);
  }).join("") || '<li class="empty">还没有群组</li>';

  // 只列出聊过的私聊（以及当前打开的那个）；要和新的 Agent 私聊，在 Agent 卡片上点“对话”
  $("#dm-list").innerHTML = live.filter((c) => c.kind === "dm" && (c.message_count || opened(c)))
    .sort((a, b) => b.updated_at - a.updated_at)
    .map((c) => item(c.id, opened(c), avatar(aname(c.agent_id), "sm"), aname(c.agent_id),
      plain(c.last_message) || "还没有消息", c)).join("")
    || '<li class="empty">还没有私聊。在 Agent 卡片上点“对话”开始。</li>';

  const archived = convs.filter((c) => c.archived);
  $("#archived").hidden = !archived.length;
  const day = (ts) => new Date(ts * 1000).toLocaleDateString([], { month: "numeric", day: "numeric" });
  $("#archived-list").innerHTML = archived.sort((a, b) => b.updated_at - a.updated_at).map((c) => item(c.id, opened(c),
    '<span class="avatar sm" aria-hidden="true">·</span>', `${c.title} · ${day(c.updated_at)}`,
    plain(c.last_message) || "（空）", c)).join("");
  if (archived.some(opened)) $("#archived").open = true;
}

function renderChats() {
  $("#view-chats").classList.remove("start");
  renderThread();
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
  [`mcp__agentnet__share_knowledge`]: "共享知识", [`mcp__agentnet__list_tasks`]: "查看委托",
  [`mcp__agentnet__accept_task`]: "验收", [`mcp__agentnet__request_changes`]: "退回修改",
  [`mcp__agentnet__retry_task`]: "续接委托", [`mcp__agentnet__propose_agent`]: "提议新 Agent", web_search: "搜索", web_fetch: "抓取网页", read: "读取",
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

// Agent 提议新建的 Agent：人在这里审核，可以改字段；权限只能在提议者当时的有效权限之内勾选。
function proposalCard(p) {
  if (p.status !== "pending") {
    return `<li class="msg system"><div class="content"><div class="body">提议新建「${esc(p.name)}」${pill(p.status)}
      ${p.note ? `<span class="muted">· ${esc(p.note)}</span>` : ""}</div></div></li>`;
  }
  const boxes = p.max_authority.map((k) => `<label><input type="checkbox" name="authority" value="${esc(k)}" ${p.authority.includes(k) ? "checked" : ""}>
    <span class="opt"><code>${esc(k)}</code></span></label>`).join("");
  return `<li class="msg proposal"><div class="content">
    <form class="proposal-card" data-id="${esc(p.id)}" aria-label="审核提议 ${esc(p.name)}">
      <div class="who"><b>${esc(aname(p.proposer_id))}</b> 提议新建 Agent ${pill("pending")}</div>
      <p class="muted">${esc(p.reason)}</p>
      <div class="field"><label for="pp-name-${esc(p.id)}">名称</label><input id="pp-name-${esc(p.id)}" name="name" value="${esc(p.name)}" required minlength="2"></div>
      <div class="field"><label for="pp-desc-${esc(p.id)}">描述</label><textarea id="pp-desc-${esc(p.id)}" name="description" rows="2" required minlength="2">${esc(p.description)}</textarea></div>
      <div class="field"><label for="pp-caps-${esc(p.id)}">能力</label><input id="pp-caps-${esc(p.id)}" name="capabilities" value="${esc(p.capabilities.join(", "))}"></div>
      <fieldset class="checks"><legend>权限 · 不能超出提议者的有效权限</legend>${boxes || '<span class="muted">提议者没有可授予的权限</span>'}</fieldset>
      <div class="field"><label for="pp-note-${esc(p.id)}">审核意见（会告诉提议者）</label><input id="pp-note-${esc(p.id)}" name="note"></div>
      <p class="hint">批准后以你的身份创建，并加入群组「${esc(group(p.group_id)?.name || "")}」。</p>
      <p class="error" role="alert"></p>
      <div class="actions"><button type="submit">批准并创建</button><button type="button" class="ghost" data-act="reject">拒绝</button></div>
    </form></div></li>`;
}

function bindProposals(root) {
  root.querySelectorAll(".proposal-card").forEach((f) => {
    const id = f.dataset.id;
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = { name: f.name.value.trim(), description: f.description.value.trim(), note: f.note.value.trim(),
        capabilities: f.capabilities.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
        authority: [...f.querySelectorAll('[name="authority"]:checked')].map((c) => c.value) };
      try { await api(`/api/proposals/${id}/approve`, { method: "POST", body }); await reload(); }
      catch (err) { $(".error", f).textContent = err.message; }
    });
    $('[data-act="reject"]', f).addEventListener("click", async () => {
      try { await api(`/api/proposals/${id}/reject`, { method: "POST", body: { note: f.note.value.trim() } }); await reload(); }
      catch (err) { $(".error", f).textContent = err.message; }
    });
  });
}

// ---------- 侧栏：成员 / 委托详情 ----------
function sideTab(which) {
  S.sideTab = which;
  $("#tab-members").setAttribute("aria-selected", String(which === "members"));
  $("#tab-task").setAttribute("aria-selected", String(which === "task"));
  $("#members-panel").hidden = which !== "members";
  $("#task-detail").hidden = which !== "task";
  if (which === "task") renderTask();
}

// 群成员随时加减：在这里加人、移人，变动会作为一条事件出现在群聊里。私聊显示对方。
function renderMembers() {
  const box = $("#members-panel"), c = S.conv;
  if (!c || c.missing) { box.innerHTML = ""; return; }
  const row = (a, extra = "") => `<li>${avatar(a.name, "sm")}<span class="m-body"><span class="n">${esc(a.name)}
      ${c.live?.[a.id] ? '<span class="dot-live" title="运行中"></span>' : ""}</span>
      <span class="s">${esc(a.capabilities.join(" · ") || a.description)}</span></span>${extra}</li>`;
  if (c.kind === "dm") {
    const a = agent(c.agent_id);
    box.innerHTML = a ? `<ul class="member-list">${row(a)}</ul>
      <dl><dt>自身权限</dt><dd>${tags(a.authority, "ok")}</dd><dt>模型</dt><dd>${esc(modelLabel(effectiveModel(a)) || "运行时默认")}</dd></dl>
      <p class="hint">私聊里它只能自己完成，不能委托。要让它和别的成员协作，在群聊里 @ 它。</p>` : placeholder("这个 Agent 已删除。");
    return;
  }
  const g = group(c.group_id);
  if (!g) { box.innerHTML = placeholder("群组已删除。"); return; }
  const members = g.member_ids.map(agent).filter(Boolean);
  const others = S.snap.agents.filter((a) => !g.member_ids.includes(a.id));
  box.innerHTML = `<h3>成员 · ${members.length}</h3>
    <ul class="member-list">${members.map((a) => row(a, `<button type="button" class="ghost icon sm rm" data-id="${esc(a.id)}"
      aria-label="把 ${esc(a.name)} 移出群组" title="移出群组"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M4 8h8"/></svg></button>`)).join("")
      || '<li class="empty">还没有成员</li>'}</ul>
    ${others.length ? `<form class="add-member" aria-label="添加成员">
      <label for="add-member-sel" class="sr-only">添加成员</label>
      <select id="add-member-sel">${others.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("")}</select>
      <button type="submit">添加</button></form>` : '<p class="hint">所有 Agent 都已在群组里。</p>'}
    <p class="error" role="alert"></p>
    <p class="hint">移出群组不会打断它手上已经接下的任务，只是之后不能再被 @、被委托。</p>`;
  const err = (e) => { $(".error", box).textContent = e.message; };
  box.querySelectorAll(".rm").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/api/groups/${g.id}/members/${b.dataset.id}`, { method: "DELETE" }); await reload(); } catch (e) { err(e); }
  }));
  $(".add-member", box)?.addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await api(`/api/groups/${g.id}/members`, { method: "POST", body: { agent_id: $("#add-member-sel").value } }); await reload(); }
    catch (e2) { err(e2); }
  });
}

function renderThread() {
  const c = S.conv;
  if (!c) return;
  if (c.missing) {
    $("#thread-head").innerHTML = "";
    $("#thread").innerHTML = `<li>${placeholder("这个对话不存在或已删除。")}</li>`;
    $("#msg-form").hidden = true; renderTask(); return;
  }
  $("#msg-form").hidden = !!c.archived;
  const g = group(c.group_id), dm = c.kind === "dm", peer = agent(c.agent_id);
  const head = dm
    ? `${avatar(peer?.name)}<div class="info"><h2 class="panel-title" style="margin:0">${esc(peer?.name || c.title)} ${pill(c.status)}</h2>
        <div class="muted">${c.archived ? "Agent 已删除，只读" : `私聊 · ${esc(peer?.description || "")}`}</div></div>`
    : `<span class="avatar group" aria-hidden="true" style="background:${g ? gcolor(g.id) : "var(--surface-3)"}">#</span>
       <div class="info"><h2 class="panel-title" style="margin:0">${esc(c.title)} ${pill(c.status)}</h2>
        <div class="muted">${c.archived ? "已归档，只读" : `${c.members.length} 名成员 · @ 谁，谁接任务${g?.description ? ` · ${esc(g.description)}` : ""}`}</div></div>`;
  const actions = c.archived ? '<button type="button" class="danger" id="conv-del">删除</button>'
    : '<button type="button" class="ghost" id="conv-clear" title="清空聊天记录，成员从新的会话开始">清空记录</button>';
  $("#thread-head").innerHTML = head + actions;
  $("#conv-del")?.addEventListener("click", async () => {
    if (!confirm("删除这个已归档的对话？")) return;
    try { await api(`/api/conversations/${c.id}`, { method: "DELETE" }); S.convId = null; S.conv = null; location.hash = "network"; await reload(); }
    catch (err) { alert(err.message); }
  });
  $("#conv-clear")?.addEventListener("click", async () => {
    if (!confirm("清空这个对话的聊天记录？成员的会话会从头开始；委托与知识不受影响。")) return;
    try { await api(`/api/conversations/${c.id}/clear`, { method: "POST", body: {} }); await reload(); }
    catch (err) { alert(err.message); }
  });
  renderMembers();

  const byParent = {};
  c.tasks.forEach((t) => (byParent[t.parent_task_id || "root"] ||= []).push(t));
  const treeOf = (ids) => {
    const out = [];
    const walk = (t, child) => { out.push(delegBtn(t, child)); (byParent[t.id] || []).forEach((x) => walk(x, true)); };
    c.tasks.filter((t) => ids.includes(t.id)).forEach((t) => walk(t, false));
    return out.join("");
  };
  const assigned = new Set(c.messages.flatMap((m) => m.task_ids));
  const wakeHtml = (m) => (m.wake || []).length
    ? `<div class="wake" aria-label="唤醒这一轮的通知">${m.wake.map((w) => `<span class="tag">${esc(w)}</span>`).join("")}</div>` : "";
  // 每个中断席位只在它最后一条系统消息上给“继续”
  const lastSys = {};
  c.messages.forEach((m, i) => { if (m.role === "system" && m.agent_id) lastSys[m.agent_id] = i; });
  const mentionHtml = (text) => esc(text).replace(/@([^\s@，。,:：]+(?: [A-Z][\w-]*)*)/g, (s) =>
    members().some((a) => s.slice(1).toLowerCase().startsWith(a.name.toLowerCase())) ? `<span class="mention">${s}</span>` : s);
  const items = c.messages.map((m, i) => {
    const delegs = m.task_ids.length ? `<div class="delegs" aria-label="本轮委托">${treeOf(m.task_ids)}</div>` : "";
    const who = agent(m.agent_id);
    if (m.role === "event") return `<li class="msg event"><span>${esc(m.text)}</span><span class="muted">${time(m.ts)}</span></li>`;
    if (m.role === "system") {
      const st = c.seats?.[m.agent_id];
      const resume = lastSys[m.agent_id] === i && st?.blocked && !c.live?.[m.agent_id]
        ? `<div class="actions"><button type="button" class="resume" data-agent="${esc(m.agent_id)}">继续</button><span class="hint">${esc(who?.name || "")} 在同一会话里从中断处接着做</span></div>` : "";
      return `<li class="msg system"><div class="content">${wakeHtml(m)}${stepsHtml(m.steps || [], false)}${delegs}<div class="body">${esc(m.text)}</div>${resume}</div></li>`;
    }
    if (m.role === "user") {
      const to = !dm && m.mentions.length ? `<span class="to">→ ${m.mentions.map((x) => esc(aname(x))).join("、")}</span>`
        : !dm ? '<span class="to muted">未 @ 任何人</span>' : "";
      return `<li class="msg user"><div class="content"><div class="who"><b>你</b>${to}${time(m.ts)}</div><div class="body">${mentionHtml(m.text)}</div></div></li>`;
    }
    return `<li class="msg agent">${avatar(who?.name, "sm")}<div class="content">
      <div class="who"><b>${esc(who?.name || "（已删除）")}</b>${time(m.ts)}</div>${wakeHtml(m)}${stepsHtml(m.steps || [], false)}${delegs}<div class="body md">${renderMarkdown(m.text)}</div></div></li>`;
  });
  (c.proposals || []).forEach((p) => items.push(proposalCard(p)));
  (c.approvals || []).forEach((ap) => items.push(`<li class="msg proposal"><div class="content"><div class="proposal-card approval-card">
    <div class="who"><b>${esc(aname(ap.agent_id))}</b> 申请在工作目录以外操作 ${pill("pending")}</div>
    <p class="reason">${esc(ap.reason)}</p>
    ${ap.can_decide ? `<div class="actions"><button type="button" data-ap="${esc(ap.id)}" data-allow="1">允许这一次</button>
      <button type="button" class="ghost" data-ap="${esc(ap.id)}" data-allow="0">拒绝</button></div>`
      : '<p class="hint">等这个 Agent 的所有者审批。</p>'}</div></div></li>`));
  if (c.status === "waiting") {
    const open = c.tasks.filter((t) => t.status === "running" && !t.parent_task_id);
    items.push(`<li class="msg system"><div class="content"><div class="body">等 ${open.length} 个委托回报：
      ${open.map((t) => `${esc(aname(t.delegator_id))} → ${esc(aname(t.delegatee_id))}`).join("、")}。结果到了会自动唤醒委托方，你也可以继续发消息。</div></div></li>`);
  }
  // 正在跑的席位：各自一条实时过程
  Object.entries(c.live || {}).forEach(([aid, live]) => {
    const who = agent(aid);
    const pending = c.tasks.filter((t) => !t.parent_task_id && !assigned.has(t.id) && t.delegator_id === aid).map((t) => t.id);
    items.push(`<li class="msg agent">${avatar(who?.name, "sm")}<div class="content"><div class="who"><b>${esc(who?.name)}</b>运行中</div>
      ${stepsHtml(live, true)}${pending.length ? `<div class="delegs">${treeOf(pending)}</div>` : ""}
      <span class="typing" aria-label="处理中"><i></i><i></i><i></i></span></div></li>`);
  });
  const th = $("#thread"), atBottom = th.scrollHeight - th.scrollTop - th.clientHeight < 80;
  // 正在填审核表单时不重绘线程，否则输入会被事件驱动的刷新清掉
  const typing = document.activeElement?.matches?.(".proposal-card input, .proposal-card textarea");
  if (typing && th.contains(document.activeElement)) { renderTask(); return; }
  // 保留用户手动展开的“过程”，重绘不把它们收起来
  const open = new Set([...th.querySelectorAll("details[open]")].map((d) => d.closest(".msg")?.dataset.i));
  th.innerHTML = items.join("") || `<li>${placeholder(dm ? "发一条消息开始私聊。"
    : "@ 一个成员把任务交给它。它可以自己做，也可以在群组里找合适的成员委托。")}</li>`;
  [...th.children].forEach((li, i) => { li.dataset.i = i; if (open.has(String(i))) li.querySelector("details")?.setAttribute("open", ""); });
  if (atBottom) th.scrollTop = th.scrollHeight;
  th.querySelectorAll(".deleg").forEach((b) => b.addEventListener("click", () => {
    S.taskSel = b.dataset.id;
    th.querySelectorAll(".deleg").forEach((x) => x.classList.toggle("selected", x === b));
    sideTab("task");
  }));
  bindProposals(th);
  th.querySelectorAll("[data-ap]").forEach((btn) => btn.addEventListener("click", async () => {
    btn.parentElement.querySelectorAll("button").forEach((x) => { x.disabled = true; });
    try { await api(`/api/approvals/${btn.dataset.ap}`, { method: "POST", body: { allow: btn.dataset.allow === "1" } }); await reload(); }
    catch (err) { alert(err.message); }
  }));
  th.querySelectorAll(".resume").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await api(`/api/conversations/${c.id}/resume`, { method: "POST", body: { agent_id: b.dataset.agent } }); await reload(); }
    catch (err) { alert(err.message); b.disabled = false; }
  }));
  // Agent 运行中也可以发消息：进被 @ 成员的收件箱排队，这一轮结束后送达
  $("#msg-form button[type=submit]").disabled = !dm && !g;
  $("#msg").disabled = !dm && !g;
  renderComposer();
  renderTask();
}

function delegBtn(t, child) {
  return `<button type="button" class="deleg${child ? " child" : ""}${S.taskSel === t.id ? " selected" : ""}" data-id="${esc(t.id)}">
    <span class="route"><span class="name">${esc(aname(t.delegator_id))}</span><span class="arrow">→</span><span class="name">${esc(t.delegatee_id ? aname(t.delegatee_id) : "（群组外）")}</span>${pill(t.status)}</span>
    <span class="mission">${esc(t.delegation.mission)}</span></button>`;
}

async function renderTask(target) {
  // 对话侧栏或看板侧栏：看板里点卡片时传入看板的详情框
  const box = target || (S.view === "board" ? $("#board-detail") : $("#task-detail")), id = S.taskSel;
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
    ${t.revisions || t.retries ? `<dt>返工</dt><dd>退回 ${t.revisions} 次 · 续接 ${t.retries} 次</dd>` : ""}
    ${t.auto_accepted ? `<dt>验收</dt><dd class="muted">委托方没有显式验收，视为接受</dd>` : ""}
    ${t.violations.length ? `<dt>越权</dt><dd class="bad">${t.violations.map(esc).join("<br>")}</dd>` : ""}
    ${t.error ? `<dt>原因</dt><dd class="bad">${esc(t.error)}</dd>` : ""}</dl>
    ${t.status === "failed" ? `<div class="actions"><button type="button" id="task-retry">续接这个任务</button>
      <span class="hint">执行者在原会话里接着做</span></div><p class="error" role="alert"></p>` : ""}
    ${t.notes.length ? `<h3>进展说明</h3>${t.notes.map((n) => `<div class="md note">${renderMarkdown(n)}</div>`).join("")}` : ""}
    ${t.status === "running" ? `<h3>实时过程</h3>${stepsHtml(t.live || [], true) || '<span class="typing" aria-label="处理中"><i></i><i></i><i></i></span>'}` : ""}
    ${t.result ? `<h3>交付结果</h3><div class="md report">${renderMarkdown(t.result.report)}</div>` : ""}
    ${t.evidence.length ? `<h3>工具调用证据（${t.evidence.length}）</h3><pre>${esc(t.evidence.map((e) => `${e.status} ${e.tool} ${e.input}`).join("\n"))}</pre>` : ""}
    <details><summary>执行者收到的提示词</summary><pre>${esc(t.prompt)}</pre></details>`;
  box.scrollTop = keep;
  $("#task-retry", box)?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await api(`/api/tasks/${t.id}/retry`, { method: "POST", body: {} }); await reload(); }
    catch (err) { $(".error", box).textContent = err.message; e.target.disabled = false; }
  });
}

async function sendMsg(e) {
  e.preventDefault();
  const text = $("#msg").value.trim();
  if (!text || !S.convId) return;
  $("#msg-form button[type=submit]").disabled = true;
  try {
    await api(`/api/conversations/${S.convId}/messages`, { method: "POST", body: { text } });  // @名称 由服务端识别
    $("#msg").value = ""; composerHint(); await reload();
  } catch (err) { alert(err.message); }
  $("#msg-form button[type=submit]").disabled = false;
}

// ---------- 输入框：@ 成员 ----------
// 群聊里 @ 谁谁接任务：输入 @ 弹出成员列表补全。私聊不需要 @。
const members = () => (S.conv?.members || []).map(agent).filter(Boolean);
const mentionedIn = (text) => members().filter((a) => text.toLowerCase().includes(`@${a.name.toLowerCase()}`));

function renderComposer() {
  composerHint();
}

function composerHint() {
  const c = S.conv;
  if (!c) return;
  const m = $("#msg"), who = mentionedIn(m.value);
  if (c.kind === "dm") {
    m.placeholder = "和它说点什么";
    $("#msg-hint").innerHTML = "<kbd>⌘</kbd> <kbd>Enter</kbd> 发送";
    return;
  }
  m.placeholder = "@ 一个成员把任务交给它，例如：@Product Strategy Agent 研究一个新的 AI 产品机会";
  $("#msg-hint").innerHTML = (who.length ? `交给 ${who.map((a) => esc(a.name)).join("、")}`
    : m.value.trim() ? '<span class="warn">没有 @ 任何人：消息会留在群聊里，但不会有 Agent 处理</span>' : "@ 谁，谁接任务")
    + " · <kbd>⌘</kbd> <kbd>Enter</kbd> 发送";
}

function insertMention(a) {
  if (!a) return;
  const m = $("#msg"), at = m.selectionStart ?? m.value.length;
  // 如果光标前是正在输入的 @片段，替换它；否则在光标处插入
  const before = m.value.slice(0, at).replace(/@[^\s@]*$/, ""), after = m.value.slice(at);
  const ins = `@${a.name} `;
  m.value = before + ins + after;
  const pos = before.length + ins.length;
  m.focus(); m.setSelectionRange(pos, pos);
  closeMentionPop(); composerHint();
}

function mentionQuery() {
  const m = $("#msg"), at = m.selectionStart ?? 0;
  const hit = m.value.slice(0, at).match(/(?:^|\s)@([^\s@]*)$/);
  return hit ? hit[1].toLowerCase() : null;
}

function openMentionPop() {
  const q = mentionQuery(), pop = $("#mention-pop");
  if (S.conv?.kind !== "group" || q === null) return closeMentionPop();
  const opts = members().filter((a) => a.name.toLowerCase().includes(q));
  if (!opts.length) return closeMentionPop();
  S.popIndex = Math.min(S.popIndex || 0, opts.length - 1);
  pop.innerHTML = opts.map((a, i) => `<li role="option" id="mp-${i}" data-id="${esc(a.id)}" aria-selected="${i === S.popIndex}">
    ${avatar(a.name, "xs")}<span class="n">${esc(a.name)}</span><span class="muted">${esc(a.capabilities.join(" · "))}</span></li>`).join("");
  pop.hidden = false;
  $("#msg").setAttribute("aria-activedescendant", `mp-${S.popIndex}`);
  pop.querySelectorAll("li").forEach((li) => li.addEventListener("mousedown", (e) => { e.preventDefault(); insertMention(agent(li.dataset.id)); }));
}

function closeMentionPop() {
  $("#mention-pop").hidden = true; S.popIndex = 0;
  $("#msg").removeAttribute("aria-activedescendant");
}

function bindComposer() {
  const m = $("#msg");
  m.setAttribute("role", "combobox");
  m.setAttribute("aria-autocomplete", "list");
  m.setAttribute("aria-controls", "mention-pop");
  m.addEventListener("input", () => { openMentionPop(); composerHint(); });
  m.addEventListener("blur", () => setTimeout(closeMentionPop, 100));
  m.addEventListener("keydown", (e) => {
    const pop = $("#mention-pop");
    if (!pop.hidden) {
      const n = pop.children.length;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault(); S.popIndex = (S.popIndex + (e.key === "ArrowDown" ? 1 : n - 1)) % n; openMentionPop(); return;
      }
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); insertMention(agent(pop.children[S.popIndex].dataset.id)); return; }
      if (e.key === "Escape") { e.preventDefault(); closeMentionPop(); return; }
    }
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("#msg-form").requestSubmit();
  });
}

init().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<p class="error" role="alert">加载失败：${esc(e.message)}</p>`); });
