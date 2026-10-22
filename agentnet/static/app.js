// Matrix UI：网络图 / Agent 管理 / 项目管理 / 对话管理。状态来自 /api/network，事件流驱动刷新。
import { renderMarkdown } from "./md.js";

const $ = (s, el = document) => el.querySelector(s);
const svgNS = "http://www.w3.org/2000/svg";
const S = { snap: null, view: "network", agentSel: null, projectSel: null, convId: null, conv: null, sel: null, running: new Map() };
const STATUS_CN = { running: "执行中", in_review: "待验收", completed: "已完成", failed: "失败", rejected: "已拒绝",
  idle: "空闲", busy: "运行中", thinking: "思考中", waiting: "等委托", pending: "待审核", approved: "已批准" };
const modelLabel = (m) => m ? `${m.provider} / ${m.model}${m.reasoning_effort ? ` · ${m.reasoning_effort}` : ""}` : "";
const PROJECT_COLORS = ["#b48cff", "#3fbf7f", "#f2a541", "#e5534b", "#4cc9f0", "#ff7eb6"];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pill = (st) => `<span class="pill status ${esc(st)}">${esc(STATUS_CN[st] || st)}</span>`;
const initial = (name) => esc([...String(name || "?").replace(/ Agent$/, "").trim()][0] || "?").toUpperCase();
const avatar = (name, cls = "") => `<span class="avatar ${cls}" aria-hidden="true">${initial(name)}</span>`;
const placeholder = (text) => `<div class="placeholder"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M5 7.5h14M5 12h9M5 16.5h6"/></svg>${text}</div>`;
const tags = (xs, cls = "") => (xs || []).map((x) => `<span class="tag ${cls}">${esc(x)}</span>`).join("") || '<span class="muted">无</span>';
const agent = (id) => S.snap?.agents.find((a) => a.id === id);
const proj = (id) => S.snap?.projects.find((g) => g.id === id);
const aname = (id) => agent(id)?.name || id || "（未知）";
const gcolor = (id) => PROJECT_COLORS[Math.max(0, S.snap.projects.findIndex((g) => g.id === id)) % PROJECT_COLORS.length];

// 错误带上 HTTP 状态码（断网、超时为 0）：调用方据此区分“确实不存在”（404）和“这次没拿到”。
// 慢网、网关（如 Caddy 连不上后端时的 502）下后者很常见，不能当成前者显示。
const API_TIMEOUT_MS = 30000;
const httpError = (message, status) => Object.assign(new Error(message), { status });

async function api(path, opts = {}) {
  if (opts.body && typeof opts.body !== "string") {
    opts = { ...opts, body: JSON.stringify(opts.body), headers: { "Content-Type": "application/json" } };
  }
  let r;
  try {
    r = await fetch(path, { ...opts, signal: AbortSignal.timeout(API_TIMEOUT_MS) });
  } catch (e) {
    throw httpError(e?.name === "TimeoutError" ? "请求超时，请检查网络" : "网络连接失败，请检查网络", 0);
  }
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && !path.startsWith("/api/auth/")) {  // 登录过期 / 退出：回到登录页
    showGate("login");
    throw httpError("请先登录", 401);
  }
  if (!r.ok) {
    const d = data.detail;
    throw httpError(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("；")
      : r.status >= 500 ? `服务暂时不可用（${r.status}），请稍后重试` : `${r.status}`, r.status);
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
  $("#auth-sub").textContent = reg ? (S.info?.has_unclaimed ? "你是第一个注册的人，会接管这里已有的 Agent、项目和对话。"
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
  const pairing = location.hash.startsWith("#pair=");  // 先记下：start() 里的路由会把它换成 #devices
  if (!S.started) { S.started = true; await start(); }
  else await reload();
  // 从连接器打开的配对链接：先让人批准设备，不拦在“创建第一个 Agent”的引导上
  if (S.me.agent_count === 0 && !S.skipOnboard && !pairing) showGate("onboard");
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

// 浅色 / 深色：初始值在 index.html 的 <head> 里定（避免闪烁）。用户点过就记住；没点过就跟着系统变。
function initTheme() {
  const btn = $("#theme-toggle"), root = document.documentElement;
  const paint = () => {
    const label = root.dataset.theme === "light" ? "切换到深色模式" : "切换到浅色模式";
    btn.setAttribute("aria-label", label); btn.title = label;
  };
  btn.addEventListener("click", () => {
    root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
    localStorage.setItem("theme", root.dataset.theme);
    paint();
  });
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", (e) => {
    if (localStorage.getItem("theme")) return;
    root.dataset.theme = e.matches ? "light" : "dark";
    paint();
  });
  paint();
}

async function start() {
  const info = S.info;
  S.defaultModel = info.default_model;
  $("#runtime").textContent = `Runtime · ${info.runtime}`;
  if (info.default_model) $("#runtime").title = `未单独配置模型的 Agent 使用：${modelLabel(info.default_model)}`;
  $("#board-agent").addEventListener("change", () => { S.boardAgent = $("#board-agent").value; renderBoard(); });
  $("#board-project").addEventListener("change", () => { S.boardProject = $("#board-project").value; renderBoard(); });
  // 走 URL 而不是直接 show()：否则切回对话页时 URL 还停在 "#network"，
  // 刷新后会被路由回网络图。
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => { location.hash = b.dataset.view; }));
  $("#agent-new").addEventListener("click", () => { S.agentSel = null; renderAgentEditor(true); renderAgents(); });
  $("#device-new").addEventListener("click", () => {
    if (S.devicePanel?.token && !confirm("令牌还没确认复制，离开后就再也看不到了。确定离开？")) return;
    S.devicePanel = "new"; renderDeviceDetail();
  });
  $("#project-new").addEventListener("click", () => { S.projectSel = null; renderProjectEditor(true); renderProjects(); });
  $("#project-quick").addEventListener("click", () => { S.projectSel = null; S.newProject = true; location.hash = "projects"; });
  $("#tab-members").addEventListener("click", () => sideTab("members"));
  initTheme();
  $("#tab-task").addEventListener("click", () => sideTab("task"));
  $("#tab-archive").addEventListener("click", () => sideTab("archive"));
  bindComposer();
  $("#msg-form").addEventListener("submit", sendMsg);
  window.addEventListener("resize", () => S.view === "network" && renderGraph());
  window.addEventListener("hashchange", route);
  await reload();
  loadModels();
  route();
  connect();
}

// 云侧预置模型目录：部署方维护的 models.json，改了即生效。active=false 时云侧照旧自由填写。
async function loadModels() {
  try {
    const m = await api("/api/models");
    const changed = JSON.stringify(m) !== JSON.stringify(S.models);
    S.models = m;
    if (changed && S.view === "agents") renderAgentsView();
  } catch { S.models = S.models || null; }
}

function route() {
  const hash = location.hash.slice(1);
  const PLAIN = ["network", "projects", "board", "agents", "devices"];
  // 参照 Raft：#g-<项目> 打开项目的群聊，#dm-<Agent> 打开和它的私聊（第一次打开时创建）
  // #pair=<配对码>：连接器 setup 打开的批准页（docs/device-runtimes.md 第 2.5 节）
  const pair = hash.match(/^pair=([A-Za-z0-9-]{4,16})$/);
  if (pair) {
    S.devicePanel = { pair: pair[1] };
    S.convId = null; S.conv = null;
    show("devices");
    return;
  }
  const m = hash.match(/^(p|dm)-(.+)$/);
  if (m) {
    const body = m[1] === "p" ? { project_id: m[2] } : { agent_id: m[2] };
    const still = () => location.hash === `#${hash}`;
    api("/api/conversations", { method: "POST", body })
      .then((c) => { if (still()) location.replace(`#${c.id}`); })
      .catch((err) => { if (still()) { alert(err.message); location.replace("#network"); } });
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
  if (view === "agents") loadModels();
  render();
}

async function reload() {
  S.snap = await api("/api/network");
  if (S.me && !agent(S.me.assistant_id)) S.me = await api("/api/me");  // 第一个 Agent 创建 / 助理被删后服务端会重新指定
  if (S.convId) await loadConv(S.convId);
  render();
}

function render() {
  if (!S.snap) return;
  const busy = S.snap.agents.filter((a) => a.status === "busy").length;
  if (!S.linkLost) $("#live").textContent = busy ? `${busy} 个 Agent 运行中` : "全部空闲";
  $("#live").classList.toggle("busy", busy > 0);
  renderConvList();
  const todo = (S.snap.approvals || []).length + (S.snap.proposals || []).filter((p) => p.status === "pending").length
    + (S.snap.invitations || []).length
    + S.snap.projects.filter(isHost).reduce((n, g) => n + (g.suggestions || []).filter((x) => x.status === "pending").length, 0);
  $("#todo-count").hidden = !todo;
  $("#todo-count").textContent = todo;
  $("#todo-count").setAttribute("aria-label", `${todo} 件待处理`);
  ({ network: renderGraph, projects: renderProjectsView, board: loadBoard, agents: renderAgentsView,
     chats: renderChats, devices: renderDevicesView })[S.view]();
}

const REQ_OPEN = (r) => r.status === "in_progress" || r.status === "in_review";

// ---------- 看板（参照 Raft task board） ----------
// 看板只有人交代的、有交付目标的事（Agent 认定后登记）；Agent 之间的委托只在任务详情里展示。
const BOARD_COLS = [
  { key: "running", title: "进行中", match: (r) => r.status === "in_progress" },
  { key: "in_review", title: "待验收", match: (r) => r.status === "in_review" },
  { key: "completed", title: "已完成", match: (r) => r.status === "completed" },
  { key: "failed", title: "已取消", match: (r) => r.status === "cancelled" },
];
const REQ_CN = { in_progress: "进行中", in_review: "待验收", completed: "已完成", cancelled: "已取消" };
const reqPillOf = (r) => reqPill(r.status);
const reqPill = (st) => `<span class="pill status ${st === "in_progress" ? "running" : st === "cancelled" ? "rejected" : esc(st)}">${REQ_CN[st] || esc(st)}</span>`;

async function loadBoard() {
  try { S.board = await api("/api/board"); } catch { return; }
  renderBoard();
}

function renderBoard() {
  const b = S.board;
  if (!b || S.view !== "board") return;
  // 筛选：接任务的 Agent
  const ids = [...new Set(b.requests.map((r) => r.agent_id))];
  const sel = $("#board-agent"), want = S.boardAgent || "";
  sel.innerHTML = `<option value="">全部 Agent</option>` + ids.map((id) => `<option value="${esc(id)}">${esc(aname(id))}</option>`).join("");
  sel.value = ids.includes(want) ? want : "";
  // 筛选：项目（none = 不属于项目）
  const psel = $("#board-project"), pwant = S.boardProject || "";
  const popts = [["", "全部项目"], ...S.snap.projects.map((pj) => [pj.id, pj.name]), ["none", "不属于项目"]];
  psel.innerHTML = popts.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
  psel.value = popts.some(([v]) => v === pwant) ? pwant : "";
  const inProject = (r) => !psel.value || (psel.value === "none" ? !r.project_id : r.project_id === psel.value);
  const reqs = b.requests.filter((r) => (!sel.value || r.agent_id === sel.value) && inProject(r));

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
    ...(b.invitations || []).map((iv) => `<li class="todo-item"><span class="avatar sm group" aria-hidden="true">#</span>
      <span><b>${esc(iv.host)}</b> 邀请你加入联合项目「${esc(iv.name)}」${iv.description ? `<span class="muted"> · ${esc(iv.description)}</span>` : ""}</span>
      <span class="actions"><button type="button" class="sm" data-inv="${esc(iv.project_id)}" data-ok="1">接受</button>
        <button type="button" class="ghost sm" data-inv="${esc(iv.project_id)}" data-ok="0">拒绝</button></span></li>`),
    ...(b.suggestions || []).map((x) => `<li class="todo-item"><span class="avatar sm group" aria-hidden="true">#</span>
      <span><b>${esc(x.by_agent ? aname(x.by_agent) : "参与者")}</b> 建议修改「${esc(x.project_name)}」的纲要</span>
      <a class="button sm" href="#p-${esc(x.project_id)}">去看看</a></li>`),
  ];
  $("#todo").hidden = !todo.length;
  const editing = $("#todo-list").contains(document.activeElement) && document.activeElement.matches("textarea");
  if (!editing) $("#todo-list").innerHTML = todo.join("");
  if (!editing) $("#todo-list").querySelectorAll("[data-inv]").forEach((btn) => btn.addEventListener("click", async () => {
    btn.parentElement.querySelectorAll("button").forEach((x) => { x.disabled = true; });
    try { await api(`/api/projects/${btn.dataset.inv}/invitation`, { method: "POST", body: { accept: btn.dataset.ok === "1" } }); }
    catch (err) { alert(err.message); }
    await loadBoard(); await reload();
  }));
  if (!editing) $("#todo-list").querySelectorAll("[data-ap]").forEach((btn) => btn.addEventListener("click", async () => {
    btn.closest(".actions").querySelectorAll("button").forEach((x) => { x.disabled = true; });
    try { await api(`/api/approvals/${btn.dataset.ap}`, { method: "POST", body: { allow: btn.dataset.allow === "1" } }); }
    catch (err) { alert(err.message); }
    await loadBoard(); await reload();
  }));

  const card = (r) => `<li><button type="button" class="task-card ${S.reqSel === r.id ? "selected" : ""}" data-id="${esc(r.id)}">
      <span class="route">${avatar(r.agent_name, "xs")}<span class="name">${esc(r.agent_name)}</span></span>
      <span class="mission">${esc(r.title)}</span>
      <span class="meta">${r.project_name ? `<span class="proj">${esc(r.project_name)}</span>` : ""}
        <span>${esc(convWhere(r))}</span><span>${time(r.created_at)}</span>
        ${r.requester_id && r.requester_id !== S.me?.id ? `<span>${esc(r.requester)} 下发</span>` : ""}
        ${r.delegations ? `<span>委托 ${r.delegations}</span>` : ""}${r.revisions ? `<span>退回 ${r.revisions}</span>` : ""}</span>
    </button></li>`;
  $("#board-cols").innerHTML = BOARD_COLS.map((col) => {
    const xs = reqs.filter(col.match);
    return `<section class="board-col ${col.key}" aria-label="${col.title}">
      <h3>${col.title}<span class="count">${xs.length}</span></h3>
      <ul>${xs.map(card).join("") || '<li class="empty">无</li>'}</ul></section>`;
  }).join("");
  $("#board-cols").querySelectorAll(".task-card").forEach((btn) => btn.addEventListener("click", () => {
    S.reqSel = btn.dataset.id; S.taskSel = null; renderBoard();
  }));

  const busy = Object.entries(b.ledger || {});
  $("#busy").hidden = !busy.length;
  $("#busy-list").innerHTML = busy.map(([aid, items]) => `<li>${avatar(aname(aid), "sm")}<b>${esc(aname(aid))}</b>
    <span class="muted">同时在做 ${items.length} 件：</span>${items.map((it) => `<span class="tag">${esc(it.kind === "task" ? `委托：${it.what}` : it.what)}${it.user ? ` · ${esc(it.user)}` : ""}</span>`).join("")}</li>`).join("");
  if (S.reqSel && reqs.some((r) => r.id === S.reqSel)) renderRequest($("#board-detail"));
  else $("#board-detail").innerHTML = placeholder("点一张任务卡片，查看交付、验收，以及 Agent 为它发起的委托。");
}

