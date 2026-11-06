// 轻量 Markdown 渲染：先整体转义再识别语法。原文里的 HTML 只认白名单里的标签和属性，由这里按白名单重新拼出来，
// 不会原样进页面：事件属性、style、不安全的链接都丢掉，白名单外的标签照旧显示成文字；标签最后补齐配对。
// 支持标题、段落、列表（含嵌套与任务项）、引用、代码块、表格、分隔线、链接、行内强调与这些 HTML。
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

function safeUrl(raw) {
  const u = raw.replace(/&amp;/g, "&").trim();
  return /^(https?:|mailto:|#|\/)/i.test(u) ? esc(u) : null;
}

// ---- Markdown 里的 HTML ----
// 每个标签允许的属性（title 所有标签都允许）。数值属性只认数字，align 只认三个值。
const TAGS = {
  b: [], strong: [], i: [], em: [], u: [], s: [], del: [], ins: [], mark: [], small: [], sub: [], sup: [], kbd: [], code: [],
  abbr: [], span: [], br: [], a: ["href"], img: ["src", "alt", "width", "height"],
  p: ["align"], div: ["align"], hr: [], h1: [], h2: [], h3: [], h4: [], h5: [], h6: [], blockquote: [], pre: [],
  ul: [], ol: ["start"], li: [], dl: [], dt: [], dd: [], details: ["open"], summary: [], figure: [], figcaption: [],
  table: [], thead: [], tbody: [], tfoot: [], tr: [], th: ["colspan", "rowspan", "align"], td: ["colspan", "rowspan", "align"],
};
const VOID = new Set(["br", "hr", "img", "input"]);
const BLOCK = /^<\/?(p|div|h[1-6]|hr|blockquote|pre|ul|ol|li|dl|dt|dd|details|summary|figure|figcaption|table|thead|tbody|tfoot|tr|th|td)(?=[\s/>]|$)/i;
const unesc = (s) => s.replace(/&(amp|lt|gt|quot|#39);/g, (m, e) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" })[e]);

// 一个已转义的标签（&lt;tag a="b"&gt;）：在白名单里就按白名单重拼，否则原样留作文字
function tag(m, close, name, rawAttrs) {
  const t = name.toLowerCase();
  const allowed = TAGS[t];
  if (!allowed) return m;
  if (close) return VOID.has(t) ? "" : `</${t}>`;
  const attrs = {};
  for (const [, k, v1, v2, v3] of unesc(rawAttrs).matchAll(/([a-zA-Z_:][-\w:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g)) {
    const key = k.toLowerCase(), val = v1 ?? v2 ?? v3 ?? "";
    if (key === "title" || allowed.includes(key)) attrs[key] = val;
  }
  let out = "";
  for (const [k, v] of Object.entries(attrs)) {
    if (["width", "height", "colspan", "rowspan", "start"].includes(k)) { if (/^\d{1,4}$/.test(v)) out += ` ${k}="${v}"`; }
    else if (k === "align") { if (/^(left|center|right)$/i.test(v)) out += ` align="${v.toLowerCase()}"`; }
    else if (k === "open") out += " open";
    else if (k !== "href" && k !== "src") out += ` ${k}="${esc(v)}"`;
  }
  if (t === "a") {
    const href = attrs.href && safeUrl(attrs.href);
    return href ? `<a href="${href}" target="_blank" rel="noopener noreferrer"${out}>` : "<a>";
  }
  if (t === "img") {
    // 只直接显示本站的图；外站的图不自动加载（加载本身就会把地址里的内容发出去），显示成一个链接，点了才看
    const src = (attrs.src || "").trim();
    if (/^\/(?!\/)/.test(src)) return `<img src="${esc(src)}" loading="lazy"${out}>`;
    if (/^https?:\/\//i.test(src)) return `<a href="${esc(src)}" target="_blank" rel="noopener noreferrer">${esc(attrs.alt || "图片")}</a>`;
    return m;
  }
  return `<${t}${out}>`;
}
// 重拼好的标签先换成占位符（\u0001序号\u0001），等后面的 Markdown 规则跑完再换回：链接、强调不会改到属性里去
const htmlTags = (s, keep) => s.replace(/&lt;!--[\s\S]*?--&gt;/g, "")
  .replace(/&lt;(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s(?:(?!&gt;|&lt;).)*?)?)\s*\/?&gt;/g, (m, c, n, a) => {
    const html = tag(m, c, n, a);
    return html === m ? m : `\u0001${keep.push(html) - 1}\u0001`;
  });

// 补齐标签配对：多出来的结束标签丢掉，没关的在末尾关上。消息是拼进一大段 HTML 里的，不能让一条消息的 <div> 吞掉后面的消息
function balance(html) {
  const stack = [];
  const out = html.replace(/<(\/?)([a-z][a-z0-9]*)\b[^>]*>/g, (m, close, t) => {
    if (VOID.has(t)) return m;
    if (!close) { stack.push(t); return m; }
    const at = stack.lastIndexOf(t);
    if (at < 0) return "";
    return stack.splice(at).reverse().map((x) => `</${x}>`).join("");
  });
  return out + stack.reverse().map((x) => `</${x}>`).join("");
}

function inline(src) {
  const codes = [];
  let s = esc(src).replace(/`([^`\n]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
  const tags = [];
  s = htmlTags(s, tags);  // 在反引号之后：`<b>` 这样写在代码里的照旧是文字
  // 链接到不能点的地址（如工作目录里的相对路径 wiki/x.md）：不留原样的 [文字](路径)，显示文字和路径
  s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, text, url) => {
    const href = safeUrl(url);
    return href ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`
      : `${text} \u0000${codes.push(url) - 1}\u0000`;
  });
  // 裸链接：前面不能是字母数字或 href=" / > 之类，避免把已生成的 <a> 再包一层
  // 中文标点（全角括号、引号、冒号等）不算进链接：“地址（说明）”里的“（”是正文
  s = s.replace(/(^|[^\w/"'=>])(https?:\/\/[^\s<)）（「」【】《》，。；、：！？\u0001]+?)(?=[。，；、：！？（「」【】《》\u0001]|&quot;|&#39;|[\s<)）]|$)/g, (_, pre, url) =>
    `${pre}<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  s = s.replace(/\u0001(\d+)\u0001/g, (_, i) => tags[i] ?? "");
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[i]}</code>`);
}

const LIST = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const isTableSep = (l) => /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(l) && l.includes("-");
const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

function list(lines, i) {
  const base = lines[i].match(LIST)[1].length;
  const ordered = /\d/.test(lines[i].match(LIST)[2]);
  const items = [];
  while (i < lines.length) {
    const m = lines[i].match(LIST);
    if (m && m[1].length === base && /\d/.test(m[2]) === ordered) {
      items.push([m[3]]);
      i++;
    } else if (m && m[1].length > base && items.length) {
      const [html, next] = list(lines, i);
      items[items.length - 1].push({ html });
      i = next;
    } else if (!m && lines[i].trim() && /^\s{2,}/.test(lines[i]) && items.length) {
      items[items.length - 1][0] += " " + lines[i].trim();
      i++;
    } else break;
  }
  const li = items.map(([text, ...nested]) => {
    const task = text.match(/^\[([ xX])\]\s+(.*)$/);
    const body = task ? `<input type="checkbox" disabled${task[1] !== " " ? " checked" : ""}> ${inline(task[2])}` : inline(text);
    return `<li${task ? ' class="task"' : ""}>${body}${nested.map((n) => n.html).join("")}</li>`;
  }).join("");
  return [`<${ordered ? "ol" : "ul"}>${li}</${ordered ? "ol" : "ul"}>`, i];
}

// 只改 renderMarkdown 输出里的正文文字（如把 @成员 标出来）：跳过标签本身，也跳过 code / pre / a 里面的文字。
// 输出里正文的 < 都已转义成 &lt;，所以按 < … > 切开就能分出标签。
export function mapText(html, fn) {
  let skip = 0;
  return html.split(/(<[^>]*>)/).map((part) => {
    if (part.startsWith("<")) {
      const tag = part.match(/^<(\/?)(code|pre|a)\b/i);
      if (tag) skip = Math.max(0, skip + (tag[1] ? -1 : 1));
      return part;
    }
    return skip ? part : fn(part);
  }).join("");
}

export function renderMarkdown(src) {
  const lines = String(src ?? "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const fence = line.match(/^\s*(```|~~~)\s*([\w+-]*)/);
    if (fence) {
      const body = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith(fence[1]); i++) body.push(lines[i]);
      i++;
      out.push(`<pre class="md-code"><code${fence[2] ? ` data-lang="${esc(fence[2])}"` : ""}>${esc(body.join("\n"))}</code></pre>`);
      continue;
    }
    if (/^\s*<!--/.test(line)) {  // 注释块：整段不显示
      while (i < lines.length && !lines[i].includes("-->")) i++;
      i++;
      continue;
    }
    if (BLOCK.test(line.trim())) {  // HTML 块：一直到空行，不包 <p>（和 CommonMark 一样，空行之后照常是 Markdown）
      const body = [];
      for (; i < lines.length && lines[i].trim(); i++) body.push(inline(lines[i]));
      // 行之间用空格接：消息正文是 pre-wrap，换行会变成空行；<pre> 里的换行要保留
      out.push(body.join(/^\s*<pre\b/i.test(line) ? "\n" : " "));
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (h) { out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i++; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
    if (/^\s*>/.test(line)) {
      const body = [];
      for (; i < lines.length && /^\s*>/.test(lines[i]); i++) body.push(lines[i].replace(/^\s*>\s?/, ""));
      out.push(`<blockquote>${renderMarkdown(body.join("\n"))}</blockquote>`);
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const head = cells(line);
      const rows = [];
      for (i += 2; i < lines.length && lines[i].includes("|") && lines[i].trim(); i++) rows.push(cells(lines[i]));
      out.push(`<div class="md-table"><table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead>`
        + `<tbody>${rows.map((r) => `<tr>${head.map((_, k) => `<td>${inline(r[k] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    if (LIST.test(line)) { const [html, next] = list(lines, i); out.push(html); i = next; continue; }
    const para = [];
    for (; i < lines.length && lines[i].trim() && !/^\s*(```|~~~|#{1,6}\s|>|<!--)/.test(lines[i]) && !LIST.test(lines[i])
      && !BLOCK.test(lines[i].trim())
      && !(lines[i].includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])); i++) para.push(lines[i]);
    out.push(`<p>${para.map(inline).join("<br>")}</p>`);
  }
  return balance(out.join(""));
}
