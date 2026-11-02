"""设计语言的结构检查（docs/design-system.md）：变量只有一个来源，迁移过的文件不写死颜色。

参照 AGENTS.md 工作方式第 8 条：靠记忆的规则会失效，所以写成能变红的检查。
覆盖主界面的全部样式表、页面与脚本，以及示例插件的页面：新加的文件自动纳入。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "matrix" / "static"
TOKENS = STATIC / "tokens.css"

# 只用 tokens.css 变量的文件：主界面全部，加上示例插件的页面（经 ../_matrix/tokens.css 用同一份变量）
MIGRATED = sorted(
    {p for p in STATIC.iterdir() if p.suffix in {".css", ".html", ".js"} and p != TOKENS}
    | {p for p in ROOT.glob("examples/**/page/*") if p.suffix in {".css", ".html", ".js"}})

COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")
VAR_USE = re.compile(r"var\(\s*(--[a-z0-9-]*[a-z0-9])\s*[,)]")  # 拼出来的变量名（var(--sp-${n})）不算
VAR_DEF = re.compile(r"(--[a-z0-9-]+)\s*:")


def _css_of(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".html":  # 只看 <style> 和 style="" 里的样式，正文里的 #锚点 不算
        return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", text, re.S) + re.findall(r'style="([^"]*)"', text))
    if path.suffix == ".js":  # 只看拼进页面的 style="…" 与单独写成字符串的颜色；#id 选择器不算
        return "\n".join(re.findall(r'style="([^"]*)"', text)
                         + re.findall(r"""["'`](#[0-9a-fA-F]{3,8}|rgba?\([^)]*\))["'`]""", text))
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def test_tokens_defined_for_both_themes():
    """深色定义的每个颜色变量，浅色里要么重定义、要么是两种主题通用的（字体、刻度、动效等）。"""
    text = TOKENS.read_text(encoding="utf-8")
    dark, light = text.split(':root[data-theme="light"]', 1)
    light = light.split("@media", 1)[0]
    light_defs = set(VAR_DEF.findall(light))
    colored = {name for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", dark) if COLOR.search(value)}
    shared = {"--on-color", "--scrim", "--scrim-strong", "--shadow-pop",
              "--file-other", "--file-doc", "--file-sheet", "--file-code", "--file-image",
              *(f"--project-{n}" for n in range(1, 7))}
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


SPACE_SCALE = {0, 1, 2, 4, 6, 8, 12, 16, 20, 24, 32, 40, 48, 56, 64}


def test_migrated_files_stay_on_the_scale():
    """字号、圆角只用变量；外边距、内边距、间隔落在 4px 刻度上（1、2、6 是细线与紧凑处的例外，见 docs/design-system.md）。"""
    bad = []
    for path in MIGRATED:
        for line in _css_of(path).splitlines():
            if re.match(r"\s*--[a-z0-9-]+\s*:", line):
                continue  # 变量定义
            where = f"{path.relative_to(ROOT)}: {line.strip()[:100]}"
            if re.search(r"font-size:\s*[0-9.]+px|\bfont:\s*[0-9.]+px", line):
                bad.append("字号不用变量  " + where)
            if re.search(r"border(?:-[a-z]+)?-radius:[^;}]*\b[1-9][0-9.]*px", line):
                bad.append("圆角不用变量  " + where)
            for m in re.finditer(r"(?:padding|margin|gap|row-gap|column-gap)(?:-[a-z]+)?:\s*([^;}\"]+)", line):
                for v in re.findall(r"\b([0-9.]+)px", m.group(1)):
                    if float(v) not in SPACE_SCALE and float(v) <= 64:
                        bad.append(f"间距 {v}px 不在刻度上  " + where)
    assert not bad, "\n".join(bad)


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
    """每个页面都加载 tokens.css，且在 style.css 之前。"""
    for page in ("index.html", "admin.html", "ui.html", "about.html"):
        html = (STATIC / page).read_text(encoding="utf-8")
        assert '/static/tokens.css' in html, page
        if "/static/style.css" in html:
            assert html.index("/static/tokens.css") < html.index("/static/style.css"), page


def test_checked_files_found():
    """检查确实覆盖到了文件（路径改了不会让检查悄悄变成空跑）。"""
    names = {p.name for p in MIGRATED}
    assert {"style.css", "admin.css", "app.js", "about.html", "index.html", "ui.html"} <= names
    assert sum(1 for p in MIGRATED if p.name == "style.css" and "examples" in p.parts) >= 4
