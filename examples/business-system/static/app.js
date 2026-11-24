// 模拟报销系统的前端：只和报销系统自己的后端说话（/api/*），令牌不在浏览器里。
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const S = { state: {}, steps: [], claims: [], current: null, reviews: {}, done: new Set(), streaming: false };
const STATUS = { running: "在做", waiting: "等执行方上线", in_review: "交付了，等验收", completed: "验收通过", failed: "失败", rejected: "没批准" };

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

// ---------------- 顶部：拿到的 id ----------------
function renderIds() {
  const s = S.state, bits = [];
  if (s.project) bits.push(`<span>项目 ${esc(s.project)}</span>`);
  if (s.service) bits.push(`<span>服务 ${esc(s.service)}</span>`);
  bits.push(`<a href="${esc(s.matrix_url)}/system" target="_blank" rel="noopener">在 Matrix 里查看 ↗</a>`);
  $("#ids").innerHTML = bits.join("");
  $("#setup-banner").hidden = !!s.service;
}

// ---------------- 报销单 ----------------
function renderClaims() {
  $("#claim-list").innerHTML = S.claims.map((c) => {
    const rv = S.reviews[c.id];
    const badge = !rv ? '<span class="badge">待审核</span>' : rv.status === "running" ? '<span class="badge run">审核中</span>'
      : rv.status === "done" ? '<span class="badge ok">已出结论</span>' : '<span class="badge bad">没审完</span>';
    return `<li class="claim ${S.current === c.id ? "active" : ""}" data-id="${esc(c.id)}">
      <span class="t">${esc(c.id)} · ${esc(c.title)}</span><span class="amt">¥${Number(c.amount).toLocaleString("zh-CN")}</span>
      <span class="meta">${esc(c.who)} ${badge}</span>
      <span><button class="${rv ? "" : "primary"}" data-act="${rv ? "show" : "review"}" ${S.streaming ? "disabled" : ""}>${rv ? "查看" : "提交审核"}</button></span></li>`;
  }).join("");
  for (const b of document.querySelectorAll("#claim-list button")) {
    b.addEventListener("click", () => {
      const id = b.closest(".claim").dataset.id;
      S.current = id;
      if (b.dataset.act === "review") review(id, S.claims.find((c) => c.id === id).message);
      else { renderClaims(); renderReview(); }
    });
  }
}

function itemHtml(it) {
  const row = (cls, k, v) => `<li class="${cls}"><span class="k">${esc(k)}</span><span class="v">${v}</span></li>`;
  const d = it.data;
  switch (it.event) {
    case "ask": return row("ask", "报销系统", esc(d.text));
    case "session": return row("beat", "会话", `<code>${esc(d.session_id)}</code>（追问时带回去，接着同一个会话）`);
    case "delta": return row("delta", "入口", esc(d.text.trim()));
    case "tool": return row("tool", "工具", `${esc(d.name)} ${esc(d.input?.brief || "")}`);
    case "done": return row("done", "结论", esc(d.text));
    case "error": return row("error", "出错", esc(d.message));
    case "progress":
      if (d.kind === "task") return row("task sub", "委托", `${esc(d.from)} → <b>${esc(d.agent)}</b>：${esc(STATUS[d.status] || d.status)}<br><span class="muted">${esc(d.mission)}</span>`);
      if (d.kind === "tool") return row("tool sub", d.agent, `${esc(d.tool)} ${esc(d.brief || "")}`);
      if (d.kind === "text") return row("delta sub", d.agent, esc(d.text));
      if (d.kind === "heartbeat") return row("beat", "心跳", `还在做：${esc((d.working || []).join("、") || "收尾中")} · 已 ${d.elapsed_s} 秒`);
      return "";  // 不认识的 kind 跳过
    default: return "";
  }
}

function renderReview() {
  const c = S.claims.find((x) => x.id === S.current), rv = S.reviews[S.current];
  $("#rv-title").textContent = c ? `${c.id} · ${c.title}（¥${Number(c.amount).toLocaleString("zh-CN")}）` : "选一张报销单提交审核";
  $("#rv-sub").textContent = c ? `${c.who}提交 · 经 Matrix 服务「报销审核」` : "过程会实时显示：入口说的话、调的工具、交给了谁、被委托方在做什么。";
  $("#timeline").innerHTML = rv ? rv.items.map(itemHtml).join("") : "";
  $("#interrupt").hidden = !(rv && rv.status === "running" && rv.session_id);
  $("#followup").hidden = !(rv && rv.session_id && rv.status !== "running");
}

async function review(id, message) {
  const rv = S.reviews[id] || (S.reviews[id] = { items: [], status: "running", session_id: null });
  rv.status = "running";
  rv.items.push({ event: "ask", data: { text: message } });
  S.streaming = true;
  renderClaims(); renderReview();
  try {
    const r = await fetch("/api/review", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: rv.session_id }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "", ended = false;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, i); buf = buf.slice(i + 2);
        let event = "message", data = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!data) continue;
        const d = JSON.parse(data);
        if (event === "session") { if (rv.session_id === d.session_id) continue; rv.session_id = d.session_id; }
        rv.items.push({ event, data: d });
        if (event === "done" || event === "error") { rv.status = event === "done" ? "done" : "error"; ended = true; }
        renderReview();
        $("#timeline").lastElementChild?.scrollIntoView({ block: "nearest" });
      }
    }
    if (!ended) { rv.status = "error"; rv.items.push({ event: "error", data: { message: "连接断了：带着会话 id 再问一次可以接着拿" } }); }
  } catch (e) {
    rv.status = "error";
    rv.items.push({ event: "error", data: { message: e.message } });
  }
  S.streaming = false;
  renderClaims(); renderReview();
}

