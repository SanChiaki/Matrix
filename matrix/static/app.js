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
// 快捷键的修饰键按系统显示：Mac 是 ⌘，Windows / Linux 是 Ctrl（两种按法都接，见 bindComposer）
const IS_MAC = /mac|iphone|ipad|ipod/i.test(navigator.userAgentData?.platform || navigator.platform || navigator.userAgent);
const MOD_KEY = IS_MAC ? "⌘" : "Ctrl";
const SEND_HINT = `<kbd>${MOD_KEY}</kbd> <kbd>Enter</kbd> 发送`;
const httpError = (message, status) => Object.assign(new Error(message), { status });

async function api(path, opts = {}) {
  if (opts.body && typeof opts.body !== "string" && !(opts.body instanceof Blob)) {  // 文件（附件上传）原样发送
    opts = { ...opts, body: JSON.stringify(opts.body), headers: { "Content-Type": "application/json" } };
  }
  let r;
  try {
    r = await fetch(path, { ...opts, signal: AbortSignal.timeout(opts.timeoutMs || API_TIMEOUT_MS) });
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

// ---------- 登录（注册即有一个助理，不再引导创建第一个 Agent） ----------
function showGate(mode) {
  S.gate = mode;
  $("#gate").hidden = false;
  document.body.classList.add("gated");
  $("#auth-form").hidden = false;
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

// 登录后：拿到身份（第一次会带上网络给的助理）
async function enter() {
  S.me = await api("/api/me");
  $("#me-email").textContent = S.me.email;
  $("#admin-link").hidden = !S.me.admin;
  if (!S.started) { S.started = true; await start(); }
  else { await reload(); route(); }
  hideGate();
}

async function init() {
  S.info = await api("/api/info");
  $("#auth-form").addEventListener("submit", submitAuth);
  $("#auth-switch").addEventListener("click", () => showGate(S.gate === "register" ? "login" : "register"));
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
  document.querySelectorAll(".tab[data-view]").forEach((b) => b.addEventListener("click", () => { location.hash = b.dataset.view; }));
  setupMore();
  $("#agent-new").addEventListener("click", () => { S.agentSel = null; renderAgentEditor(true); renderAgents(); });
  const upMine = (folder) => pickSkillPackage("/api/me/skills", folder).then(async (x) => {
    if (!x) return; S.skillSel = x.name; await loadMine(true); renderSkillsView();
  }).catch((e) => alert(e.message));
  $("#skill-new").addEventListener("click", () => upMine(false));
  $("#conn-custom").addEventListener("click", () => { S.connPanel = "__custom"; S.connSel = null; renderConnectorsView(); $("#cu-name")?.focus(); });
  $("#skill-new-dir").addEventListener("click", () => upMine(true));
  // 设备详情里的服务标签：跳到连接器页并选中它
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-conn]");
    if (t) { S.connSel = t.dataset.conn; S.connPanel = null; }
    const tab = e.target.closest("[data-pagetab]");  // 从别处跳到项目页的某个标签
    if (tab) S.pendingTab = tab.dataset.pagetab;
  });
  $("#device-new").addEventListener("click", () => {
    if (S.devicePanel?.token && !confirm("设备授权码还没确认复制，离开后就再也看不到了。确定离开？")) return;
    S.devicePanel = "new"; renderDeviceDetail();
  });
  $("#project-new").addEventListener("click", () => { S.projectSel = null; renderProjectsView(); renderProjectEditor(true); });
  $("#project-quick").addEventListener("click", () => { S.projectSel = null; S.newProject = true; location.hash = "projects"; });
  $("#tab-members").addEventListener("click", () => sideTab("members"));
  initTheme();
  $("#tab-task").addEventListener("click", () => sideTab("task"));
  $("#tab-archive").addEventListener("click", () => sideTab("archive"));
  bindComposer();
  bindAttachments();
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
  const PLAIN = ["network", "projects", "board", "agents", "skills", "connectors", "devices"];
  // 参照 Raft：#g-<项目> 打开项目的群聊，#dm-<Agent> 打开和它的私聊（第一次打开时创建）
  // #pair=<配对码>：连接器 setup 打开的批准页（docs/device-runtimes.md 第 2.5 节）
  const pair = hash.match(/^pair=([A-Za-z0-9-]{4,16})$/);
  if (pair) {
    S.devicePanel = { pair: pair[1] };
    S.convId = null; S.conv = null;
    show("devices");
    return;
  }
  if ((!hash || hash === "home") && S.me?.assistant_id) {  // 侧栏顶部的“助理”：和助理的私聊（第一次打开时创建）
    location.replace(`#dm-${S.me.assistant_id}`);
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
    // #project-<项目> 打开项目页（可收藏、刷新、前进后退）；#projects 是项目列表
    const page = hash.match(/^project-(.+)$/);
    S.convId = null; S.conv = null;
    S.projectSel = page ? page[1] : null; S.projectEdit = false;
    show(page ? "projects" : PLAIN.includes(hash) ? hash : "network");
  }
}

function show(view) {
  S.view = view;
  // 对话不是导航 tab：群聊与私聊常驻左栏，点哪个开哪个。
  document.querySelectorAll(".tab[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === view)));
  const inMore = $(`#more-menu [data-view="${view}"]`);  // 当前视图收在“更多”里：“更多”也亮起，并写出是哪一个
  $("#nav-more").setAttribute("aria-pressed", String(!!inMore));
  $("#more-cur").textContent = inMore ? inMore.textContent.trim() : "";
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== `view-${view}`; });
  if (view !== "chats") history.replaceState(null, "", view === "projects" && proj(S.projectSel) ? `#project-${S.projectSel}` : `#${view}`);
  if (view === "agents") loadModels();
  if (["agents", "skills", "connectors", "projects", "chats", "devices"].includes(view)) loadMine();
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
    + (S.snap.invitations || []).length + (S.snap.drafts || []).length + (S.snap.held || []).length
    + S.snap.projects.filter(isHost).reduce((n, g) => n + (g.suggestions || []).filter((x) => x.status === "pending").length, 0);
  $("#todo-count").hidden = !todo;
  $("#todo-count").textContent = todo;
  $("#todo-count").setAttribute("aria-label", `${todo} 件待处理`);
  ({ network: renderGraph, projects: renderProjectsView, board: loadBoard, agents: renderAgentsView,
     chats: renderChats, devices: renderDevicesView, skills: renderSkillsView, connectors: renderConnectorsView })[S.view]();
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

  // 待我处理：工作区外溢申请、共享项目里用我的连接器（我是 Agent 所有者）+ 我对话里的 Agent 提议
  const todo = [
    ...b.approvals.map((ap) => `<li class="todo-item approval">${approvalBody(ap, true, true)}</li>`),
    ...b.proposals.map((p) => `<li class="todo-item">${avatar(aname(p.proposer_id), "sm")}<span><b>${esc(aname(p.proposer_id))}</b> 提议新建
      「${esc(p.name)}」</span><a class="button sm" href="#${esc(p.conversation_id)}">去审核</a></li>`),
    ...(b.invitations || []).map((iv) => `<li class="todo-item"><span class="avatar sm group" aria-hidden="true">#</span>
      <span><b>${esc(iv.host)}</b> 邀请你加入项目「${esc(iv.name)}」${iv.description ? `<span class="muted"> · ${esc(iv.description)}</span>` : ""}</span>
      <span class="actions"><button type="button" class="sm" data-inv="${esc(iv.project_id)}" data-ok="1">接受</button>
        <button type="button" class="ghost sm" data-inv="${esc(iv.project_id)}" data-ok="0">拒绝</button></span></li>`),
    ...(b.drafts || []).map((d) => `<li class="todo-item">${avatar(d.agent_name, "sm")}<span><b>${esc(d.agent_name)}</b>
      起草了发往「${esc(d.project_name)}」的消息，等你确认</span><a class="button sm" href="#${esc(d.home_id)}">去确认</a></li>`),
    ...(b.held || []).map((h) => h.conversation_id
      ? `<li class="todo-item">${avatar(h.agent_name, "sm")}<span><b>${esc(h.agent_name)}</b> 在「${esc(h.project_name)}」的回复用到了别处的资料，等你确认</span>
        <a class="button sm" href="#${esc(h.conversation_id)}">去确认</a></li>`
      : heldCard(h).replace('<li class="msg proposal">', '<li class="todo-item held">')),
    ...(b.suggestions || []).map((x) => `<li class="todo-item"><span class="avatar sm group" aria-hidden="true">#</span>
      <span><b>${esc(x.by_agent ? aname(x.by_agent) : "参与者")}</b> ${x.kind === "skill" ? `提议给「${esc(x.project_name)}」新增技能 <code>${esc(x.skill_name)}</code>` : `建议修改「${esc(x.project_name)}」的纲要`}</span>
      <a class="button sm" href="#project-${esc(x.project_id)}" data-pagetab="${x.kind === "skill" ? "skills" : "brief"}">去看看</a></li>`),
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
  if (!editing) bindHeld($("#todo-list"), async () => { await loadBoard(); await reload(); });
  if (!editing) bindApprovals($("#todo-list"), loadBoard);

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
  if (ev.type === "plugin.records") {  // 插件记录变了：通知正在看的插件页面自己刷新；插件管理页更新条数
    const f = S.pluginFrame;
    if (f?.el.isConnected && f.pid === d.project_id && f.name === d.plugin)
      f.el.contentWindow?.postMessage({ matrix: 1, event: "records.changed", data: { table: d.table } }, "*");
    if (S.view === "projects" && S.projectSel === d.project_id && S.assets?.tab === "plugins") renderProjectPage();
    return;
  }
  if (ev.type === "plugin.violation") return;
  if (ev.type === "models.updated") { loadModels(); return; }  // 管理后台改了模型目录
  if (ev.type === "schedule.updated") { loadSchedules(); return; }
  if (ev.type === "draft.updated" && S.view === "board") loadBoard();
  if (ev.type === "held.updated") {
    if (S.view === "board") loadBoard();
    if (S.view === "chats" && d.held?.conversation_id === S.convId) scheduleConv();
  }
  if (ev.type === "skills.updated" || ev.type === "connection.updated") { loadMine(true).then(schedule); return; }
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
    box.innerHTML = "";
  }
  // 概览常驻在图下方的一行，不占弹出框
  $("#net-stats").innerHTML = [[S.snap.agents.length, "Agent"], [S.snap.projects.length, "项目"], [S.snap.conversations.length, "对话"],
    [S.snap.relations.length, "委托关系"], [S.snap.active_tasks.length, "进行中委托"],
    [(S.snap.proposals || []).filter((p) => p.status === "pending").length, "待审核提议"]]
    .map(([n, l]) => `<span><b>${n}</b> ${l}</span>`).join("") + '<span class="muted">点击节点或连线查看详情</span>';
}

// ================= Agent 管理 =================
function renderAgentsView() { renderAgents(); renderAgentEditor(); }

// 卡片上的“对话”：链接到 #g-<项目> / #dm-<Agent>，路由负责打开（私聊第一次打开时创建）
const chatBtn = (hash, label) => `<a class="button sm chat-btn" href="#${esc(hash)}" aria-label="${esc(label)}">
  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M2.5 4.5a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2H7l-3 2.5v-2.5h0a1.5 1.5 0 0 1-1.5-1.5z"/></svg>对话</a>`;

