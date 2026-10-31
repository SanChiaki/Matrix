"""搭一个“蓝图”的演示数据目录（docs/blueprints.md）。

    uv run python examples/blueprints/setup_demo.py              # 部署方放进蓝图「研发小队」，王经理用它建项目、外部来一个需求
    uv run python examples/blueprints/setup_demo.py --prepare    # 只放进蓝图，建项目留给你在界面上点

跑完的状态（不加 --prepare）：
1. 部署方（ops@demo.local，管理员）把 examples/blueprints/dev-team 放进部署方目录。
2. 王经理用它建了项目「白板研发」：填了产品名与仓库，五个岗位（反馈、方案、编码、审查、部署）各新建一个 Agent；纲要、研发流程、
   技能里的占位符换成了产品名与岗位上的人；装上「队友协作」，“谁接外部来的新需求”设成岗位“反馈”；连接器面板里有 GitHub；
   项目页顶部列出剩下的手工步骤。
3. 王经理启用外部回调，外部系统来了一个问题：插件按岗位 @ 反馈。

用真实模型实测见 README（--prepare 只放进蓝图、建项目，回调与对话留给你）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1]))

from matrix import auth, plugins as pluginfs  # noqa: E402
from matrix.engine import NeedsConfirm, Network  # noqa: E402
from matrix.runtime import ScriptedRuntime  # noqa: E402
from matrix.scripted import scenario_handler  # noqa: E402

PASSWORD = "demo-pass-123"
CATALOG = ROOT.parent / "plugins.json"


def main() -> None:
    ap = argparse.ArgumentParser(description="搭“蓝图”的演示数据")
    ap.add_argument("--data", default=".matrix-blueprint-demo")
    ap.add_argument("--fresh", action="store_true", help="先删掉已有的数据目录")
    ap.add_argument("--prepare", action="store_true", help="放进蓝图、建项目、启用外部回调，不发需求（留给真实模型）")
    args = ap.parse_args()
    data = Path(args.data).resolve()
    if data.exists():
        if not args.fresh:
            sys.exit(f"{data} 已经存在：加 --fresh 重建")
        shutil.rmtree(data)
    data.mkdir(parents=True)

    net = Network(ScriptedRuntime(scenario_handler), data, seed=False, plugins=pluginfs.PluginCatalog(CATALOG))
    ops = auth.register(net.store, "ops@demo.local", PASSWORD)[0].id  # 最早注册的是管理员
    wang = auth.register(net.store, "wang@demo.local", PASSWORD)[0].id
    for u in (ops, wang):
        net.ensure_assistant(u)
    src = ROOT / "dev-team"
    entries = [(f"dev-team/{p.relative_to(src).as_posix()}", p.read_bytes()) for p in sorted(src.rglob("*")) if p.is_file()]
    bp = net.admin_blueprint_upload(ops, entries)
    print(f"· 部署方把蓝图「{bp['title']}」放进了目录", flush=True)
    if True:
        params = {"product": "协同白板", "repo": "acme/whiteboard"}
        try:
            net.create_project_from_blueprint(bp["id"], "白板研发", params, actor=wang)
        except NeedsConfirm as e:
            print("· 王经理看到的确认卡：\n    " + "\n    ".join(e.items), flush=True)
            out = net.create_project_from_blueprint(bp["id"], "白板研发", params, confirm=True, digest=e.digest, actor=wang)
        g = net.store.projects[out["project"]["id"]]
        print(f"· 建好了项目「{g.name}」：岗位 " + "、".join(f"{r['name']}（{net._name(r['members'][0])}）" for r in g.roles.values()), flush=True)
        net.set_plugin_trigger(g.id, "teammates", "intake", True, actor=wang)
        token = net.reset_plugin_hook(g.id, "teammates", "intake", actor=wang)
        net.set_setup_step(g.id, g.setup_steps[0]["id"], True, actor=wang)
        (data / "hook-token.txt").write_text(token, encoding="utf-8")
        print(f"· 王经理启用了外部回调（地址 /hooks/{token}，也写在 {data / 'hook-token.txt'}）", flush=True)
        if not args.prepare:
            r = net.plugin_webhook(token, {"title": "导出 PDF 时字体丢失", "url": "https://github.com/acme/whiteboard/issues/42"})
            net.wait_quiet(timeout=30)
            msg = next(m for m in net.project_chat(g.id).messages if m.id == r["message"])
            print(f"· 外部回调来了一个问题，插件按岗位发出：{msg.text}", flush=True)
    net.store.save()
    net.services.stop_all()
    print(f"""
演示数据在 {data}。看效果：
  uv run matrix --runtime scripted --data {args.data} --plugins examples/plugins.json
登录 wang@demo.local（或管理员 ops@demo.local），密码 {PASSWORD}；左栏“蓝图”里有「研发小队」，项目「白板研发」是用它建的。""")


if __name__ == "__main__":
    main()