// 任务出自哪里：私聊、项目会话或项目群聊
const convWhere = (r) => r.conversation_kind === "dm" ? "私聊" : r.conversation_kind === "session"
  ? `会话「${r.conversation_title}」` : `群聊「${r.conversation_title}」`;

// 任务的项目归属：项目里的跟随项目；私聊里的由下发的人手动归类
function reqProjectField(r, mine) {
  const label = r.project_name ? esc(r.project_name) : "";
  const hosted = S.snap.projects.filter(isHost);
  if (r.conversation_kind !== "dm" || !mine || !hosted.length)
    return label ? `<p class="muted">项目：${label}${r.conversation_kind === "dm" ? "" : "（跟随项目）"}</p>` : "";
  const opts = [["", "不属于项目"], ...hosted.map((pj) => [pj.id, pj.name])];
  return `<div class="field"><label for="req-proj">项目</label><select id="req-proj">${opts.map(([v, t]) =>
    `<option value="${esc(v)}" ${v === (r.project_id || "") ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></div>`;
}

// 用户任务详情：交代了什么、交付了什么、验收；为它发起的委托点开看（复用委托详情）
async function renderRequest(box = $("#board-detail")) {
  const id = S.reqSel;
  if (!id) return;
  if (box.contains(document.activeElement) && document.activeElement.matches("textarea")) return;  // 正在写退回意见
  const r = await api(`/api/requests/${id}`).catch(() => null);
  if (!r || S.reqSel !== id) return;
  const mine = !r.requester_id || r.requester_id === S.me?.id;
  box.innerHTML = `<h2 class="panel-title">${esc(r.title)} ${reqPillOf(r)}</h2>
    <p class="muted">${esc(r.agent_name)} 接下 · ${esc(convWhere(r))} · ${time(r.created_at)}
      ${r.requester_id && !mine ? ` · ${esc(r.requester)} 下发` : ""}</p>
    <dl><dt>交代的原话</dt><dd>${esc(r.message)}</dd><dt>交付要求</dt><dd>${esc(r.expected_outcome)}</dd>
      ${r.summary ? `<dt>交付说明</dt><dd class="md">${renderMarkdown(r.summary)}</dd>` : ""}
      ${r.revisions ? `<dt>返工</dt><dd>退回 ${r.revisions} 次${r.feedback ? ` · 最近意见：${esc(r.feedback)}` : ""}</dd>` : ""}</dl>
    ${r.status === "in_review" && mine ? `<form id="req-review" class="stack">
      <div class="actions"><button type="button" id="req-accept">验收通过</button></div>
      <label for="req-feedback" class="sr-only">要改什么</label>
      <textarea id="req-feedback" rows="3" placeholder="不满意：写明要改什么，${esc(r.agent_name)} 会在原会话里修改后重新交付"></textarea>
      <div class="actions"><button type="submit" class="ghost">退回修改</button></div></form>`
      : r.status === "in_review" ? `<p class="hint">等 ${esc(r.requester)} 验收。</p>` : ""}
    ${(r.status === "in_progress" || r.status === "in_review") && mine ? '<p><button type="button" class="link" id="req-cancel">取消这个任务</button></p>' : ""}
    ${reqProjectField(r, mine)}
    <div class="actions"><a class="button ghost sm" href="#${esc(r.conversation_id)}">打开对话</a></div>
    ${r.tasks.length ? `<h3>为它发起的委托 · ${r.tasks.length}</h3><div class="delegs">${r.tasks.map((t) => delegBtn(t, !!t.parent_task_id)).join("")}</div>
      <div id="req-task"></div>` : ""}
    <p class="error" role="alert"></p>`;
  const err = (e) => { $(".error", box).textContent = e.message; };
  const review = async (accept) => {
    try { await api(`/api/requests/${id}/review`, { method: "POST", body: { accept, feedback: accept ? "" : $("#req-feedback").value } }); await loadBoard(); }
    catch (e) { err(e); }
  };
  $("#req-proj", box)?.addEventListener("change", async (e) => {
    try { await api(`/api/requests/${id}/project`, { method: "PUT", body: { project_id: e.target.value || null } }); await loadBoard(); }
    catch (e2) { err(e2); }
  });
  $("#req-accept", box)?.addEventListener("click", () => review(true));
  $("#req-review", box)?.addEventListener("submit", (e) => { e.preventDefault(); review(false); });
  $("#req-cancel", box)?.addEventListener("click", async () => {
    if (!confirm("取消这个任务？Agent 已经在做的委托不会被打断。")) return;
    try { await api(`/api/requests/${id}/cancel`, { method: "POST", body: {} }); await loadBoard(); } catch (e) { err(e); }
  });
  box.querySelectorAll(".deleg").forEach((b) => b.addEventListener("click", () => {
    S.taskSel = b.dataset.id;
    box.querySelectorAll(".deleg").forEach((x) => x.classList.toggle("selected", x === b));
    renderTask($("#req-task"));
  }));
  if (S.taskSel && r.tasks.some((t) => t.id === S.taskSel)) renderTask($("#req-task"));
}

// 刷新节流：结构变化（Agent、项目、对话、委托状态）重拉整个快照；
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
  S.seq = ev.seq;  // 事件流按序到达：最后收到的就是续接点（服务重启后序号会从头计）
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
  if (ev.type === "request.updated") {
    if (S.view === "board") loadBoard();
    if (S.view === "chats" && d.conversation_id === S.convId) scheduleConv();
    return;
  }
  if (ev.type === "tool.call" || ev.type === "runtime.invoke") return;  // 已由 run.progress 覆盖
  if (ev.type === "project.invited" && S.view === "board") loadBoard();
  if (ev.type === "project.left" && d.user_id === S.me?.id && S.conv?.project_id === d.project_id) {
    S.convId = null; S.conv = null; location.hash = "projects";  // 自己退出 / 被移出：离开这个项目
  }
  schedule();
  if (S.taskSel && ev.type === "task.updated" && d.task.id === S.taskSel) scheduleTask();
}

function connect() {
  clearTimeout(S.esRetry);
  S.es?.close();
  const es = S.es = new EventSource(`/api/events?since=${S.seq ?? S.snap.seq}`);
  es.onmessage = (m) => onEvent(JSON.parse(m.data));
  // 断开期间可能漏掉状态，重连后整体对齐一次（会连同当前对话一起重拉）。
  es.onopen = () => {
    S.esRetryN = 0;
    const lost = S.linkLost;
    S.linkLost = false;
    setLink(true);
    if (lost) schedule();
  };
  // 网络错误时浏览器会自己重连（带 Last-Event-ID）；但重连收到非 200（服务重启中、网关 502）时
  // EventSource 直接关闭、不再重试，页面从此不再刷新——这种情况由这里按退避重建连接。
  es.onerror = () => {
    setLink(false);
    if (es.readyState === EventSource.CLOSED && S.es === es) {
      const wait = Math.min(30000, 1000 * 2 ** Math.min(S.esRetryN = (S.esRetryN || 0) + 1, 5));
      S.esRetry = setTimeout(() => { if (S.es === es) connect(); }, wait);
    }
  };
}

// 网络恢复（换网、从休眠唤醒）时不等退避：立刻重连并对齐一次
window.addEventListener("online", () => {
  if (!S.snap) return;
  if (!S.es || S.es.readyState === EventSource.CLOSED) connect();
  schedule();
});

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
  [["arrow", "--edge"], ["arrow-active", "--running"], ["arrow-sel", "--select"]].forEach(([id, c]) => {
    const m = el("marker", { id, viewBox: "0 0 10 10", refX: 10, refY: 5, markerWidth: 6, markerHeight: 6,
      markerUnits: "userSpaceOnUse", orient: "auto-start-reverse" }, defs);
    el("path", { d: "M0,0 L10,5 L0,10 z", style: `fill: var(${c})` }, m);
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
      "aria-label": a.status ? `${a.name}，${STATUS_CN[a.status] || a.status}` : a.name }, nodes);
    el("circle", { r: 34, class: "halo" }, g);
    S.snap.projects.filter((gr) => gr.member_ids.includes(a.id)).forEach((gr, i) =>
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
      <dt>模型</dt><dd>${modelDd(x)}</dd>
      <dt>可对话</dt><dd>${x.chat_enabled ? "是" : "否"}</dd>
      ${x.created_by.startsWith("proposal:") ? `<dt>来源</dt><dd>由 Agent 提议、人批准创建</dd>` : ""}
      <dt>项目</dt><dd>${tags(S.snap.projects.filter((g) => g.member_ids.includes(a)).map((g) => g.name))}</dd>
      <dt>委托出去</dt><dd>${relList(out, "to")}</dd>
      <dt>被委托</dt><dd>${relList(inn, "from")}</dd></dl>`;
  } else if (kind === "rel") {
    const r = S.snap.relations.find((r) => r.from === a && r.to === b);
    if (!r) { box.innerHTML = ""; return; }
    box.innerHTML = `<h2 class="panel-title">${esc(aname(a))} <span class="muted">→</span> ${esc(aname(b))}</h2>
      <p class="muted">长期委托关系</p>
      <div class="stats"><div class="stat"><b>${r.count}</b><span>委托次数</span></div><div class="stat"><b>${r.completed}</b><span>已完成</span></div>
      <div class="stat"><b>${r.failed}</b><span>失败 / 拒绝</span></div><div class="stat"><b>${r.running}</b><span>进行中</span></div></div>
      <dl><dt>项目</dt><dd>${tags(r.projects.map((g) => proj(g)?.name || "（已删除）"))}</dd>
      <dt>最近</dt><dd>${new Date(r.last_at * 1000).toLocaleString()}</dd></dl>`;
  } else {
    box.innerHTML = `<h2 class="panel-title">概览</h2><p class="muted">点击节点或连线查看详情。</p>
      <div class="stats"><div class="stat"><b>${S.snap.agents.length}</b><span>Agent</span></div><div class="stat"><b>${S.snap.projects.length}</b><span>项目</span></div>
      <div class="stat"><b>${S.snap.conversations.length}</b><span>对话</span></div><div class="stat"><b>${S.snap.relations.length}</b><span>委托关系</span></div>
      <div class="stat"><b>${S.snap.active_tasks.length}</b><span>进行中委托</span></div>
      <div class="stat"><b>${(S.snap.proposals || []).filter((p) => p.status === "pending").length}</b><span>待审核提议</span></div></div>
      <p class="muted">连线粗细代表历史委托次数，流动的虚线是正在进行的委托，节点外圈颜色对应所属项目。</p>`;
  }
}