function renderAgents() {
  // 卡片主体选中编辑；底部“对话”按钮直接打开私聊。按钮不能嵌套，所以“对话”是卡片里的另一个按钮。
  // 自己的在前；别人的只出现在和你同在的共享项目里，只能看名片
  const list = [...S.snap.agents].sort((a, b) => (b.mine ? 1 : 0) - (a.mine ? 1 : 0));
  $("#agent-list").innerHTML = list.map((a) => `
    <li class="${S.agentSel === a.id ? "selected" : ""}"><div class="card">
      <button type="button" class="card-main" data-id="${esc(a.id)}" aria-label="编辑 ${esc(a.name)}">
        <div class="card-head">${avatar(a.name)}<span class="name">${esc(a.name)}</span>${pill(a.status)}</div>
        <p class="card-desc">${esc(a.description)}</p>
        <div class="card-meta">${tags(a.authority, "ok")}${modelTag(a)}${a.device_runtime
          ? `<span class="tag model" title="在我的电脑上">${a.device_online ? esc(a.harness ? harnessLabel(a.harness) : "我的电脑") : "设备离线"}</span>` : ""}</div>
      </button>
      <div class="card-foot"><span>${a.mine ? "" : `<span class="vis-tag other">${esc(a.owner_email || "他人")} 的</span>`}</span>
        ${!a.mine ? '<span class="muted">共享项目里协作</span>' : a.chat_enabled ? chatBtn(`dm-${a.id}`, `和 ${a.name} 私聊`) : '<span class="muted">不开放私聊</span>'}</div>
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
const modelSourceCN = { preset: "预置模型", custom: "单独配置", default: "默认模型", runtime: "默认" };
const modelTag = (a) => {
  const i = a.model_info, m = effectiveModel(a);
  if (!m) return "";
  const dflt = !i || i.source === "default" || i.source === "runtime";
  const title = i?.unavailable ? "选的模型已下架，改用默认模型" : modelSourceCN[i?.source || "runtime"];
  return `<span class="tag model${i?.unavailable ? " warn" : ""}" title="${esc(title)}">${esc(i?.label || m.model)}${i?.unavailable ? " · 原选择已下架" : dflt ? " · 默认" : ""}</span>`;
};
function modelDd(a) {
  const i = a.model_info, m = effectiveModel(a);
  if (!m) return a.device_runtime ? 'AI 工具里的默认模型 <span class="muted">（我的电脑上）</span>' : "默认";
  const text = i?.label ? `${i.label} · ${modelLabel(m)}` : modelLabel(m);
  const note = i?.unavailable ? '<span class="hint warn">选的模型已下架，现在用默认模型</span>'
    : i?.source === "default" || i?.source === "runtime" || !i ? ' <span class="muted">（默认）</span>' : "";
  return `${esc(text)}${note}`;
}
const catalogOn = () => !!S.models?.active;
// 预置模型下拉框的选项；sel 是当前选中的 id（"" = 默认）
function presetOptions(sel) {
  const d = S.models.models.find((m) => m.id === S.models.default);
  const dflt = d ? `默认（${d.label}）` : S.defaultModel ? `默认（${modelLabel(S.defaultModel)}）` : "默认";
  return `<option value="">${esc(dflt)}</option>` + S.models.models.map((m) =>
    `<option value="${esc(m.id)}" ${m.id === sel ? "selected" : ""}>${esc(m.label)} · ${esc(m.model)}${m.reasoning_effort ? ` · ${esc(m.reasoning_effort)}` : ""}</option>`).join("");
}

function renderAgentReadonly(box, a) {
  box.innerHTML = `<h2 class="panel-title">${avatar(a.name)} ${esc(a.name)}</h2>
    <p class="muted">${esc(a.owner_email || "")} 的 Agent</p>
    <dl><dt>描述</dt><dd>${esc(a.description)}</dd><dt>能力</dt><dd>${tags(a.capabilities)}</dd>
    <dt>自身权限</dt><dd>${tags(a.authority, "ok")}</dd>
    <dt>所在的共享项目</dt><dd>${tags(S.snap.projects.filter((g) => g.member_ids.includes(a.id)).map((g) => g.name))}</dd></dl>
    <p class="hint">它只为它的所有者工作：你可以在共享项目里 @ 它、委托它，它以所有者的名义、用所有者的资源执行。不能私聊，也不能拉进你自己的项目。</p>`;
}

function renderAgentEditor(isNew = false) {
  if (S.devices === undefined) {  // 第一次打开时先取设备列表（用于“运行位置”）
    api("/api/devices").then((d) => { S.devices = d; if (S.view === "agents") renderAgentEditor(isNew); })
      .catch(() => { S.devices = []; });
  }
  const box = $("#agent-editor"), a = agent(S.agentSel);
  if (!a && !isNew) { box.innerHTML = placeholder("选择一个 Agent 编辑，或新建。"); return; }
  if (a && !a.mine) { renderAgentReadonly(box, a); return; }
  const x = a || { name: "", description: "", capabilities: [], authority: ["web.research", "connector.use"], chat_enabled: true };
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
      ${a && a.runtime_capabilities ? `<div class="field"><label>它跑的地方实际能做到的</label>
        <div class="card-meta">${Object.entries(a.runtime_capabilities.enforcement).map(([k, v]) =>
          `<span class="tag ${v === "enforced" ? "ok" : ""}" title="${esc(v)}">${esc(k)}${v === "enforced" ? "" : ` · ${esc(v === "advisory" ? "仅约定" : "不可用")}`}</span>`).join("")}</div>
        <p class="hint">只有“可强制”的权限会进入有效权限；其余会在委托时自动收窄，并写明原因。</p></div>` : ""}
      ${a ? abilityBoxes(a) : ""}
      <fieldset class="model"><legend>运行位置</legend>
        <div class="field"><label for="af-device">运行在</label>
          <select id="af-device" name="device_runtime">
            <option value="">云端</option>
            ${placementOptions(x)}
          </select>
          <p class="hint">${S.devices?.length ? "选了你的电脑，它就在那台电脑上用选定的 AI 工具跑，用你本机的登录与文件；它读不到服务器上的数据。" : "还没有设备。先到「设备」页连接一台，才可以让它在你的电脑上跑。"}</p>
        </div>
      </fieldset>
      <fieldset class="model"><legend>模型</legend>
        ${catalogOn() ? `<div class="field" id="af-preset-box"><label for="af-preset">预置模型</label>
          <select id="af-preset" name="model_preset">${presetOptions(x.model_info?.source === "preset" ? x.model_info.preset : "")}</select>
          <p class="hint">${x.model_info?.unavailable ? '<span class="warn">原来选的模型已下架，现在用默认模型；重新选一个后保存。</span> ' : ""}云端的 Agent 只能用部署方预置的模型。</p></div>` : ""}
        <div class="model-fields" id="af-free">
          <div class="field"><label for="af-provider">模型服务</label><input id="af-provider" name="provider" value="${esc(x.model?.provider || "")}" placeholder="${esc(S.defaultModel?.provider || "")}"></div>
          <div class="field"><label for="af-model">模型</label><input id="af-model" name="model" value="${esc(x.model?.model || "")}" placeholder="${esc(S.defaultModel?.model || "")}"></div>
          <div class="field"><label for="af-effort">推理强度</label><input id="af-effort" name="effort" value="${esc(x.model?.reasoning_effort || "")}" placeholder="默认"></div>
        </div>
        <p class="hint" id="af-free-hint">留空则使用${catalogOn() ? " AI 工具自己的默认模型。在你电脑上跑时用的是你本机 AI 工具里的模型" : `默认模型${S.defaultModel ? `（${esc(modelLabel(S.defaultModel))}）` : ""}。模型服务必须是已经配置好的`}。</p>
      </fieldset>
      <fieldset class="model"><legend>运行上限</legend>
        <div class="model-fields">
          <div class="field"><label for="af-timeout">每轮超时（秒）</label><input id="af-timeout" name="timeout_s" type="number" min="1" step="1" value="${x.timeout_s ?? ""}" placeholder="不限制"></div>
          <div class="field"><label for="af-maxdel">委托次数上限</label><input id="af-maxdel" name="max_delegations" type="number" min="1" step="1" value="${x.max_delegations ?? ""}" placeholder="不限制"></div>
        </div>
        <p class="hint">留空不限制。超时到了这一轮会被终止（任务可续接）；委托次数按每次被 @ 引出的工作、每个任务分别计。</p>
      </fieldset>
      <label class="switch"><input type="checkbox" name="chat_enabled" ${x.chat_enabled ? "checked" : ""}> 允许你和它私聊（群聊里随时可以 @ 它）</label>
      ${a ? (S.me?.assistant_id === a.id
        ? '<p class="hint"><span class="tag ok">你的助理</span> 你加入项目时它跟着加入，项目会话里由它陪你聊。</p>'
        : '<p class="hint"><button type="button" class="link" id="af-assistant">设为我的助理</button> · 你加入项目时助理跟着加入，项目会话里由它陪你聊。</p>') : ""}
      <p class="hint">它只为你工作。要和别人协作，把它带进共享项目：别人可以在那里 @ 它，但它仍然以你的名义、用你的资源运行，工作区外的操作由你审批。</p>
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
      timeout_s: f.timeout_s.value ? Number(f.timeout_s.value) : null,
      max_delegations: f.max_delegations.value ? Number(f.max_delegations.value) : null,
      ...placementOf(f.device_runtime.value),
    };
    if (a && f.querySelector("#af-skills")) body.skills = [...f.querySelectorAll('[name="skill"]:checked')].map((c) => c.value);
    if (a && f.querySelector("#af-conns")) body.connections = [...f.querySelectorAll('[name="conn"]:checked')].map((c) => c.value);
    if (usePreset()) {
      Object.assign(body, { model: null, model_preset: f.model_preset.value || null });
    } else if (f.device_runtime.value || S.models) {
      // 端侧总是自由填写；云侧要等目录取回来才知道该怎么填（没取到时不改模型，免得把选好的预置模型冲掉）
      if ((f.provider.value.trim() !== "") !== (f.model.value.trim() !== "")) {
        $(".error", f).textContent = "模型服务与模型要么都填，要么都留空。"; return;
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
  if (S.newProject) S.projectSel = null;
  renderProjects();
  renderProjectEditor(S.newProject);
  renderProjectPage();
  if (S.newProject) { S.newProject = false; $("#gf-name")?.focus(); }
}

// 项目没有类型：有别人（参与者或待接受的邀请）就是共享的
const kindTag = (g) => g.shared
  ? `<span class="vis-tag shared">与 ${g.participants.length + (g.invited || []).length} 人共享</span>`
  : '<span class="vis-tag private">仅你</span>';
const isHost = (g) => !!g && (!g.owner_id || g.owner_id === S.me?.id);

function renderProjects() {
  $("#project-list").innerHTML = S.snap.projects.map((g) => `
    <li><div class="card">
      <button type="button" class="card-main" data-id="${esc(g.id)}" aria-label="打开 ${esc(g.name)}">
        <div class="card-head"><span class="swatch" style="background:${gcolor(g.id)}"></span><span class="name">${esc(g.name)}</span>${kindTag(g)}<span class="pill">${g.member_ids.length} 名成员</span></div>
        <p class="card-desc">${esc(g.description || "没有描述")}</p>
        <div class="card-meta">${tags(g.member_ids.map(aname))}</div>
      </button>
      <div class="card-foot"><span>${g.participants.length ? `<span class="muted">${g.participants.length + 1} 人参与</span>` : ""}</span>${chatBtn(`p-${g.id}`, `进入 ${g.name} 的群聊`)}</div>
    </div></li>`).join("") || '<li class="empty">还没有项目。</li>';
  $("#project-list").querySelectorAll(".card-main").forEach((b) =>
    b.addEventListener("click", () => { location.hash = `project-${b.dataset.id}`; }));
}

function renderProjectEditor(isNew = false) {
  const box = $("#project-editor"), g = proj(S.projectSel);
  if (!isNew && !(g && S.projectEdit)) { box.innerHTML = placeholder("选择一个项目编辑成员，或新建。"); return; }
  if (g && !isHost(g)) { renderProjectGuest(box, g); return; }
  const x = g || { name: "", description: "", member_ids: [] };
  // 只能加入自己的 Agent；有别人的项目里别人的成员由他们自己加减（主持人可以移出）
  const mine = S.snap.agents.filter((a) => a.mine);
  const theirs = (x.member_ids || []).filter((id) => !agent(id)?.mine);
  box.innerHTML = `<h2 class="panel-title">${g ? `项目配置 ${kindTag(g)}` : "新建项目"}</h2>
    <p class="muted">委托只能在项目成员之间进行。</p>
    <form class="editor" id="project-form">
      ${g ? "" : '<p class="hint">建好以后随时可以邀请别人：各自带上自己的 Agent，群聊和档案对所有参与者可见，委托只给两端的所有者看。</p>'}
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
    ${g ? '<div id="gf-people"></div>' : ""}`;
  const f = $("#project-form");
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { name: f.name.value.trim(), description: f.description.value.trim(),
      member_ids: [...theirs, ...[...f.querySelectorAll('[name="member"]:checked')].map((c) => c.value)] };
    try {
      const r = await api(g ? `/api/projects/${g.id}` : "/api/projects", { method: g ? "PATCH" : "POST", body });
      await reload();
      if (!g) location.hash = `project-${r.id}`;  // 新建后直接进它的项目页
    } catch (err) { $(".error", f).textContent = err.message; }
  });
  $("#gf-chat")?.addEventListener("click", () => { location.hash = `p-${g.id}`; });
  $("#gf-del")?.addEventListener("click", async () => {
    if (!confirm(`删除项目 ${g.name}？群聊归档为只读（只有你看得到），不再能委托。`)) return;
    try { await api(`/api/projects/${g.id}`, { method: "DELETE" }); location.hash = "projects"; await reload(); }
    catch (err) { $(".error", f).textContent = err.message; }
  });
  if (g) renderPeople($("#gf-people"), g);
}

// ---- 项目页（主区域，#project-<项目>）：面包屑 + 四块（说明 / 资料 / 技能 / 连接器）+ 插件；右侧是“项目配置”（renderProjectEditor）
// 说明 = 纲要 + 已定的事（每轮推送给成员）；资料 = 文件（存在这里）+ 链接（指向别处）；技能 = 项目技能；
// 连接器 = 这个项目用到的外部服务，从资料、插件、实际调用、手动添加汇总，项目从不持有谁的授权。
// 文件：所有成员可预览、下载，主持人可上传（也可以拖进来）、删除；“更新人”取日志里最近一次发布它的人 / Agent。
// 预览只在页面里转义后渲染（Markdown 走 renderMarkdown），不让浏览器直接打开文件：文件可能来自其他参与方或 Agent。
const ASSET_LIMIT = 200_000;  // 与 models.FILE_LIMIT 一致
const TEXT_EXT = ["MD", "TXT", "CSV", "TSV", "JSON", "YAML", "YML", "LOG", "XML", "HTML", "PY", "JS", "TS", "SH"];
const IMAGE_EXT = ["PNG", "JPG", "JPEG", "GIF", "WEBP"];
const fmtSize = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
const fileExt = (name) => (name.includes(".") ? name.split(".").pop().toUpperCase() : "文件");
const dateTime = (ts) => {
  const d = new Date(ts * 1000), now = new Date(), day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()) / 864e5;
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return day(now) - day(d) === 0 ? `今天 ${hm}` : day(now) - day(d) === 1 ? `昨天 ${hm}` : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
};
const FILE_KIND = { MD: "doc", TXT: "doc", LOG: "doc", CSV: "sheet", TSV: "sheet", JSON: "code", YAML: "code", YML: "code", XML: "code",
  HTML: "code", PY: "code", JS: "code", TS: "code", SH: "code", PNG: "image", JPG: "image", JPEG: "image", GIF: "image", WEBP: "image" };
const svgIcon = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const ICON = {
  file: svgIcon('<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>'),
  down: svgIcon('<path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/>'),
  trash: svgIcon('<path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13"/><path d="M9 7V4h6v3"/>'),
  link: svgIcon('<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>'),
  copy: svgIcon('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/>'),
  upload: svgIcon('<path d="M12 20V9"/><path d="m7 13 5-5 5 5"/><path d="M5 4h14"/>'),
  skill: svgIcon('<path d="M5 3h10l4 4v14H5z"/><path d="M8.5 10h7M8.5 13.5h7M8.5 17h4"/>'),
  plug: svgIcon('<path d="M9 2.5v4M15 2.5v4"/><path d="M6 6.5h12v4a6 6 0 0 1-12 0z"/><path d="M12 16.5v5"/>'),
};
const fileIcon = (ext) => `<span class="file-ico ${FILE_KIND[ext] || "other"}">${ICON.file}</span>`;
const fileUrl = (pid, name) => `/api/projects/${encodeURIComponent(pid)}/files/${encodeURIComponent(name)}`;
const isTextFile = (file) => file.type.startsWith("text/") || file.type === "application/json" || TEXT_EXT.includes(fileExt(file.name));
const assetWho = (d) => (f) => f.by_agent ? aname(f.by_agent) : !f.by_user ? "—" : f.by_user === S.me?.id ? "你"
  : f.by_user === d.owner_id ? d.host : d.participant_emails?.[f.by_user] || "—";
const ASSET_SORT = { name: (a, b) => a.name.localeCompare(b.name), updated_at: (a, b) => a.updated_at - b.updated_at, size: (a, b) => a.size - b.size };

async function renderProjectPage(force = false) {
  const index = $("#project-index"), page = $("#project-page"), g = proj(S.projectSel);
  index.hidden = !!g;
  page.hidden = !g;
  if (!g) { page.dataset.sig = ""; return; }
  // 正在填写时不让事件驱动的刷新重绘（会清掉输入和焦点）；切标签、排序这类人点的操作照常重绘
  if (!force && page.contains(document.activeElement) && document.activeElement.matches("input, textarea")) return;
  const d = await api(`/api/projects/${g.id}`).catch(() => null);
  if (!d || S.projectSel !== g.id) return;
  const st = S.assets = S.assets?.pid === g.id ? S.assets
    : { pid: g.id, tab: "brief", q: "", type: "", by: "updated_at", asc: false, note: "", bad: false, fresh: [] };
  if (S.pendingTab) { st.tab = S.pendingTab; S.pendingTab = null; }
  st.tab = { refs: "files", needs: "connectors" }[st.tab] || st.tab;  // 旧的标签名（资源引用并进资料，需要的连接器换成连接器面板）
  // 网络里每来一个事件都会走到这里：数据和视图状态都没变就不动 DOM，不打断悬停、焦点与滚动
  const plugins = (d.plugin_list || []).filter((p) => p.enabled !== false && !p.broken);  // 停用的插件不出标签页
  const ptab = st.tab.startsWith("plugin:") ? st.tab.split(":") : null;  // plugin:<插件>:<标签>
  if (ptab && !plugins.some((p) => p.name === ptab[1] && (p.tabs || []).some((t) => t.key === ptab[2]))) st.tab = "plugins";
  // 插件标签里是沙箱 iframe：记录变化不重绘（会重新加载插件页面），由事件转给页面自己刷新；只有插件结构变了才重绘
  const sig = JSON.stringify(st.tab.startsWith("plugin:")
    ? [g.name, st.tab, plugins.map((p) => [p.name, p.version, p.tabs, p.page_permissions])]
    : [d, g.name, g.description, st.tab, st.by, st.asc, st.note, st.skill, st.skillNew, S.catalog, S.pluginCatalog, st.hook]);
  if (page.dataset.sig === sig) return;
  page.dataset.sig = sig;
  const tab = (key, label, n) => `<button type="button" role="tab" id="pt-${key}" data-tab="${key}" aria-selected="${st.tab === key}"
    aria-controls="pp-panel">${label}${n == null ? "" : `<span class="count">${n}</span>`}</button>`;
  const asks = d.suggestions.filter((x) => x.status === "pending" && x.kind !== "skill").length;
  page.innerHTML = `<nav class="crumbs" aria-label="位置"><a href="#projects">项目</a><span aria-hidden="true">/</span>
      <span class="here">${esc(g.name)}</span>${kindTag(g)}<span class="grow"></span>
      <button type="button" class="ghost sm" id="pp-config">项目配置</button><a class="button ghost sm" href="#p-${esc(g.id)}">进入群聊</a></nav>
    ${g.description ? `<p class="page-desc">${esc(g.description)}</p>` : ""}
    <div class="page-tabs" role="tablist" aria-label="项目内容">${tab("brief", "说明", asks || null)}${tab("files", "资料", d.files.length + d.refs.length)}${tab("skills", "技能", (d.skill_list || []).length)}${tab("connectors", "连接器", d.connector_panel.services.length)}${tab("plugins", "插件", plugins.length)}${
      plugins.flatMap((p) => (p.tabs || []).map((t) => `<button type="button" role="tab" class="plugin-tab" id="pt-plugin-${esc(p.name)}-${esc(t.key)}"
        data-tab="plugin:${esc(p.name)}:${esc(t.key)}" aria-selected="${st.tab === `plugin:${p.name}:${t.key}`}" aria-controls="pp-panel"
        title="插件「${esc(p.title)}」的页面"><span class="plugin-dot" aria-hidden="true"></span>${esc(t.label)}<span class="count" data-badge="${esc(p.name)}:${esc(t.key)}">${esc(S.pluginBadges?.[`${d.id}:${p.name}:${t.key}`] || "")}</span></button>`)).join("")}</div>
    <div id="pp-panel" role="tabpanel" aria-labelledby="pt-${st.tab}"></div>`;
  $("#pp-config", page).addEventListener("click", () => { S.projectEdit = true; renderProjectEditor(); });
  page.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => {
    st.tab = b.dataset.tab; st.note = ""; renderProjectPage(true).then(() => page.querySelector(`[data-tab="${CSS.escape(st.tab)}"]`)?.focus());
  }));
  if (st.tab.startsWith("plugin:")) { renderPluginFrame($("#pp-panel", page), d, st); return; }
  S.pluginFrame = null;
  ({ brief: renderBriefTab, files: renderFilesTab, skills: renderSkillsTab, connectors: renderConnectorsTab, plugins: renderPluginsTab }[st.tab]
    || renderBriefTab)($("#pp-panel", page), d, st);
}

// 说明：纲要（主持人写，别人建议修改）+ 已定的事（谁都能记、能撤销）。每一轮都推送给所有成员（不变量 17）
const BRIEF_STALE_DAYS = 14;
function renderBriefTab(box, d, st) {
  const host = d.is_host;
  const who = (x) => x.by_agent ? aname(x.by_agent) : x.by_user === S.me?.id ? "你" : (d.participant_emails?.[x.by_user] || "");
  const stale = d.brief && d.brief_updated_at && (Date.now() / 1000 - d.brief_updated_at) > BRIEF_STALE_DAYS * 86400;
  const active = d.decisions.filter((x) => x.status === "active").reverse();
  const inactive = d.decisions.filter((x) => x.status !== "active").reverse();
  const pend = d.suggestions.filter((x) => x.status === "pending" && x.kind !== "skill");
  const decision = (x) => `<li class="${x.status}"><div>${esc(x.text)}${x.reason ? `<span class="muted"> —— ${esc(x.reason)}</span>` : ""}</div>
    <div class="meta">${esc(who(x))} · ${time(x.created_at)}${x.supersedes ? " · 取代了一条旧的" : ""}
      ${x.status === "active" ? `<button type="button" class="link" data-revoke="${esc(x.id)}">撤销</button>` : ` · ${x.status === "revoked" ? "已撤销" : "已被取代"}`}</div></li>`;
  box.innerHTML = `<p class="muted tab-intro">这件事是什么、已经定了什么。每一轮都会推送给所有成员${d.shared ? "；共享项目里所有参与者都看得到" : ""}。</p>
    ${assetNote(st)}
    <div class="brief-grid">
    <section class="archive-sec" aria-labelledby="pb-brief"><h3 id="pb-brief">纲要</h3>
      ${stale ? `<p class="hint warn">超过 ${BRIEF_STALE_DAYS} 天没确认，还准确吗？${host ? '<button type="button" class="link" id="brief-ok">还准确</button>' : ""}</p>` : ""}
      ${host ? `<form id="brief-form"><label for="brief-text" class="sr-only">纲要</label>
        <textarea id="brief-text" rows="10" maxlength="4000" placeholder="目标、范围、成功标准、当前阶段。">${esc(d.brief)}</textarea>
        <div class="bar"><span class="hint">${d.brief.length} / 4000</span><button type="submit">保存</button></div></form>`
        : `<div class="md brief">${d.brief ? renderMarkdown(d.brief) : '<span class="muted">主持人还没写纲要。</span>'}</div>
        <details><summary>建议修改</summary><form id="suggest-form" class="stack">
          <label for="sug-text" class="sr-only">新的纲要</label><textarea id="sug-text" rows="6" maxlength="4000" required>${esc(d.brief)}</textarea>
          <label for="sug-reason" class="sr-only">理由</label><input id="sug-reason" placeholder="为什么要改">
          <button type="submit">提交给主持人</button></form></details>`}
      ${pend.length && host ? `<ul class="suggestions">${pend.map((x) => `<li><div class="who"><b>${esc(who(x))}</b> 建议修改纲要</div>
        ${x.reason ? `<p class="muted">${esc(x.reason)}</p>` : ""}<div class="md">${renderMarkdown(x.text)}</div>
        <div class="actions"><button type="button" data-sug="${esc(x.id)}" data-ok="1">采纳</button><button type="button" class="ghost" data-sug="${esc(x.id)}" data-ok="0">不采纳</button></div></li>`).join("")}</ul>`
        : pend.length ? `<p class="hint">${pend.length} 条修改建议等主持人决定。</p>` : ""}
    </section>
    <section class="archive-sec" aria-labelledby="pb-dec"><h3 id="pb-dec">已定的事 · ${active.length}</h3>
      <ul class="decisions">${active.map(decision).join("") || '<li class="empty">还没有。群聊里定下来的事，Agent 会记在这里，你也可以自己记。</li>'}</ul>
      <form id="decision-form" class="stack"><label for="dec-text" class="sr-only">新决定</label>
        <input id="dec-text" required maxlength="2000" placeholder="记下一件定了的事">
        <label for="dec-reason" class="sr-only">理由</label><input id="dec-reason" maxlength="2000" placeholder="理由（可选）">
        <label for="dec-sup" class="sr-only">取代哪一条</label>
        <select id="dec-sup"><option value="">不取代旧的</option>${active.map((x) => `<option value="${esc(x.id)}">取代：${esc(x.text.slice(0, 40))}</option>`).join("")}</select>
        <button type="submit">记下</button></form>
      ${inactive.length ? `<details><summary>已取代 / 撤销 · ${inactive.length}</summary><ul class="decisions">${inactive.map(decision).join("")}</ul></details>` : ""}
    </section></div>`;
  const call = async (path, opts, msg = "") => {
    try { await api(path, opts); st.note = msg; st.bad = false; } catch (e) { st.note = e.message; st.bad = true; }
    document.activeElement?.blur();
    await reload(); renderProjectPage(true);
  };
  const gid = d.id;
  $("#brief-form", box)?.addEventListener("submit", (e) => { e.preventDefault(); call(`/api/projects/${gid}/brief`, { method: "PUT", body: { text: $("#brief-text").value } }, "纲要已保存"); });
  $("#brief-ok", box)?.addEventListener("click", () => call(`/api/projects/${gid}/brief/confirm`, { method: "POST", body: {} }));
  $("#suggest-form", box)?.addEventListener("submit", (e) => { e.preventDefault();
    call(`/api/projects/${gid}/suggestions`, { method: "POST", body: { text: $("#sug-text").value, reason: $("#sug-reason").value } }, "已提交给主持人"); });
  box.querySelectorAll("[data-sug]").forEach((b) => b.addEventListener("click", () =>
    call(`/api/projects/${gid}/suggestions/${b.dataset.sug}`, { method: "POST", body: { accept: b.dataset.ok === "1" } })));
  $("#decision-form", box).addEventListener("submit", (e) => { e.preventDefault();
    call(`/api/projects/${gid}/decisions`, { method: "POST", body: { text: $("#dec-text").value, reason: $("#dec-reason").value, supersedes: $("#dec-sup").value || null } }, "已记下"); });
  box.querySelectorAll("[data-revoke]").forEach((b) => b.addEventListener("click", () => {
    if (confirm("撤销这一条？之后不再推送给成员。")) call(`/api/projects/${gid}/decisions/${b.dataset.revoke}`, { method: "DELETE" });
  }));
}

const assetNote = (st) => st.note ? `<p class="asset-note ${st.bad ? "bad" : ""}" role="status">${esc(st.note)}</p>` : "";

function renderFilesTab(box, d, st) {
  const host = d.is_host, who = assetWho(d);
  const types = [...new Set(d.files.map((f) => fileExt(f.name)))].sort();
  const total = d.files.reduce((n, f) => n + f.size, 0);
  const rows = () => d.files.filter((f) => (!st.type || fileExt(f.name) === st.type) && f.name.toLowerCase().includes(st.q.toLowerCase()))
    .sort((a, b) => (st.asc ? 1 : -1) * ASSET_SORT[st.by](a, b));
  const upBtn = (cls = "") => `<label class="button ${cls} file-up" title="文本文件（md、txt、csv、json…），单个不超过 ${ASSET_LIMIT / 1000} KB；也可以把文件拖进列表">
    ${ICON.upload}上传文件<input type="file" multiple hidden data-up></label>`;
  const body = () => {
    const list = rows();
    if (list.length) return list.map((f) => `<tr class="${st.fresh.includes(f.name) ? "fresh" : ""}">
      <td class="name"><div class="cell">${fileIcon(fileExt(f.name))}<button type="button" class="file-open" data-open="${esc(f.name)}" title="预览 ${esc(f.name)}">${esc(f.name)}</button></div></td>
      <td>${esc(fileExt(f.name))}</td><td>${esc(who(f))}</td>
      <td title="${esc(new Date(f.updated_at * 1000).toLocaleString())}">${dateTime(f.updated_at)}</td><td>${fmtSize(f.size)}</td>
      <td class="row-act"><a class="icon-btn" href="${fileUrl(d.id, f.name)}" download title="下载" aria-label="下载 ${esc(f.name)}">${ICON.down}</a>${host
        ? `<button type="button" class="icon-btn bad" data-rmasset="${esc(f.name)}" title="删除" aria-label="删除 ${esc(f.name)}">${ICON.trash}</button>` : ""}</td></tr>`).join("");
    if (d.files.length) return `<tr><td colspan="6" class="empty">没有匹配的文件 · <button type="button" class="link" data-clear>清除筛选</button></td></tr>`;
    return `<tr><td colspan="6" class="empty"><div class="empty-state">${fileIcon("")}<b>还没有文件</b>
      <span>在群聊里让 Agent 把产出发布到项目${host ? "，或者把文件拖到这里" : ""}。发布进来的文件所有成员都能看到。</span>${host ? upBtn() : ""}</div></td></tr>`;
  };
  const th = (key, label) => `<th aria-sort="${st.by === key ? (st.asc ? "ascending" : "descending") : "none"}"><button type="button" class="sort" data-sort="${key}">
    ${label}<span aria-hidden="true">${st.by === key ? (st.asc ? "↑" : "↓") : "↕"}</span></button></th>`;
  box.innerHTML = `<p class="muted tab-intro">这个项目的材料：文件存在这里，链接指向别处。成员 Agent 每一轮都拿得到文件的只读副本，
      链接作为背景告诉它们${d.shared ? "；共享项目里所有参与者都看得到" : ""}。</p>
    <h3 class="sec-h">文件 · ${d.files.length}</h3>
    <div class="asset-bar">
      ${host && d.files.length ? upBtn("ghost") : ""}
      <span class="muted">${d.files.length} 个文件 · ${fmtSize(total)}</span>
      <span class="grow"></span>
      ${d.files.length > 1 || st.q || st.type ? `<label class="sr-only" for="asset-type">文件类型</label>
        <select id="asset-type"><option value="">全部类型</option>${types.map((t) => `<option ${t === st.type ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>
        <label class="sr-only" for="asset-q">搜索文件</label><input id="asset-q" type="search" placeholder="搜索文件名" value="${esc(st.q)}">` : ""}
    </div>
    ${assetNote(st)}
    <div class="table-wrap"><table class="assets"><thead><tr>${th("name", "名称")}<th>类型</th><th>更新人</th>${th("updated_at", "更新时间")}${th("size", "大小")}
      <th><span class="sr-only">操作</span></th></tr></thead><tbody>${body()}</tbody></table>
      ${host ? `<div class="drop-mask" aria-hidden="true">${ICON.upload}松开，上传到「${esc(d.name)}」</div>` : ""}</div>
    <section class="links-sec" id="pp-links" aria-labelledby="pp-links-h"></section>`;
  renderLinks($("#pp-links", box), d, st);
  st.fresh = [];  // 刚上传的行只高亮这一次
  const bindRows = () => {
    box.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => previewAsset(d, d.files.find((f) => f.name === b.dataset.open))));
    box.querySelectorAll("[data-rmasset]").forEach((b) => b.addEventListener("click", async () => {
      const name = b.dataset.rmasset;
      if (!confirm(`删除 ${name}？成员之后看不到它，Agent 下一轮也拿不到。`)) return;
      try { await api(fileUrl(d.id, name), { method: "DELETE" }); st.note = `已删除 ${name}`; st.bad = false; }
      catch (e) { st.note = `删除失败：${e.message}`; st.bad = true; }
      await reload();
    }));
    box.querySelectorAll("[data-up]").forEach((inp) => inp.addEventListener("change", () => { uploadAssets(d, st, [...inp.files]); inp.value = ""; }));
    $("[data-clear]", box)?.addEventListener("click", () => {
      st.q = ""; st.type = ""; $("#asset-q", box).value = ""; $("#asset-type", box).value = ""; refresh();
    });
  };
  const refresh = () => { $("tbody", box).innerHTML = body(); bindRows(); };
  $("#asset-q", box)?.addEventListener("input", (e) => { st.q = e.target.value; refresh(); });
  $("#asset-type", box)?.addEventListener("change", (e) => { st.type = e.target.value; refresh(); });
  box.querySelectorAll("[data-sort]").forEach((b) => b.addEventListener("click", () => {
    st.asc = st.by === b.dataset.sort ? !st.asc : b.dataset.sort === "name"; st.by = b.dataset.sort;
    renderProjectPage(true).then(() => $(`[data-sort="${st.by}"]`)?.focus());
  }));
  if (host) {
    // 拖文件进列表上传：dragleave 在子元素之间也会触发，按计数判断是否真的离开
    const wrap = $(".table-wrap", box);
    let depth = 0;
    const has = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
    wrap.addEventListener("dragenter", (e) => { if (!has(e)) return; e.preventDefault(); depth++; wrap.classList.add("dropping"); });
    wrap.addEventListener("dragover", (e) => { if (has(e)) e.preventDefault(); });
    wrap.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; wrap.classList.remove("dropping"); } });
    wrap.addEventListener("drop", (e) => {
      e.preventDefault(); depth = 0; wrap.classList.remove("dropping");
      if (e.dataTransfer.files.length) uploadAssets(d, st, [...e.dataTransfer.files]);
    });
  }
  bindRows();
}

