"""前端 Markdown 渲染（matrix/static/md.js）：链接只认安全地址，其余显示成文字加路径，不出现原样的 [文字](路径)。

用 node 直接跑 md.js；没装 node 时跳过（CI 三端的 runner 都带 node）。
"""

from __future__ import annotations

import json
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
