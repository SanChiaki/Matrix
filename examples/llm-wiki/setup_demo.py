"""搭一个“项目 Wiki”的演示数据目录，然后打开看效果。

    uv run python examples/llm-wiki/setup_demo.py              # 脚本运行时，离线把整个流程跑一遍
    uv run python examples/llm-wiki/setup_demo.py --prepare    # 只搭好项目，流程留给真实模型（见 README）

跑完的状态（不加 --prepare）：项目「白板定价」装了部署方目录里的插件「项目 Wiki」；知识管家把两份项目资料存成原始资料、
编成两页 wiki；有人问“竞品定价大概在什么区间？”，它在推来的 wiki 目录里找到页面、带出处作答；
下发的人验收后，插件的 after 挂点代表验收的人 @ 知识管家，把验收过的结论写回 wiki。
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


def main() -> None:
    ap = argparse.ArgumentParser(description="搭“项目 Wiki”的演示数据")
    ap.add_argument("--data", default=".matrix-wiki-demo")
    ap.add_argument("--fresh", action="store_true", help="先删掉已有的数据目录")
    ap.add_argument("--prepare", action="store_true", help="只搭项目、装插件、放资料，不跑对话")
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
    keeper = net.create_agent("知识管家", "维护项目 Wiki：把资料编进 wiki、按 wiki 带出处回答问题、把验收过的结论写回",
                              ["整理资料", "回答问题"], ["file.write"], actor=wang)
    g = net.create_project("白板定价", "协同白板新产品的定价调研", [keeper.id], actor=wang)
    net.set_brief(g.id, "为协同白板新产品定价：先把竞品定价和用户访谈整理进项目 Wiki，之后的讨论都以 wiki 为准、带出处。", actor=wang)
    for f in sorted((ROOT / "materials").glob("*.md")):
        net.publish_file(g.id, f.name, f.read_bytes(), actor=wang)
    net.install_plugin(g.id, "llm-wiki", actor=wang)
    print("· 王经理建了项目「白板定价」，放进两份资料，从部署方目录装上了插件「项目 Wiki」", flush=True)
    chat = net.project_chat(g.id, actor=wang)

    if not args.prepare:
        net.send_message(chat.id, "@知识管家 把项目资料收进 wiki", actor=wang, background=False)
        print("· 知识管家把两份资料存成原始资料、编成 wiki 页面，体检通过", flush=True)
        net.send_message(chat.id, "@知识管家 竞品的定价大概在什么区间？", actor=wang, background=False)
        print("· 提问：提示词里带着 wiki 目录（context 挂点），知识管家带出处作答", flush=True)
        req = next(r for r in net.store.requests.values() if r.title.startswith("竞品的定价"))
        net.review_request(req.id, True, actor=wang)
        net.wait_quiet(timeout=30)
        print("· 王经理验收了回答：插件代表他 @ 知识管家，结论写回了 wiki（after 挂点）", flush=True)
    net.store.save()
    net.services.stop_all()
    print(f"""
演示数据在 {data}。看效果：
  uv run matrix --runtime scripted --data {args.data} --plugins examples/plugins.json
登录 wang@demo.local，密码 {PASSWORD}；项目「白板定价」页上有“Wiki”标签。
用真实模型（dsh）：把 --runtime scripted 换成 --runtime dsh（需要 PATH 里有 dsh 并配好模型）。""")


if __name__ == "__main__":
    main()