async function uploadAssets(d, st, files) {
  if (!files.length) return;
  const dup = files.filter((f) => d.files.some((x) => x.name === f.name)).map((f) => f.name);
  if (dup.length && !confirm(`已经有同名文件：${dup.join("、")}。覆盖它们？`)) return;
  const ok = [], bad = [];
  const bar = $("#project-page .asset-bar");
  bar?.insertAdjacentHTML("afterend", `<p class="asset-note" role="status">正在上传 ${files.length} 个文件…</p>`);
  for (const file of files) {
    if (!isTextFile(file)) { bad.push(`${file.name}（只支持文本文件）`); continue; }
    if (file.size > ASSET_LIMIT) { bad.push(`${file.name}（超过 ${ASSET_LIMIT / 1000} KB）`); continue; }
    try { await api(`/api/projects/${d.id}/files`, { method: "POST", body: { name: file.name, content: await file.text() } }); ok.push(file.name); }
    catch (e) { bad.push(`${file.name}（${e.message}）`); }
  }
  st.note = [ok.length ? `已上传 ${ok.join("、")}` : "", bad.length ? `没有上传：${bad.join("、")}` : ""].filter(Boolean).join("；");
  st.bad = !ok.length; st.fresh = ok;
  $("#project-page").dataset.sig = "";  // 结果文字可能和上次一样：强制重绘，换掉“正在上传”
  await reload();
}

// 资料里的链接：仓库、路径、文档。只是指针，不授予访问权限；认得出服务的标上用哪个连接器打开（在连接器面板里看谁能打开）
function renderLinks(box, d, st) {
  const host = d.is_host;
  const service = {};
  d.connector_panel.services.forEach((x) => x.sources.forEach((src) => { if (src.kind === "ref") service[src.ref_id] = x.name; }));
  const loc = (x) => /^https?:\/\//i.test(x.location)
    ? `<a href="${esc(x.location)}" target="_blank" rel="noopener noreferrer">${esc(x.location)}</a>` : `<code>${esc(x.location)}</code>`;
  const via = (x) => service[x.id] ? ` <a class="tag" href="#project-${esc(d.id)}" data-pagetab="connectors" title="在连接器面板里看谁能打开">${esc(service[x.id])}</a>` : "";
  box.innerHTML = `<h3 class="sec-h" id="pp-links-h">链接 · ${d.refs.length}</h3>
    <div class="table-wrap"><table class="assets"><thead><tr><th>名称</th><th>位置</th><th>说明</th><th><span class="sr-only">操作</span></th></tr></thead><tbody>
      ${d.refs.map((x) => `<tr><td class="name"><div class="cell"><span class="file-ico ref">${ICON.link}</span><span>${esc(x.name)}</span></div></td><td class="loc">${loc(x)}${via(x)}</td>
        <td class="ref-note">${esc(x.note) || '<span class="muted">—</span>'}</td>
        <td class="row-act"><button type="button" class="icon-btn" data-copy="${esc(x.location)}" title="复制位置" aria-label="复制 ${esc(x.name)} 的位置">${ICON.copy}</button>${host
          ? `<button type="button" class="icon-btn bad" data-rmref="${esc(x.id)}" title="移除" aria-label="移除 ${esc(x.name)}">${ICON.trash}</button>` : ""}</td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">还没有链接${host ? "，在下面添加" : "，由主持人添加"}。</td></tr>`}
    </tbody></table></div>
    ${host ? `<form class="ref-add" id="pp-ref-form" aria-label="添加链接">
      <label class="sr-only" for="pr-name">名称</label><input id="pr-name" required maxlength="200" placeholder="名称，如：前端仓库">
      <label class="sr-only" for="pr-loc">位置</label><input id="pr-loc" required maxlength="1000" placeholder="路径或 URL">
      <label class="sr-only" for="pr-note">说明</label><input id="pr-note" maxlength="500" placeholder="说明（可选）">
      <button type="submit">添加</button></form>` : ""}`;
  const done = async (path, opts, msg) => {
    try { await api(path, opts); st.note = msg; st.bad = false; } catch (e) { st.note = e.message; st.bad = true; }
    document.activeElement?.blur();
    await reload();
  };
  $("#pp-ref-form", box)?.addEventListener("submit", (e) => {
    e.preventDefault();
    done(`/api/projects/${d.id}/refs`, { method: "POST", body: { name: $("#pr-name").value, location: $("#pr-loc").value, note: $("#pr-note").value } },
      `已添加 ${$("#pr-name").value}`);
  });
  box.querySelectorAll("[data-rmref]").forEach((b) => b.addEventListener("click", () => {
    const x = d.refs.find((r) => r.id === b.dataset.rmref);
    if (confirm(`移除链接「${x?.name}」？`)) done(`/api/projects/${d.id}/refs/${b.dataset.rmref}`, { method: "DELETE" }, `已移除 ${x?.name}`);
  }));
  box.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); b.title = "已复制"; b.classList.add("ok"); setTimeout(() => { b.title = "复制位置"; b.classList.remove("ok"); }, 1200); }
    catch { prompt("复制这个位置：", b.dataset.copy); }
  }));
  box.querySelectorAll("[data-pagetab]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault(); e.stopPropagation(); st.tab = a.dataset.pagetab; st.note = ""; renderProjectPage(true);
  }));
}

// 项目技能：档案的一部分（不变量 17）。主持人写或批准提议，所有参与者能读全文；Agent 每轮在工作目录的 skills/ 里拿到只读副本
function renderSkillsTab(box, d, st) {
  const host = d.is_host, list = d.skill_list || [];
  const pend = d.suggestions.filter((x) => x.kind === "skill" && x.status === "pending");
  box.innerHTML = `<p class="muted tab-intro">这个项目里大家约定的做法。成员 Agent 每一轮都会拿到，和它们自己技能库里的重名时用项目的。${host ? "" : "由主持人维护；Agent 可以提议，主持人批准后才生效。"}</p>
    ${assetNote(st)}
    ${pend.length ? `<section class="pending-skills" aria-label="等主持人决定的技能提议"><h3>提议 · ${pend.length}</h3><ul class="suggestions">${pend.map((x) => `<li>
      <div class="who"><b>${esc(x.by_agent ? aname(x.by_agent) : d.participant_emails?.[x.by_user] || "")}</b> 提议新增技能 <code>${esc(x.skill_name)}</code></div>
      <p>${esc(x.skill_description)}</p>${x.reason ? `<p class="muted">${esc(x.reason)}</p>` : ""}
      <details><summary>做法</summary><div class="md">${renderMarkdown(x.text)}</div></details>
      ${host ? `<div class="actions"><button type="button" data-sug="${esc(x.id)}" data-ok="1">批准</button><button type="button" class="ghost" data-sug="${esc(x.id)}" data-ok="0">不批准</button></div>` : ""}
      </li>`).join("")}</ul></section>` : ""}
    <div class="table-wrap"><table class="assets"><thead><tr><th>名字</th><th>什么时候用</th><th>作者</th><th>更新</th></tr></thead><tbody>
      ${list.map((k) => `<tr class="${st.skill === k.name ? "selected" : ""}"><td class="name"><div class="cell"><span class="file-ico ref">${ICON.skill}</span>
        <button type="button" class="link" data-skill="${esc(k.name)}">${esc(k.name)}</button></div></td>
        <td>${esc(k.description)}</td><td>${esc(k.author || "—")}</td><td class="muted">${dateTime(k.updated_at)}</td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">还没有项目技能${host ? "，上传一个技能包" : ""}。</td></tr>`}
    </tbody></table></div>
    ${host ? '<div class="actions"><button type="button" class="ghost" id="ps-new">上传技能（zip）</button><button type="button" class="ghost" id="ps-new-dir">上传文件夹</button></div>' : ""}
    <div id="ps-detail"></div>`;
  const base = `/api/projects/${d.id}/skills`;
  const done = async (what, name) => {
    st.note = what === "deleted" ? `已删除 ${name}` : what === "file" ? `已添加文件` : `已保存 ${name}`; st.bad = false;
    st.skillNew = false; st.skill = what === "deleted" ? null : name;
    await reload(); renderProjectPage(true);
  };
  box.querySelectorAll("[data-skill]").forEach((b) => b.addEventListener("click", () => {
    st.skill = st.skill === b.dataset.skill ? null : b.dataset.skill; st.skillNew = false; st.note = ""; renderProjectPage(true);
  }));
  const up = (folder) => pickSkillPackage(base, folder).then((x) => x && done("saved", x.name))
    .catch((e) => { st.note = e.message; st.bad = true; renderProjectPage(true); });
  $("#ps-new", box)?.addEventListener("click", () => up(false));
  $("#ps-new-dir", box)?.addEventListener("click", () => up(true));
  box.querySelectorAll("[data-sug]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/api/projects/${d.id}/suggestions/${b.dataset.sug}`, { method: "POST", body: { accept: b.dataset.ok === "1" } }); st.note = ""; }
    catch (e) { st.note = e.message; st.bad = true; }
    await reload(); renderProjectPage(true);
  }));
  const detail = $("#ps-detail", box);
  const x = st.skill && list.find((k) => k.name === st.skill);
  if (x) {
    const by = x.by_agent ? `${aname(x.by_agent)} 提议，${d.participant_emails?.[x.approved_by] || "主持人"} 批准` : x.author || "";
    detail.innerHTML = skillCardHtml(x, esc(by), host);
    $("#sk-del", detail)?.addEventListener("click", async () => {
      if (!confirm(`删除项目技能「${x.name}」？`)) return;
      try { await api(`${base}/${encodeURIComponent(x.name)}`, { method: "DELETE" }); } catch (e) { $(".error", detail).textContent = e.message; return; }
      await done("deleted", x.name);
    });
  }
}

// 定时触发给人看的说法：常见写法换成“每天 / 每周几 / 每月几号 几点”，其余原样显示
function cronText(expr) {
  const [mi, h, dom, mon, dow] = String(expr || "").trim().split(/\s+/);
  const at = /^\d+$/.test(mi) && /^\d+$/.test(h) ? `${h.padStart(2, "0")}:${mi.padStart(2, "0")}` : null;
  const week = "日一二三四五六日";
  if (at && mon === "*") {
    if (dom === "*" && dow === "*") return `每天 ${at}`;
    if (dom === "*" && /^[0-7](,[0-7])*$/.test(dow)) return `每周${dow.split(",").map((d) => week[+d]).join("、")} ${at}`;
    if (dom === "*" && dow === "1-5") return `工作日 ${at}`;
    if (/^\d+$/.test(dom) && dow === "*") return `每月 ${dom} 号 ${at}`;
  }
  return `定时「${expr}」`;
}

// 连接器面板：这个项目用到的外部服务，从四处汇总——资料里的链接（按域名认，或主持人指定）、插件声明的、实际调用过的、
// 主持人手动添加的。项目不持有任何人的授权：就绪状态按成员给，不给是谁的账号；“你的授权”只有你自己看得到（不变量 19）
const READY_CN = { ready: ["ok", "能用"], ask: ["warn", "用到时问所有者"], not_enabled: ["", "没开放"], unauthorized: ["bad", "还没连"] };
const GRANT_CN = { ask: "用到时问我", always: "这个项目里都可以", deny: "不行" };
function connSources(x, d) {
  return x.sources.map((src) => src.kind === "ref" ? `<span class="tag">资料「${esc(src.name)}」${src.assigned ? "（指定）" : ""}</span>`
    : src.kind === "plugin" ? `<span class="tag">插件「${esc(src.title)}」</span>`
    : src.kind === "manual" ? `<span class="tag">手动添加${src.purpose ? `：${esc(src.purpose)}` : ""}${d.is_host
      ? `<button type="button" class="x" data-rmneed="${esc(src.need_id)}" title="去掉" aria-label="去掉手动添加的 ${esc(x.name)}">×</button>` : ""}</span>`
    : `<span class="tag">用过 ${src.count} 次${src.last_at ? ` · 最近 ${dateTime(src.last_at)}` : ""}${src.last_agent ? `（${esc(src.last_agent)}）` : ""}${d.is_host
      ? `<button type="button" class="x" data-rmusage="${esc(x.connector_id)}" title="收起这条记录" aria-label="收起 ${esc(x.name)} 的使用记录">×</button>` : ""}</span>`).join("");
}