// ================= Agent 管理 =================
function renderAgentsView() { renderAgents(); renderAgentEditor(); }

// 卡片上的“对话”：链接到 #g-<项目> / #dm-<Agent>，路由负责打开（私聊第一次打开时创建）
const chatBtn = (hash, label) => `<a class="button sm chat-btn" href="#${esc(hash)}" aria-label="${esc(label)}">
  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M2.5 4.5a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2H7l-3 2.5v-2.5h0a1.5 1.5 0 0 1-1.5-1.5z"/></svg>对话</a>`;

function renderAgents() {
  // 卡片主体选中编辑；底部“对话”按钮直接打开私聊。按钮不能嵌套，所以“对话”是卡片里的另一个按钮。
  // 自己的在前；别人的只出现在和你同在的联合项目里，只能看名片
  const list = [...S.snap.agents].sort((a, b) => (b.mine ? 1 : 0) - (a.mine ? 1 : 0));
  $("#agent-list").innerHTML = list.map((a) => `
    <li class="${S.agentSel === a.id ? "selected" : ""}"><div class="card">
      <button type="button" class="card-main" data-id="${esc(a.id)}" aria-label="编辑 ${esc(a.name)}">
        <div class="card-head">${avatar(a.name)}<span class="name">${esc(a.name)}</span>${pill(a.status)}</div>
        <p class="card-desc">${esc(a.description)}</p>
        <div class="card-meta">${tags(a.authority, "ok")}${modelTag(a)}${a.device_runtime
          ? `<span class="tag model" title="端侧运行时">${a.device_online ? esc(a.harness ? harnessLabel(a.harness) : "本机") : "设备离线"}</span>` : ""}</div>
      </button>
      <div class="card-foot"><span>${a.mine ? "" : `<span class="vis-tag other">${esc(a.owner_email || "他人")} 的</span>`}</span>
        ${!a.mine ? '<span class="muted">联合项目里协作</span>' : a.chat_enabled ? chatBtn(`dm-${a.id}`, `和 ${a.name} 私聊`) : '<span class="muted">不开放私聊</span>'}</div>
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

// 实际用的模型由服务器算（model_info）：预置 / 自己填的 / 目录默认 / 运行时默认，以及选的是否已下架
const effectiveModel = (a) => a.model_info?.model || (a.device_runtime ? null : S.defaultModel);
const modelSourceCN = { preset: "预置模型", custom: "单独配置", default: "默认模型", runtime: "运行时默认" };
const modelTag = (a) => {
  const i = a.model_info, m = effectiveModel(a);
  if (!m) return "";
  const dflt = !i || i.source === "default" || i.source === "runtime";
  const title = i?.unavailable ? "选的模型已下架，改用默认模型" : modelSourceCN[i?.source || "runtime"];
  return `<span class="tag model${i?.unavailable ? " warn" : ""}" title="${esc(title)}">${esc(i?.label || m.model)}${i?.unavailable ? " · 原选择已下架" : dflt ? " · 默认" : ""}</span>`;
};
function modelDd(a) {
  const i = a.model_info, m = effectiveModel(a);
  if (!m) return a.device_runtime ? 'harness 默认 <span class="muted">（端侧）</span>' : "运行时默认";
  const text = i?.label ? `${i.label} · ${modelLabel(m)}` : modelLabel(m);
  const note = i?.unavailable ? '<span class="hint warn">选的模型已下架，现在用默认模型</span>'
    : i?.source === "default" || i?.source === "runtime" || !i ? ' <span class="muted">（默认）</span>' : "";
  return `${esc(text)}${note}`;
}
const catalogOn = () => !!S.models?.active;
// 预置模型下拉框的选项；sel 是当前选中的 id（"" = 默认）
function presetOptions(sel) {
  const d = S.models.models.find((m) => m.id === S.models.default);
  const dflt = d ? `默认（${d.label}）` : S.defaultModel ? `默认（${modelLabel(S.defaultModel)}）` : "运行时默认";
  return `<option value="">${esc(dflt)}</option>` + S.models.models.map((m) =>
    `<option value="${esc(m.id)}" ${m.id === sel ? "selected" : ""}>${esc(m.label)} · ${esc(m.model)}${m.reasoning_effort ? ` · ${esc(m.reasoning_effort)}` : ""}</option>`).join("");
}

function renderAgentReadonly(box, a) {
  box.innerHTML = `<h2 class="panel-title">${avatar(a.name)} ${esc(a.name)}</h2>
    <p class="muted">${esc(a.owner_email || "")} 的 Agent</p>
    <dl><dt>描述</dt><dd>${esc(a.description)}</dd><dt>能力</dt><dd>${tags(a.capabilities)}</dd>
    <dt>自身权限</dt><dd>${tags(a.authority, "ok")}</dd>
    <dt>所在的联合项目</dt><dd>${tags(S.snap.projects.filter((g) => g.member_ids.includes(a.id)).map((g) => g.name))}</dd></dl>
    <p class="hint">它只为它的所有者工作：你可以在联合项目里 @ 它、委托它，它以所有者的名义、用所有者的资源执行。不能私聊，也不能拉进你自己的项目。</p>`;
}

function renderAgentEditor(isNew = false) {
  if (S.devices === undefined) {  // 第一次打开时先取设备列表（用于“运行位置”）
    api("/api/devices").then((d) => { S.devices = d; if (S.view === "agents") renderAgentEditor(isNew); })
      .catch(() => { S.devices = []; });
  }
  const box = $("#agent-editor"), a = agent(S.agentSel);
  if (!a && !isNew) { box.innerHTML = placeholder("选择一个 Agent 编辑，或新建。"); return; }
  if (a && !a.mine) { renderAgentReadonly(box, a); return; }
  const x = a || { name: "", description: "", capabilities: [], authority: ["web.research"], chat_enabled: true };
  box.innerHTML = `<h2 class="panel-title">${a ? esc(a.name) : "新建 Agent"}</h2>
    <p class="muted">${a ? "修改后点击保存生效。" : "定义它能做什么，以及最多能被授予哪些权限。"}</p>
    <form class="editor" id="agent-form">
      <div class="field"><label for="af-name">名称</label><input id="af-name" name="name" required minlength="2" value="${esc(x.name)}"></div>
      <div class="field"><label for="af-desc">描述</label>
        <textarea id="af-desc" name="description" rows="3" required minlength="2" maxlength="2000">${esc(x.description)}</textarea>
        <p class="hint">会出现在 list_members 里，供其他 Agent 判断能不能委托。</p></div>
      <div class="field"><label for="af-caps">能力</label><input id="af-caps" name="capabilities" value="${esc(x.capabilities.join(", "))}">
        <p class="hint">逗号分隔。</p></div>
      <fieldset class="checks"><legend>自身权限 · 委托只能在此之内再收窄</legend>${authorityBoxes(x.authority)}</fieldset>
      ${a && a.runtime_capabilities ? `<div class="field"><label>这个运行时的实际能力</label>
        <div class="card-meta">${Object.entries(a.runtime_capabilities.enforcement).map(([k, v]) =>
          `<span class="tag ${v === "enforced" ? "ok" : ""}" title="${esc(v)}">${esc(k)}${v === "enforced" ? "" : ` · ${esc(v === "advisory" ? "仅约定" : "不可用")}`}</span>`).join("")}</div>
        <p class="hint">只有“可强制”的权限会进入有效权限；其余会在委托时自动收窄，并写明原因。</p></div>` : ""}
      <fieldset class="model"><legend>运行位置</legend>
        <div class="field"><label for="af-device">运行在</label>
          <select id="af-device" name="device_runtime">
            <option value="">平台云侧（内置 dsh）</option>
            ${placementOptions(x)}
          </select>
          <p class="hint">${S.devices?.length ? "端侧 Agent 在你这台电脑上用选定的 harness 跑，用你本机的登录与文件；它读不到服务器上的数据。" : "还没有设备。先到「设备」页连接一台，才可以选择端侧运行。"}</p>
        </div>
      </fieldset>
      <fieldset class="model"><legend>模型</legend>
        ${catalogOn() ? `<div class="field" id="af-preset-box"><label for="af-preset">预置模型</label>
          <select id="af-preset" name="model_preset">${presetOptions(x.model_info?.source === "preset" ? x.model_info.preset : "")}</select>
          <p class="hint">${x.model_info?.unavailable ? '<span class="warn">原来选的模型已下架，现在用默认模型；重新选一个后保存。</span> ' : ""}平台云侧的 Agent 只能用部署方预置的模型。</p></div>` : ""}
        <div class="model-fields" id="af-free">
          <div class="field"><label for="af-provider">Provider</label><input id="af-provider" name="provider" value="${esc(x.model?.provider || "")}" placeholder="${esc(S.defaultModel?.provider || "")}"></div>
          <div class="field"><label for="af-model">Model</label><input id="af-model" name="model" value="${esc(x.model?.model || "")}" placeholder="${esc(S.defaultModel?.model || "")}"></div>
          <div class="field"><label for="af-effort">推理强度</label><input id="af-effort" name="effort" value="${esc(x.model?.reasoning_effort || "")}" placeholder="默认"></div>
        </div>
        <p class="hint" id="af-free-hint">留空则使用${catalogOn() ? " harness 自己的默认模型。端侧用的是你本机 harness 里的模型" : `运行时默认模型${S.defaultModel ? `（${esc(modelLabel(S.defaultModel))}）` : ""}。provider 必须是运行时里已配置的`}。</p>
      </fieldset>
      <label class="switch"><input type="checkbox" name="chat_enabled" ${x.chat_enabled ? "checked" : ""}> 允许你和它私聊（群聊里随时可以 @ 它）</label>
      ${a ? (S.me?.assistant_id === a.id
        ? '<p class="hint"><span class="tag ok">你的助理</span> 你加入项目时它跟着加入，项目会话里由它陪你聊。</p>'
        : '<p class="hint"><button type="button" class="link" id="af-assistant">设为我的助理</button> · 你加入项目时助理跟着加入，项目会话里由它陪你聊。</p>') : ""}
      <p class="hint">它只为你工作。要和别人协作，把它带进联合项目：别人可以在那里 @ 它，但它仍然以你的名义、用你的资源运行，工作区外的操作由你审批。</p>
      <p class="error" role="alert"></p>
      <div class="actions"><button type="submit">${a ? "保存" : "创建"}</button>
        ${a ? `${a.chat_enabled ? '<button type="button" class="ghost" id="af-chat">私聊</button>' : ""}<button type="button" class="danger" id="af-del">删除</button>` : ""}</div>
    </form>`;
  const f = $("#agent-form");
  // 启用预置模型时：云侧只能选预置模型，端侧（用户自己的 harness）仍然自由填写
  const usePreset = () => !!f.model_preset && !f.device_runtime.value;
  const syncModelFields = () => {
    if (!f.model_preset) return;
    $("#af-preset-box").hidden = !usePreset();
    $("#af-free").hidden = $("#af-free-hint").hidden = usePreset();
  };
  f.device_runtime.addEventListener("change", syncModelFields);
  syncModelFields();
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      name: f.name.value.trim(), description: f.description.value.trim(),
      capabilities: f.capabilities.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
      authority: [...f.querySelectorAll('[name="authority"]:checked')].map((c) => c.value),
      chat_enabled: f.chat_enabled.checked,
      ...placementOf(f.device_runtime.value),
    };
    if (usePreset()) {
      Object.assign(body, { model: null, model_preset: f.model_preset.value || null });
    } else if (f.device_runtime.value || S.models) {
      // 端侧总是自由填写；云侧要等目录取回来才知道该怎么填（没取到时不改模型，免得把选好的预置模型冲掉）
      if ((f.provider.value.trim() !== "") !== (f.model.value.trim() !== "")) {
        $(".error", f).textContent = "Provider 与 Model 要么都填，要么都留空。"; return;
      }
      Object.assign(body, { model: readModel(f), model_preset: null });
    }
    try {
      const r = await api(a ? `/api/agents/${a.id}` : "/api/agents", { method: a ? "PATCH" : "POST", body });
      S.agentSel = r.id; await reload();
    } catch (err) { $(".error", f).textContent = err.message; }
  });
  $("#af-del")?.addEventListener("click", async () => {
    if (!confirm(`删除 ${a.name}？它会从所有项目中移除。`)) return;
    try { await api(`/api/agents/${a.id}`, { method: "DELETE" }); S.agentSel = null; await reload(); }
    catch (err) { $(".error", f).textContent = err.message; }
  });
  $("#af-chat")?.addEventListener("click", () => { location.hash = `dm-${a.id}`; });
  $("#af-assistant")?.addEventListener("click", async () => {
    try { S.me = { ...S.me, ...await api("/api/me/assistant", { method: "PUT", body: { agent_id: a.id } }) }; await reload(); }
    catch (err) { $(".error", f).textContent = err.message; }
  });
}

// ================= 项目管理 =================
function renderProjectsView() {
  renderProjects();
  renderProjectEditor(S.newProject);
  if (S.newProject) { S.newProject = false; $("#gf-name")?.focus(); }
}

const KIND_CN = { internal: "内部", joint: "联合" };
const kindTag = (g) => `<span class="vis-tag ${g.kind === "joint" ? "shared" : "private"}">${KIND_CN[g.kind] || ""}</span>`;
const isHost = (g) => !!g && (!g.owner_id || g.owner_id === S.me?.id);

function renderProjects() {
  $("#project-list").innerHTML = S.snap.projects.map((g) => `
    <li class="${S.projectSel === g.id ? "selected" : ""}"><div class="card">
      <button type="button" class="card-main" data-id="${esc(g.id)}" aria-label="${isHost(g) ? "编辑" : "查看"} ${esc(g.name)}">
        <div class="card-head"><span class="swatch" style="background:${gcolor(g.id)}"></span><span class="name">${esc(g.name)}</span>${kindTag(g)}<span class="pill">${g.member_ids.length} 名成员</span></div>
        <p class="card-desc">${esc(g.description || "没有描述")}</p>
        <div class="card-meta">${tags(g.member_ids.map(aname))}</div>
      </button>
      <div class="card-foot"><span>${g.kind === "joint" ? `<span class="muted">${g.participants.length + 1} 人参与</span>` : ""}</span>${chatBtn(`p-${g.id}`, `进入 ${g.name} 的群聊`)}</div>
    </div></li>`).join("") || '<li class="empty">还没有项目。</li>';
  $("#project-list").querySelectorAll(".card-main").forEach((b) =>
    b.addEventListener("click", () => { S.projectSel = b.dataset.id; renderProjects(); renderProjectEditor(); }));
}

function renderProjectEditor(isNew = false) {
  const box = $("#project-editor"), g = proj(S.projectSel);
  if (!g && !isNew) { box.innerHTML = placeholder("选择一个项目编辑成员，或新建。"); return; }
  if (g && !isHost(g)) { renderProjectGuest(box, g); return; }
  const x = g || { name: "", description: "", member_ids: [], kind: "internal" };
  // 只能加入自己的 Agent；联合项目里别人的成员由他们自己加减（主持人可以移出）
  const mine = S.snap.agents.filter((a) => a.mine);
  const theirs = (x.member_ids || []).filter((id) => !agent(id)?.mine);
  box.innerHTML = `<h2 class="panel-title">${g ? `<span class="swatch" style="background:${gcolor(g.id)}"></span>${esc(g.name)} ${kindTag(g)}` : "新建项目"}</h2>
    <p class="muted">委托只能在项目成员之间进行。</p>
    <form class="editor" id="project-form">
      ${g ? "" : `<fieldset class="seg" aria-label="项目类型">
        <label><input type="radio" name="kind" value="internal" checked> 内部项目</label>
        <label><input type="radio" name="kind" value="joint"> 联合项目</label></fieldset>
        <p class="hint">内部项目只有你和你的 Agent。联合项目可以邀请别人，各自带上自己的 Agent；群聊和档案对所有参与者可见，委托只给两端的所有者看。建好后不能转换类型。</p>`}
      <div class="field"><label for="gf-name">名称</label><input id="gf-name" name="name" required value="${esc(x.name)}"></div>
      <div class="field"><label for="gf-desc">描述</label><textarea id="gf-desc" name="description" rows="2">${esc(x.description)}</textarea></div>
      <fieldset class="checks"><legend>你的 Agent</legend>
        ${mine.map((a) => `<label><input type="checkbox" name="member" value="${esc(a.id)}" ${x.member_ids.includes(a.id) ? "checked" : ""}>
          <span class="opt">${esc(a.name)}<small>${esc(a.authority.join(" · ") || "无权限")}</small></span></label>`).join("") || '<p class="hint">你还没有 Agent。</p>'}
      </fieldset>
      ${theirs.length ? `<p class="hint">另有 ${theirs.length} 个别人的 Agent：${theirs.map((id) => esc(aname(id))).join("、")}（在群聊侧栏里移出）。</p>` : ""}
      <p class="error" role="alert"></p>
      <div class="actions"><button type="submit">${g ? "保存" : "创建"}</button>
        ${g ? '<button type="button" class="ghost" id="gf-chat">进入群聊</button><button type="button" class="danger" id="gf-del">删除项目</button>' : ""}</div>
    </form>
    ${g?.kind === "joint" ? '<div id="gf-people"></div>' : ""}`;
  const f = $("#project-form");
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { name: f.name.value.trim(), description: f.description.value.trim(),
      member_ids: [...theirs, ...[...f.querySelectorAll('[name="member"]:checked')].map((c) => c.value)] };
    if (!g) body.kind = f.querySelector('[name="kind"]:checked').value;
    try {
      const r = await api(g ? `/api/projects/${g.id}` : "/api/projects", { method: g ? "PATCH" : "POST", body });
      S.projectSel = r.id; await reload();
    } catch (err) { $(".error", f).textContent = err.message; }
  });
  $("#gf-chat")?.addEventListener("click", () => { location.hash = `p-${g.id}`; });
  $("#gf-del")?.addEventListener("click", async () => {
    if (!confirm(`删除项目 ${g.name}？群聊归档为只读（只有你看得到），不再能委托。`)) return;
    try { await api(`/api/projects/${g.id}`, { method: "DELETE" }); S.projectSel = null; await reload(); }
    catch (err) { $(".error", f).textContent = err.message; }
  });
  if (g?.kind === "joint") renderPeople($("#gf-people"), g);
}

// 联合项目的参与者：主持人邀请、撤回、移出
async function renderPeople(box, g) {
  const d = await api(`/api/projects/${g.id}`).catch(() => null);
  if (!d || !box.isConnected) return;
  const emails = d.participant_emails || {};
  box.innerHTML = `<h3>参与者</h3>
    <ul class="member-list">
      <li>${avatar(d.host, "sm")}<span class="m-body"><span class="n">${esc(d.host)}</span><span class="s">主持人</span></span></li>
      ${d.participants.map((u) => `<li>${avatar(emails[u], "sm")}<span class="m-body"><span class="n">${esc(emails[u])}</span><span class="s">参与者</span></span>
        <button type="button" class="ghost sm" data-rm="${esc(u)}">移出</button></li>`).join("")}
      ${Object.entries(d.invited_emails || {}).map(([u, e]) => `<li>${avatar(e, "sm")}<span class="m-body"><span class="n">${esc(e)}</span><span class="s">已邀请，等对方接受</span></span>
        <button type="button" class="ghost sm" data-rm="${esc(u)}">撤回</button></li>`).join("")}
    </ul>
    <form class="add-member" id="invite-form" aria-label="邀请参与者">
      <label for="invite-email" class="sr-only">对方的邮箱</label>
      <input id="invite-email" type="email" required placeholder="对方的邮箱">
      <button type="submit">邀请</button></form>
    <p class="error" role="alert"></p>
    <p class="hint">参与者退出或被移出时，他的 Agent 一并移出，之后看不到项目与群聊；已经接下的委托照常完成。</p>`;
  const err = (e) => { $(".error", box).textContent = e.message; };
  $("#invite-form", box).addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await api(`/api/projects/${g.id}/invitations`, { method: "POST", body: { email: $("#invite-email").value } }); await reload(); }
    catch (e2) { err(e2); }
  });
  box.querySelectorAll("[data-rm]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("确定？他的 Agent 会一并移出。")) return;
    try { await api(`/api/projects/${g.id}/participants/${b.dataset.rm}`, { method: "DELETE" }); await reload(); }
    catch (e2) { err(e2); }
  }));
}

// 参与者看联合项目：加减自己的 Agent，退出
function renderProjectGuest(box, g) {
  const mine = S.snap.agents.filter((a) => a.mine);
  box.innerHTML = `<h2 class="panel-title"><span class="swatch" style="background:${gcolor(g.id)}"></span>${esc(g.name)} ${kindTag(g)}</h2>
    <p class="muted">${esc(g.description || "")}</p>
    <form class="editor" id="guest-form">
      <fieldset class="checks"><legend>你带进来的 Agent</legend>
        ${mine.map((a) => `<label><input type="checkbox" name="member" value="${esc(a.id)}" ${g.member_ids.includes(a.id) ? "checked" : ""}>
          <span class="opt">${esc(a.name)}<small>${esc(a.authority.join(" · ") || "无权限")}</small></span></label>`).join("") || '<p class="hint">你还没有 Agent。</p>'}
      </fieldset>
      <p class="hint">它们只为你工作：别的参与者可以 @、委托它们，但它们以你的名义、用你的资源运行，工作区外的操作由你审批。私有目录默认不挂载，可以在群聊侧栏里按 Agent 打开。</p>
      <p class="error" role="alert"></p>
      <div class="actions"><button type="submit">保存</button><button type="button" class="ghost" id="gg-chat">进入群聊</button>
        <button type="button" class="danger" id="gg-leave">退出项目</button></div>
    </form>`;
  const f = $("#guest-form");
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const want = [...f.querySelectorAll('[name="member"]:checked')].map((c) => c.value);
    const others = g.member_ids.filter((id) => !agent(id)?.mine);
    try { await api(`/api/projects/${g.id}`, { method: "PATCH", body: { member_ids: [...others, ...want] } }); await reload(); }
    catch (err) { $(".error", f).textContent = err.message; }
  });
  $("#gg-chat").addEventListener("click", () => { location.hash = `p-${g.id}`; });
  $("#gg-leave").addEventListener("click", async () => {
    if (!confirm(`退出 ${g.name}？你的 Agent 会一并移出，之后看不到这个项目和群聊。`)) return;
    try { await api(`/api/projects/${g.id}/leave`, { method: "POST", body: {} }); S.projectSel = null; location.hash = "projects"; await reload(); }
    catch (err) { $(".error", f).textContent = err.message; }
  });
}

// ================= 对话 =================
const plain = (s) => String(s || "").replace(/[#*_`>|~]+/g, "").replace(/\s+/g, " ").trim();

