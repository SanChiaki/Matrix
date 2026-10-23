"""`matrix-plugin check <目录或 zip>`：在本地校验一个插件包，不用装进项目（docs/plugin-authoring.md）。

错误用的是安装时同一套校验（`plugins.check_package`），装得上就一定过得了这里。另外给出“提醒”：
装得上、但多半不是作者想要的写法（沙箱会拦下的外部资源与联网、桥客户端路径不对、用到了没申请的能力、
触发消息里的 {字段} 会换成空）。提醒是启发式的，不影响安装；`--strict` 时提醒也算失败（给 CI 用）。
"""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
from pathlib import Path

from . import plugins as pluginfs
from .models import PLUGIN_PAGE_CAPS

TEXT_SUFFIXES = (".html", ".htm", ".js", ".mjs", ".css")
SDK = "_matrix/sdk.js"
# 页面里的 SDK 调用 → 要申请的能力（matrix.call("op") 也算）
SDK_CAPS = (
    (r"matrix\.records\.list\b", "records.read"),
    (r"matrix\.records\.(?:create|update|remove)\b", "records.write"),
    (r"matrix\.archive\.(?:read|file)\b", "archive.read"),
    (r"matrix\.members\b", "members.read"),
    (r"matrix\.chat\.read\b", "chat.read"),
    (r"matrix\.chat\.post\b", "chat.post"),
    (r"matrix\.requests\b", "requests.read"),
)
CALL_CAPS = {"records.list": "records.read", "records.create": "records.write", "records.update": "records.write",
             "records.delete": "records.write", "archive.read": "archive.read", "archive.file": "archive.read",
             "members.read": "members.read", "chat.read": "chat.read", "chat.post": "chat.post",
             "requests.read": "requests.read"}
EXTERNAL = re.compile(r"""(?:\b(?:src|href)\s*=\s*["']|url\(\s*["']?|\bimport\s*(?:[^"';]*?\bfrom\s*)?["']|\bimport\(\s*["'])"""
                      r"""((?:https?:)?//[^"')\s]+)""", re.I)
NETWORK = re.compile(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|navigator\.sendBeacon)\s*\(|new\s+(XMLHttpRequest|WebSocket|EventSource)\b")
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{0,39})\}")
TRIGGER_CN = {"webhook": "外部回调", "schedule": "定时", "record": "记录变化"}


def load(src: Path) -> dict[Path, bytes]:
    """目录（和部署方目录读法一样）或 zip（和上传读法一样）。"""
    if src.is_dir():
        return pluginfs.dir_files(src)
    if src.is_file() and src.suffix.lower() == ".zip":
        return pluginfs.package_files(pluginfs.unzip(src.read_bytes()))
    raise pluginfs.PluginError(f"要给一个插件目录或 .zip 文件：{src}")


