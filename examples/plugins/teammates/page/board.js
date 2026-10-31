// 任务板：只读。经桥调插件服务的只读工具 tasks（service.call），看的人的权限。
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const COLS = [["todo", "待认领"], ["in_progress", "进行中"], ["in_review", "待验收"], ["done", "已完成"], ["closed", "已关闭"]];

async function load() {
  $("#err").textContent = "";
  const rows = JSON.parse(await matrix.call("service.call", { tool: "tasks", arguments: { format: "json" } }));
  $("#kpis").innerHTML = COLS.slice(0, 3).map(([k, label]) =>
    `<div class="kpi"><b>${rows.filter((t) => t.status === k).length}</b><span>${label}</span></div>`).join("");
  $("#board").innerHTML = COLS.map(([k, label]) => {
    const items = rows.filter((t) => t.status === k);
    return `<section class="col"><h2><span>${label}</span><span>${items.length}</span></h2>${items.map((t) => `
      <div class="task"><span class="id">#${t.id}</span>${esc(t.title)}
        <div class="meta">${t.assignee ? `负责人 ${esc(t.assignee)}` : "无人认领"} · 创建 ${esc(t.creator)}</div>
        <details><summary>记录</summary><ul>${t.history.slice(-8).map((h) =>
          `<li>${esc(h.at)} ${esc(h.by)}：${esc(h.change)}</li>`).join("")}</ul></details></div>`).join("")}</section>`;
  }).join("");
}

matrix.on((event) => { if (event === "records.changed") load().catch((e) => { $("#err").textContent = e.message; }); });
(async () => {
  const ctx = await matrix.context();
  if (ctx.theme === "light") document.documentElement.classList.add("light");
  await load();
})().catch((e) => { $("#err").textContent = e.message; });