function renderConvList() {
  // 参照 Raft 的侧栏：群聊（每个项目一个）和私聊（每个可私聊的 Agent 一个），点进去就能聊。
  const convs = S.snap.conversations;
  const live = convs.filter((c) => !c.archived);
  const pending = new Set((S.snap.proposals || []).filter((p) => p.status === "pending").map((p) => p.conversation_id));
  const dot = (c) => !c ? "" : c.status === "thinking" ? '<span class="dot-live" title="运行中"></span>'
    : c.status === "waiting" ? '<span class="dot-live waiting" title="等委托回报"></span>'
    : c.blocked ? '<span class="dot-live blocked" title="有成员上一轮中断"></span>' : "";
  const item = (href, sel, icon, name, sub, c, cls = "") => `<li class="${sel ? "selected" : ""} ${cls}"><a href="#${esc(href)}"${sel ? ' aria-current="page"' : ""}>
      ${icon}<span class="c-body"><span class="t"><span class="n">${esc(name)}</span>${dot(c)}
        ${c && pending.has(c.id) ? '<span class="pill pending" title="有待审核的 Agent 提议">待审核</span>' : ""}</span>
        <span class="s">${esc(sub)}</span></span></a></li>`;
  const opened = (c) => S.view === "chats" && S.convId === c?.id;

  $("#project-chat-list").innerHTML = S.snap.projects.map((g) => {
    const c = live.find((x) => x.kind === "project" && x.project_id === g.id);
    const icon = `<span class="avatar sm group" aria-hidden="true" style="background:${gcolor(g.id)}">#</span>`;
    // 自己在这个项目里的会话挂在项目下面（只有自己看得到）
    const mine = live.filter((x) => x.kind === "session" && x.project_id === g.id).sort((a, b) => b.updated_at - a.updated_at);
    return item(c ? c.id : `p-${g.id}`, opened(c), icon, g.name,
      plain(c?.last_message) || `${g.member_ids.length} 名成员`, c)
      + mine.map((x) => item(x.id, opened(x), '<span class="avatar sm session" aria-hidden="true">›</span>', x.title,
        plain(x.last_message) || "还没有消息", x, "nested")).join("");
  }).join("") || '<li class="empty">还没有项目</li>';

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
  // 新对话的数据到之前先显示“加载中”：否则慢网下屏幕上还是上一个对话，@ 列表也是它的成员
  if (S.conv?.id !== id) { S.conv = { id, loading: true }; render(); }
  await loadConv(id);
  if (S.convId === id) render();
}