def _text(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


def lint(files: dict[Path, bytes], m: pluginfs.Manifest) -> list[str]:
    """装得上、但多半不对的地方。每条是一句给作者看的话。"""
    out: list[str] = []
    pages = {rel: _text(b) for rel, b in files.items()
             if rel.parts[0] == pluginfs.PAGE_DIR and rel.suffix.lower() in TEXT_SUFFIXES}

    # 桥客户端：每个标签页都要引入，路径相对页面文件
    for t in m.tabs:
        html = pages.get(Path(t.entry), "")
        want = posixpath.relpath(SDK, posixpath.dirname(t.entry))
        refs = re.findall(r"""src\s*=\s*["']([^"']*_matrix/sdk\.js)["']""", html)
        if not refs:
            out.append(f"标签 {t.key} 的页面 {t.entry} 没有引入桥客户端：加上 <script src=\"{want}\"></script>")
        elif any(r != want for r in refs):
            out.append(f"标签 {t.key} 的页面 {t.entry} 里桥客户端的路径要写 {want!r}（相对页面文件），现在是 {refs[0]!r}")

    # 沙箱：外部资源与联网都会被拦下
    for rel, text in sorted(pages.items()):
        ext = sorted(set(EXTERNAL.findall(text)))
        if ext:
            out.append(f"{rel.as_posix()} 引用了外部资源 {ext[0]}{' 等' if len(ext) > 1 else ''}：页面在沙箱里，"
                       "只能取包里 page/ 下的文件，把它下载进 page/ 再用相对路径引用")
        net = NETWORK.search(text)
        if net:
            out.append(f"{rel.as_posix()} 用了 {net.group(1) or net.group(2)}：页面不能联网，读写用 matrix.*，"
                       "外部系统的数据经外部回调触发写成记录")

    # 能力：用到了没申请的会被拒绝；申请了没用到的会白白出现在安装说明里
    code = "\n".join(t for rel, t in pages.items() if rel.suffix.lower() != ".css")
    used = {cap for pat, cap in SDK_CAPS if re.search(pat, code)}
    used |= {CALL_CAPS[op] for op in re.findall(r"""matrix\.call\(\s*["']([\w.]+)["']""", code) if op in CALL_CAPS}
    for cap in sorted(used - set(m.page_permissions)):
        out.append(f"页面用到了「{PLUGIN_PAGE_CAPS[cap]}」，但 page_permissions 里没有 {cap}：调用会被拒绝")
    if pages:
        for cap in m.page_permissions:
            if cap not in used:
                out.append(f"page_permissions 申请了 {cap}，页面代码里没看到用它：不需要就去掉，安装说明里会少一项")

    # 触发消息：{字段} 换成什么
    for t in m.triggers:
        names = PLACEHOLDER.findall(t.text)
        table = m.table(t.table) if t.table else None
        if t.type == "schedule" and names:
            out.append(f"触发 {t.key} 是定时触发，没有记录，消息里的 {{{names[0]}}} 会换成空")
        elif table is not None:
            for n in names:
                if table.field(n) is None:
                    out.append(f"触发 {t.key} 的消息里 {{{n}}} 不是表 {table.key} 的字段，会换成空")

    # 浏览器只取 page/ 下的文件
    stray = [rel.as_posix() for rel in files if rel.suffix.lower() in TEXT_SUFFIXES + (".png", ".svg", ".jpg")
             and rel.parts[0] not in (pluginfs.PAGE_DIR, pluginfs.SKILLS_DIR)]
    if stray:
        out.append(f"{stray[0]}{' 等' if len(stray) > 1 else ''} 不在 page/ 下：页面取不到它")
    return out


def summary(files: dict[Path, bytes], m: pluginfs.Manifest) -> list[str]:
    size = sum(len(b) for b in files.values())
    skills = sorted({rel.parts[1] for rel in files if rel.parts[0] == pluginfs.SKILLS_DIR and len(rel.parts) >= 3})
    lines = [f"✓ {m.name} {m.version}「{m.title}」 {len(files)} 个文件，{size / 1000:.1f} KB"]
    if m.tabs:
        lines.append("  标签页：" + "、".join(f"{t.label}（{t.entry}）" for t in m.tabs))
    for t in m.tables:
        lines.append(f"  表 {t.key}「{t.title}」：" + "、".join(
            f"{f.label}{'*' if f.required else ''}（{f.type}）" for f in t.fields))
    lines.append("  页面能做：" + ("、".join(PLUGIN_PAGE_CAPS[c] for c in m.page_permissions) or "不读写任何数据"))
    lines.append("  Agent 能做：" + ("、".join({"records.read": "查记录", "records.write": "建 / 改记录"}[c]
                                             for c in m.agent_tools) or "不开放记录工具"))
    for t in m.triggers:
        when = (f"cron {t.cron}" if t.type == "schedule" else f"表 {t.table} 新增记录" if t.on_create
                else f"表 {t.table} 的 {t.when_field} 变成「{t.when_equals}」" if t.type == "record"
                else f"外部回调{f'，写进表 {t.table}' if t.table else ''}")
        lines.append(f"  触发 {t.key}（{TRIGGER_CN[t.type]}）：{when} → @{t.to} {t.text}")
    if skills:
        lines.append("  打法：" + "、".join(skills))
    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="matrix-plugin", description="Matrix 项目插件的本地工具")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="校验一个插件目录或 zip：装得上吗、有没有多半写错的地方")
    c.add_argument("path", type=Path, help="插件目录（含 plugin.json）或 .zip")
    c.add_argument("--strict", action="store_true", help="提醒也算失败（退出码 1），给 CI 用")
    args = p.parse_args(argv)
    try:
        files = load(args.path)
        m = pluginfs.check_package(files)
    except (OSError, pluginfs.PluginError) as e:
        print(f"✗ 装不上：{e}", file=sys.stderr)
        return 1
    print("\n".join(summary(files, m)))
    warns = lint(files, m)
    if warns:
        print(f"\n提醒（{len(warns)} 条，不影响安装）：")
        print("\n".join(f"  - {w}" for w in warns))
    if m.triggers:
        to = "、".join(dict.fromkeys(t.to for t in m.triggers))
        print(f"\n装进项目后再确认：触发要 @ 的 {to} 必须是项目里成员的名字；触发默认不开，由主持人逐个启用。")
    return 1 if warns and args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
