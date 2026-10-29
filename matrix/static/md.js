// 轻量 Markdown 渲染：先整体转义再识别语法，输出里不会出现模型原文中的 HTML。
// 支持标题、段落、列表（含嵌套与任务项）、引用、代码块、表格、分隔线、链接与行内强调。
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

function safeUrl(raw) {
  const u = raw.replace(/&amp;/g, "&").trim();
  return /^(https?:|mailto:|#|\/)/i.test(u) ? esc(u) : null;
}

function inline(src) {
  const codes = [];
  let s = esc(src).replace(/`([^`\n]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
  s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, text, url) => {
    const href = safeUrl(url);
    return href ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>` : m;
  });
  // 裸链接：前面不能是字母数字或 href=" / > 之类，避免把已生成的 <a> 再包一层
  // 中文标点（全角括号、引号、冒号等）不算进链接：“地址（说明）”里的“（”是正文
  s = s.replace(/(^|[^\w/"'=>])(https?:\/\/[^\s<)）（「」【】《》，。；、：！？]+?)(?=[。，；、：！？（「」【】《》]|&quot;|&#39;|[\s<)）]|$)/g, (_, pre, url) =>
    `${pre}<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
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
    for (; i < lines.length && lines[i].trim() && !/^\s*(```|~~~|#{1,6}\s|>)/.test(lines[i]) && !LIST.test(lines[i])
      && !(lines[i].includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])); i++) para.push(lines[i]);
    out.push(`<p>${para.map(inline).join("<br>")}</p>`);
  }
  return out.join("");
}
