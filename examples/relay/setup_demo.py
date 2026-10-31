"""搭一个“接力分派”的演示数据目录（脚本运行时，离线），然后打开看效果。

    uv run python examples/relay/setup_demo.py              # 跑一遍整个接力
    uv run python examples/relay/setup_demo.py --prepare    # 只搭好项目，接力留给真实模型（见 README）

跑完的状态（不加 --prepare）：研发项目里五个成员按角色接力。王经理在群聊里说“登录页在 Safari 上白屏”，没 @ 任何人：
插件把它分派给“反馈”认领（登记成任务，下发人是王经理），反馈 → 方案 → 编码一路自动接力；编码说“需要审查”时，
这一串的 3 次自动唤醒已经用完，插件请人接手。王经理 @ 审查，审查做完说“需要部署”，插件请部署看一下；部署是高风险的一环，
等王经理 @ 部署“确认部署”后才执行。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1]))

from matrix import auth, plugins as pluginfs  # noqa: E402
from matrix.engine import Network  # noqa: E402
from matrix.runtime import ScriptedRuntime  # noqa: E402
from matrix.scripted import scenario_handler  # noqa: E402

PASSWORD = "demo-pass-123"
CATALOG = ROOT.parent / "plugins.json"
ROLES = [  # 名字、描述、能力（插件按能力关键词分派）、权限
    ("反馈", "接用户反馈的问题：复现、定位影响范围", ["问题反馈", "白屏", "报错", "崩溃", "bug"], ["file.write"]),
    ("方案", "出修复方案：改哪里、怎么验证", ["方案", "设计"], ["file.write"]),
    ("编码", "按方案改代码、补用例", ["编码", "实现"], ["file.write", "shell.exec"]),
    ("审查", "审查改动：正确性、风险、用例", ["审查", "代码审查"], ["file.write"]),
    ("部署", "上线与回滚：只在人确认后执行", ["部署", "上线"], ["file.write", "shell.exec"]),
]


def main() -> None:
    ap = argparse.ArgumentParser(description="搭“接力分派”的演示数据")
    ap.add_argument("--data", default=".matrix-relay-demo")
    ap.add_argument("--fresh", action="store_true", help="先删掉已有的数据目录")
    ap.add_argument("--prepare", action="store_true", help="只搭项目、装插件，不跑对话")
    args = ap.parse_args()
    data = Path(args.data).resolve()
    if data.exists():
        if not args.fresh:
            sys.exit(f"{data} 已经存在：加 --fresh 重建")
        shutil.rmtree(data)
    data.mkdir(parents=True)

    net = Network(ScriptedRuntime(scenario_handler), data, seed=False, plugins=pluginfs.PluginCatalog(CATALOG))
    wang = auth.register(net.store, "wang@demo.local", PASSWORD)[0].id
    net.ensure_assistant(wang)
    members = [net.create_agent(n, d, caps, auth_, actor=wang).id for n, d, caps, auth_ in ROLES]
    g = net.create_project("登录页", "Web 登录页的问题修复与上线", members, actor=wang)
    net.set_brief(g.id, "问题直接发在群聊里，不用 @ 人：插件按能力分派、成员按“反馈 → 方案 → 编码 → 审查 → 部署”接力。"
                        "部署要人确认。", actor=wang)
    net.install_plugin(g.id, "relay", actor=wang)
    print("· 王经理建了项目「登录页」，五个成员按角色分工，装上了插件「接力分派」", flush=True)
    chat = net.project_chat(g.id, actor=wang)
    if not args.prepare:
        net.send_message(chat.id, "登录页在 Safari 上白屏，用户进不去，帮忙看看", actor=wang, background=False)
        print("· 没 @ 人的问题：插件分派给反馈认领，反馈 → 方案 → 编码自动接力，3 次自动唤醒用完后请人接手", flush=True)
        net.send_message(chat.id, "@审查 帮忙审查一下编码的改动", actor=wang, background=False)
        print("· 王经理 @ 审查；审查说“需要部署”，插件请部署看一下，部署等人确认", flush=True)
        net.send_message(chat.id, "@部署 确认部署", actor=wang, background=False)
        print("· 王经理确认后部署执行", flush=True)
    net.store.save()
    net.services.stop_all()
    print(f"""
演示数据在 {data}。看效果：
  uv run matrix --runtime scripted --data {args.data} --plugins examples/plugins.json
登录 wang@demo.local，密码 {PASSWORD}；打开项目「登录页」的群聊。""")


if __name__ == "__main__":
    main()
