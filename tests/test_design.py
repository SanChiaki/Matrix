"""设计语言的结构检查（docs/design-system.md）：变量只有一个来源，迁移过的文件不写死颜色。

参照 AGENTS.md 工作方式第 8 条：靠记忆的规则会失效，所以写成能变红的检查。
还没迁移的文件列在 PENDING 里；迁移完一个就从列表里删掉，检查随之覆盖它。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "matrix" / "static"
TOKENS = STATIC / "tokens.css"

# 已经只用 tokens.css 变量的文件：不许出现字面颜色
MIGRATED = [STATIC / "style.css", STATIC / "admin.css", STATIC / "index.html", STATIC / "admin.html",
            STATIC / "ui.html"]
# 还没迁移的（盘点见 docs/design-system.md 的“迁移进度”）：迁完就删掉，并加进 MIGRATED
PENDING = [STATIC / "about.html", STATIC / "app.js",
           *sorted(ROOT.glob("examples/**/page/style.css"))]

COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")
VAR_USE = re.compile(r"var\(\s*(--[a-z0-9-]*[a-z0-9])\s*[,)]")  # 拼出来的变量名（var(--sp-${n})）不算
VAR_DEF = re.compile(r"(--[a-z0-9-]+)\s*:")


def _css_of(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".html":  # 只看 <style> 和 style="" 里的样式，正文里的 #锚点 不算
        return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", text, re.S) + re.findall(r'style="([^"]*)"', text))
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def test_tokens_defined_for_both_themes():
    """深色定义的每个颜色变量，浅色里要么重定义、要么是两种主题通用的（字体、刻度、动效等）。"""
    text = TOKENS.read_text(encoding="utf-8")
    dark, light = text.split(':root[data-theme="light"]', 1)
    light = light.split("@media", 1)[0]
    light_defs = set(VAR_DEF.findall(light))
    colored = {name for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", dark) if COLOR.search(value)}
    shared = {"--on-color", "--scrim", "--scrim-strong", "--shadow-pop",
              "--file-other", "--file-doc", "--file-sheet", "--file-code", "--file-image"}
    missing = sorted(colored - light_defs - shared)
    assert not missing, f"tokens.css 里这些颜色变量没有浅色值（或该加进 shared）：{missing}"


def test_migrated_files_use_tokens_only():
    """迁移过的页面不写死颜色：颜色只在 tokens.css 里定义。"""
    bad = []
    for path in MIGRATED:
        for line in _css_of(path).splitlines():
            if COLOR.search(line):
                bad.append(f"{path.relative_to(ROOT)}: {line.strip()[:120]}")
    assert not bad, "写死的颜色要换成 tokens.css 里的变量（没有合适的就先在 tokens.css 加一个）：\n" + "\n".join(bad)


def test_used_variables_are_defined():
    """用到的每个 var(--x) 都在 tokens.css 或该文件自己里定义过：拼错的变量名会静默失效。"""
    defined = set(VAR_DEF.findall(TOKENS.read_text(encoding="utf-8")))
    bad = []
    for path in MIGRATED:
        css = _css_of(path)
        for name in sorted(set(VAR_USE.findall(css)) - defined - set(VAR_DEF.findall(css))):
            bad.append(f"{path.relative_to(ROOT)}: {name}")
    assert not bad, "这些变量没有定义：\n" + "\n".join(bad)


def test_pages_load_tokens_first():
    """主界面和管理后台先加载 tokens.css 再加载 style.css。"""
    for page in ("index.html", "admin.html", "ui.html"):
        html = (STATIC / page).read_text(encoding="utf-8")
        assert '/static/tokens.css' in html, page
        assert html.index("/static/tokens.css") < html.index("/static/style.css"), page


def test_pending_list_is_current():
    """PENDING 里的文件都还在，且没有同时出现在 MIGRATED 里。"""
    for path in PENDING:
        assert path.exists(), f"{path} 不存在了，从 PENDING 删掉"
        assert path not in MIGRATED, f"{path} 已迁移，从 PENDING 删掉"