async function refreshConv() {
  const id = S.convId;
  if (!id || S.view !== "chats") return;
  await loadConv(id);
  if (S.convId === id) renderThread();
}

// 对话详情的唯一入口（打开、事件刷新、整体重拉、重试都走这里）。
// - 慢网下响应会乱序：只认当前对话的、比已采用的更新的那次响应；
// - 只有 404 才算不存在；其它失败保留上一次成功的数据，标记“没拿到最新”，退避重试。
let convSeq = 0, convApplied = 0;
async function loadConv(id) {
  const n = ++convSeq;
  const fresh = () => S.convId === id && n > convApplied;
  try {
    const c = await api(`/api/conversations/${id}`);
    if (!fresh()) return;
    convApplied = n;
    S.conv = c;
    convOk();
  } catch (e) {
    if (!fresh()) return;
    convApplied = n;
    if (e.status === 404) { S.conv = { id, missing: true }; convOk(); return; }
    if (e.status === 401) return;
    S.convError = e.message;
    if (S.conv?.id !== id || S.conv.loading) S.conv = { id, failed: true };
    retryConv(id);
  }
}

function convOk() {
  S.convError = null; S.convRetryN = 0;
  clearTimeout(S.convRetry);
}

function retryConv(id) {
  clearTimeout(S.convRetry);
  const wait = Math.min(15000, 1000 * 2 ** Math.min(S.convRetryN = (S.convRetryN || 0) + 1, 4));
  S.convRetry = setTimeout(async () => {
    if (S.convId !== id || !S.convError) return;
    await loadConv(id);
    if (S.convId === id && S.view === "chats") render();
  }, wait);
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
      <div class="field"><label for="pp-desc-${esc(p.id)}">描述</label><textarea id="pp-desc-${esc(p.id)}" name="description" rows="2" required minlength="2" maxlength="2000">${esc(p.description)}</textarea></div>
      <div class="field"><label for="pp-caps-${esc(p.id)}">能力</label><input id="pp-caps-${esc(p.id)}" name="capabilities" value="${esc(p.capabilities.join(", "))}"></div>
      <fieldset class="checks"><legend>权限 · 不能超出提议者的有效权限</legend>${boxes || '<span class="muted">提议者没有可授予的权限</span>'}</fieldset>
      ${catalogOn() ? `<div class="field"><label for="pp-model-${esc(p.id)}">模型</label><select id="pp-model-${esc(p.id)}" name="model_preset">${presetOptions("")}</select></div>` : ""}
      <div class="field"><label for="pp-note-${esc(p.id)}">审核意见（会告诉提议者）</label><input id="pp-note-${esc(p.id)}" name="note"></div>
      <p class="hint">批准后以你的身份创建（归你所有），并加入项目「${esc(proj(p.project_id)?.name || "")}」。</p>
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
        authority: [...f.querySelectorAll('[name="authority"]:checked')].map((c) => c.value),
        model_preset: f.model_preset?.value || null };
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
  $("#tab-archive").setAttribute("aria-selected", String(which === "archive"));
  $("#tab-task").setAttribute("aria-selected", String(which === "task"));
  $("#members-panel").hidden = which !== "members";
  $("#archive-panel").hidden = which !== "archive";
  $("#task-detail").hidden = which !== "task";
  if (which === "task") renderTask();
  if (which === "archive") renderArchive();
}

// ---------- 项目档案：纲要、决定、文件、资源引用 ----------
// 归项目所有，不放进任何项目仓库。纲要和仍然有效的决定每轮推送给成员；文件只读挂载；资源只是引用。
const BRIEF_STALE_DAYS = 14;

