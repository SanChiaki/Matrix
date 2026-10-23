// 销售漏斗：看板（按阶段分列，拖动改阶段）、新增 / 编辑线索、把概况发到群聊（宿主页面会弹确认卡）。
// 这段代码跑在 Matrix 的沙箱里：不能联网，所有读写都经 window.matrix（桥）。
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let ctx, table, members = [], rows = [];

const fieldOf = (k) => table.fields.find((f) => f.key === k);
const memberName = (id) => members.find((m) => m.id === id)?.name || "";
const money = (n) => n >= 10000 ? `¥ ${(n / 10000).toFixed(n % 10000 ? 1 : 0)} 万` : `¥ ${n || 0}`;
const fail = (e) => { $("#err").textContent = e.message || String(e); };

async function load() {
  rows = await matrix.records.list("leads", { q: $("#q").value, limit: 500 });
  render();
}

function render() {
  const stages = fieldOf("stage").options;
  const open = rows.filter((r) => !["成交", "流失"].includes(r.fields.stage));
  const quoting = rows.filter((r) => r.fields.stage === "报价").reduce((n, r) => n + (r.fields.amount || 0), 0);
  const won = rows.filter((r) => r.fields.stage === "成交");
  $("#kpis").innerHTML = `<div class="kpi"><b>${open.length}</b><span>进行中</span></div>
    <div class="kpi"><b>${money(quoting)}</b><span>报价中金额</span></div>
    <div class="kpi"><b>${won.length}</b><span>已成交</span></div>`;
  matrix.ui.badge(String(rows.filter((r) => (r.fields.stage || "线索") === "线索").length || ""));
  $("#board").innerHTML = stages.map((s) => {
    const list = rows.filter((r) => (r.fields.stage || "线索") === s);
    return `<section class="col" data-stage="${esc(s)}"><h2><span>${esc(s)}</span><span>${list.length}</span></h2>
      ${list.map((r) => `<div class="lead" draggable="true" data-id="${esc(r.id)}">${esc(r.fields.company)}
        <div class="meta"><span>${[r.fields.source && esc(r.fields.source), r.fields.amount && money(r.fields.amount)].filter(Boolean).join(" · ")}</span>
        <span class="who">${r.fields.owner_agent ? "@" + esc(memberName(r.fields.owner_agent)) : "未分配"}</span></div></div>`).join("")}</section>`;
  }).join("");
}

function openEditor(rec) {
  const f = rec?.fields || { stage: "线索" };
  const opt = (k) => fieldOf(k).options.map((o) => `<option ${f[k] === o ? "selected" : ""}>${esc(o)}</option>`).join("");
  const by = (b) => !b ? "" : b.agent ? memberName(b.agent) || "Agent" : b.trigger ? "外部回调" : b.via ? "页面" : "人";
  $("#form").innerHTML = `<h3 style="margin:0">${rec ? esc(f.company) : "新线索"}</h3>
    <label>公司</label><input name="company" required value="${esc(f.company || "")}">
    <label>联系人</label><input name="contact" value="${esc(f.contact || "")}">
    <label>来源</label><select name="source"><option value=""></option>${opt("source")}</select>
    <label>阶段</label><select name="stage">${opt("stage")}</select>
    <label>金额（元）</label><input name="amount" type="number" min="0" value="${esc(f.amount ?? "")}">
    <label>负责成员</label><select name="owner_agent"><option value="">未分配</option>${members.map((m) => `<option value="${esc(m.id)}" ${f.owner_agent === m.id ? "selected" : ""}>${esc(m.name)}</option>`).join("")}</select>
    <label>备注</label><textarea name="note" rows="3">${esc(f.note || "")}</textarea>
    ${rec ? `<div class="hist">创建：${by(rec.created_by)} · 最近修改：${by(rec.updated_by)} ${new Date(rec.updated_at * 1000).toLocaleString()}</div>` : ""}
    <p class="err" id="ferr"></p>
    <div class="row">${rec ? '<button type="button" class="ghost" id="del">删除</button>' : ""}<button type="button" class="ghost" value="cancel" id="cancel">取消</button><button type="submit">保存</button></div>`;
  const dlg = $("#edit");
  $("#cancel").onclick = () => dlg.close();
  $("#del")?.addEventListener("click", async () => { await matrix.records.remove("leads", rec.id).catch(fail); dlg.close(); load(); });
  $("#form").onsubmit = async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData($("#form")));
    data.amount = data.amount === "" ? null : Number(data.amount);
    try {
      if (rec) await matrix.records.update("leads", rec.id, data); else await matrix.records.create("leads", data);
      dlg.close(); load();
    } catch (err) { $("#ferr").textContent = err.message; }
  };
  dlg.showModal();
}

$("#board").addEventListener("click", (e) => {
  const c = e.target.closest(".lead");
  if (c) openEditor(rows.find((r) => r.id === c.dataset.id));
});
$("#board").addEventListener("dragstart", (e) => e.dataTransfer.setData("text/plain", e.target.closest(".lead")?.dataset.id || ""));
$("#board").addEventListener("dragover", (e) => { const col = e.target.closest(".col"); if (col) { e.preventDefault(); col.classList.add("over"); } });
$("#board").addEventListener("dragleave", (e) => e.target.closest(".col")?.classList.remove("over"));
$("#board").addEventListener("drop", async (e) => {
  const col = e.target.closest(".col"); if (!col) return;
  e.preventDefault(); col.classList.remove("over");
  const id = e.dataTransfer.getData("text/plain");
  const r = rows.find((x) => x.id === id);
  if (r && r.fields.stage !== col.dataset.stage) {
    await matrix.records.update("leads", id, { stage: col.dataset.stage }).catch(fail);  // 改到“报价”会引发触发
    load();
  }
});
$("#new").onclick = () => openEditor(null);
$("#q").oninput = () => load().catch(fail);
$("#post").onclick = async () => {
  const count = (s) => rows.filter((r) => r.fields.stage === s).length;
  const quoting = rows.filter((r) => r.fields.stage === "报价").map((r) => r.fields.company).slice(0, 5).join("、");
  const owner = members[0]?.name;
  const text = `销售漏斗概况：线索 ${count("线索")}、沟通 ${count("沟通")}、报价 ${count("报价")}${quoting ? `（${quoting}）` : ""}、成交 ${count("成交")}、流失 ${count("流失")}。`
    + (owner ? `\n@${owner} 看一下报价中的几家，有需要我拍板的告诉我。` : "");
  try { const r = await matrix.chat.post(text); if (r.sent) $("#err").textContent = ""; } catch (err) { fail(err); }
};

matrix.on((event) => { if (event === "records.changed") load().catch(fail); });

(async () => {
  try {
    ctx = await matrix.context();
    if (ctx.theme === "light") document.documentElement.classList.add("light");
    table = ctx.tables.find((t) => t.key === "leads");
    members = await matrix.members();
    await load();
  } catch (e) { fail(e); }
})();
