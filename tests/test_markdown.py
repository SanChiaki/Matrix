"""前端 Markdown 渲染（matrix/static/md.js）：链接只认安全地址，其余显示成文字加路径，不出现原样的 [文字](路径)；
群聊里人发的消息也走它，@成员 用 mapText 只在正文里标出。Markdown 里的 HTML 只认白名单里的标签和属性，按白名单重拼，
外站图片不自动加载，标签补齐配对。

用 node 直接跑 md.js；没装 node 时跳过（CI 三端的 runner 都带 node）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

MD = Path(__file__).resolve().parents[1] / "matrix" / "static" / "md.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")


def render(*sources: str) -> list[str]:
    script = (f"import {{ renderMarkdown }} from {json.dumps(MD.as_uri())};\n"
              f"console.log(JSON.stringify({json.dumps(list(sources))}.map(renderMarkdown)));")
    out = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return json.loads(out.stdout)


def test_relative_link_shows_text_and_path():
    [html] = render("出处：[协同白板竞品定价调研](wiki/research/market-report.md)。")
    assert html == "<p>出处：协同白板竞品定价调研 <code>wiki/research/market-report.md</code>。</p>"


def test_safe_links_stay_links_and_unsafe_never_do():
    ok, bad, injected = render("[官网](https://example.com/a?b=1&c=2)", "[点我](javascript:alert(1))",
                               "[x](wiki/<img src=x onerror=alert(1)>.md)")
    assert '<a href="https://example.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">官网</a>' in ok
    assert "<a" not in bad and "href" not in bad
    assert "<img" not in injected and "&lt;img" in injected


def mention(*sources: str) -> list[str]:
    """和群聊里一样：先渲染 Markdown，再用 mapText 只在正文里把 @知识管家 标出来。"""
    script = (f"import {{ renderMarkdown, mapText }} from {json.dumps(MD.as_uri())};\n"
              "const mark = (h) => mapText(renderMarkdown(h), (p) => p.replace(/@知识管家/g, '<span class=\"mention\">$&</span>'));\n"
              f"console.log(JSON.stringify({json.dumps(list(sources))}.map(mark)));")
    out = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return json.loads(out.stdout)


def test_human_message_markdown_keeps_mentions():
    [html] = mention("@知识管家 看一下：\n- **定价**区间\n- 出处 [报告](https://example.com/r)")
    assert '<span class="mention">@知识管家</span>' in html
    assert "<ul><li><strong>定价</strong>区间</li>" in html and '<a href="https://example.com/r"' in html


def test_mentions_not_marked_inside_code_links_or_tags():
    code, link, attr, unsafe = mention("`@知识管家`", "[@知识管家](https://example.com/@知识管家)",
                                        "[x](https://example.com/?q=@知识管家)", "<b>@知识管家</b>")
    assert code == "<p><code>@知识管家</code></p>"
    assert "mention" not in link and "mention" not in attr  # 链接文字和 href 里都不动
    assert unsafe == '<p><b><span class="mention">@知识管家</span></b></p>'  # 白名单里的 HTML 照常渲染，里面的 @ 照样标出


def test_html_in_markdown_renders_through_an_allowlist():
    basic, block, attrs, table = render(
        "a <b>粗</b> <kbd>Ctrl</kbd> H<sub>2</sub>O<br>换行",
        "<details>\n<summary>展开</summary>\n\n- **列表**\n\n</details>\n\n后面",
        '<div align="center" style="color:red" onclick="x()">居中</div>',
        '<table>\n<tr><td colspan=2 rowspan="x">c</td></tr>\n</table>')
    assert basic == "<p>a <b>粗</b> <kbd>Ctrl</kbd> H<sub>2</sub>O<br>换行</p>"
    assert block == "<details> <summary>展开</summary><ul><li><strong>列表</strong></li></ul></details><p>后面</p>"
    assert attrs == '<div align="center">居中</div>'  # style、事件属性丢掉
    assert '<td colspan="2">c</td>' in table and "\n" not in table  # 消息正文是 pre-wrap：HTML 块里不留换行


def test_html_outside_the_allowlist_never_reaches_the_page():
    out = render("<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "<svg onload=alert(1)>",
                 '<a href="javascript:alert(1)">坏</a>', '<a href="https://e.com" onmouseover="x">好</a>',
                 '<b title="**x** https://e.com">t</b>', "`<b>代码</b>` 和 <!-- 注释 --> 后", "<iframe src=//e.com>")
    script, img, svg, js, ok, title, code, iframe = out
    for h in (script, img, svg, iframe):
        assert "<" not in h.replace("<p>", "").replace("</p>", "")  # 原样显示成文字
    assert js == "<p><a>坏</a></p>"
    assert ok == '<p><a href="https://e.com" target="_blank" rel="noopener noreferrer">好</a></p>'
    assert title == '<p><b title="**x** https://e.com">t</b></p>'  # 属性里的内容不再被当成 Markdown
    assert code == "<p><code>&lt;b&gt;代码&lt;/b&gt;</code> 和  后</p>"
    real_tags = [t for h in out for t in re.findall(r"<[^>]*>", h)]  # 显示成文字的 &lt;…&gt; 不算
    assert not [t for t in real_tags if re.search(r"\s(on\w+|style)\s*=", t)]


def test_external_images_are_links_not_loaded():
    [html] = render('<img src="https://evil.com/p?d=secret" alt="图"> <img src="/static/logo.svg" width=20> <img src="//evil.com/x">')
    assert '<a href="https://evil.com/p?d=secret" target="_blank" rel="noopener noreferrer">图</a>' in html
    assert '<img src="/static/logo.svg" loading="lazy" width="20">' in html
    assert html.count("<img") == 1  # 外站的（含 //host 写法）都不会自己去加载


def test_tags_are_balanced_so_one_message_cannot_swallow_the_next():
    unclosed, extra = render("<div>没关\n\n下一段", "多余</div></span>关闭")
    assert unclosed == "<div>没关<p>下一段</p></div>"
    assert extra == "<p>多余关闭</p>"