function renderConnectorsTab(box, d, st) {
  const host = d.is_host, { services, unrecognized } = d.connector_panel;
  const catalogOpts = (sel = "") => (S.catalog || []).map((k) => `<option value="${esc(k.id)}" ${k.id === sel ? "selected" : ""}>${esc(k.name)}</option>`).join("");
  const memberRow = (m) => {
    const [cls, t] = READY_CN[m.status] || ["", m.status], mine = agent(m.agent_id)?.mine;
    const text = m.status === "not_enabled" ? (d.shared ? "没勾给它，或所有者在这里选了不行" : "授权了，没勾给它")
      : m.status === "ask" && mine ? "用到时问你" : t;
    return `<li><span class="r-dot ${cls}" aria-hidden="true"></span>${esc(m.agent)}<span class="muted">${mine ? "你的" : `${esc(m.owner || "")} 的`} · ${text}</span>
      ${mine && m.status === "unauthorized" ? '<a href="#connectors">去连接</a>' : ""}${mine && m.status === "not_enabled" ? '<a href="#connectors">勾给它</a>' : ""}</li>`;
  };
  // 电脑上的服务网络拦不到调用：没有“用到时问”，没设就是不给
  const grantRow = (g) => `<li><label for="gr-${esc(g.agent_id)}-${esc(g.connection_id)}">${esc(g.agent)} 用你的「${esc(g.connection)}」</label>
    <select id="gr-${esc(g.agent_id)}-${esc(g.connection_id)}" data-grant="${esc(g.agent_id)}" data-conn="${esc(g.connection_id)}">${
      (g.cloud ? ["ask", "always", "deny"] : ["always", "deny"]).map((k) => `<option value="${k}" ${(g.grant === k || (!g.cloud && g.grant === "ask" && k === "deny")) ? "selected" : ""}>${GRANT_CN[k]}</option>`).join("")}</select>
    ${g.cloud ? "" : '<span class="muted">电脑上的服务，不能用到时再问</span>'}</li>`;
  box.innerHTML = `<p class="muted tab-intro">这个项目用到的外部服务：从资料里的链接、插件、实际调用和手动添加汇总到这里。项目不持有任何人的授权，
      每个成员的 Agent 用的是它所有者自己连接的${d.shared ? "；别人让你的 Agent 用你的服务时，第一次会先问你" : ""}。要用别人的服务，就委托他的 Agent。</p>
    ${assetNote(st)}
    <ul class="needs">${services.map((x) => `<li><div class="need-head"><span class="ability-ico">${ICON.plug}</span><b>${esc(x.name)}</b></div>
      <div class="conn-src"><span class="muted">用在</span>${connSources(x, d)}</div>
      <ul class="ready">${x.members.map(memberRow).join("") || '<li class="muted">项目里还没有成员</li>'}</ul>
      ${x.mine.length ? `<div class="grants"><h4>你的授权<span class="muted"> · 只有你看得到</span></h4><ul>${x.mine.map(grantRow).join("")}</ul></div>` : ""}
      </li>`).join("")
      || `<li class="empty">还没有用到外部服务。贴进资料的链接认得出服务时会自动出现在这里${host ? "，也可以在下面手动添加" : ""}。</li>`}</ul>
    ${unrecognized.length ? `<section class="unrecognized" aria-labelledby="pc-unk"><h3 id="pc-unk">认不出服务的链接 · ${unrecognized.length}</h3>
      <ul>${unrecognized.map((u) => `<li><span class="file-ico ref">${ICON.link}</span><b>${esc(u.name)}</b><code>${esc(u.location)}</code>${host
        ? `<label class="sr-only" for="pc-map-${esc(u.ref_id)}">用哪个服务打开</label><select id="pc-map-${esc(u.ref_id)}"><option value="">用哪个服务打开…</option>${catalogOpts()}</select>
           <button type="button" class="ghost sm" data-map="${esc(u.ref_id)}">指定</button>` : ""}</li>`).join("")}</ul>
      <p class="hint">多数网页不需要连接器，不用管；要登录才能看的（公司文档、内部系统）指定一下，大家就知道谁打得开。</p></section>` : ""}
    ${host ? `<form class="ref-add" id="pp-need-form" aria-label="手动添加连接器">
      <label class="sr-only" for="pn-conn">连接器</label>
      <select id="pn-conn">${catalogOpts()}<option value="">其他（电脑上配好的服务）…</option></select>
      <label class="sr-only" for="pn-other">服务名</label><input id="pn-other" maxlength="80" placeholder="服务名，如 notion" hidden>
      <label class="sr-only" for="pn-purpose">用来做什么</label><input id="pn-purpose" maxlength="300" placeholder="用来做什么（可选）">
      <button type="submit">手动添加</button></form>` : ""}`;
  const done = async (path, opts, msg) => {
    try { await api(path, opts); st.note = msg; st.bad = false; } catch (e) { st.note = e.message; st.bad = true; }
    document.activeElement?.blur();
    await reload(); renderProjectPage(true);
  };
  const sel = $("#pn-conn", box), other = $("#pn-other", box);
  const sync = () => { if (other) { other.hidden = !!sel.value; other.required = !sel.value; } };
  sel?.addEventListener("change", sync); sync();
  $("#pp-need-form", box)?.addEventListener("submit", (e) => {
    e.preventDefault();
    done(`/api/projects/${d.id}/needs`, { method: "POST", body: { connector_id: sel.value || other.value.trim(), purpose: $("#pn-purpose").value } }, "已添加");
  });
  box.querySelectorAll("[data-rmneed]").forEach((b) => b.addEventListener("click", () => {
    if (confirm("去掉手动添加的这一项？资料、插件或调用记录里还用到它的话，这一行会留着。"))
      done(`/api/projects/${d.id}/needs/${b.dataset.rmneed}`, { method: "DELETE" }, "已去掉");
  }));
  box.querySelectorAll("[data-rmusage]").forEach((b) => b.addEventListener("click", () =>
    done(`/api/projects/${d.id}/connector-usage/${encodeURIComponent(b.dataset.rmusage)}`, { method: "DELETE" }, "已收起使用记录")));
  box.querySelectorAll("[data-map]").forEach((b) => b.addEventListener("click", () => {
    const v = $(`#pc-map-${CSS.escape(b.dataset.map)}`, box).value;
    if (v) done(`/api/projects/${d.id}/refs/${b.dataset.map}/connector`, { method: "PUT", body: { connector_id: v } }, "已指定");
  }));
  box.querySelectorAll("[data-grant]").forEach((x) => x.addEventListener("change", () => {
    if (x.value === "always" && !confirm("选“这个项目里都可以”：别的参与者 @ 它时，它会直接用你连接的这个服务、读你账号里的数据，回复别人也看得到。确定？")) {
      $("#project-page").dataset.sig = ""; renderProjectPage(true); return;
    }
    done(`/api/projects/${d.id}/grants`, { method: "PUT", body: { agent_id: x.dataset.grant, connection_id: x.dataset.conn, grant: x.value } },
      `已改成“${GRANT_CN[x.value]}”`);
  }));
}

// ---- 项目插件（docs/project-plugins.md）：插件页面跑在沙箱 iframe 里（不透明来源、没有登录 cookie、不能联网），
// 读写只经这里的桥转给 /bridge，由网络按插件申请的能力与看的人的可见性执行；以人名义发群聊要在确认卡上点“发送”（不变量 20）。
const TRIGGER_CN = { webhook: "外部回调", schedule: "定时", record: "记录变化" };
const triggerWhen = (t, p) => {
  const tb = (p.tables || []).find((x) => x.key === t.table);
  const table = tb?.title || t.table, field = tb?.fields.find((f) => f.key === t.when?.field)?.label || t.when?.field;
  return t.type === "webhook" ? `外部系统调用回调地址${t.table ? `，新增一条「${table}」` : ""}`
    : t.type === "schedule" ? cronText(t.cron)
    : t.when?.event === "create" ? `「${table}」新增记录` : `「${table}」的${field}变成「${t.when?.equals}」`;
};

function renderPluginsTab(box, d, st) {
  const host = d.is_host, list = d.plugin_list || [];
  if (S.pluginCatalog === undefined) {
    S.pluginCatalog = null;
    api("/api/plugins").then((c) => { S.pluginCatalog = c; renderProjectPage(true); }).catch(() => { S.pluginCatalog = []; });
  }
  const installed = new Set(list.map((p) => p.name));
  const catalog = (S.pluginCatalog || []).filter((p) => !installed.has(p.name));
  const caps = (p) => (p.page_permissions || []).map((c) => `<span class="pill plugin" title="${esc(c.key)}">${esc(c.label)}</span>`).join("")
    || '<span class="muted">不读写任何数据</span>';
  const trig = (p, t) => {
    const s = p.trigger_state?.[t.key] || {};
    const state = s.paused ? `<span class="pill bad" title="${esc(s.error || "")}">已暂停：连续失败</span>`
      : s.enabled ? `<span class="pill ok">${esc(s.by || "")} 启用</span>` : '<span class="pill">未启用</span>';
    const hook = t.type === "webhook" ? (st.hook?.key === `${p.name}:${t.key}`
      ? `<div class="hook-url"><code>${esc(location.origin + st.hook.path)}</code><button type="button" class="icon-btn" data-copy-hook="${esc(location.origin + st.hook.path)}" title="复制">${ICON.copy}</button>
         <span class="muted">只显示这一次：现在复制，配到外部系统里。</span></div>`
      : `<div class="muted">${s.has_hook ? "回调地址已生成（不再显示）" : "还没有回调地址"}${host ? ` · <button type="button" class="link" data-hook="${esc(p.name)}:${esc(t.key)}">${s.has_hook ? "重置地址" : "生成地址"}</button>` : ""}</div>`) : "";
    return `<li class="trig"><span class="pill">${TRIGGER_CN[t.type]}</span>
      <div class="trig-main">${esc(triggerWhen(t, p))} → <b>@${esc(t.to)}</b>“${esc(t.text)}”${hook}
        ${s.error && !s.paused ? `<div class="error">上次失败：${esc(s.error)}</div>` : ""}</div>
      ${state}${host ? `<button type="button" class="ghost sm" data-trig="${esc(p.name)}:${esc(t.key)}" data-on="${s.enabled && !s.paused ? 0 : 1}">${s.enabled && !s.paused ? "停用" : s.paused ? "重新启用" : "启用"}</button>` : ""}</li>`;
  };
  box.innerHTML = `<p class="muted tab-intro">插件给项目加上一个业务场景：自己的页面、记录、打法和触发。页面代码只在每个人的浏览器沙箱里跑，读写都经网络、按看的人的权限；
      触发只往群聊发一条 @ 成员的消息，代表启用它的人，之后照常由 Agent 自己做或委托。${d.shared ? "共享项目里插件的记录对所有参与方可见。" : ""}${host ? "" : "插件由主持人安装。"}</p>
    ${assetNote(st)}
    <ul class="plugins">${list.map((p) => p.broken ? `<li class="plugin-card${p.enabled === false ? " off" : ""}"><b>${esc(p.title)}</b><p class="error">${esc(p.broken)}</p></li>` : `<li class="plugin-card${p.enabled ? "" : " off"}">
      <div class="plugin-head"><span class="plugin-ico" aria-hidden="true">${esc((p.title || p.name).slice(0, 1))}</span>
        <div><b>${esc(p.title)}</b> <span class="muted">${esc(p.version)} · <code>${esc(p.name)}</code></span>${p.enabled ? "" : ' <span class="pill warn">已停用</span>'}
          <div class="muted">${esc(p.description)}</div>
          <div class="muted small">${p.source === "catalog" ? "来自部署方目录" : "上传的插件包"} · ${esc(p.installed_by || "")} 安装${p.installed_at ? ` · ${dateTime(p.installed_at)}` : ""}</div></div>
        <span class="grow"></span>
        ${p.update && host ? `<button type="button" class="sm" data-upgrade="${esc(p.name)}">升级到 ${esc(p.update)}</button>` : p.update ? `<span class="pill warn">有新版本 ${esc(p.update)}</span>` : ""}
        ${p.enabled ? (p.tabs || []).map((t) => `<button type="button" class="ghost sm" data-goto-tab="plugin:${esc(p.name)}:${esc(t.key)}">打开「${esc(t.label)}」</button>`).join("") : ""}
        ${host ? `<label class="switch plugin-switch" title="${p.enabled ? "停用后页面、记录工具、打法和触发都暂停；记录与设置保留" : "重新启用，恢复页面、记录工具、打法和触发"}">
          <input type="checkbox" role="switch" data-enable="${esc(p.name)}" ${p.enabled ? "checked" : ""} aria-label="启用插件「${esc(p.title)}」">${p.enabled ? "已启用" : "已停用"}</label>` : ""}
        ${host ? `<button type="button" class="icon-btn bad" data-uninstall="${esc(p.name)}" title="卸载" aria-label="卸载 ${esc(p.title)}">${ICON.trash}</button>` : ""}</div>
      <dl class="plugin-facts">
        <dt>页面能做什么</dt><dd>${caps(p)}</dd>
        <dt>Agent 能做什么</dt><dd>${(p.agent_tools || []).length ? [p.agent_tools.includes("records.read") && "查记录", p.agent_tools.includes("records.write") && "建 / 改记录（Agent 改的不引发“记录变化”触发）"].filter(Boolean).join("、") : '<span class="muted">不开放记录工具</span>'}</dd>
        ${(p.tables || []).length ? `<dt>记录</dt><dd>${p.tables.map((t) => `${esc(t.title)} <span class="muted">${p.counts?.[t.key] ?? 0} 条</span>`).join(" · ")}</dd>` : ""}
      </dl>
      ${p.enabled ? "" : '<p class="muted small">插件停用中：页面、Agent 的记录工具、打法与触发都不可用，记录和下面的设置保留，重新启用即恢复。</p>'}
      ${(p.triggers || []).length ? `<h3>触发</h3><ul class="trigs">${p.triggers.map((t) => trig(p, t)).join("")}</ul>` : ""}
    </li>`).join("") || `<li class="empty">还没有装插件${host ? "：从下面的目录安装，或上传插件包" : ""}。</li>`}</ul>
    ${host ? `<h3>部署方目录</h3><ul class="plugins catalog">${S.pluginCatalog === null ? '<li class="muted">正在读取…</li>' : catalog.map((p) => `<li class="plugin-card">
        <div class="plugin-head"><span class="plugin-ico" aria-hidden="true">${esc((p.title || p.name).slice(0, 1))}</span>
          <div><b>${esc(p.title)}</b> <span class="muted">${esc(p.version)}</span><div class="muted">${esc(p.description)}</div>
          <div class="small">${caps(p)}</div></div><span class="grow"></span>
          <button type="button" class="sm" data-install="${esc(p.name)}">安装</button></div></li>`).join("")
        || '<li class="muted">目录里没有别的插件了。</li>'}</ul>
      <div class="actions"><button type="button" class="ghost" id="pl-up">上传插件包（zip）</button><button type="button" class="ghost" id="pl-up-dir">上传文件夹</button>
        <span class="muted small">装进项目的是这一版的拷贝；代码会在项目里每个人的浏览器沙箱里跑。</span></div>` : ""}`;
  const base = `/api/projects/${d.id}/plugins`;
  const done = async (fn, msg) => {
    try { await fn(); st.note = msg; st.bad = false; } catch (e) { st.note = e.message; st.bad = true; }
    await reload(); renderProjectPage(true);
  };
  const withConfirm = async (fn) => {  // 升级会删字段 / 改类型：先问，再带 confirm 重发
    try { return await fn(false); } catch (e) {
      if (e.status === 409 && e.message.startsWith("升级会改变") && confirm(e.message)) return fn(true);
      throw e;
    }
  };
  box.querySelectorAll("[data-install], [data-upgrade]").forEach((b) => b.addEventListener("click", () => {
    const name = b.dataset.install || b.dataset.upgrade;
    done(() => withConfirm((c) => api(base, { method: "POST", body: { name, confirm: c } })), b.dataset.install ? "已安装：触发默认不开，看过后逐个启用" : "已升级");
  }));
  box.querySelectorAll("[data-enable]").forEach((b) => b.addEventListener("change", () => {
    if (!b.checked && !confirm("停用这个插件？页面、Agent 的记录工具、打法和触发都会暂停，记录与设置保留。")) { b.checked = true; return; }
    done(() => api(`${base}/${encodeURIComponent(b.dataset.enable)}`, { method: "PUT", body: { enabled: b.checked } }),
      b.checked ? "已启用" : "已停用：记录与设置保留");
  }));
  box.querySelectorAll("[data-uninstall]").forEach((b) => b.addEventListener("click", () => {
    if (confirm("卸载这个插件？记录会保留，重新安装后恢复。")) done(() => api(`${base}/${encodeURIComponent(b.dataset.uninstall)}`, { method: "DELETE" }), "已卸载");
  }));
  box.querySelectorAll("[data-trig]").forEach((b) => b.addEventListener("click", () => {
    const [name, key] = b.dataset.trig.split(":");
    done(() => api(`${base}/${encodeURIComponent(name)}/triggers/${encodeURIComponent(key)}`, { method: "PUT", body: { enabled: b.dataset.on === "1" } }),
      b.dataset.on === "1" ? "已启用：之后它代表你发消息" : "已停用");
  }));
  box.querySelectorAll("[data-hook]").forEach((b) => b.addEventListener("click", async () => {
    const [name, key] = b.dataset.hook.split(":");
    if (b.textContent === "重置地址" && !confirm("重置后旧地址立即失效，确定？")) return;
    try { const r = await api(`${base}/${encodeURIComponent(name)}/triggers/${encodeURIComponent(key)}/hook`, { method: "POST" }); st.hook = { key: b.dataset.hook, path: r.path }; st.note = ""; }
    catch (e) { st.note = e.message; st.bad = true; }
    await reload(); renderProjectPage(true);
  }));
  box.querySelectorAll("[data-copy-hook]").forEach((b) => b.addEventListener("click", () => navigator.clipboard.writeText(b.dataset.copyHook).catch(() => prompt("复制回调地址：", b.dataset.copyHook))));
  box.querySelectorAll("[data-goto-tab]").forEach((b) => b.addEventListener("click", () => { st.tab = b.dataset.gotoTab; renderProjectPage(true); }));
  const up = (folder) => pickPluginPackage(base, folder).then((x) => x && done(async () => {}, `已安装「${x.title}」${x.version}：触发默认不开，看过后逐个启用`))
    .catch((e) => { st.note = e.message; st.bad = true; renderProjectPage(true); });
  $("#pl-up", box)?.addEventListener("click", () => up(false));
  $("#pl-up-dir", box)?.addEventListener("click", () => up(true));
}

function pickPluginPackage(base, folder) {
  return new Promise((resolve, reject) => {
    const inp = document.createElement("input");
    inp.type = "file";
    if (folder) inp.webkitdirectory = true; else inp.accept = ".zip,application/zip";
    inp.addEventListener("change", async () => {
      const files = [...inp.files];
      if (!files.length) return resolve(null);
      try {
        let body, headers;
        if (folder) {
          const enc = async (f) => { const b = new Uint8Array(await f.arrayBuffer()); let t = ""; for (let i = 0; i < b.length; i += 0x8000) t += String.fromCharCode(...b.subarray(i, i + 0x8000)); return btoa(t); };
          body = { files: await Promise.all(files.map(async (f) => ({ path: f.webkitRelativePath || f.name, data: await enc(f) }))) };
        } else { body = files[0]; headers = { "Content-Type": "application/zip" }; }
        const send = (c) => api(`${base}/upload${c ? "?confirm=true" : ""}`, { method: "POST", body, headers });
        try { resolve(await send(false)); } catch (e) {
          if (e.status === 409 && e.message.startsWith("升级会改变") && confirm(e.message)) resolve(await send(true)); else throw e;
        }
      } catch (e) { reject(e); }
    });
    inp.click();
  });
}