$("#interrupt").addEventListener("click", async () => {
  const rv = S.reviews[S.current];
  if (rv?.session_id) await api("/api/interrupt", { session_id: rv.session_id }).catch((e) => alert(e.message));
});
$("#followup").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = $("#fu-text").value.trim();
  if (!t || S.streaming) return;
  $("#fu-text").value = "";
  review(S.current, t);
});
$("#prepare").addEventListener("click", async (e) => {
  e.target.disabled = true;
  try {
    const r = await api("/api/prepare", {});
    for (const run of r.runs) logRun(run);
    S.state = r.state;
    const bad = r.runs.find((x) => x.error || x.calls.some((c) => c.status >= 400));
    if (bad) alert(`准备到「${bad.step}」停下了：${bad.error || "请求失败"}（详情见“对接能力”）`);
  } catch (err) { alert(err.message); }
  e.target.disabled = false;
  renderIds(); renderSteps();
});

// ---------------- 对接能力 ----------------
const GROUPS = ["查", "组", "发", "调", "看", "错误处理", "清理"];

function renderSteps() {
  const s = S.state;
  const html = GROUPS.map((g) => {
    if (g === "看") {
      const lg = s.system_login || {};
      return `<div class="step-group"><h3>看：在 Matrix 网页上看</h3><div class="info">
        <p>系统账号可以登录 Matrix 网页（只读）：看它调用服务的会话、它发布的服务的用量。</p>
        <p>打开 <a href="${esc(s.matrix_url)}/system" target="_blank" rel="noopener">${esc(s.matrix_url)}/system</a>，
          用 <code>${esc(lg.email)}</code> / <code>${esc(lg.password)}</code> 登录。</p>
        <p class="muted">负责人 maya@demo.local 用自己的账号登录 Matrix，在项目「报销审核」里等同主持人：看群聊、改配置、撤回服务。</p></div></div>`;
    }
    const rows = S.steps.filter((x) => x.group === g).map((x) => `<div class="step">
      <span class="t">${esc(x.title)}${S.done.has(x.id) ? '<span class="mark badge ok">已执行</span>' : ""}</span>
      <span class="api">${esc(x.api)}</span><span class="d">${esc(x.desc)}</span>
      <button data-step="${esc(x.id)}">执行</button></div>`).join("");
    return `<div class="step-group"><h3>${esc(g)}</h3>${rows}</div>`;
  }).join("");
  $("#steps").innerHTML = html;
  for (const b of document.querySelectorAll("#steps button[data-step]")) b.addEventListener("click", () => runStep(b.dataset.step, b));
}

function callHtml(c) {
  const cls = c.status >= 500 ? "e5" : c.status >= 400 ? "e4" : "";
  const json = (v) => esc(typeof v === "string" ? v : JSON.stringify(v, null, 1));
  return `<li class="call"><div class="line"><span class="m">${esc(c.method)}</span><span class="p">${esc(c.path)}</span>
      <span class="s ${cls}">${c.status}</span><span class="who">令牌：${esc(c.token)}</span></div>
    ${c.note ? `<div class="note">${esc(c.note)}</div>` : ""}
    ${c.body !== null && c.body !== undefined ? `<details><summary>请求体</summary><pre>${json(c.body)}</pre></details>` : ""}
    <details ${c.status >= 400 ? "open" : ""}><summary>响应</summary><pre>${json(c.response)}</pre></details></li>`;
}

function logRun(r) {
  const log = $("#log");
  log.querySelector(".empty")?.remove();
  const title = S.steps.find((x) => x.id === r.step)?.title || r.step;
  log.insertAdjacentHTML("afterbegin", `<li><div class="muted"><b>${esc(title)}</b>${r.error ? ` <span class="step-err">· ${esc(r.error)}</span>` : ""}</div>
    <ol class="log">${r.calls.map(callHtml).join("")}</ol></li>`);
  if (!r.error) S.done.add(r.step);
}

async function runStep(id, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api(`/api/steps/${id}`, {});
    S.state = r.state;
    logRun(r);
  } catch (e) { alert(e.message); }
  renderIds(); renderSteps();
}

$("#run-all").addEventListener("click", async (e) => {
  e.target.disabled = true;
  for (const x of S.steps.filter((x) => !["withdraw", "delete"].includes(x.id))) {
    await runStep(x.id);
  }
  e.target.disabled = false;
});
$("#reset").addEventListener("click", async () => {
  if (!confirm("只清空报销系统这边记的项目 / 服务 id（Matrix 里的不动）。要删掉 Matrix 里的，先执行“清理”。")) return;
  S.state = (await api("/api/reset", {})).state;
  S.done.clear();
  renderIds(); renderSteps();
});

// ---------------- 标签 ----------------
for (const t of document.querySelectorAll(".tabs button")) {
  t.addEventListener("click", () => {
    for (const x of document.querySelectorAll(".tabs button")) x.setAttribute("aria-selected", String(x === t));
    $("#claims").hidden = t.dataset.tab !== "claims";
    $("#console").hidden = t.dataset.tab !== "console";
  });
}

(async () => {
  const r = await api("/api/state");
  Object.assign(S, { state: r.state, steps: r.steps, claims: r.claims });
  renderIds(); renderClaims(); renderSteps();
})();