async function renderArchive() {
  const box = $("#archive-panel"), c = S.conv;
  const has = !!c && (c.kind === "project" || c.kind === "session") && !!c.project_id && !c.archived && !!proj(c.project_id);
  $("#tab-archive").hidden = !has;
  if (!has && S.sideTab === "archive") { sideTab("members"); return; }
  if (!has || box.hidden) return;
  // 正在填表时不重绘（事件驱动的刷新会清掉输入）
  if (box.contains(document.activeElement) && document.activeElement.matches("input, textarea")) return;
  const d = await api(`/api/projects/${c.project_id}`).catch(() => null);
  if (!d || S.conv?.project_id !== c.project_id) return;
  const host = d.is_host, joint = d.kind === "joint";
  const who = (x) => x.by_agent ? aname(x.by_agent) : (d.participant_emails?.[x.by_user] || "");
  const stale = d.brief && d.brief_updated_at && (Date.now() / 1000 - d.brief_updated_at) > BRIEF_STALE_DAYS * 86400;
  const active = d.decisions.filter((x) => x.status === "active").reverse();
  const inactive = d.decisions.filter((x) => x.status !== "active").reverse();
  const pend = d.suggestions.filter((x) => x.status === "pending");
  const decision = (x) => `<li class="${x.status}"><div>${esc(x.text)}${x.reason ? `<span class="muted"> —— ${esc(x.reason)}</span>` : ""}</div>
    <div class="meta"><code>${esc(x.id)}</code> · ${esc(who(x))} · ${time(x.created_at)}${x.supersedes ? ` · 取代 <code>${esc(x.supersedes)}</code>` : ""}
      ${x.status === "active" ? `<button type="button" class="link" data-revoke="${esc(x.id)}">撤销</button>` : ` · ${x.status === "revoked" ? "已撤销" : "已被取代"}`}</div></li>`;
  box.innerHTML = `${joint ? '<p class="hint warn-box">联合项目：档案对所有参与者可见，写进去等于对所有参与方公开。</p>' : ""}
    <section class="archive-sec" aria-labelledby="ar-brief"><h3 id="ar-brief">纲要</h3>
      ${stale ? `<p class="hint warn">超过 ${BRIEF_STALE_DAYS} 天没确认，还准确吗？${host ? '<button type="button" class="link" id="brief-ok">还准确</button>' : ""}</p>` : ""}
      ${host ? `<form id="brief-form"><label for="brief-text" class="sr-only">纲要</label>
        <textarea id="brief-text" rows="6" maxlength="4000" placeholder="目标、范围、成功标准、当前阶段。每一轮都会推送给所有成员。">${esc(d.brief)}</textarea>
        <div class="bar"><span class="hint">${d.brief.length} / 4000</span><button type="submit">保存</button></div></form>`
        : `<div class="md brief">${d.brief ? renderMarkdown(d.brief) : '<span class="muted">主持人还没写纲要。</span>'}</div>
        <details><summary>建议修改</summary><form id="suggest-form">
          <label for="sug-text" class="sr-only">新的纲要</label><textarea id="sug-text" rows="5" maxlength="4000" required>${esc(d.brief)}</textarea>
          <label for="sug-reason" class="sr-only">理由</label><input id="sug-reason" placeholder="为什么要改">
          <div class="bar"><span></span><button type="submit">提交给主持人</button></div></form></details>`}
      ${pend.length && host ? `<ul class="suggestions">${pend.map((x) => `<li><div class="who"><b>${esc(who(x))}</b> 建议修改纲要</div>
        ${x.reason ? `<p class="muted">${esc(x.reason)}</p>` : ""}<div class="md">${renderMarkdown(x.text)}</div>
        <div class="actions"><button type="button" data-sug="${esc(x.id)}" data-ok="1">采纳</button><button type="button" class="ghost" data-sug="${esc(x.id)}" data-ok="0">不采纳</button></div></li>`).join("")}</ul>`
        : pend.length ? `<p class="hint">${pend.length} 条修改建议等主持人决定。</p>` : ""}
    </section>
    <section class="archive-sec" aria-labelledby="ar-dec"><h3 id="ar-dec">决定 · ${active.length}</h3>
      <ul class="decisions">${active.map(decision).join("") || '<li class="empty">还没有决定</li>'}</ul>
      <form id="decision-form" class="stack"><label for="dec-text" class="sr-only">新决定</label>
        <input id="dec-text" required maxlength="2000" placeholder="记录一个决定">
        <label for="dec-reason" class="sr-only">理由</label><input id="dec-reason" maxlength="2000" placeholder="理由（可选）">
        <label for="dec-sup" class="sr-only">取代哪一条</label>
        <select id="dec-sup"><option value="">不取代旧决定</option>${active.map((x) => `<option value="${esc(x.id)}">取代：${esc(x.text.slice(0, 40))}</option>`).join("")}</select>
        <button type="submit">记录</button></form>
      ${inactive.length ? `<details><summary>已取代 / 撤销 · ${inactive.length}</summary><ul class="decisions">${inactive.map(decision).join("")}</ul></details>` : ""}
    </section>
    <section class="archive-sec" aria-labelledby="ar-files"><h3 id="ar-files">文件 · ${d.files.length}</h3>
      <ul class="files">${d.files.map((x) => `<li><a href="/api/projects/${esc(d.id)}/files/${encodeURIComponent(x.name)}" download>${esc(x.name)}</a>
        <span class="muted">${(x.size / 1024).toFixed(1)} KB</span>${host ? `<button type="button" class="link" data-rmfile="${esc(x.name)}">删除</button>` : ""}</li>`).join("")
        || '<li class="empty">还没有文件。Agent 可以用 publish_file 发布，你也可以上传文本文件。</li>'}</ul>
      <label class="button ghost sm file-up">上传文本文件<input type="file" id="file-up" accept=".md,.txt,.csv,.json,text/*" hidden></label>
    </section>
    <section class="archive-sec" aria-labelledby="ar-refs"><h3 id="ar-refs">相关资源</h3>
      <ul class="refs">${d.refs.map((x) => `<li><b>${esc(x.name)}</b> <code>${esc(x.location)}</code>${x.note ? `<span class="muted"> · ${esc(x.note)}</span>` : ""}
        ${host ? `<button type="button" class="link" data-rmref="${esc(x.id)}">移除</button>` : ""}</li>`).join("") || '<li class="empty">还没有引用资源（仓库、路径、文档）</li>'}</ul>
      ${host ? `<form id="ref-form" class="stack"><label for="rf-name" class="sr-only">资源名称</label><input id="rf-name" required placeholder="名称">
        <label for="rf-loc" class="sr-only">位置</label><input id="rf-loc" required placeholder="路径或 URL">
        <label for="rf-note" class="sr-only">说明</label><input id="rf-note" placeholder="说明（可选）"><button type="submit">添加</button></form>` : ""}
      <p class="hint">引用只是指针，不授予任何访问权限：Agent 能不能读某个仓库，仍由它自身的权限决定。</p>
    </section>
    <p class="error" role="alert"></p>`;
  const gid = d.id, err = (e) => { $(".error", box).textContent = e.message; };
  const call = async (path, opts) => { try { await api(path, opts); await reload(); renderArchive(); } catch (e) { err(e); } };
  $("#brief-form", box)?.addEventListener("submit", (e) => { e.preventDefault(); call(`/api/projects/${gid}/brief`, { method: "PUT", body: { text: $("#brief-text").value } }); });
  $("#brief-ok", box)?.addEventListener("click", () => call(`/api/projects/${gid}/brief/confirm`, { method: "POST", body: {} }));
  $("#suggest-form", box)?.addEventListener("submit", (e) => { e.preventDefault(); call(`/api/projects/${gid}/suggestions`, { method: "POST", body: { text: $("#sug-text").value, reason: $("#sug-reason").value } }); });
  box.querySelectorAll("[data-sug]").forEach((b) => b.addEventListener("click", () =>
    call(`/api/projects/${gid}/suggestions/${b.dataset.sug}`, { method: "POST", body: { accept: b.dataset.ok === "1" } })));
  $("#decision-form", box).addEventListener("submit", (e) => { e.preventDefault();
    call(`/api/projects/${gid}/decisions`, { method: "POST", body: { text: $("#dec-text").value, reason: $("#dec-reason").value, supersedes: $("#dec-sup").value || null } }); });
  box.querySelectorAll("[data-revoke]").forEach((b) => b.addEventListener("click", () => {
    if (confirm("撤销这个决定？之后不再推送给成员。")) call(`/api/projects/${gid}/decisions/${b.dataset.revoke}`, { method: "DELETE" });
  }));
  box.querySelectorAll("[data-rmfile]").forEach((b) => b.addEventListener("click", () => {
    if (confirm(`删除文件 ${b.dataset.rmfile}？`)) call(`/api/projects/${gid}/files/${encodeURIComponent(b.dataset.rmfile)}`, { method: "DELETE" });
  }));
  $("#file-up", box).addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    if (file.size > 200_000) { err(new Error("文件太大：最多 200 KB")); return; }
    call(`/api/projects/${gid}/files`, { method: "POST", body: { name: file.name, content: await file.text() } });
  });
  $("#ref-form", box)?.addEventListener("submit", (e) => { e.preventDefault();
    call(`/api/projects/${gid}/refs`, { method: "POST", body: { name: $("#rf-name").value, location: $("#rf-loc").value, note: $("#rf-note").value } }); });
  box.querySelectorAll("[data-rmref]").forEach((b) => b.addEventListener("click", () => call(`/api/projects/${gid}/refs/${b.dataset.rmref}`, { method: "DELETE" })));
}

// 群成员随时加减：在这里加人、移人，变动会作为一条事件出现在群聊里。私聊显示对方。
// 自己的 Agent 在这个对话里的席位工作目录：展开时再加载
const workdirsHtml = (c) => {
  const mine = Object.keys(c.seats || {}).map(agent).filter((a) => a && a.mine);
  const open = S.wdOpen || new Set();
  return mine.length ? `<h3>工作目录</h3>${mine.map((a) => {
    const base = `/api/conversations/${c.id}/seats/${a.id}/files`, cached = S.wdCache?.[base];
    return `<details class="workdir" data-wd="${esc(a.id)}" ${open.has(base) ? "open" : ""}><summary>${esc(a.name)}</summary>
    <div class="wd-body">${cached ? filesHtml(base, cached) : '<span class="muted">加载中…</span>'}</div></details>`;
  }).join("")}` : "";
};
function bindWorkdirs(box, c) {
  S.wdOpen ||= new Set(); S.wdCache ||= {};
  const load = async (d) => {
    const base = `/api/conversations/${c.id}/seats/${d.dataset.wd}/files`;
    if (!d.open) { S.wdOpen.delete(base); return; }
    S.wdOpen.add(base);
    S.wdCache[base] = await api(base).catch((e) => ({ files: [], note: e.message }));
    $(".wd-body", d).innerHTML = filesHtml(base, S.wdCache[base]);
  };
  box.querySelectorAll("[data-wd]").forEach((d) => { d.addEventListener("toggle", () => load(d)); });
}

function renderMembers() {
  const box = $("#members-panel"), c = S.conv;
  if (!c || c.missing || c.loading || c.failed) { box.innerHTML = ""; return; }
  const row = (a, extra = "", whose = "") => `<li>${avatar(a.name, "sm")}<span class="m-body"><span class="n">${esc(a.name)}
      ${c.live?.[a.id] ? '<span class="dot-live" title="运行中"></span>' : ""}</span>
      <span class="s">${whose ? `${esc(whose)} · ` : ""}${esc(a.capabilities.join(" · ") || a.description)}</span></span>${extra}</li>`;
  if (c.kind === "dm") {
    const a = agent(c.agent_id);
    box.innerHTML = a ? `${workdirsHtml(c)}<ul class="member-list">${row(a)}</ul>
      <dl><dt>自身权限</dt><dd>${tags(a.authority, "ok")}</dd><dt>模型</dt><dd>${modelDd(a)}</dd></dl>
      <p class="hint">私聊里它只能自己完成，不能委托。要让它和别的成员协作，在群聊里 @ 它。</p>` : placeholder("这个 Agent 已删除。");
    bindWorkdirs(box, c);
    return;
  }
  const g = proj(c.project_id);
  if (!g) { box.innerHTML = placeholder(c.kind === "session" ? "你已不在这个项目里，会话只读。" : "项目已删除。"); return; }
  if (c.kind === "session") {
    const a = agent(c.agent_id);
    const chat = S.snap.conversations.find((x) => x.kind === "project" && x.project_id === g.id && !x.archived);
    box.innerHTML = `${a ? `<ul class="member-list">${row(a, "", "你的助理")}</ul>` : placeholder("助理已删除。")}
      <p class="hint">项目会话只有你看得到。助理每一轮都会拿到「${esc(g.name)}」的纲要、决定、成员、没结束的任务和群聊里最近的消息，
        也能委托项目成员、登记任务；它写纲要、决定、文件时会在群聊里留一条事件。</p>
      <p><a href="#${esc(chat ? chat.id : `p-${g.id}`)}">回到项目群聊</a> · ${g.member_ids.length} 名成员</p>
      ${workdirsHtml(c)}`;
    bindWorkdirs(box, c);
    return;
  }
  const joint = g.kind === "joint", host = isHost(g);
  const members = g.member_ids.map(agent).filter(Boolean);
  const others = S.snap.agents.filter((a) => a.mine && !g.member_ids.includes(a.id));  // 只能加入自己的 Agent
  const rm = (a) => (a.mine || host) ? `<button type="button" class="ghost icon sm rm" data-id="${esc(a.id)}"
      aria-label="把 ${esc(a.name)} 移出项目" title="移出项目"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M4 8h8"/></svg></button>` : "";
  // 联合项目：标出属于谁；自己的 Agent 可以打开私有目录挂载（默认不挂载）
  const extra = (a) => joint && a.mine ? `${`<label class="switch home-mount" title="挂载后它在这个项目里能用自己的私有目录；其他参与方的消息可能诱导它带出里面的内容">
        <input type="checkbox" data-home="${esc(a.id)}" ${g.home_enabled.includes(a.id) ? "checked" : ""}> 私有目录</label>`}` : "";
  box.innerHTML = `<h3>成员 · ${members.length}</h3>
    <ul class="member-list">${members.map((a) => row(a, extra(a) + rm(a), joint ? (a.mine ? "你的" : `${a.owner_email || ""} 的`) : "")).join("")
      || '<li class="empty">还没有成员</li>'}</ul>
    ${others.length ? `<form class="add-member" aria-label="添加成员">
      <label for="add-member-sel" class="sr-only">添加你的 Agent</label>
      <select id="add-member-sel">${others.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("")}</select>
      <button type="submit">添加</button></form>` : '<p class="hint">你的 Agent 都已在项目里。</p>'}
    <p class="error" role="alert"></p>
    ${joint ? `<p class="hint">联合项目：主持人 ${esc(g.host_email || "")}。每个 Agent 只代表它的所有者，委托只给两端的所有者看，运行过程只给 Agent 的所有者看。</p>
      ${host ? "" : '<p><button type="button" class="ghost sm" id="leave-project">退出项目</button></p>'}` : ""}
    <p class="hint">移出项目不会打断它手上已经接下的任务，只是之后不能再被 @、被委托。</p>
    ${workdirsHtml(c)}`;
  bindWorkdirs(box, c);
  const err = (e) => { $(".error", box).textContent = e.message; };
  box.querySelectorAll("[data-home]").forEach((cb) => cb.addEventListener("change", async () => {
    if (cb.checked && !confirm("在联合项目里挂载私有目录：它能读到你积累的笔记，其他参与方的消息可能诱导它带出来。确定打开？")) { cb.checked = false; return; }
    try { await api(`/api/projects/${g.id}/members/${cb.dataset.home}/home`, { method: "PUT", body: { enabled: cb.checked } }); await reload(); }
    catch (e) { err(e); }
  }));
  $("#leave-project", box)?.addEventListener("click", async () => {
    if (!confirm(`退出 ${g.name}？你的 Agent 会一并移出，之后看不到这个项目和群聊。`)) return;
    try { await api(`/api/projects/${g.id}/leave`, { method: "POST", body: {} }); location.hash = "projects"; await reload(); }
    catch (e) { err(e); }
  });
  box.querySelectorAll(".rm").forEach((b) => b.addEventListener("click", async () => {
    const a = agent(b.dataset.id);
    if (!confirm(`把 ${a?.name || "这个 Agent"} 移出 ${g.name}？它手上已经接下的任务会照常完成，之后不能再被 @、被委托。`)) return;
    try { await api(`/api/projects/${g.id}/members/${b.dataset.id}`, { method: "DELETE" }); await reload(); } catch (e) { err(e); }
  }));
  $(".add-member", box)?.addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await api(`/api/projects/${g.id}/members`, { method: "POST", body: { agent_id: $("#add-member-sel").value } }); await reload(); }
    catch (e2) { err(e2); }
  });
}