// 插件标签：框上写明这是谁装的、哪一版、申请了什么，框里是插件自己的代码
async function renderPluginFrame(box, d, st) {
  const [, name, key] = st.tab.split(":");
  const p = (d.plugin_list || []).find((x) => x.name === name), t = p?.tabs.find((x) => x.key === key);
  if (!p || !t) return;
  box.innerHTML = `<div class="sandbox"><div class="sandbox-bar"><span class="lock">插件页面</span>
      <span>${esc(p.title)} <code>${esc(p.name)} ${esc(p.version)}</code> · ${esc(p.installed_by || "")} 安装 · 代码在沙箱里运行，读写按你的权限</span>
      <span class="grow"></span>${(p.page_permissions || []).map((c) => `<span class="pill plugin">${esc(c.label)}</span>`).join("")}</div>
    <p class="muted plugin-loading">正在加载…</p></div>`;
  let src;
  try { src = (await api(`/api/projects/${d.id}/plugins/${encodeURIComponent(name)}/frame`, { method: "POST", body: { tab: key } })).src; }
  catch (e) { $(".plugin-loading", box).textContent = e.message; return; }
  if (S.assets?.tab !== st.tab || !box.isConnected) return;
  const frame = Object.assign(document.createElement("iframe"), { title: `插件「${p.title}」：${t.label}`, src, className: "plugin-frame" });
  frame.setAttribute("sandbox", "allow-scripts allow-forms");   // 不给 allow-same-origin：不透明来源，读不到父页面与登录 cookie；
  // allow-forms 只为让表单的 submit 事件照常触发，真正的提交被 CSP 的 form-action 'none' 挡下
  frame.setAttribute("referrerpolicy", "no-referrer");
  $(".plugin-loading", box).replaceWith(frame);
  S.pluginFrame = { el: frame, pid: d.id, name, key, title: p.title };
}

const pluginReply = (f, id, ok, value) => f.el.contentWindow?.postMessage({ matrix: 1, id, ok, ...(ok ? { result: value } : { error: value }) }, "*");

window.addEventListener("message", async (e) => {
  const f = S.pluginFrame;
  if (!f || !f.el.isConnected || e.source !== f.el.contentWindow) return;  // 只认当前这个插件 iframe 发来的
  const m = e.data || {};
  if (m.matrix !== 1 || typeof m.op !== "string" || typeof m.id !== "string") return;
  const args = m.args && typeof m.args === "object" ? m.args : {};
  try {
    if (m.op === "ui.badge") {  // 只影响这个插件自己的标签
      const text = String(args.text ?? "").slice(0, 12);
      (S.pluginBadges ||= {})[`${f.pid}:${f.name}:${f.key}`] = text;
      const el = document.querySelector(`[data-badge="${CSS.escape(`${f.name}:${f.key}`)}"]`);
      if (el) el.textContent = text;
      return pluginReply(f, m.id, true, { ok: true });
    }
    if (m.op === "ui.file") {
      const d = await api(`/api/projects/${f.pid}`);
      previewAsset(d, d.files.find((x) => x.name === String(args.name || "")));
      return pluginReply(f, m.id, true, { ok: true });
    }
    const bridge = (op, a) => api(`/api/projects/${f.pid}/plugins/${encodeURIComponent(f.name)}/bridge`, { method: "POST", body: { op, args: a } }).then((r) => r.result);
    if (m.op === "chat.post") {
      const draft = await bridge("chat.post", args);
      const sent = await confirmPluginPost(f, draft);
      return pluginReply(f, m.id, true, { sent });
    }
    const result = await bridge(m.op, args);
    if (m.op === "context") result.theme = document.documentElement.dataset.theme;
    pluginReply(f, m.id, true, result);
  } catch (err) { pluginReply(f, m.id, false, err.message || String(err)); }
});

// 确认卡由宿主页面画，插件改不了它的内容；只对这一条生效
function confirmPluginPost(f, draft) {
  return new Promise((resolve) => {
    let dlg = $("#plugin-confirm");
    if (!dlg) { dlg = Object.assign(document.createElement("dialog"), { id: "plugin-confirm", className: "preview confirm-card" }); document.body.append(dlg); }
    dlg.innerHTML = `<form method="dialog" class="preview-card" aria-labelledby="pc-title">
      <p class="muted"><span class="pill plugin">${esc(draft.title)}</span> 想以<b>你</b>的名义在群聊里发一条消息</p>
      <h2 id="pc-title">发到项目群聊</h2>
      <blockquote class="draft">${esc(draft.text)}</blockquote>
      <p class="muted small">${draft.mentions.length ? `会 @ ${draft.mentions.map(esc).join("、")}，被 @ 的 Agent 会被唤醒处理。` : "不 @ 任何成员，不会唤醒 Agent。"}
        ${draft.joint ? "共享项目里所有参与方都看得到。" : ""}只对这一条生效。</p>
      <p class="error" role="alert"></p>
      <div class="actions"><button type="button" class="ghost" value="cancel" data-no>取消</button><button type="button" data-yes>发送</button></div></form>`;
    let settled = false;
    const finish = (v) => { if (!settled) { settled = true; resolve(v); } if (dlg.open) dlg.close(); };
    $("[data-no]", dlg).addEventListener("click", () => finish(false));
    $("[data-yes]", dlg).addEventListener("click", async () => {
      try { await api(`/api/plugin-drafts/${draft.id}/send`, { method: "POST" }); finish(true); }
      catch (err) { $(".error", dlg).textContent = err.message; }
    });
    dlg.addEventListener("close", () => finish(false), { once: true });
    dlg.showModal();
    $("[data-yes]", dlg).focus();
  });
}

// 资产预览：弹层里转义后渲染；图片用 blob URL；其他二进制只给下载
function parseCsv(text, sep) {
  const rows = []; let row = [], cell = "", q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) { if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (c === '"') q = false; else cell += c; }
    else if (c === '"') q = true;
    else if (c === sep) { row.push(cell); cell = ""; }
    else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(cell); rows.push(row); row = []; cell = ""; }
    else cell += c;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows;
}

function previewHtml(ext, text) {
  if (text.includes("\u0000")) return '<p class="muted">这是二进制文件，不能在这里预览，下载后用本地应用打开。</p>';
  if (ext === "MD") return `<div class="md">${renderMarkdown(text)}</div>`;
  if (ext === "CSV" || ext === "TSV") {
    const rows = parseCsv(text, ext === "TSV" ? "\t" : ","), MAX = 500;
    if (rows.length) return `<div class="csv-wrap"><table class="csv"><thead><tr>${rows[0].map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${rows.slice(1, MAX + 1).map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
      ${rows.length > MAX + 1 ? `<p class="hint">只显示前 ${MAX} 行，共 ${rows.length - 1} 行。下载查看全部。</p>` : ""}`;
  }
  if (ext === "JSON") { try { text = JSON.stringify(JSON.parse(text), null, 2); } catch { /* 不是合法 JSON：按原文显示 */ } }
  return text.trim() ? `<pre class="code">${esc(text)}</pre>` : '<p class="muted">空文件。</p>';
}

async function previewAsset(d, f) {
  if (!f) return;
  let dlg = $("#asset-preview");
  if (!dlg) {
    dlg = Object.assign(document.createElement("dialog"), { id: "asset-preview", className: "preview" });
    document.body.append(dlg);
    // 点遮罩或“关闭”收起；Esc 由 <dialog> 自己处理
    dlg.addEventListener("click", (e) => { if (e.target === dlg || e.target.closest("[data-close]")) dlg.close(); });
    dlg.addEventListener("close", () => { if (dlg.dataset.blob) URL.revokeObjectURL(dlg.dataset.blob); dlg.dataset.blob = ""; dlg.dataset.name = ""; });
  }
  const ext = fileExt(f.name), url = fileUrl(d.id, f.name);
  dlg.dataset.name = f.name;
  dlg.innerHTML = `<div class="preview-card" aria-labelledby="pv-title"><header>${fileIcon(ext)}<div class="pv-title"><h2 id="pv-title">${esc(f.name)}</h2>
      <span class="muted">${fmtSize(f.size)} · ${esc(assetWho(d)(f))} · ${dateTime(f.updated_at)}</span></div>
      <a class="button ghost sm" href="${url}" download>${ICON.down}下载</a><button type="button" class="ghost sm" data-close>关闭</button></header>
    <div class="preview-body"><p class="muted">加载中…</p></div></div>`;
  if (!dlg.open) dlg.showModal();
  $("[data-close]", dlg).focus();
  const body = $(".preview-body", dlg), still = () => dlg.open && dlg.dataset.name === f.name;
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(r.status === 404 ? "文件已经不在了，可能刚被删除" : `加载失败（${r.status}）`);
    if (IMAGE_EXT.includes(ext)) {
      const u = URL.createObjectURL(await r.blob());
      if (!still()) { URL.revokeObjectURL(u); return; }
      dlg.dataset.blob = u;
      body.innerHTML = `<img src="${u}" alt="${esc(f.name)}">`;
      return;
    }
    const text = await r.text();
    if (still()) body.innerHTML = previewHtml(ext, text);
  } catch (e) { if (still()) body.innerHTML = `<p class="error">${esc(e.message)}</p>`; }
}

// 共享项目的参与者：主持人邀请、撤回、移出
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
    if (!d.shared && !confirm(`邀请以后，对方会看到这个项目已有的 ${d.chat_messages || 0} 条群聊消息、`
      + `${(d.files || []).length} 个文件、${(d.decisions || []).filter((x) => x.status === "active").length} 条决定。\n\n`
      + "你的 Agent 在这里带着技能和在这个项目里写的笔记；用到别处的资料时，会先问你。")) return;
    try { await api(`/api/projects/${g.id}/invitations`, { method: "POST", body: { email: $("#invite-email").value } }); await reload(); }
    catch (e2) { err(e2); }
  });
  box.querySelectorAll("[data-rm]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("确定？他的 Agent 会一并移出。")) return;
    try { await api(`/api/projects/${g.id}/participants/${b.dataset.rm}`, { method: "DELETE" }); await reload(); }
    catch (e2) { err(e2); }
  }));
}

// 参与者看别人主持的项目：加减自己的 Agent，退出
function renderProjectGuest(box, g) {
  const mine = S.snap.agents.filter((a) => a.mine);
  box.innerHTML = `<h2 class="panel-title">项目配置 ${kindTag(g)}</h2>
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

  const home = homeConv();
  const helper = agent(S.me?.assistant_id);
  $("#nav-home").hidden = !helper;
  $("#nav-home").setAttribute("aria-pressed", String(opened(home)));
  $("#nav-home").title = helper ? `${helper.name}：汇总各项目等你处理的事、起草、定时` : "";
  $("#home-dot").innerHTML = dot(home);
  $("#project-chat-list").innerHTML = S.snap.projects.map((g) => {
    const c = live.find((x) => x.kind === "project" && x.project_id === g.id);
    const icon = `<span class="avatar sm group" aria-hidden="true" style="background:${gcolor(g.id)}">#</span>`;
    // 自己在这个项目里的会话挂在项目下面（只有自己看得到）；发第一条消息之前不列出，打开着时项目这一行保持选中
    const sess = live.filter((x) => x.kind === "session" && x.project_id === g.id);
    const mine = sess.filter((x) => x.message_count).sort((a, b) => b.updated_at - a.updated_at);
    const blank = sess.some((x) => !x.message_count && opened(x));
    return item(c ? c.id : `p-${g.id}`, opened(c) || blank, icon, g.name,
      plain(c?.last_message) || `${g.member_ids.length} 名成员`, c)
      + mine.map((x) => item(x.id, opened(x), '<span class="avatar sm session" aria-hidden="true">›</span>', x.title,
        plain(x.last_message) || "还没有消息", x, "nested")).join("");
  }).join("") || '<li class="empty">还没有项目</li>';

  // 只列出聊过的私聊（以及当前打开的那个）；要和新的 Agent 私聊，在 Agent 卡片上点“对话”
  $("#dm-list").innerHTML = live.filter((c) => c.kind === "dm" && c !== home && (c.message_count || opened(c)))
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

// 侧栏顶部的“助理”：和当前助理的私聊（换了助理就跟着换）
const homeConv = () => S.snap?.conversations.find((c) => c.kind === "dm" && !c.archived && c.agent_id === S.me?.assistant_id);

// “更多”：少用的视图收在这里。点开是一列按钮（不是菜单语义：每一项就是切到那个视图）
function setupMore() {
  const wrap = $(".more-wrap"), btn = $("#nav-more"), menu = $("#more-menu");
  const items = () => [...menu.querySelectorAll("button")];
  const close = (focus) => {
    if (menu.hidden) return;
    menu.hidden = true; btn.setAttribute("aria-expanded", "false");
    if (focus) btn.focus();
  };
  const place = () => {  // 侧栏竖着时弹在右边，窄屏横着时弹在下面；不超出窗口
    const r = btn.getBoundingClientRect(), rail = $(".rail").getBoundingClientRect();
    const side = rail.height > rail.width;
    const left = side ? rail.right + 6 : r.left, top = side ? r.top : r.bottom + 6;
    menu.style.left = `${Math.max(8, Math.min(left, innerWidth - menu.offsetWidth - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(top, innerHeight - menu.offsetHeight - 8))}px`;
  };
  btn.addEventListener("click", () => {
    if (!menu.hidden) { close(); return; }
    menu.hidden = false; btn.setAttribute("aria-expanded", "true");
    place();
    (items().find((b) => b.getAttribute("aria-pressed") === "true") || items()[0]).focus();
  });
  menu.addEventListener("click", (e) => { if (e.target.closest("button")) close(); });
  document.addEventListener("click", (e) => { if (!wrap.contains(e.target)) close(); });
  wrap.addEventListener("focusout", (e) => { if (e.relatedTarget && !wrap.contains(e.relatedTarget)) close(); });
  wrap.addEventListener("keydown", (e) => {
    if (menu.hidden) return;
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); return; }
    const list = items(), i = list.indexOf(document.activeElement);
    const go = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: list.length - 1 }[e.key];
    if (go === undefined) return;
    e.preventDefault();
    list[(go + list.length) % list.length].focus();
  });
  window.addEventListener("resize", () => close());
}

function renderChats() {
  $("#view-chats").classList.remove("start");
  renderThread();
}

