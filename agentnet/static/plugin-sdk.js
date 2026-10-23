// Matrix 插件页面的桥客户端（在插件页面里用：<script src="../_matrix/sdk.js"></script>，路径相对插件页面）。
// 插件页面跑在沙箱里：拿不到登录凭证、不能联网。一切读写经父页面转给网络，按看页面的人的权限执行。
// 用法：
//   const ctx = await matrix.context();                 // 项目、插件、表结构、能用的能力、主题
//   const rows = await matrix.records.list("leads", { q: "启明", where: { stage: "沟通" } });
//   await matrix.records.create("leads", { company: "…", stage: "线索" });
//   await matrix.records.update("leads", id, { stage: "报价" });
//   const { sent } = await matrix.chat.post("@销售助理 …");   // 父页面弹确认卡，人点“发送”才发
//   matrix.on((event, data) => { if (event === "records.changed") reload(); });
(function () {
  const pending = new Map();
  const listeners = new Set();
  let seq = 0;
  addEventListener("message", (e) => {
    if (e.source !== parent) return;
    const d = e.data || {};
    if (d.matrix !== 1) return;
    if (d.id && pending.has(d.id)) {
      const [ok, no] = pending.get(d.id);
      pending.delete(d.id);
      if (d.ok) ok(d.result); else no(new Error(d.error || "调用失败"));
    } else if (d.event) {
      listeners.forEach((f) => { try { f(d.event, d.data); } catch (err) { console.error(err); } });
    }
  });
  function call(op, args) {
    return new Promise((ok, no) => {
      const id = `c${++seq}`;
      pending.set(id, [ok, no]);
      parent.postMessage({ matrix: 1, id, op, args: args || {} }, "*");
    });
  }
  window.matrix = {
    call,
    context: () => call("context"),
    records: {
      list: (table, opts) => call("records.list", { table, ...(opts || {}) }),
      create: (table, fields) => call("records.create", { table, fields }),
      update: (table, id, fields) => call("records.update", { table, id, fields }),
      remove: (table, id) => call("records.delete", { table, id }),
    },
    archive: { read: () => call("archive.read"), file: (name) => call("archive.file", { name }) },
    members: () => call("members.read"),
    chat: { read: (limit) => call("chat.read", { limit }), post: (text) => call("chat.post", { text }) },
    requests: () => call("requests.read"),
    ui: {
      badge: (text) => call("ui.badge", { text }),
      openFile: (name) => call("ui.file", { name }),
    },
    on: (f) => { listeners.add(f); return () => listeners.delete(f); },
  };
})();