function renderThread() {
  const c = S.conv;
  if (!c) return;
  if (c.missing || c.loading || c.failed) {
    $("#thread-head").innerHTML = "";
    $("#thread").innerHTML = `<li>${placeholder(c.missing ? "这个对话不存在或已删除。" : c.loading ? "正在加载…"
      : `对话加载失败：${esc(S.convError || "网络错误")}。正在自动重试… <button type="button" class="ghost sm" id="conv-retry">立即重试</button>`)}</li>`;
    $("#conv-retry")?.addEventListener("click", () => { S.convRetryN = 0; refreshConv(); });
    $("#msg-form").hidden = true; renderTask(); return;
  }
  $("#msg-form").hidden = !!c.archived;
  const g = proj(c.project_id), dm = c.kind === "dm", sess = c.kind === "session", peer = agent(c.agent_id);
  const head = sess
    ? `<span class="avatar session" aria-hidden="true">›</span><div class="info"><h2 class="panel-title" style="margin:0">${esc(c.title)} ${pill(c.status)}</h2>
        <div class="muted">${c.archived ? "已归档，只读" : `项目会话 · ${esc(g?.name || "")} · 和 ${esc(peer?.name || "助理")} 单独聊，只有你看得到`}</div></div>`
    : dm
    ? `${avatar(peer?.name)}<div class="info"><h2 class="panel-title" style="margin:0">${esc(peer?.name || c.title)} ${pill(c.status)}</h2>
        <div class="muted">${c.archived ? "Agent 已删除，只读" : `私聊 · ${esc(peer?.description || "")}`}</div></div>`
    : `<span class="avatar group" aria-hidden="true" style="background:${g ? gcolor(g.id) : "var(--surface-3)"}">#</span>
       <div class="info"><h2 class="panel-title" style="margin:0">${esc(c.title)} ${pill(c.status)}</h2>
        <div class="muted">${c.archived ? "已归档，只读" : `${g?.kind === "joint" ? "联合项目 · " : ""}${c.members.length} 名成员 · @ 谁，谁接任务${g?.description ? ` · ${esc(g.description)}` : ""}`}</div></div>`;
  const actions = sess ? (c.archived
      ? `${g ? '<button type="button" class="ghost" id="sess-restore">恢复</button>' : ""}<button type="button" class="danger" id="conv-del">删除</button>`
      : '<button type="button" class="ghost" id="sess-rename">改名</button><button type="button" class="ghost" id="sess-archive">归档</button>')
    : c.archived ? '<button type="button" class="danger" id="conv-del">删除</button>'
    : c.kind === "project" && g ? `<button type="button" class="ghost" id="sess-new" title="和你的助理单独聊这个项目，不打扰群聊">新会话</button>${c.is_host === false ? ""
      : '<button type="button" class="ghost" id="conv-clear" title="清空聊天记录，成员从新的会话开始">清空记录</button>'}`
    : c.is_host === false ? ""  // 联合项目的群聊只有主持人能清空
    : '<button type="button" class="ghost" id="conv-clear" title="清空聊天记录，成员从新的会话开始">清空记录</button>';
  const stale = S.convError ? `<span class="net-warn" role="status" title="${esc(S.convError)}">网络不稳定，显示的可能不是最新内容，正在重试…</span>` : "";
  $("#thread-head").innerHTML = head + stale + actions;
  $("#conv-del")?.addEventListener("click", async () => {
    if (!confirm("删除这个已归档的对话？")) return;
    try { await api(`/api/conversations/${c.id}`, { method: "DELETE" }); S.convId = null; S.conv = null; location.hash = "network"; await reload(); }
    catch (err) { alert(err.message); }
  });
  $("#sess-new")?.addEventListener("click", async () => {
    try { const r = await api(`/api/projects/${g.id}/sessions`, { method: "POST", body: {} }); await reload(); location.hash = r.id; }
    catch (err) { alert(err.message); }
  });
  const patchSession = async (body) => {
    try { await api(`/api/sessions/${c.id}`, { method: "PATCH", body }); await reload(); } catch (err) { alert(err.message); }
  };
  $("#sess-rename")?.addEventListener("click", () => {
    const t = prompt("会话名称", c.title);
    if (t && t.trim()) patchSession({ title: t.trim() });
  });
  $("#sess-archive")?.addEventListener("click", () => patchSession({ archived: true }));
  $("#sess-restore")?.addEventListener("click", () => patchSession({ archived: false }));
  $("#conv-clear")?.addEventListener("click", async () => {
    if (!confirm("清空这个对话的聊天记录？成员的会话会从头开始；委托与知识不受影响。")) return;
    try { await api(`/api/conversations/${c.id}/clear`, { method: "POST", body: {} }); await reload(); }
    catch (err) { alert(err.message); }
  });
  renderMembers();
  renderArchive();

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
      const sender = !m.user_id || m.user_id === S.me?.id ? "你" : (c.senders?.[m.user_id] || "参与者");
      // 被认定为任务的消息：标出任务与状态，点开到看板
      const reqs = (c.requests || []).filter((r) => r.message_id === m.id).map((r) =>
        `<a class="req-tag" href="#board" data-req="${esc(r.id)}">任务 · ${esc(r.agent_name)} ${reqPillOf(r)}</a>`).join("");
      return `<li class="msg user"><div class="content"><div class="who"><b>${esc(sender)}</b>${to}${time(m.ts)}</div><div class="body">${mentionHtml(m.text)}</div>${reqs ? `<div class="req-tags">${reqs}</div>` : ""}</div></li>`;
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
    : "@ 一个成员把任务交给它。它可以自己做，也可以在项目里找合适的成员委托。")}</li>`;
  [...th.children].forEach((li, i) => { li.dataset.i = i; if (open.has(String(i))) li.querySelector("details")?.setAttribute("open", ""); });
  if (atBottom) th.scrollTop = th.scrollHeight;
  th.querySelectorAll(".deleg").forEach((b) => b.addEventListener("click", () => {
    S.taskSel = b.dataset.id;
    th.querySelectorAll(".deleg").forEach((x) => x.classList.toggle("selected", x === b));
    sideTab("task");
  }));
  bindProposals(th);
  th.querySelectorAll("[data-req]").forEach((a) => a.addEventListener("click", () => { S.reqSel = a.dataset.req; S.taskSel = null; }));
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
    <span class="route"><span class="name">${esc(aname(t.delegator_id))}</span><span class="arrow">→</span><span class="name">${esc(t.delegatee_id ? aname(t.delegatee_id) : "（项目外）")}</span>${pill(t.status)}</span>
    <span class="mission">${esc(t.delegation.mission)}</span></button>`;
}

// 工作目录（只读）：Agent 的所有者看它写出来的文件；运行时文件、缓存、只读副本不列
const filesHtml = (base, d) => d.note ? `<p class="hint">${esc(d.note)}</p>`
  : `<ul class="files">${d.files.map((f) => `<li><a href="${esc(base)}/${f.path.split("/").map(encodeURIComponent).join("/")}" download>${esc(f.path)}</a>
      <span class="muted">${(f.size / 1024).toFixed(1)} KB</span></li>`).join("") || '<li class="empty">还没有文件</li>'}</ul>`;

async function renderTask(target) {
  // 对话侧栏或看板侧栏：看板里点卡片时传入看板的详情框
  // 看板里委托详情嵌在用户任务详情里（#req-task），不再占用整个看板侧栏
  const box = target || (S.view === "board" ? $("#req-task") : $("#task-detail")), id = S.taskSel;
  if (!box) return;
  if (!id) { box.innerHTML = placeholder("点击对话里的委托，查看授权、约束、实时过程与结果。"); return; }
  const [t, wd] = await Promise.all([api(`/api/tasks/${id}`).catch(() => null), api(`/api/tasks/${id}/files`).catch(() => null)]);
  if (!t || S.taskSel !== id) return;
  const keep = box.scrollTop;
  const d = t.delegation;
  box.innerHTML = `<h2 class="panel-title">${esc(aname(t.delegator_id))} <span class="muted">→</span> ${esc(t.delegatee_id ? aname(t.delegatee_id) : "（项目外）")} ${pill(t.status)}</h2>
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
    ${wd ? `<h3>执行者工作目录 · ${wd.files.length}</h3>${filesHtml(`/api/tasks/${t.id}/files`, wd)}
      ${t.result?.files?.length ? `<p class="hint">交付时已拷给委托方：<code>${esc(t.result.files_dir)}/</code></p>` : ""}` : ""}
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
const members = () => (S.conv?.members || proj(S.conv?.project_id)?.member_ids || []).map(agent).filter(Boolean);
const mentionedIn = (text) => members().filter((a) => text.toLowerCase().includes(`@${a.name.toLowerCase()}`));

function renderComposer() {
  composerHint();
}

function composerHint() {
  const c = S.conv;
  if (!c) return;
  const m = $("#msg"), who = mentionedIn(m.value);
  if (c.kind === "dm" || c.kind === "session") {
    m.placeholder = c.kind === "session" ? "问问项目里的情况，或者交代一件事" : "和它说点什么";
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
  if (S.conv?.kind !== "project" || q === null) return closeMentionPop();
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

// ================= 设备（端侧运行时） =================
// 一台设备 = 一台电脑上的一个连接器，可以同时提供多个 harness（claude-code / codex / dsh）。
// 接入走设备码配对：电脑上跑 setup，浏览器里批准（#pair=<码>）；手动签发令牌留作没有浏览器时的备用。
const harnessLabel = (h) => S.info?.harnesses?.[h] || h;
const harnessTags = (d) => (d.harnesses || []).map((h, i) =>
  `<span class="tag model" title="${i === 0 ? "默认 harness" : "harness"}">${esc(harnessLabel(h))}</span>`).join("");

async function renderDevicesView() {
  let devices = [];
  try { devices = await api("/api/devices"); } catch { devices = []; }
  S.devices = devices;
  $("#device-list").innerHTML = devices.map((d) => {
    const online = d.online;
    const agents = S.snap.agents.filter((a) => a.device_runtime === d.id);
    return `<li class="${S.deviceSel === d.id ? "selected" : ""}"><div class="card">
      <button type="button" class="card-main" data-id="${esc(d.id)}" aria-label="查看设备 ${esc(d.name)}">
        <div class="card-head"><span class="name">${esc(d.name)}</span>
          ${d.revoked ? '<span class="vis-tag other">已吊销</span>'
            : `<span class="pill status ${online ? "busy" : "idle"}">${online ? "在线" : "离线"}</span>`}</div>
        <p class="card-desc">${agents.length ? `运行 ${agents.map((a) => esc(a.name)).join("、")}` : "还没有 Agent 绑到这台设备"}</p>
        <div class="card-meta">${harnessTags(d)}<span class="muted">${d.last_seen_at ? `最近连接 ${time(d.last_seen_at)}` : "还没连接过"}</span></div>
      </button>
      <div class="card-foot"><span></span>${d.revoked ? "" : `<button type="button" class="danger sm" data-revoke="${esc(d.id)}">吊销</button>`}</div>
    </div></li>`;
  }).join("") || '<li class="empty">还没有设备。点“连接一台设备”，在要运行 Agent 的电脑上执行一条命令。</li>';
  $("#device-list").querySelectorAll(".card-main").forEach((b) => b.addEventListener("click", () => {
    if (S.devicePanel?.token && !confirm("令牌还没确认复制，离开后就再也看不到了。确定离开？")) return;
    S.devicePanel = null; S.deviceSel = b.dataset.id; renderDevicesView();
  }));
  $("#device-list").querySelectorAll("[data-revoke]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("吊销后这台设备上的连接器会立即断开，绑在它上面的 Agent 进入等待。")) return;
    try { await api(`/api/devices/${b.dataset.revoke}`, { method: "DELETE" }); await reload(); }
    catch (e) { alert(e.message); }
  }));
  renderDeviceDetail();
}

// Agent 的“运行在”：每台设备按它报告的 harness 各一项，值是 "<设备 id>|<harness>"；
// 还没连过的设备不知道装了什么，只有一项（用设备的默认 harness）。没指定 harness 的旧 Agent 落在第一项上。
function placementOptions(x) {
  return (S.devices || []).filter((d) => !d.revoked).map((d) => {
    const hs = d.harnesses || [];
    if (!hs.length) {
      return `<option value="${esc(d.id)}" ${x.device_runtime === d.id ? "selected" : ""}>${esc(d.name)} · 还没连接过</option>`;
    }
    const mine = x.device_runtime === d.id;
    const cur = mine ? x.harness || hs[0] : null;
    // 绑定的 harness 不在最近一次报告里（卸掉了）：仍列出来，免得保存时被悄悄换掉
    const all = mine && cur && !hs.includes(cur) ? [...hs, cur] : hs;
    return all.map((h) => `<option value="${esc(`${d.id}|${h}`)}" ${cur === h ? "selected" : ""}>${esc(d.name)} · ${esc(harnessLabel(h))}${hs.includes(h) ? "" : "（设备上没有）"}</option>`).join("");
  }).join("");
}
const placementOf = (v) => {
  const [device, harness] = (v || "").split("|");
  return { device_runtime: device || null, harness: harness || null };
};

// 在电脑上执行的一条命令：安装（或更新）连接器，然后配对并装成后台服务。命令里没有任何密钥。
const setupCmd = () =>
  `uv tool install --force git+https://github.com/SanChiaki/Matrix.git && matrix-connect setup ${location.origin}`;
// 备用：手动签发的令牌（没有浏览器的机器）。令牌只显示一次。
const connectCmd = (token) => {
  const origin = location.origin.replace(/^http/, "ws");
  return `uvx --from git+https://github.com/SanChiaki/Matrix.git matrix-connect --url ${origin}/api/devices/connect --token ${token} --driver claude-code`;
};

function copyButton(btnSel, preSel, statusSel, text) {
  $(btnSel).addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      $(statusSel).textContent = "已复制到剪贴板。";
    } catch {  // 剪贴板不可用（非安全上下文等）：选中文本，让人手动复制
      const r = document.createRange(); r.selectNodeContents($(preSel));
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
      $(statusSel).textContent = "无法自动复制，已选中命令，请按 ⌘C / Ctrl+C。";
    }
  });
}