async function openConv(id) {
  if (S.convId !== id) S.taskSel = null;
  if (S.conv?.id !== id) $(".chat-side").classList.remove("open");  // 换对话就收起上一个对话的成员与档案
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

const TOOL_CN = { [`mcp__matrix_net__delegate`]: "委托", [`mcp__matrix_net__list_members`]: "查看成员",
  [`mcp__matrix_net__share_knowledge`]: "共享知识", [`mcp__matrix_net__list_tasks`]: "查看委托",
  [`mcp__matrix_net__accept_task`]: "验收", [`mcp__matrix_net__request_changes`]: "退回修改",
  [`mcp__matrix_net__retry_task`]: "续接委托", [`mcp__matrix_net__propose_agent`]: "提议新 Agent", web_search: "搜索", web_fetch: "抓取网页", read: "读取",
  write: "写入", edit: "编辑", bash: "执行命令", glob: "查找文件", grep: "搜索文件", todo_write: "更新计划" };
const toolName = (t) => TOOL_CN[t] || t.replace(/^mcp__matrix_net__/, "");
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
  root.querySelectorAll("form.proposal-card").forEach((f) => {
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
function sideTab(which, open = true) {
  S.sideTab = which;
  if (open) $(".chat-side").classList.add("open");
  $("#tab-members").setAttribute("aria-selected", String(which === "members"));
  $("#tab-archive").setAttribute("aria-selected", String(which === "archive"));
  $("#tab-task").setAttribute("aria-selected", String(which === "task"));
  $("#members-panel").hidden = which !== "members";
  $("#archive-panel").hidden = which !== "archive";
  $("#task-detail").hidden = which !== "task";
  if (which === "task") renderTask();
  if (which === "archive") renderArchive();
}

// ---------- 群聊旁的项目概览：纲要与已定的事（只读）+ 去项目页的入口 ----------
// 编辑都在项目页（说明 / 资料 / 技能 / 连接器），这里只给聊天时顺手看一眼。
async function renderArchive() {
  const box = $("#archive-panel"), c = S.conv;
  const has = !!c && (c.kind === "project" || c.kind === "session") && !!c.project_id && !c.archived && !!proj(c.project_id);
  $("#tab-archive").hidden = !has;
  if (!has && S.sideTab === "archive") { sideTab("members", false); return; }
  if (!has || box.hidden) return;
  const d = await api(`/api/projects/${c.project_id}`).catch(() => null);
  if (!d || S.conv?.project_id !== c.project_id) return;
  const active = d.decisions.filter((x) => x.status === "active").reverse();
  const pend = d.suggestions.filter((x) => x.status === "pending").length;
  const go = (tab, label, n) => `<li><a href="#project-${esc(d.id)}" data-pagetab="${tab}">${label}</a><span class="muted">${n}</span></li>`;
  box.innerHTML = `<section class="archive-sec" aria-labelledby="ar-brief"><h3 id="ar-brief">纲要
      <a class="h3-link" href="#project-${esc(d.id)}" data-pagetab="brief">${d.is_host ? "编辑" : "查看"}</a></h3>
      <div class="md brief">${d.brief ? renderMarkdown(d.brief) : '<span class="muted">还没写纲要。</span>'}</div>
      ${pend ? `<p class="hint">${pend} 条建议等${d.is_host ? "你" : "主持人"}决定，<a href="#project-${esc(d.id)}" data-pagetab="brief">去看看</a>。</p>` : ""}
    </section>
    <section class="archive-sec" aria-labelledby="ar-dec"><h3 id="ar-dec">已定的事 · ${active.length}</h3>
      <ul class="decisions">${active.slice(0, 8).map((x) => `<li><div>${esc(x.text)}</div></li>`).join("") || '<li class="empty">还没有</li>'}</ul>
      ${active.length > 8 ? `<a href="#project-${esc(d.id)}" data-pagetab="brief">全部 ${active.length} 条</a>` : ""}
    </section>
    <section class="archive-sec" aria-labelledby="ar-more"><h3 id="ar-more">项目页</h3>
      <ul class="files">${go("files", "资料", `${d.files.length} 个文件 · ${d.refs.length} 个链接`)}${go("skills", "技能", (d.skill_list || []).length)}${go("connectors", "连接器", d.connector_panel.services.length)}</ul>
    </section>`;
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
  if (c.kind === "dm" && c.is_home) {
    const a = agent(c.agent_id);
    box.innerHTML = `${a ? `<ul class="member-list">${row(a, "", "你的助理")}</ul>` : ""}
      <h3>定时</h3><ul class="member-list sched-list">${schedulesHtml()}</ul><p class="error sched-error" role="alert"></p>
      <p class="hint">定时只唤醒你自己的助理，到点后它在这里回复你；没有新情况就不打扰。在这里说一句就能建，比如“每个工作日早上 8 点给我早报”。</p>
      <p class="hint">助理看得到你参与的各个项目（只是你能看到的部分），往项目里说话要先起草、你确认后才以你的名义发出。
        共享项目里合作方写的内容送到的那一轮，它不用你授权的服务。</p>
      ${a ? `<dl><dt>自身权限</dt><dd>${tags(a.authority, "ok")}</dd><dt>模型</dt><dd>${modelDd(a)}</dd></dl>` : ""}
      ${workdirsHtml(c)}`;
    bindSchedules(box);
    bindWorkdirs(box, c);
    return;
  }
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
    const more = (c.members || []).filter((id) => id !== c.agent_id).map(agent).filter(Boolean);
    box.innerHTML = `${a ? `<ul class="member-list">${row(a, "", "你的助理")}</ul>` : placeholder("助理已删除。")}
      ${more.length ? `<h3>也能 @ · ${more.length}</h3><ul class="member-list">${more.map((x) => row(x)).join("")}</ul>
        <p class="hint">你在这个项目里的其他 Agent：@ 它就直接交给它，没 @ 时交给助理。别人的 Agent 看不到这里，要找它们去群聊。</p>` : ""}
      <p class="hint">项目会话只有你看得到。助理每一轮都会拿到「${esc(g.name)}」的纲要、决定、成员、没结束的任务和群聊里最近的消息，
        也能委托项目成员、登记任务；它写纲要、决定、文件时会在群聊里留一条事件。</p>
      <p><a href="#${esc(chat ? chat.id : `p-${g.id}`)}">回到项目群聊</a> · ${g.member_ids.length} 名成员</p>
      ${workdirsHtml(c)}`;
    bindWorkdirs(box, c);
    return;
  }
  const joint = g.shared, host = isHost(g);
  const members = g.member_ids.map(agent).filter(Boolean);
  const others = S.snap.agents.filter((a) => a.mine && !g.member_ids.includes(a.id));  // 只能加入自己的 Agent
  const rm = (a) => (a.mine || host) ? `<button type="button" class="ghost icon sm rm" data-id="${esc(a.id)}"
      aria-label="把 ${esc(a.name)} 移出项目" title="移出项目"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M4 8h8"/></svg></button>` : "";
  // 共享项目：标出属于谁（我的连接器在这个项目里怎么用，在项目页的连接器面板里设）
  box.innerHTML = `<h3>成员 · ${members.length}</h3>
    <ul class="member-list">${members.map((a) => row(a, rm(a), joint ? (a.mine ? "你的" : `${a.owner_email || ""} 的`) : "")).join("")
      || '<li class="empty">还没有成员</li>'}</ul>
    ${others.length ? `<form class="add-member" aria-label="添加成员">
      <label for="add-member-sel" class="sr-only">添加你的 Agent</label>
      <select id="add-member-sel">${others.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("")}</select>
      <button type="submit">添加</button></form>` : '<p class="hint">你的 Agent 都已在项目里。</p>'}
    <p class="error" role="alert"></p>
    ${joint ? `<p class="hint">共享项目：主持人 ${esc(g.host_email || "")}。每个 Agent 只代表它的所有者，委托只给两端的所有者看，运行过程只给 Agent 的所有者看。</p>
      ${host ? "" : '<p><button type="button" class="ghost sm" id="leave-project">退出项目</button></p>'}` : ""}
    <p class="hint">移出项目不会打断它手上已经接下的任务，只是之后不能再被 @、被委托。</p>
    ${workdirsHtml(c)}`;
  bindWorkdirs(box, c);
  const err = (e) => { $(".error", box).textContent = e.message; };
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
    : dm && c.is_home
    ? `${avatar(peer?.name)}<div class="info"><h2 class="panel-title" style="margin:0">助理 ${pill(c.status)}</h2>
        <div class="muted">你和助理 ${esc(peer?.name || "")} · 只有你看得到 · 它看得到你各个项目的进展，替你起草消息、按你的话定时提醒</div></div>`
    : dm
    ? `${avatar(peer?.name)}<div class="info"><h2 class="panel-title" style="margin:0">${esc(peer?.name || c.title)} ${pill(c.status)}</h2>
        <div class="muted">${c.archived ? "Agent 已删除，只读" : `私聊 · ${esc(peer?.description || "")}`}</div></div>`
    : `<span class="avatar group" aria-hidden="true" style="background:${g ? gcolor(g.id) : "var(--surface-3)"}">#</span>
       <div class="info"><h2 class="panel-title" style="margin:0">${esc(c.title)} ${pill(c.status)}</h2>
        <div class="muted">${c.archived ? "已归档，只读" : `${g?.shared ? "共享项目 · " : ""}${c.members.length} 名成员 · @ 谁，谁接任务${g?.description ? ` · ${esc(g.description)}` : ""}`}</div></div>`;
  const actions = sess ? (c.archived
      ? `${g ? '<button type="button" class="ghost" id="sess-restore">恢复</button>' : ""}<button type="button" class="danger" id="conv-del">删除</button>`
      : '<button type="button" class="ghost" id="sess-rename">改名</button><button type="button" class="ghost" id="sess-archive">归档</button>')
    : c.archived ? '<button type="button" class="danger" id="conv-del">删除</button>'
    : c.kind === "project" && g ? `<button type="button" class="ghost" id="sess-new" title="和你的助理单独聊这个项目，不打扰群聊">新会话</button>${c.is_host === false ? ""
      : '<button type="button" class="ghost" id="conv-clear" title="清空聊天记录，成员从新的会话开始">清空记录</button>'}`
    : c.is_host === false ? ""  // 共享项目的群聊只有主持人能清空
    : '<button type="button" class="ghost" id="conv-clear" title="清空聊天记录，成员从新的会话开始">清空记录</button>';
  const stale = S.convError ? `<span class="net-warn" role="status" title="${esc(S.convError)}">网络不稳定，显示的可能不是最新内容，正在重试…</span>` : "";
  const pageBtn = g && !c.archived && (c.kind === "project" || sess)
    ? `<a class="button ghost" href="#project-${esc(g.id)}" title="说明、资料、技能、连接器与项目配置">项目页</a>` : "";
  const sideBtn = c.missing ? "" : c.is_home ? `<button type="button" class="ghost" id="side-open" title="你的助理与定时">助理与定时</button>`
    : `<button type="button" class="ghost" id="side-open" title="成员、项目档案与委托详情">成员与档案</button>`;
  $("#tab-members").textContent = c.is_home ? "助理与定时" : "成员";
  $("#thread-head").innerHTML = head + stale + pageBtn + sideBtn + actions;
  $("#side-open")?.addEventListener("click", () => sideTab(S.sideTab === "task" || !S.sideTab ? "members" : S.sideTab));
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
    if (m.role === "event" && m.draft_id && c.drafts?.[m.draft_id]) return draftCard(c.drafts[m.draft_id]);
    if (m.role === "event" && m.held_id && c.held?.[m.held_id]) return heldCard(c.held[m.held_id]);
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
      const body = m.text ? `<div class="body">${mentionHtml(m.text)}</div>` : "";
      return `<li class="msg user"><div class="content"><div class="who"><b>${esc(sender)}</b>${m.via ? `<span class="pill plugin" title="${m.via.endsWith(" 起草") ? "助理起草、确认后以本人的名义发出" : `经插件「${esc(m.via)}」发出：触发或插件页面里确认后发送`}">${m.via.endsWith(" 起草") ? esc(m.via) : `经 ${esc(m.via)}`}</span>` : ""}${to}${time(m.ts)}</div>${body}${attachmentsHtml(c.id, m.attachments)}${reqs ? `<div class="req-tags">${reqs}</div>` : ""}</div></li>`;
    }
    return `<li class="msg agent">${avatar(who?.name, "sm")}<div class="content">
      <div class="who"><b>${esc(who?.name || "（已删除）")}</b>${time(m.ts)}</div>${wakeHtml(m)}${stepsHtml(m.steps || [], false)}${delegs}<div class="body md">${renderMarkdown(m.text)}</div></div></li>`;
  });
  (c.proposals || []).forEach((p) => items.push(proposalCard(p)));
  (c.approvals || []).forEach((ap) => items.push(`<li class="msg proposal"><div class="content"><div class="proposal-card approval-card">
    ${approvalBody(ap, ap.can_decide)}</div></div></li>`));
  if (c.status === "waiting") {
    const open = c.tasks.filter((t) => t.status === "running" && !t.parent_task_id);
    items.push(`<li class="msg system"><div class="content"><div class="body">等 ${open.length} 个委托回报：
      ${open.map((t) => `${esc(aname(t.delegator_id))} → ${esc(aname(t.delegatee_id))}`).join("、")}。结果到了会自动唤醒委托方，你也可以继续发消息。</div></div></li>`);
  }
  // 正在跑的席位：各自一条实时过程
  Object.entries(c.live || {}).forEach(([aid, live]) => {
    const who = agent(aid);
    const pending = c.tasks.filter((t) => !t.parent_task_id && !assigned.has(t.id) && t.delegator_id === aid).map((t) => t.id);
    items.push(`<li class="msg agent">${avatar(who?.name, "sm")}<div class="content"><div class="who"><b>${esc(who?.name)}</b>运行中
      <button type="button" class="ghost sm stop-run" data-agent="${esc(aid)}" title="终止这一轮；会话保留，之后可以点“继续”接着做">停止</button></div>
      ${stepsHtml(live, true)}${pending.length ? `<div class="delegs">${treeOf(pending)}</div>` : ""}
      <span class="typing" aria-label="处理中"><i></i><i></i><i></i></span></div></li>`);
  });
  const th = $("#thread"), atBottom = th.scrollHeight - th.scrollTop - th.clientHeight < 80;
  // 正在填审核表单时不重绘线程，否则输入会被事件驱动的刷新清掉
  const typing = document.activeElement?.matches?.(".proposal-card input, .proposal-card textarea");
  if (typing && th.contains(document.activeElement)) { renderTask(); return; }
  // 保留用户手动展开的“过程”，重绘不把它们收起来
  const open = new Set([...th.querySelectorAll("details[open]")].map((d) => d.closest(".msg")?.dataset.i));
  th.innerHTML = items.join("") || `<li>${placeholder(c.is_home ? "问问助理“今天有什么要我处理的”，或者让它“每天早上 9 点给我一份早报”。"
    : dm ? "发一条消息开始私聊。"
    : "@ 一个成员把任务交给它。它可以自己做，也可以在项目里找合适的成员委托。")}</li>`;
  [...th.children].forEach((li, i) => { li.dataset.i = i; if (open.has(String(i))) li.querySelector("details")?.setAttribute("open", ""); });
  if (atBottom) th.scrollTop = th.scrollHeight;
  th.querySelectorAll(".stop-run").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true; b.textContent = "正在停止…";
    try { await api(`/api/conversations/${c.id}/seats/${b.dataset.agent}/stop`, { method: "POST" }); }
    catch (err) { b.disabled = false; b.textContent = "停止"; alert(err.message); }
  }));
  th.querySelectorAll(".deleg").forEach((b) => b.addEventListener("click", () => {
    S.taskSel = b.dataset.id;
    th.querySelectorAll(".deleg").forEach((x) => x.classList.toggle("selected", x === b));
    sideTab("task");
  }));
  bindProposals(th);
  bindDrafts(th);
  bindHeld(th);
  th.querySelectorAll("[data-req]").forEach((a) => a.addEventListener("click", () => { S.reqSel = a.dataset.req; S.taskSel = null; }));
  bindApprovals(th);
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

// 助理起草、要以你的名义发进项目群聊的消息：你确认（可以改）才发出
// 申请卡片：工作目录以外的操作（dsh 沙箱升权，只放行这一次调用），或共享项目里第一次用我的连接器（不变量 19：
// 只这一轮 / 这个项目里都可以 / 不行；选“都可以”和在连接器面板里选的一样，之后可以在那里改回来）
function approvalBody(ap, canDecide, board = false) {
  const conn = ap.kind === "connector", id = esc(ap.id);
  const head = `<div class="who">${board ? avatar(aname(ap.agent_id), "sm") : ""}<b>${esc(aname(ap.agent_id))}</b>
    ${conn ? "想用你的连接器" : "申请在工作目录以外操作"} ${pill("pending")}${board ? `<span class="muted">${time(ap.created_at)}</span>` : ""}</div>
    <p class="reason">${esc(ap.reason)}</p>`;
  if (!canDecide) return `${head}<p class="hint">等这个 Agent 的所有者${conn ? "同意" : "审批"}。</p>`;
  const look = board ? `<a href="#${esc(ap.conversation_id)}" class="muted">查看对话</a>` : "";
  return conn ? `${head}<div class="actions"><button type="button" data-ap="${id}" data-allow="1" data-scope="once">只这一次</button>
      <button type="button" class="ghost" data-ap="${id}" data-allow="1" data-scope="project">这个项目里都可以</button>
      <button type="button" class="ghost" data-ap="${id}" data-allow="0">不行</button>${look}</div>
    <p class="hint">“只这一次”只管这一轮；选“都可以”之后不再问，可以在项目页的连接器面板里改。</p>`
    : `${head}<div class="actions"><button type="button" data-ap="${id}" data-allow="1">允许这一次</button>
      <button type="button" class="ghost" data-ap="${id}" data-allow="0">拒绝</button>${look}</div>
    ${board ? '<p class="hint">获批后只放行这一次调用（以完全访问权限运行），不是放行某个目录。</p>' : ""}`;
}

function bindApprovals(root, after) {
  root.querySelectorAll("[data-ap]").forEach((btn) => btn.addEventListener("click", async () => {
    btn.closest(".actions").querySelectorAll("button").forEach((x) => { x.disabled = true; });
    try { await api(`/api/approvals/${btn.dataset.ap}`, { method: "POST", body: { allow: btn.dataset.allow === "1", scope: btn.dataset.scope || "once" } }); }
    catch (err) { alert(err.message); }
    if (after) await after();
    await reload();
  }));
}

function draftCard(d) {
  const joint = d.project_shared;
  const head = `<div class="who"><b>${esc(d.agent_name)}</b> 起草了发往「${esc(d.project_name)}」群聊的消息
    ${d.status === "pending" ? '<span class="pill status pending">待确认</span>' : d.status === "sent" ? '<span class="pill status completed">已发出</span>' : '<span class="pill status rejected">没发</span>'}</div>`;
  const body = d.status === "pending"
    ? `<label class="sr-only" for="draft-${esc(d.id)}">消息内容</label>
      <textarea id="draft-${esc(d.id)}" class="draft-text" rows="3" maxlength="4000">${esc(d.text)}</textarea>
      <p class="hint">${d.mentions.length ? `会唤醒 ${d.mentions.map(esc).join("、")}` : "没有 @ 任何成员，不会唤醒谁"} · 以你的名义发出${joint ? " · 项目里有别人，所有参与者都看得到" : ""}</p>
      <div class="actions"><button type="button" data-dsend="${esc(d.id)}">发送</button>
        <button type="button" class="ghost" data-ddiscard="${esc(d.id)}">不发了</button></div><p class="error" role="alert"></p>`
    : `<div class="body">${esc(d.text)}</div>${d.status === "sent" ? `<p class="hint"><a href="#p-${esc(d.project_id)}">去群聊看看</a></p>` : ""}`;
  return `<li class="msg proposal"><div class="content"><div class="proposal-card draft-card">${head}${body}</div></div></li>`;
}

// 你的 Agent 的回复或交付用到了别处的资料、这里有当时不在场的人：你决定发不发（网络只看来源，不看内容）
const HELD_CN = { sent: "已发出", granted: "已发出，以后直接用", rewritten: "已让它重写" };
function heldCard(h) {
  const what = h.kind === "reply" ? `在「${esc(h.project_name)}」群聊里的回复` : `给「${esc(h.project_name)}」里的委托交付`;
  const head = `<div class="who"><b>${esc(h.agent_name)}</b> ${what}用到了${h.source_names.map(esc).join("、")}的资料
    ${h.status === "pending" ? '<span class="pill status pending">等你确认</span>' : `<span class="pill status completed">${HELD_CN[h.status] || ""}</span>`}</div>`;
  const src = h.source_names.map(esc).join("、");
  const body = `<div class="body">${esc(h.text)}</div>` + (h.status !== "pending" ? "" : `
      <p class="hint">发出后${h.audience_emails.length ? ` ${h.audience_emails.map(esc).join("、")} ` : "这里的人"}都看得到。只发这一次的话，它之后的回复还会再来问你。</p>
      <div class="actions"><button type="button" data-held="${esc(h.id)}" data-act="send">发这一次</button>
        <button type="button" class="ghost" data-held="${esc(h.id)}" data-act="grant" title="以后它在这个项目里可以直接用${src}的资料">发出，以后在这里都能用</button>
        <button type="button" class="ghost" data-held="${esc(h.id)}" data-act="rewrite">不用那些资料重写</button></div><p class="error" role="alert"></p>`);
  return `<li class="msg proposal"><div class="content"><div class="proposal-card held-card">${head}${body}</div></div></li>`;
}

function bindHeld(root, after) {
  root.querySelectorAll("[data-held]").forEach((b) => b.addEventListener("click", async () => {
    const card = b.closest(".held-card");
    card.querySelectorAll("button").forEach((x) => { x.disabled = true; });
    try { await api(`/api/held/${b.dataset.held}`, { method: "POST", body: { action: b.dataset.act } }); await (after || reload)(); }
    catch (err) { $(".error", card).textContent = err.message; card.querySelectorAll("button").forEach((x) => { x.disabled = false; }); }
  }));
}

function bindDrafts(root) {
  const act = (sel, path, body) => root.querySelectorAll(`[${sel}]`).forEach((b) => b.addEventListener("click", async () => {
    const card = b.closest(".draft-card");
    card.querySelectorAll("button").forEach((x) => { x.disabled = true; });
    try { await api(path(b.getAttribute(sel)), { method: "POST", body: body(card) }); await reload(); }
    catch (err) { $(".error", card).textContent = err.message; card.querySelectorAll("button").forEach((x) => { x.disabled = false; }); }
  }));
  act("data-dsend", (id) => `/api/drafts/${id}/send`, (card) => ({ text: $(".draft-text", card).value }));
  act("data-ddiscard", (id) => `/api/drafts/${id}/discard`, () => ({}));
}

// ---------- 定时：只属于你，唤醒你自己的助理；在助理对话里说一句就能建 ----------
async function loadSchedules() {
  try { S.schedules = await api("/api/schedules"); } catch { return; }
  if (S.view === "chats" && S.conv?.is_home) renderMembers();
}

function schedulesHtml() {
  if (!S.schedules) { loadSchedules(); return '<li class="empty">加载中…</li>'; }
  return S.schedules.map((x) => `<li><span class="m-body"><span class="n">${esc(x.title)}</span>
      <span class="s">${esc(x.when)} · ${esc(x.by)}${x.agent_id !== S.me?.assistant_id ? ` · 唤醒 ${esc(x.agent_name)}` : ""}${x.error ? ` · ${esc(x.error)}` : ""}</span></span>
      <label class="switch"><input type="checkbox" data-sched="${esc(x.id)}" ${x.enabled ? "checked" : ""}> 启用</label>
      <button type="button" class="ghost icon sm rm" data-sched-del="${esc(x.id)}" aria-label="删除定时 ${esc(x.title)}" title="删除">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M4 8h8"/></svg></button></li>`).join("")
    || '<li class="empty">还没有定时</li>';
}

