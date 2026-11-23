// 业务系统账号的只读页面（/system，docs/open-api.md，不变量 30）：它调用服务的会话（完整内容，它就是调用方）
// 与它发布的服务被调用的用量（只有计数，不看内容）。服务端只给系统账号放行 /api/me 与 /api/system/* 的 GET。
import { renderMarkdown } from "./md.js";

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (n) => Number(n || 0).toLocaleString("zh-CN");
const S = { days: 30, data: null };
const STATUS = { idle: "空闲", running: "进行中", active: "发布中", withdrawn: "已撤回" };

function when(ts) {
  if (!ts) return '<span class="muted">—</span>';
  const d = new Date(ts * 1000);
  return `<time datetime="${d.toISOString()}">${esc(d.toLocaleString("zh-CN", { hour12: false }))}</time>`;
}

async function api(path) {
  const r = await fetch(path, { signal: AbortSignal.timeout(30000) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.detail || `HTTP ${r.status}`), { status: r.status });
  return data;
}

function deny(title, text) {
  $("#home").hidden = $("#session").hidden = true;
  $("#denied").hidden = false;
  $("#denied-title").textContent = title;
  $("#denied-text").textContent = text;
}

function table(id, cols, rows, empty) {
  const t = $(id);
  if (!rows.length) { t.innerHTML = `<tbody><tr><td class="empty">${esc(empty)}</td></tr></tbody>`; return; }
  t.innerHTML = `<thead><tr>${cols.map((c) => `<th${c.num ? ' class="num"' : ""} scope="col">${esc(c.label)}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td${c.num ? ' class="num"' : ""}>${c.html(r)}</td>`).join("")}</tr>`).join("")}</tbody>`;
}

function renderHome(d) {
  $("#sys-title").textContent = d.account.app ? `「${d.account.app}」的业务系统账号` : "业务系统账号";
  $("#managers").textContent = d.account.managers.length ? ` 负责人：${d.account.managers.join("、")}。` : "";
  table("#t-sessions", [
    { label: "会话", html: (r) => `<a href="#s=${esc(r.id)}">${esc(r.first_message || r.title)}</a><div class="muted">${esc(r.last_message || "")}</div>` },
    { label: "服务", html: (r) => r.service ? `${esc(r.service.title)}<div class="muted">${esc(r.service.project_name)}</div>` : '<span class="muted">已删除</span>' },
    { label: "消息", num: true, html: (r) => num(r.message_count) },
    { label: "最近", html: (r) => when(r.updated_at) },
  ], d.sessions, "还没有调用过服务");
  table("#t-published", [
    { label: "服务", html: (r) => `<b>${esc(r.title)}</b><div class="muted">${esc(r.project_name)} · v${esc(r.version)}</div>` },
    { label: "状态", html: (r) => `<span class="pill ${r.status === "active" ? "completed" : "cancelled"}">${esc(STATUS[r.status] || r.status)}</span>` },
    { label: "会话", num: true, html: (r) => num(r.usage.sessions) },
    { label: "轮次", num: true, html: (r) => num(r.usage.runs) },
    { label: "失败", num: true, html: (r) => num(r.usage.failed) },
    { label: "token", num: true, html: (r) => num(r.usage.tokens.total) },
    { label: "调用方", html: (r) => r.usage.users.length
        ? r.usage.users.map((u) => `${esc(u.user || "（已删除）")} <span class="muted">${num(u.runs)} 轮</span>`).join("<br>") : '<span class="muted">—</span>' },
  ], d.published, "没有发布服务");
}

function messageHtml(m, d) {
  const files = (m.attachments || []).map((a) =>
    `<a href="/api/system/sessions/${encodeURIComponent(d.id)}/attachments/${encodeURIComponent(a.sha)}">${esc(a.name)}</a>`).join("");
  const att = files ? `<div class="att">${files}</div>` : "";
  if (m.role === "event") return `<li class="msg event">${esc(m.text)}</li>`;
  if (m.role === "user") {
    return `<li class="msg user"><div class="content"><div class="who"><b>${esc(d.senders?.[m.user_id] || "调用方")}</b>${when(m.ts)}</div>
      <div class="body md">${renderMarkdown(m.text)}</div>${att}</div></li>`;
  }
  const name = d.agent_names?.[m.agent_id] || (m.role === "system" ? "Matrix" : "Agent");
  return `<li class="msg agent"><div class="content"><div class="who"><b>${esc(name)}</b>${when(m.ts)}</div>
    <div class="body md">${renderMarkdown(m.text)}</div>${att}</div></li>`;
}

async function showSession(id) {
  $("#home").hidden = true;
  $("#session").hidden = false;
  $("#s-messages").innerHTML = "";
  try {
    const d = await api(`/api/system/sessions/${encodeURIComponent(id)}`);
    $("#s-title").textContent = d.title;
    const svc = d.service;
    $("#s-sub").textContent = `${svc ? `${svc.title} · ${svc.project_name} · 入口 ${svc.entry.name}` : "服务已删除"}${d.archived ? " · 已只读" : ""}`;
    $("#s-messages").innerHTML = d.messages.map((m) => messageHtml(m, d)).join("") || '<li class="empty">还没有消息</li>';
  } catch (e) {
    $("#s-title").textContent = "找不到这个会话";
    $("#s-sub").textContent = e.message;
  }
}

async function showHome() {
  $("#session").hidden = true;
  $("#home").hidden = false;
  try { renderHome(S.data = await api(`/api/system/overview?days=${S.days}`)); }
  catch (e) { $("#sys-error").textContent = e.message; }
}

function route() {
  const m = location.hash.match(/^#s=([\w-]+)$/);
  return m ? showSession(m[1]) : showHome();
}

async function init() {
  let me;
  try { me = await api("/api/me"); }
  catch { return deny("请先登录", "用业务系统账号的邮箱与密码登录后再来。"); }
  if (me.kind !== "system") { location.replace("/"); return; }
  $("#me").textContent = me.email;
  $("#logout").addEventListener("click", async () => {
    await fetch("/api/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }).catch(() => {});
    location.replace("/");
  });
  $("#back").addEventListener("click", (e) => { e.preventDefault(); location.hash = ""; });
  for (const r of document.querySelectorAll('input[name="days"]')) {
    r.addEventListener("change", () => { S.days = Number(r.value); showHome(); });
  }
  addEventListener("hashchange", route);
  await route();
}

init();
