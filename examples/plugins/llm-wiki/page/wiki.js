// 项目 Wiki 的页面：只读。经桥调插件服务的只读工具（service.call），按看页面的人的权限执行。
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const call = (tool, args) => matrix.call("service.call", { tool, arguments: args || {} });
let current = "wiki/index.md";

function fail(e) { $("#err").textContent = e.message || String(e); }

// 最小的 markdown：标题、引用、列表、代码、行内链接与代码。先转义再渲染；站内链接按 wiki 路径打开
function resolve(base, href) {
  if (/^(wiki|raw)\//.test(href)) return href.split("#")[0];  // 相对数据根目录的写法
  const parts = base.split("/").slice(0, -1);
  for (const p of href.split("#")[0].split("/")) {
    if (p === "..") parts.pop(); else if (p && p !== ".") parts.push(p);
  }
  return parts.join("/");
}
function inline(s, base) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, (_, t, h) => {
      const href = h.replace(/&amp;/g, "&");
      if (/^https?:\/\//.test(href)) return `<a href="${esc(href)}" target="_blank" rel="noopener">${t}</a>`;
      return `<a href="#" data-path="${esc(resolve(base, href))}">${t}</a>`;
    });
}
function render(md, base) {
  const out = []; let list = null, code = null;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const line of md.split("\n")) {
    if (line.startsWith("```")) { if (code === null) { close(); code = []; } else { out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`); code = null; } continue; }
    if (code !== null) { code.push(line); continue; }
    let m;
    if ((m = line.match(/^(#{1,3}) (.*)/))) { close(); out.push(`<h${m[1].length}>${inline(m[2], base)}</h${m[1].length}>`); }
    else if ((m = line.match(/^> ?(.*)/))) { close(); out.push(`<blockquote>${inline(m[1], base)}</blockquote>`); }
    else if ((m = line.match(/^\s*[-*] (.*)/))) { if (list !== "ul") { close(); out.push("<ul>"); list = "ul"; } out.push(`<li>${inline(m[1], base)}</li>`); }
    else if ((m = line.match(/^\s*\d+\. (.*)/))) { if (list !== "ol") { close(); out.push("<ol>"); list = "ol"; } out.push(`<li>${inline(m[1], base)}</li>`); }
    else if (!line.trim()) close();
    else { close(); out.push(`<p>${inline(line, base)}</p>`); }
  }
  close();
  return out.join("");
}

async function nav() {
  const text = await call("list");
  const [pages, raws] = text.split("## 原始资料");
  const items = (s) => [...s.matchAll(/^- (\S+?)(?:：(.*))?$/gm)].map((m) => ({ path: m[1], title: m[2] || m[1].split("/").pop() }));
  const link = (x) => `<a href="#" data-path="${esc(x.path)}" class="${x.path === current ? "on" : ""}">${esc(x.title)}</a>`;
  const p = items(pages || ""), r = items(raws || "");
  $("#nav").innerHTML = `<h3>页面</h3>${p.map(link).join("") || '<div class="empty">还没有页面</div>'}`
    + `<h3>原始资料</h3>${r.map(link).join("") || '<div class="empty">还没有原始资料</div>'}`;
}

async function open(path, version) {
  $("#err").textContent = "";
  current = path;
  const text = await call("read", version ? { path, version } : { path });
  const hist = path.startsWith("wiki/") && !path.endsWith("index.md") && !path.endsWith("log.md")
    ? await call("history", { path }) : "";
  const versions = [...hist.matchAll(/^- (\S+)/gm)].map((m) => m[1]);
  $("#doc").innerHTML = `<div class="path"><span>${esc(path)}${version ? `（旧版本 ${esc(version)}）` : ""}</span>`
    + (version ? `<button class="ghost" data-path="${esc(path)}">看最新版</button>` : "")
    + versions.map((v) => `<button class="ghost" data-path="${esc(path)}" data-version="${esc(v)}">${esc(v)}</button>`).join("")
    + `</div>${render(text, path)}`;
  await nav();
}

async function search(q) {
  const text = await call("search", { query: q, scope: "all" });
  const hits = [...text.matchAll(/^- (\S+)（(.*?)）\n((?:  > .*\n?)*)/gm)];
  $("#doc").innerHTML = `<h1>搜索：${esc(q)}</h1>` + (hits.length ? `<div class="hits">${hits.map((m) =>
    `<div class="hit"><a href="#" data-path="${esc(m[1])}">${esc(m[2])}</a><small>${esc(m[3].replace(/^  > /gm, ""))}</small></div>`).join("")}</div>`
    : `<p>${esc(text)}</p>`);
}

async function lint() {
  const text = await call("lint");
  const [head, ...rest] = text.split("\n");
  $("#doc").innerHTML = `<h1>体检结果</h1><p>${esc(head)}</p>`
    + (rest.length ? `<ul>${rest.map((l) => `<li class="bad">${inline(l.replace(/^- /, ""), "wiki/x.md")}</li>`).join("")}</ul>` : "")
    + "<p><small>要修就在群聊里 @ 成员，让它按技能 wiki-lint 处理。</small></p>";
}

document.addEventListener("click", (e) => {
  const a = e.target.closest("[data-path]");
  if (!a) return;
  e.preventDefault();
  open(a.dataset.path, a.dataset.version).catch(fail);
});
$("#home").onclick = () => open("wiki/index.md").catch(fail);
$("#log").onclick = () => open("wiki/log.md").catch(fail);
$("#lint").onclick = () => lint().catch(fail);
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.value.trim()) search(e.target.value.trim()).catch(fail); });
matrix.on((event) => { if (event === "records.changed") open(current).catch(fail); });

(async () => {
  const ctx = await matrix.context();
  if (ctx.theme === "light") document.documentElement.classList.add("light");
  await open("wiki/index.md");
})().catch(fail);