function bindSchedules(box) {
  const err = (e) => { $(".sched-error", box).textContent = e.message; };
  box.querySelectorAll("[data-sched]").forEach((cb) => cb.addEventListener("change", async () => {
    try { await api(`/api/schedules/${cb.dataset.sched}`, { method: "PATCH", body: { enabled: cb.checked } }); await loadSchedules(); }
    catch (e) { cb.checked = !cb.checked; err(e); }
  }));
  box.querySelectorAll("[data-sched-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("删除这个定时？删了就不会再到点。")) return;
    try { await api(`/api/schedules/${b.dataset.schedDel}`, { method: "DELETE" }); await loadSchedules(); } catch (e) { err(e); }
  }));
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
    ${(t.attachments || []).length ? `<dt>转交的附件</dt><dd>${attachmentsHtml(t.conversation_id, t.attachments)}</dd>` : ""}
    <dt>DSH 会话</dt><dd>${esc(t.dsh_session_id || "—")}</dd>
    ${t.revisions || t.retries ? `<dt>返工</dt><dd>退回 ${t.revisions} 次 · 续接 ${t.retries} 次</dd>` : ""}
    ${t.auto_accepted ? `<dt>验收</dt><dd class="muted">委托方没有显式验收，视为接受</dd>` : ""}
    ${t.violations.length ? `<dt>越权</dt><dd class="bad">${t.violations.map(esc).join("<br>")}</dd>` : ""}
    ${t.error ? `<dt>原因</dt><dd class="bad">${esc(t.error)}</dd>` : ""}</dl>
    ${t.status === "failed" ? `<div class="actions"><button type="button" id="task-retry">续接这个任务</button>
      <span class="hint">执行者在原会话里接着做</span></div><p class="error" role="alert"></p>` : ""}
    ${t.notes.length ? `<h3>进展说明</h3>${t.notes.map((n) => `<div class="md note">${renderMarkdown(n)}</div>`).join("")}` : ""}
    ${t.status === "running" ? `<h3>实时过程</h3>${stepsHtml(t.live || [], true) || '<span class="typing" aria-label="处理中"><i></i><i></i><i></i></span>'}` : ""}
    ${t.stoppable ? `<div class="actions"><button type="button" class="ghost" id="task-stop">停止这一轮</button>
      <span class="hint">任务记为失败，会话保留，之后可以续接</span></div><p class="error" role="alert"></p>` : ""}
    ${t.result ? `<h3>交付结果</h3><div class="md report">${renderMarkdown(t.result.report)}</div>` : ""}
    ${wd ? `<h3>执行者工作目录 · ${wd.files.length}</h3>${filesHtml(`/api/tasks/${t.id}/files`, wd)}
      ${t.result?.files?.length ? `<p class="hint">交付时已拷给委托方：<code>${esc(t.result.files_dir)}/</code></p>` : ""}` : ""}
    ${t.evidence.length ? `<h3>工具调用证据（${t.evidence.length}）</h3><pre>${esc(t.evidence.map((e) => `${e.status} ${e.tool} ${e.input}`).join("\n"))}</pre>` : ""}
    <details><summary>执行者收到的提示词</summary><pre>${esc(t.prompt)}</pre></details>`;
  box.scrollTop = keep;
  $("#task-stop", box)?.addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "正在停止…";
    try { await api(`/api/tasks/${t.id}/stop`, { method: "POST" }); }
    catch (err) { $(".error", box).textContent = err.message; e.target.disabled = false; e.target.textContent = "停止这一轮"; }
  });
  $("#task-retry", box)?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await api(`/api/tasks/${t.id}/retry`, { method: "POST", body: {} }); await reload(); }
    catch (err) { $(".error", box).textContent = err.message; e.target.disabled = false; }
  });
}

async function sendMsg(e) {
  e.preventDefault();
  const text = $("#msg").value.trim(), drafts = convDrafts();
  if ((!text && !drafts.length) || !S.convId) return;
  if (drafts.some((d) => d.status === "uploading")) return alert("附件还在上传，传完再发送");
  if (drafts.some((d) => d.status === "failed")) return alert("有附件没传上去：重试或移除后再发送");
  $("#msg-form button[type=submit]").disabled = true;
  try {
    await api(`/api/conversations/${S.convId}/messages`, { method: "POST",  // @名称 由服务端识别
      body: { text, attachments: drafts.map((d) => d.meta) } });
    $("#msg").value = ""; S.drafts = S.drafts.filter((d) => !drafts.includes(d));
    renderDrafts(); composerHint(); await reload();
  } catch (err) { alert(err.message); }
  $("#msg-form button[type=submit]").disabled = false;
}

// ---------- 输入框：附件 ----------
// 参照 dsh 的附件栏（research: deepseek-harness#attachments）：选中、拖进来或粘贴就上传到当前对话，传完的随下一条消息发出。
// 不按类型区分（图片也是普通文件）；消息上的附件一律点了下载，不在页面里打开。
S.drafts = [];  // { key, conv, file, name, size, status: uploading | done | failed, meta, error }
const extOf = (name) => (/\.([^.]{1,8})$/.exec(name)?.[1] || "file").toUpperCase();
const attachLimit = () => S.info?.attachment_limit || 20_000_000;
const convDrafts = () => S.drafts.filter((d) => d.conv === S.convId);
const fileGlyph = '<svg class="glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/></svg>';

function attachmentsHtml(convId, list) {
  if (!(list || []).length) return "";
  return `<div class="atts">${list.map((a) => `<a class="att" href="/api/conversations/${esc(convId)}/attachments/${esc(a.sha)}"
    download="${esc(a.name)}" title="下载 ${esc(a.name)}">${fileGlyph}<span class="n">${esc(a.name)}</span>
    <span class="s">${esc(extOf(a.name))} · ${fmtSize(a.size)}</span></a>`).join("")}</div>`;
}

function addFiles(files) {
  const c = S.conv;
  if (!c || c.archived || $("#msg-form").hidden) return;
  for (const file of files) {
    const d = { key: Math.random().toString(36).slice(2), conv: c.id, file, name: file.name, size: file.size, status: "uploading" };
    S.drafts.push(d);
    if (file.size > attachLimit()) { d.status = "failed"; d.error = `超过 ${attachLimit() / 1e6} MB 上限`; d.file = null; }
    else uploadDraft(d);
  }
  renderDrafts();
  const rail = $("#msg-files");
  rail.scrollLeft = rail.scrollWidth;  // 新加的露在最右边
}

async function uploadDraft(d) {
  d.status = "uploading"; d.error = ""; renderDrafts();
  try {
    d.meta = await api(`/api/conversations/${d.conv}/attachments?name=${encodeURIComponent(d.name)}`, { method: "POST",
      body: d.file, headers: { "Content-Type": d.file.type || "application/octet-stream" }, timeoutMs: 300000 });
    d.status = "done";
  } catch (e) { d.status = "failed"; d.error = e.message; }
  renderDrafts();
}

function renderDrafts() {
  const ul = $("#msg-files"), list = convDrafts();
  ul.hidden = !list.length;
  ul.innerHTML = list.map((d) => `<li class="att draft ${d.status}" data-key="${d.key}">
    ${d.status === "uploading" ? '<span class="spin" aria-hidden="true"></span>' : fileGlyph}
    <span class="n" title="${esc(d.name)}">${esc(d.name)}</span>
    <span class="s">${d.status === "uploading" ? "上传中…" : d.status === "failed" ? esc(d.error || "上传失败") : `${esc(extOf(d.name))} · ${fmtSize(d.size)}`}</span>
    ${d.status === "failed" && d.file ? `<button type="button" class="ghost sm" data-retry aria-label="重新上传 ${esc(d.name)}">重试</button>` : ""}
    <button type="button" class="ghost icon sm rm" data-rm aria-label="移除 ${esc(d.name)}">×</button></li>`).join("");
  ul.querySelectorAll("li").forEach((li) => {
    const d = S.drafts.find((x) => x.key === li.dataset.key);
    $("[data-rm]", li).addEventListener("click", () => { S.drafts = S.drafts.filter((x) => x !== d); renderDrafts(); });
    $("[data-retry]", li)?.addEventListener("click", () => uploadDraft(d));
  });
}

function bindAttachments() {
  const input = $("#msg-file"), overlay = $("#drop-overlay");
  $("#msg-attach").addEventListener("click", () => input.click());
  input.addEventListener("change", () => { addFiles([...input.files]); input.value = ""; });
  $("#msg").addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) { e.preventDefault(); addFiles(files); }
  });
  // 拖放：只在对话页、能发消息时接；目录不接（拿不到内容）
  const accepts = (e) => S.view === "chats" && !$("#msg-form").hidden && [...(e.dataTransfer?.types || [])].includes("Files");
  let depth = 0;
  $("#drop-limit").textContent = `单个文件不超过 ${attachLimit() / 1e6} MB，一条消息最多 10 个`;
  document.addEventListener("dragenter", (e) => { if (accepts(e)) { depth++; overlay.hidden = false; } });
  document.addEventListener("dragleave", () => { if (depth && --depth === 0) overlay.hidden = true; });
  document.addEventListener("dragover", (e) => { if (accepts(e)) e.preventDefault(); });
  document.addEventListener("drop", (e) => {
    depth = 0; overlay.hidden = true;
    if (!accepts(e)) return;
    e.preventDefault();
    const items = [...(e.dataTransfer.items || [])];
    const dirs = items.filter((it) => it.webkitGetAsEntry?.()?.isDirectory).map((it) => it.getAsFile()?.name);
    const files = [...e.dataTransfer.files].filter((f) => !dirs.includes(f.name));
    if (dirs.length) alert(`文件夹不能作为附件：${dirs.join("、")}。可以先打成压缩包。`);
    addFiles(files);
  });
}

// ---------- 输入框：@ 成员 ----------
// 群聊里 @ 谁谁接任务：输入 @ 弹出成员列表补全。项目会话里能 @ 自己在项目里的其他 Agent（成员列表由网络给，
// 只有自己的），没 @ 时交给助理。私聊不需要 @。
const members = () => (S.conv?.members || proj(S.conv?.project_id)?.member_ids || []).map(agent).filter(Boolean);
const mentionedIn = (text) => members().filter((a) => text.toLowerCase().includes(`@${a.name.toLowerCase()}`));

function renderComposer() {
  composerHint();
  renderDrafts();
}

function composerHint() {
  const c = S.conv;
  if (!c) return;
  const m = $("#msg"), who = mentionedIn(m.value);
  if (c.kind === "dm") {
    m.placeholder = "和它说点什么";
    $("#msg-hint").innerHTML = SEND_HINT;
    return;
  }
  if (c.kind === "session") {
    const others = members().length > 1;
    m.placeholder = others ? "问问项目里的情况，或者交代一件事；@ 你的其他 Agent 直接交给它" : "问问项目里的情况，或者交代一件事";
    $("#msg-hint").innerHTML = (who.length ? `交给 ${who.map((a) => esc(a.name)).join("、")}`
      : others ? "交给助理 · @ 你的其他 Agent 直接交给它" : "交给助理") + ` · ${SEND_HINT}`;
    return;
  }
  m.placeholder = "@ 一个成员把任务交给它，例如：@Product Strategy Agent 研究一个新的 AI 产品机会";
  $("#msg-hint").innerHTML = (who.length ? `交给 ${who.map((a) => esc(a.name)).join("、")}`
    : m.value.trim() ? '<span class="warn">没有 @ 任何人：消息会留在群聊里，但不会有 Agent 处理</span>' : "@ 谁，谁接任务")
    + ` · ${SEND_HINT}`;
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
  if (!["project", "session"].includes(S.conv?.kind) || q === null) return closeMentionPop();
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
  `<span class="tag model" title="${i === 0 ? "默认的 AI 工具" : "AI 工具"}">${esc(harnessLabel(h))}</span>`).join("");

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
    if (S.devicePanel?.token && !confirm("设备授权码还没确认复制，离开后就再也看不到了。确定离开？")) return;
    S.devicePanel = null; S.deviceSel = b.dataset.id; renderDevicesView();
  }));
  $("#device-list").querySelectorAll("[data-revoke]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("吊销后这台设备上的 Matrix 客户端会立即断开，绑在它上面的 Agent 进入等待。")) return;
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
      $(statusSel).textContent = `无法自动复制，已选中命令，请按 ${IS_MAC ? "⌘C" : "Ctrl+C"}。`;
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
      <p class="hint">它会装好 Matrix 客户端，检测本机装了的 Claude Code、Codex、dsh，打开浏览器让你批准这台电脑，再设成开机后自动运行。命令里没有密钥；授权只保存在那台电脑上。</p>
      <form class="editor" id="pair-form">
        <div class="field"><label for="pair-code">浏览器没有自动打开？输入终端里显示的配对码</label>
          <input id="pair-code" name="code" placeholder="ABCD-EFGH" maxlength="16" autocomplete="off" spellcheck="false"></div>
        <p class="error" role="alert"></p>
        <div class="actions"><button type="submit">下一步</button><button type="button" class="ghost" id="dv-cancel">取消</button></div>
      </form>
      <details class="manual"><summary>高级：没有浏览器的机器，手动添加</summary>
        <form class="editor" id="device-form">
          <div class="field"><label for="dv-name">设备名称</label><input id="dv-name" name="name" placeholder="我的服务器" maxlength="80"></div>
          <p class="error" role="alert"></p>
          <div class="actions"><button type="submit" class="ghost">生成设备授权码</button></div>
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
      <div class="warn-box">设备授权码只显示这一次。关掉这个面板之后就再也看不到了，请现在复制。</div>
      <p class="muted" style="margin-top:12px">在要运行 Agent 的电脑上执行：</p>
      <pre class="cmd" id="device-cmd" tabindex="0">${esc(panel.cmd)}</pre>
      <div class="actions"><button type="button" id="dv-copy">复制命令</button>
        <button type="button" class="ghost" id="dv-done">已复制，完成</button></div>
      <p class="hint" id="dv-copied" role="status"></p>
      <p class="hint">手动添加的设备一次只接一个 AI 工具：把 --driver 换成 dsh 或 codex 即可换。能开浏览器的电脑建议改用“连接一台设备”里的一条命令，自动检测全部 AI 工具。</p>`;
    copyButton("#dv-copy", "#device-cmd", "#dv-copied", panel.cmd);
    $("#dv-done").addEventListener("click", () => {
      if (!confirm("关掉后授权码不会再显示。确认已经复制好了？")) return;
      S.devicePanel = null; renderDeviceDetail();
    });
    return;
  }
  const d = (S.devices || []).find((x) => x.id === S.deviceSel);
  if (!d) { box.innerHTML = placeholder("选择一台设备查看，或连接新设备。"); return; }
  const agents = S.snap.agents.filter((a) => a.device_runtime === d.id);
  box.innerHTML = `<h2 class="panel-title">${esc(d.name)}</h2>
    <p class="muted">${d.revoked ? "已吊销" : d.online ? "在线" : "离线"}${d.hostname ? ` · ${esc(d.hostname)}` : ""} · ${d.last_seen_at ? `最近连接 ${time(d.last_seen_at)}` : "还没连接过"}</p>
    <dl><dt>AI 工具</dt><dd>${(d.harnesses || []).length ? harnessTags(d) : '<span class="muted">还没报告（Matrix 客户端连上后显示）</span>'}</dd>
      <dt>运行在这台设备上的 Agent</dt><dd>${agents.length ? tags(agents.map((a) => a.harness ? `${a.name} · ${harnessLabel(a.harness)}` : a.name)) : '<span class="muted">无</span>'}</dd></dl>
    ${devConnsHtml(d)}
    <p class="hint">在那台电脑上装了新的 AI 工具、或在工具里配了新的服务后，重启 Matrix 客户端就会出现在这里。它只发出站请求；设备离线时绑在它上面的 Agent 进入等待，不消耗重试。</p>`;
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
      <dt>检测到的 AI 工具</dt><dd>${(p.harnesses || []).length ? harnessTags(p) : '<span class="muted">没有（装好后重启 Matrix 客户端即可）</span>'}</dd></dl>
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

// ================= 技能与连接器（docs/reference/connectors-skills.md） =================
// 个人层在这里：“技能”页是我的技能库，“连接器”页是我授权的外部服务与我电脑上配好的服务。勾给哪些 Agent
// 在 Agent 编辑页或连接器详情里改。项目层（项目技能、连接器面板）在项目页。界面不出现 MCP、命令行之类的实现名词。
async function loadMine(force = false) {
  if (S.mineLoading) return S.mineLoading;
  if (!force && S.mySkills && S.myConns && S.catalog) return;
  S.mineLoading = Promise.all([api("/api/me/skills"), api("/api/me/connections"), api("/api/connectors")])
    .then(([sk, cs, cat]) => {
      const changed = JSON.stringify([sk, cs, cat]) !== JSON.stringify([S.mySkills, S.myConns, S.catalog]);
      S.mySkills = sk; S.myConns = cs; S.catalog = cat; S.skillCache = null;
      if (changed && ["agents", "skills", "connectors", "devices"].includes(S.view)) render();
    })
    .catch(() => { S.mySkills ||= []; S.myConns ||= []; S.catalog ||= []; })
    .finally(() => { S.mineLoading = null; });
  return S.mineLoading;
}

const CONN_STATUS = { ok: ["idle", "可用"], error: ["failed", "出错"], missing: ["rejected", "电脑上没有了"] };
const connPill = (c) => { const [cls, text] = CONN_STATUS[c.status] || ["", c.status]; return `<span class="pill status ${cls}">${text}</span>`; };
const connWhere = (c) => c.kind === "cloud" ? "云端" : `${esc(c.device_name)} · ${esc(c.harness_name || "")}${c.online ? "" : "（离线）"}`;
// 这个连接能给哪些 Agent：云端的给云端的 Agent；电脑上的给同一台电脑、同一个 AI 工具上的（服务端会再校验）
const devDefault = (id) => (S.devices || []).find((d) => d.id === id)?.harnesses?.[0];
const placedFor = (c, a) => c.kind === "cloud" ? !a.device_runtime
  : a.device_runtime === c.where && (a.harness || devDefault(c.where) || c.harness) === c.harness;
const mineAgents = () => S.snap.agents.filter((a) => a.mine);

// Agent 编辑页里的两组勾选：只列我自己的，共享项目里别人看不到
function abilityBoxes(a) {
  if (!S.mySkills || !S.myConns) return '<p class="hint">正在读取你的技能与连接器…</p>';
  const skills = S.mySkills, conns = S.myConns.filter((c) => placedFor(c, a) || (a.connections || []).includes(c.id));
  const noUse = !a.authority.includes("connector.use");
  return `<fieldset class="checks" id="af-skills"><legend>技能 · 在「技能」页管理</legend>
      ${skills.map((k) => `<label><input type="checkbox" name="skill" value="${esc(k.name)}" ${(a.skills || []).includes(k.name) ? "checked" : ""}>
        <span class="opt">${esc(k.name)}<small>${esc(k.description)}</small></span></label>`).join("")
        || '<p class="hint">技能库里还没有技能。<a href="#skills">去写一个</a>。</p>'}
      <p class="hint">项目里约定的做法（项目技能）会自动给到，重名时项目的优先。它自己在私有目录里攒的技能也会用上。</p>
    </fieldset>
    <fieldset class="checks" id="af-conns"><legend>连接器 · 在「连接器」页授权</legend>
      ${conns.map((c) => `<label><input type="checkbox" name="conn" value="${esc(c.id)}" ${(a.connections || []).includes(c.id) ? "checked" : ""}>
        <span class="opt">${esc(c.name)} ${c.status === "ok" ? "" : connPill(c)}<small>${connWhere(c)}</small></span></label>`).join("")
        || `<p class="hint">${a.device_runtime ? "这台电脑上的这个 AI 工具里还没有配好的服务。" : "你还没有授权云端的连接器。"}<a href="#connectors">去看看</a>。</p>`}
      <p class="hint">${noUse ? '<span class="warn">它没有“使用连接器”的权限，勾了也用不上。</span> ' : ""}委托给别的 Agent 时不会转授；共享项目里默认不开，要在项目的成员栏里打开。</p>
    </fieldset>`;
}

// ---- 我的技能 ----
function renderSkillsView() {
  const list = S.mySkills;
  const users = (name) => mineAgents().filter((a) => (a.skills || []).includes(name));
  $("#skill-list").innerHTML = !list ? '<li class="empty">正在读取…</li>' : list.map((k) => `
    <li class="${S.skillSel === k.name ? "selected" : ""}"><div class="card">
      <button type="button" class="card-main" data-name="${esc(k.name)}" aria-label="查看技能 ${esc(k.name)}">
        <div class="card-head"><span class="ability-ico">${ICON.skill}</span><span class="name">${esc(k.name)}</span></div>
        <p class="card-desc">${esc(k.description)}</p>
        <div class="card-meta">${users(k.name).map((a) => `<span class="tag">${esc(a.name)}</span>`).join("") || '<span class="muted">还没有 Agent 用它</span>'}
          ${k.files.length ? `<span class="muted">${k.files.length} 个附带文件</span>` : ""}</div>
      </button></div></li>`).join("") || '<li class="empty">还没有技能。上传一个标准技能包（含 SKILL.md 的 zip 或文件夹）。</li>';
  $("#skill-list").querySelectorAll(".card-main").forEach((b) => b.addEventListener("click", () => {
    S.skillSel = b.dataset.name; renderSkillsView();
  }));
  renderSkillEditor();
}

