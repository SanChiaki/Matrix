"""搭一个“队友协作（Raft 式）”的演示数据目录，然后打开看效果。

    uv run python examples/teammates/setup_demo.py              # 脚本运行时，离线跑一遍
    uv run python examples/teammates/setup_demo.py --prepare    # 只搭好项目，留给真实模型（见 README）

跑完的状态（不加 --prepare）：项目「官网」里三个队友：前端、后端、测试。
1. 王经理在群聊里说“建任务：登录页加图形验证码”，没 @ 人：插件在任务板上建了 #1，并把这条消息送给三个队友。
   后端认领 #1（接口）；前端认领不了别人的，自己开了 #2（输入框）并认领；测试判断不是自己的事，只回“（略过）”，群聊里看不到它。
2. 王经理说“下午三点评审一下排期”：三个队友都收到，都略过，群聊里没有一条多余的回复。
3. 王经理 @ 后端、@ 前端问“做完了吗”：两人把各自的任务交到待验收。
4. 王经理说“#1 完成 #2 完成”：任务板上两个任务完成。
"""

from __future__ import annotations

import argparse
import json
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
TEAM = [  # 名字、描述、能力（插件按名字和能力决定先叫谁）、权限
    ("前端", "负责官网的页面与交互", ["前端", "登录页", "页面", "样式"], ["file.write"]),
    ("后端", "负责接口、账号与数据", ["后端", "接口", "验证码", "数据库"], ["file.write"]),
    ("测试", "负责用例与回归", ["测试", "用例", "回归"], ["file.write"]),
]
SCRIPT = ["建任务：登录页加图形验证码", "下午三点评审一下排期", "@后端 @前端 做完了吗", "#1 完成 #2 完成"]


def main() -> None:
    ap = argparse.ArgumentParser(description="搭“队友协作（Raft 式）”的演示数据")
    ap.add_argument("--data", default=".matrix-team-demo")
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
    members = [net.create_agent(n, d, caps, auth_, actor=wang).id for n, d, caps, auth_ in TEAM]
    g = net.create_project("官网", "公司官网的迭代", members, actor=wang)
    net.set_brief(g.id, "群聊就是频道：人发的每条消息都会送给每个队友，不用 @。工作项在“任务板”上，先认领再动手；"
                        "做完交待验收，王经理说“#N 完成”。", actor=wang)
    net.install_plugin(g.id, "teammates", actor=wang)
    print("· 王经理建了项目「官网」，三个队友：前端、后端、测试，装上了插件「队友协作（Raft 式）」", flush=True)
    chat = net.project_chat(g.id, actor=wang)
    if not args.prepare:
        for text in SCRIPT:
            n = len(chat.messages)
            net.send_message(chat.id, text, actor=wang, background=False)
            said = [f"{net._name(m.agent_id)}：{m.text}" for m in chat.messages[n + 1:] if m.role == "agent"]
            print(f"· 王经理：{text}\n    " + ("\n    ".join(said) if said else "（队友都略过了，群聊里没有回复）"), flush=True)
        board = json.loads((net.store.plugin_data_dir(g.id, "teammates") / "service" / "tasks.json").read_text(encoding="utf-8"))
        print("· 任务板：" + "；".join(f"#{t['id']} {t['title']}（{t['status']}，{t['assignee']}）" for t in board["tasks"]))
    net.store.save()
    net.services.stop_all()
    print(f"""
演示数据在 {data}。看效果：
  uv run matrix --runtime scripted --data {args.data} --plugins examples/plugins.json
登录 wang@demo.local，密码 {PASSWORD}；项目「官网」页上有“任务板”标签。""")


if __name__ == "__main__":
    main()