// 侧边栏状态放在 S.devicePanel 里："new" = 连接说明，{ pair, info?, error? } = 批准配对，
// { token, name, cmd } = 手动签发的结果。reload() 与 SSE 事件都会重渲染设备页；按状态还原，令牌一直保留到点“完成”。
function renderDeviceDetail() {
  const box = $("#device-detail"), panel = S.devicePanel;
  if (panel === "new") {
    if (box.querySelector("#device-setup")) return;  // 已在：不重绘，免得冲掉正在输入的内容
    const cmd = setupCmd();
    box.innerHTML = `<h2 class="panel-title">连接一台设备</h2>
      <p class="muted">在要运行 Agent 的电脑上执行（需要 <a href="https://docs.astral.sh/uv/" target="_blank" rel="noopener">uv</a>）：</p>
      <pre class="cmd" id="device-setup" tabindex="0">${esc(cmd)}</pre>
      <div class="actions"><button type="button" id="dv-copy-setup">复制命令</button></div>
      <p class="hint" id="dv-copied-setup" role="status"></p>
      <p class="hint">它会检测本机装了的 Claude Code、Codex、dsh，打开浏览器让你批准这台电脑，再装成后台服务（登录后自动运行）。命令里没有密钥；令牌只保存在那台电脑上。</p>
      <form class="editor" id="pair-form">
        <div class="field"><label for="pair-code">浏览器没有自动打开？输入终端里显示的配对码</label>
          <input id="pair-code" name="code" placeholder="ABCD-EFGH" maxlength="16" autocomplete="off" spellcheck="false"></div>
        <p class="error" role="alert"></p>
        <div class="actions"><button type="submit">下一步</button><button type="button" class="ghost" id="dv-cancel">取消</button></div>
      </form>
      <details class="manual"><summary>没有浏览器的机器：手动签发令牌</summary>
        <form class="editor" id="device-form">
          <div class="field"><label for="dv-name">设备名称</label><input id="dv-name" name="name" placeholder="我的服务器" maxlength="80"></div>
          <p class="error" role="alert"></p>
          <div class="actions"><button type="submit" class="ghost">签发令牌</button></div>
        </form>
      </details>`;
    copyButton("#dv-copy-setup", "#device-setup", "#dv-copied-setup", cmd);
    $("#dv-cancel").addEventListener("click", () => { S.devicePanel = null; renderDeviceDetail(); });
    $("#pair-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const code = $("#pair-code").value.trim();
      if (!code) { $(".error", $("#pair-form")).textContent = "请输入配对码。"; return; }
      S.devicePanel = { pair: code }; renderDeviceDetail();
    });
    $("#device-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = $("#device-form button[type=submit]");
      btn.disabled = true;
      try {
        const d = await api("/api/devices", { method: "POST", body: { name: $("#dv-name").value.trim() } });
        S.devicePanel = { token: d.token, name: d.name, cmd: connectCmd(d.token) };
        S.deviceSel = d.id;
        renderDeviceDetail();
        await reload();
      } catch (err) { $(".error", $("#device-form")).textContent = err.message; btn.disabled = false; }
    });
    return;
  }
  if (panel && panel.pair) { renderPairing(box, panel); return; }
  if (panel && panel.token) {
    if (box.querySelector("#device-cmd")) return;  // 已经显示：不重绘（保住选中状态与“已复制”提示）
    box.innerHTML = `<h2 class="panel-title">${esc(panel.name)}</h2>
      <div class="warn-box">令牌只显示这一次。关掉这个面板之后就再也看不到了，请现在复制。</div>
      <p class="muted" style="margin-top:12px">在要运行 Agent 的电脑上执行：</p>
      <pre class="cmd" id="device-cmd" tabindex="0">${esc(panel.cmd)}</pre>
      <div class="actions"><button type="button" id="dv-copy">复制命令</button>
        <button type="button" class="ghost" id="dv-done">已复制，完成</button></div>
      <p class="hint" id="dv-copied" role="status"></p>
      <p class="hint">手动令牌一次只接一个 harness：把 --driver 换成 dsh 或 codex 即可换。能开浏览器的电脑建议改用“连接一台设备”里的一条命令，自动检测全部 harness。</p>`;
    copyButton("#dv-copy", "#device-cmd", "#dv-copied", panel.cmd);
    $("#dv-done").addEventListener("click", () => {
      if (!confirm("关掉后令牌不会再显示。确认已经复制好了？")) return;
      S.devicePanel = null; renderDeviceDetail();
    });
    return;
  }
  const d = (S.devices || []).find((x) => x.id === S.deviceSel);
  if (!d) { box.innerHTML = placeholder("选择一台设备查看，或连接新设备。"); return; }
  const agents = S.snap.agents.filter((a) => a.device_runtime === d.id);
  box.innerHTML = `<h2 class="panel-title">${esc(d.name)}</h2>
    <p class="muted">${d.revoked ? "已吊销" : d.online ? "在线" : "离线"}${d.hostname ? ` · ${esc(d.hostname)}` : ""} · ${d.last_seen_at ? `最近连接 ${time(d.last_seen_at)}` : "还没连接过"}</p>
    <dl><dt>harness</dt><dd>${(d.harnesses || []).length ? harnessTags(d) : '<span class="muted">还没报告（连接器连上后显示）</span>'}</dd>
      <dt>运行在这台设备上的 Agent</dt><dd>${agents.length ? tags(agents.map((a) => a.harness ? `${a.name} · ${harnessLabel(a.harness)}` : a.name)) : '<span class="muted">无</span>'}</dd></dl>
    <p class="hint">在那台电脑上装了新的 harness 后，重启连接器（或后台服务）就会出现在这里。连接器只发出站请求；设备离线时绑在它上面的 Agent 进入等待，不消耗重试。</p>`;
}

// 批准一台设备：看清主机名与 harness，确认是自己刚刚在那台电脑上看到的码，再批准。
function renderPairing(box, panel) {
  if (!panel.info && !panel.error) {
    if (!panel.loading) {
      panel.loading = true;
      api(`/api/devices/pair/${encodeURIComponent(panel.pair)}`)
        .then((info) => { panel.info = info; })
        .catch((err) => { panel.error = err.message; })
        .finally(() => { if (S.devicePanel === panel) { box.innerHTML = ""; renderDeviceDetail(); } });
    }
    box.innerHTML = `<h2 class="panel-title">批准设备</h2><p class="muted">正在查找配对码 ${esc(panel.pair)}…</p>`;
    return;
  }
  if (box.querySelector("#pair-approve")) return;  // 已在：不重绘，保住正在输入的名称
  const done = () => { S.devicePanel = null; if (location.hash.startsWith("#pair=")) history.replaceState(null, "", "#devices"); };
  if (panel.error) {
    box.innerHTML = `<h2 class="panel-title">批准设备</h2>
      <p class="error" role="alert">${esc(panel.error)}</p>
      <p class="hint">配对码 10 分钟内有效。过期了就在那台电脑上重新运行 matrix-connect setup。</p>
      <div class="actions"><button type="button" class="ghost" id="pair-back">返回</button></div>`;
    $("#pair-back").addEventListener("click", () => { done(); renderDeviceDetail(); });
    return;
  }
  const p = panel.info;
  box.innerHTML = `<h2 class="panel-title">批准设备</h2>
    <div class="warn-box">只批准你刚刚在自己电脑上看到的配对码。批准后，这台电脑可以替你运行绑定到它的 Agent。</div>
    <dl style="margin-top:12px"><dt>配对码</dt><dd><code>${esc(p.user_code)}</code></dd>
      <dt>主机名</dt><dd>${esc(p.hostname || "未报告")}</dd>
      <dt>检测到的 harness</dt><dd>${(p.harnesses || []).length ? harnessTags(p) : '<span class="muted">没有（装好后重启连接器即可）</span>'}</dd></dl>
    <form class="editor" id="pair-approve">
      <div class="field"><label for="pa-name">设备名称</label><input id="pa-name" name="name" value="${esc(p.name || "")}" maxlength="80"></div>
      <p class="error" role="alert"></p>
      <div class="actions"><button type="submit">批准</button><button type="button" class="danger" id="pa-deny">拒绝</button></div>
    </form>`;
  const code = encodeURIComponent(panel.pair);
  $("#pair-approve").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#pair-approve button[type=submit]");
    btn.disabled = true;
    try {
      const d = await api(`/api/devices/pair/${code}/approve`, { method: "POST", body: { name: $("#pa-name").value.trim() } });
      done(); S.deviceSel = d.id;
      await reload();
    } catch (err) { $(".error", $("#pair-approve")).textContent = err.message; btn.disabled = false; }
  });
  $("#pa-deny").addEventListener("click", async () => {
    try { await api(`/api/devices/pair/${code}/deny`, { method: "POST", body: {} }); } catch { /* 已过期也一样 */ }
    done(); renderDeviceDetail();
  });
}