async function renderSkillEditor() {
  const box = $("#skill-editor");
  if (!S.skillSel) { box.innerHTML = placeholder("选择一个技能看详情，或点“上传技能”。"); return; }
  const x = (S.mySkills || []).find((k) => k.name === S.skillSel);
  if (!x) { S.skillSel = null; box.innerHTML = placeholder("这个技能已删除。"); return; }
  const users = mineAgents().filter((a) => (a.skills || []).includes(x.name));
  box.innerHTML = skillCardHtml(x, users.length ? `${users.map((a) => esc(a.name)).join("、")} 在用。` : "还没有 Agent 用它，在 Agent 编辑页里勾上。", true);
  $("#sk-del", box).addEventListener("click", async () => {
    if (!confirm(`删除技能「${x.name}」？用到它的 Agent 之后就没有这个做法了。`)) return;
    try { await api(`/api/me/skills/${encodeURIComponent(x.name)}`, { method: "DELETE" }); } catch (e) { $(".error", box).textContent = e.message; return; }
    S.skillSel = null; await loadMine(true); await reload(); renderSkillsView();
  });
}

// 技能只展示名字与描述；内容按 Agent Skills 标准（SKILL.md + 可选目录）在别处维护，打包上传
function skillCardHtml(x, note, canDelete) {
  return `<h2 class="panel-title">${esc(x.name)}</h2><p>${esc(x.description)}</p>
    ${note ? `<p class="muted">${note}</p>` : ""}
    ${x.files?.length ? `<p class="hint">${x.files.length} 个附带文件</p>` : ""}
    <p class="hint">更新：重新上传同名的技能包。</p><p class="error" role="alert"></p>
    ${canDelete ? '<div class="actions"><button type="button" class="danger" id="sk-del">删除</button></div>' : ""}`;
}

// 选 zip 或文件夹，上传到 base（/api/me/skills 或 /api/projects/<id>/skills）
function pickSkillPackage(base, folder) {
  return new Promise((resolve, reject) => {
    const inp = document.createElement("input");
    inp.type = "file";
    if (folder) inp.webkitdirectory = true; else inp.accept = ".zip,.skill,application/zip";
    inp.addEventListener("change", async () => {
      const files = [...inp.files];
      if (!files.length) return resolve(null);
      try {
        let res;
        if (folder) {
          const enc = async (f) => { const b = new Uint8Array(await f.arrayBuffer()); let t = ""; for (let i = 0; i < b.length; i += 0x8000) t += String.fromCharCode(...b.subarray(i, i + 0x8000)); return btoa(t); };
          const body = { files: await Promise.all(files.map(async (f) => ({ path: f.webkitRelativePath || f.name, data: await enc(f) }))) };
          res = await api(`${base}/upload`, { method: "POST", body, timeoutMs: 300000 });
        } else {
          res = await api(`${base}/upload`, { method: "POST", body: files[0], headers: { "Content-Type": "application/zip" }, timeoutMs: 300000 });
        }
        resolve(res);
      } catch (e) { reject(e); }
    });
    inp.click();
  });
}

// ---- 我的连接器 ----
function renderConnectorsView() {
  if (S.devices === undefined) api("/api/devices").then((d) => { S.devices = d; }).catch(() => { S.devices = []; });
  const conns = S.myConns, cat = S.catalog;
  const users = (c) => mineAgents().filter((a) => (a.connections || []).includes(c.id));
  $("#conn-list").innerHTML = !conns ? '<li class="empty">正在读取…</li>' : conns.map((c) => `
    <li class="${S.connSel === c.id ? "selected" : ""}"><div class="card">
      <button type="button" class="card-main" data-id="${esc(c.id)}" aria-label="查看连接器 ${esc(c.name)}">
        <div class="card-head"><span class="ability-ico">${ICON.plug}</span><span class="name">${esc(c.name)}</span>${connPill(c)}</div>
        <p class="card-desc">${c.kind === "cloud" ? esc(c.custom ? "自定义 · 云端" : c.description || "云端") : `${connWhere(c)} 里配好的服务`}</p>
        <div class="card-meta">${users(c).map((a) => `<span class="tag">${esc(a.name)}</span>`).join("") || '<span class="muted">还没给任何 Agent</span>'}</div>
      </button></div></li>`).join("")
    || '<li class="empty">还没有连接器。在下面授权一个云端服务；你电脑上 Claude Code、Codex、dsh 里配好的服务，连上设备后会自动出现在这里。</li>';
  $("#catalog-list").innerHTML = !cat ? "" : cat.map((k) => {
    const have = (conns || []).filter((c) => c.connector_id === k.id && c.kind === "cloud").length;
    return `<li class="${S.connPanel === k.id ? "selected" : ""}"><div class="card">
      <button type="button" class="card-main" data-add="${esc(k.id)}" aria-label="授权 ${esc(k.name)}">
        <div class="card-head"><span class="ability-ico">${ICON.plug}</span><span class="name">${esc(k.name)}</span>${have ? '<span class="pill">已授权</span>' : ""}</div>
        <p class="card-desc">${esc(k.description || "")}</p>
        <div class="card-meta">${k.skills.length ? `<span class="muted">自带技能</span>${k.skills.map((x) => `<span class="tag" title="${esc(x.description)}">${esc(x.name)}</span>`).join("")}` : ""}</div>
      </button>
      <div class="card-foot"><span class="muted">云端</span><span class="button sm" aria-hidden="true">${have ? "更新授权" : "授权"}</span></div>
    </div></li>`;
  }).join("") || '<li class="empty">部署方还没有提供云端连接器。可以点右上角“自定义连接器”自己接入。</li>';
  $("#conn-list").querySelectorAll(".card-main").forEach((b) => b.addEventListener("click", () => {
    S.connSel = b.dataset.id; S.connPanel = null; renderConnectorsView();
  }));
  $("#catalog-list").querySelectorAll("[data-add]").forEach((b) => b.addEventListener("click", () => {
    S.connPanel = b.dataset.add; S.connSel = null; renderConnectorsView();
  }));
  renderConnDetail();
}

function renderConnDetail() {
  const box = $("#conn-detail");
  if (box.contains(document.activeElement) && document.activeElement.matches("input, textarea")) return;
  if (S.connPanel === "__custom") {
    if (box.querySelector("#custom-form")) return;
    const cloud = mineAgents().filter((a) => !a.device_runtime);
    box.innerHTML = `<h2 class="panel-title">自定义连接器</h2>
      <form class="editor" id="custom-form">
        <div class="field"><label for="cu-name">名字</label><input id="cu-name" name="name" required maxlength="60" placeholder="团队知识库"></div>
        <div class="field"><label for="cu-url">服务地址</label><input id="cu-url" name="url" required type="url" placeholder="https://example.com/mcp" spellcheck="false">
          <p class="hint">只能是 https 的公网地址，不能是内网。</p></div>
        <div class="field"><label for="cu-header">认证请求头（可选）</label><input id="cu-header" name="header" maxlength="64" placeholder="Authorization" spellcheck="false"></div>
        <div class="field"><label for="cu-value">认证值（可选）</label><input id="cu-value" name="value" type="password" autocomplete="off" placeholder="Bearer …"></div>
        <fieldset class="checks"><legend>给哪些 Agent 用</legend>
          ${cloud.map((a) => `<label><input type="checkbox" name="agent" value="${esc(a.id)}" ${a.id === S.me?.assistant_id ? "checked" : ""}><span class="opt">${esc(a.name)}</span></label>`).join("") || '<p class="hint">你还没有在云端跑的 Agent。</p>'}
        </fieldset>
        <p class="hint">认证值加密保存，Agent 拿不到，页面也不再显示。</p>
        <p class="error" role="alert"></p>
        <div class="actions"><button type="submit">添加</button><button type="button" class="ghost" id="cu-cancel">取消</button></div>
      </form>`;
    const f = $("#custom-form", box);
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = f.querySelector("button[type=submit]");
      btn.disabled = true; btn.textContent = "正在试连…";
      try {
        const c = await api("/api/me/connections/custom", { method: "POST", body: { name: f.name.value.trim(), url: f.url.value.trim(),
          header: f.header.value.trim(), value: f.value.value, agent_ids: [...f.querySelectorAll('[name="agent"]:checked')].map((x) => x.value) } });
        S.connPanel = null; S.connSel = c.id; document.activeElement?.blur();
        await loadMine(true); await reload();
      } catch (err) { $(".error", f).textContent = err.message; btn.disabled = false; btn.textContent = "添加"; }
    });
    $("#cu-cancel").addEventListener("click", () => { S.connPanel = null; renderConnectorsView(); });
    return;
  }
  const spec = (S.catalog || []).find((k) => k.id === S.connPanel);
  if (spec) {
    if (box.querySelector(`#auth-form-${CSS.escape(spec.id)}`)) return;
    const cloud = mineAgents().filter((a) => !a.device_runtime);
    box.innerHTML = `<h2 class="panel-title">授权 ${esc(spec.name)}</h2>
      <p class="muted">${esc(spec.description || "")}</p>
      <form class="editor" id="auth-form-${esc(spec.id)}">
        ${spec.fields.map((x) => `<div class="field"><label for="au-${esc(x.key)}">${esc(x.label)}</label>
          <input id="au-${esc(x.key)}" name="${esc(x.key)}" ${x.secret ? 'type="password" autocomplete="off"' : ""} required spellcheck="false"></div>`).join("")
          || '<p class="hint">这个服务不需要填写任何东西。</p>'}
        <fieldset class="checks"><legend>给哪些 Agent 用</legend>
          ${cloud.map((a) => `<label><input type="checkbox" name="agent" value="${esc(a.id)}" ${a.id === S.me?.assistant_id ? "checked" : ""}>
            <span class="opt">${esc(a.name)}<small>${a.authority.includes("connector.use") ? "" : "没有“使用连接器”的权限，勾了也用不上"}</small></span></label>`).join("")
            || '<p class="hint">你还没有在云端跑的 Agent。电脑上的 Agent 用的是那台电脑上配好的服务。</p>'}
        </fieldset>
        <p class="hint">填的内容加密保存，只在 Agent 调用这个服务时由网络带上，Agent 自己拿不到，任何页面也不会再显示。之后可以随时撤销。</p>
        <p class="error" role="alert"></p>
        <div class="actions"><button type="submit">授权</button><button type="button" class="ghost" id="au-cancel">取消</button></div>
      </form>`;
    const f = box.querySelector("form");
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = f.querySelector("button[type=submit]");
      btn.disabled = true; btn.textContent = "正在试连…";
      const values = Object.fromEntries(spec.fields.map((x) => [x.key, f.elements[x.key].value]));
      try {
        const c = await api("/api/me/connections", { method: "POST", body: { connector_id: spec.id, values,
          agent_ids: [...f.querySelectorAll('[name="agent"]:checked')].map((x) => x.value) } });
        S.connPanel = null; S.connSel = c.id;
        document.activeElement?.blur();
        await loadMine(true); await reload();
      } catch (err) { $(".error", f).textContent = err.message; btn.disabled = false; btn.textContent = "授权"; }
    });
    $("#au-cancel").addEventListener("click", () => { S.connPanel = null; renderConnectorsView(); });
    return;
  }
  const c = (S.myConns || []).find((x) => x.id === S.connSel);
  if (!c) { box.innerHTML = placeholder("选择一个连接器查看，或在“可以添加”里授权一个。"); return; }
  const fit = mineAgents().filter((a) => placedFor(c, a) || (a.connections || []).includes(c.id));
  const sig = JSON.stringify([c, fit.map((a) => [a.id, a.connections, a.authority])]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.innerHTML = `<h2 class="panel-title">${esc(c.name)} ${connPill(c)}</h2>
    <p class="muted">${c.kind === "cloud" ? `云端 · ${esc(c.description || "")}` : `${connWhere(c)} 里配好的服务`}</p>
    ${c.error ? `<p class="hint warn">${esc(c.error)}</p>` : ""}
    <form class="editor" id="conn-agents">
      <fieldset class="checks"><legend>给哪些 Agent 用</legend>
        ${fit.map((a) => `<label><input type="checkbox" name="agent" value="${esc(a.id)}" ${(a.connections || []).includes(c.id) ? "checked" : ""}>
          <span class="opt">${esc(a.name)}<small>${a.authority.includes("connector.use") ? "" : "没有“使用连接器”的权限，勾了也用不上"}</small></span></label>`).join("")
          || `<p class="hint">${c.kind === "cloud" ? "你还没有在云端跑的 Agent。" : "还没有 Agent 跑在这台电脑的这个 AI 工具上。"}</p>`}
      </fieldset>
      ${c.skills?.length ? `<p class="hint">自带技能：${c.skills.map((x) => `<span class="tag">${esc(x)}</span>`).join("")}，有这个连接器的那一轮会一起给到。</p>` : ""}
      <p class="hint">${c.kind === "cloud" ? "Agent 调用时由网络带上你的授权，Agent 拿不到授权本身。" : "这一轮允许的服务才会在那台电脑上打开，其余的关着。"}
        共享项目里默认不开，要在那个项目的成员栏里逐个打开。</p>
      <p class="error" role="alert"></p>
      <div class="actions"><button type="submit">保存</button>
        ${c.kind === "cloud" ? '<button type="button" class="ghost" id="conn-check">重新检查</button><button type="button" class="danger" id="conn-del">撤销授权</button>'
          : c.status === "missing" ? '<button type="button" class="danger" id="conn-del">移除</button>' : ""}</div>
    </form>
    ${c.kind === "cloud" ? '<h3>最近的调用</h3><div id="conn-calls"><p class="muted">正在读取…</p></div>' : ""}`;
  const f = $("#conn-agents"), err = (e) => { $(".error", f).textContent = e.message; };
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const want = new Set([...f.querySelectorAll('[name="agent"]:checked')].map((x) => x.value));
    try {
      for (const a of fit) {
        const has = (a.connections || []).includes(c.id);
        if (has === want.has(a.id)) continue;
        const next = has ? a.connections.filter((x) => x !== c.id) : [...(a.connections || []), c.id];
        await api(`/api/agents/${a.id}`, { method: "PATCH", body: { connections: next } });
      }
      box.dataset.sig = ""; await reload(); await loadMine(true);
    } catch (e2) { err(e2); }
  });
  $("#conn-check")?.addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "正在检查…";
    try { await api(`/api/me/connections/${c.id}/check`, { method: "POST", body: {} }); box.dataset.sig = ""; await loadMine(true); }
    catch (e2) { err(e2); e.target.disabled = false; e.target.textContent = "重新检查"; }
  });
  $("#conn-del")?.addEventListener("click", async () => {
    if (!confirm(c.kind === "cloud" ? `撤销 ${c.name} 的授权？保存的授权会删除，所有 Agent 和项目里都不再能用。` : `移除 ${c.name}？那台电脑再报告它时会重新出现。`)) return;
    try { await api(`/api/me/connections/${c.id}`, { method: "DELETE" }); S.connSel = null; box.dataset.sig = ""; await loadMine(true); await reload(); }
    catch (e2) { err(e2); }
  });
  if (c.kind === "cloud") {
    api(`/api/me/connections/${c.id}/calls`).then((calls) => {
      const el = $("#conn-calls");
      if (!el) return;
      el.innerHTML = calls.length ? `<ul class="calls">${calls.slice(-20).reverse().map((x) => `<li><span class="${x.ok ? "ok" : "bad"}">${x.ok ? "✓" : "✗"}</span>
        <b>${esc(x.agent)}</b> <code>${esc(x.tool)}</code><span class="muted">${time(x.ts)} · ${x.ms} ms</span></li>`).join("")}</ul>
        <p class="hint">只记下谁、什么时候、调了哪个工具，不记内容。</p>` : '<p class="muted">还没有调用。</p>';
    }).catch(() => {});
  }
}

// 设备详情里：这台电脑上各 AI 工具里配好的服务
function devConnsHtml(d) {
  const list = (S.myConns || []).filter((c) => c.where === d.id);
  if (!list.length) return "";
  return `<dl><dt>配好的服务</dt><dd>${list.map((c) => `<a class="tag" href="#connectors" data-conn="${esc(c.id)}">${esc(c.name)} · ${esc(c.harness_name || "")}</a>`).join("")}</dd></dl>`;
}

// ---------- 弹出框：次级信息（详情、编辑器、成员与档案）不占常驻侧栏 ----------
// 各详情区照旧由各自的 render 函数填充；有实际内容（不是占位提示）就弹出，关闭时清掉对应的选中状态。
const MODAL_CLOSE = {
  "net-detail": () => { S.sel = null; renderGraph(); },
  "board-detail": () => { S.reqSel = null; S.taskSel = null; renderBoard(); },
  "agent-editor": () => { S.agentSel = null; renderAgents(); renderAgentEditor(); },
  "skill-editor": () => { S.skillSel = null; renderSkillsView(); },
  "conn-detail": () => { S.connSel = null; S.connPanel = null; $("#conn-detail").dataset.sig = ""; renderConnectorsView(); },
  "project-editor": () => { S.projectEdit = false; renderProjectEditor(); },
  "device-detail": () => {
    S.devicePanel = null; S.deviceSel = null;
    if (location.hash.startsWith("#pair=")) history.replaceState(null, "", "#devices");
    renderDevicesView();
  },
};
function closeModal(box) {
  if (box.classList.contains("chat-side")) { box.classList.remove("open"); syncBackdrop(); return; }
  MODAL_CLOSE[box.id]?.();
}
function syncBackdrop() {
  const open = [...document.querySelectorAll(".side.open")].some((b) => !b.closest(".view[hidden]"));
  $("#modal-backdrop").hidden = !open;
}
function setupModals() {
  const bd = document.createElement("div");
  bd.id = "modal-backdrop"; bd.className = "modal-backdrop"; bd.hidden = true;
  bd.addEventListener("click", () => { const b = topModal(); if (b) closeModal(b); });
  document.body.append(bd);
  document.querySelectorAll(".side").forEach((box) => {
    box.setAttribute("role", "dialog"); box.setAttribute("aria-modal", "true"); box.tabIndex = -1;
    const ensureX = () => {
      if (box.querySelector(":scope > .modal-x")) return;
      const x = document.createElement("button");
      x.type = "button"; x.className = "ghost icon modal-x"; x.setAttribute("aria-label", "关闭"); x.textContent = "×";
      x.addEventListener("click", () => closeModal(box));
      box.prepend(x);
    };
    ensureX();
    if (box.classList.contains("chat-side")) {
      new MutationObserver(syncBackdrop).observe(box, { attributes: true, attributeFilter: ["class"] });
      return;
    }
    new MutationObserver(() => {
      ensureX();
      const kids = [...box.children].filter((k) => !k.classList.contains("modal-x"));
      const has = kids.length > 0 && !(kids.length === 1 && kids[0].classList.contains("placeholder"));
      const was = box.classList.contains("open");
      box.classList.toggle("open", has);
      if (has && !was) box.focus({ preventScroll: true });
      syncBackdrop();
    }).observe(box, { childList: true });
  });
  // 切换视图时只显示当前视图里的弹出框
  document.querySelectorAll(".view").forEach((v) =>
    new MutationObserver(syncBackdrop).observe(v, { attributes: true, attributeFilter: ["hidden"] }));
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (e.target.closest?.(".mention-pop") || !$("#mention-pop").hidden) return;
    const b = topModal(); if (b) { e.preventDefault(); closeModal(b); }
  });
}
const topModal = () => [...document.querySelectorAll(".side.open")].find((b) => !b.closest(".view[hidden]"));
setupModals();
