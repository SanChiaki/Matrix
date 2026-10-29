"""搭一个交付看板的演示数据目录（脚本运行时，离线），然后用 matrix 打开它看效果。

    uv run python examples/delivery-dashboard/setup_demo.py            # 建 .matrix-dashboard-demo/
    uv run python examples/delivery-dashboard/delivery_data_mcp.py     # 另一个终端：演示用的交付数据服务
    uv run matrix --runtime scripted --data .matrix-dashboard-demo \\
        --connectors examples/delivery-dashboard/connectors.json --plugins examples/delivery-dashboard/plugins.json

三个人（密码都是 demo-pass-123）：
- zhang@demo.local：华东一线交付，主持「华东交付」项目；他的“看板 Agent”用他授权的交付数据（只能看华东）做看板。
- li@demo.local：华东区域负责人，不在项目里；读者。他的助理订阅了看板发布物，问它就按快照回答。
- wang@demo.local：华南一线交付；订阅了公开的技能包“做交付看板的技能包”，在自己的项目里照着做。

跑完的状态：项目里装了“交付看板”插件，看板 Agent 做好了「华东交付周报」，标为待发布后写了发布说明；
张工把技能发布成公开的技能包、把看板发布成给李总的交付件（带看板视图、跟随档案）；李总的助理订阅并回答了一次；
李总写了一条反馈，回到项目群聊。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1]))

from matrix import auth  # noqa: E402
from matrix.connector_catalog import ConnectorCatalog  # noqa: E402
from matrix.engine import Network  # noqa: E402
from matrix.plugins import PluginCatalog  # noqa: E402
from matrix.runtime import ScriptedRuntime  # noqa: E402
from matrix.scripted import scenario_handler  # noqa: E402

sys.path.insert(0, str(ROOT))
import delivery_data_mcp  # noqa: E402

PASSWORD = "demo-pass-123"


def step(text: str) -> None:
    print(f"· {text}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="搭交付看板演示数据")
    ap.add_argument("--data", default=".matrix-dashboard-demo")
    ap.add_argument("--port", type=int, default=8790, help="演示交付数据服务的端口（要和 connectors.json 里一致）")
    ap.add_argument("--fresh", action="store_true", help="先删掉已有的数据目录")
    args = ap.parse_args()
    data = Path(args.data).resolve()
    if data.exists():
        if not args.fresh:
            sys.exit(f"{data} 已经存在：加 --fresh 重建")
        shutil.rmtree(data)

    server = delivery_data_mcp.serve(args.port)  # 搭数据时临时起一个；看效果时另开终端常驻
    threading.Thread(target=server.serve_forever, daemon=True).start()

    net = Network(ScriptedRuntime(scenario_handler), data, seed=False,
                  connectors=ConnectorCatalog(ROOT / "connectors.json"), plugins=PluginCatalog(ROOT / "plugins.json"))
    zhang, li, wang = (auth.register(net.store, e, PASSWORD)[0].id
                       for e in ("zhang@demo.local", "li@demo.local", "wang@demo.local"))
    for u in (zhang, li, wang):
        net.ensure_assistant(u)
    step("三个人注册好了，各自有助理")

    dash = net.create_agent("看板 Agent", "用交付数据做交付看板：查数、生成看板文件、登记、刷新", ["交付看板", "查数"],
                            ["file.write", "connector.use"], actor=zhang)
    net.authorize_connector("delivery-data", {"token": "demo-huadong"}, actor=zhang, agent_ids=[dash.id])
    net.authorize_connector("delivery-data", {"token": "demo-huanan"}, actor=wang)
    step("张工授权了交付数据（令牌只能看华东），勾给看板 Agent；王工授权了华南的")

    g = net.create_project("华东交付", "华东区域的项目交付：看板、周报、风险跟踪", [dash.id], actor=zhang)
    net.set_brief(g.id, "目标：每周给区域负责人一份交付看板（按期率、延期、风险项目），一线不用再各自装技能、配 MCP。\n"
                        "数据：交付数据连接器，各自用自己的授权，数据权限跟着人走。\n"
                        "节奏：工作日早上 8 点自动刷新；看板发布给区域负责人，跟随档案更新。", actor=zhang)
    net.install_plugin(g.id, "delivery-dashboard", actor=zhang)
    for key in ("daily", "ready"):
        net.set_plugin_trigger(g.id, "delivery-dashboard", key, True, actor=zhang)
    step("建了项目「华东交付」，装了“交付看板”插件，开了每日刷新与待发布两个触发")

    chat = net.project_chat(g.id, actor=zhang)
    net.send_message(chat.id, "@看板 Agent 做一个华东交付周报看板，每天刷新", actor=zhang, background=False)
    rec = net.plugin_records(g.id, "delivery-dashboard", "dashboards", actor=zhang)[0]
    step(f"看板 Agent 查数、生成了 {rec['fields']['file']}，登记进看板表")

    req = next(r for r in net.store.requests.values() if r.project_id == g.id)
    net.review_request(req.id, True, actor=zhang)
    net.write_plugin_record(g.id, "delivery-dashboard", "dashboards", {"status": "待发布"}, rec["id"], actor=zhang)
    net.wait_idle(chat.id)
    step("张工验收、标为待发布；触发叫看板 Agent 核对并写了发布说明")

    method = net.create_release(g.id, "delivery-dashboard-method", "method", "做交付看板的技能包",
                                "用交付数据做看板的做法：查数口径、看板文件格式、登记与刷新。不含任何项目数据。",
                                skills=["delivery-dashboard"], public=True, actor=zhang)
    fact = net.create_release(g.id, "huadong-weekly", "fact", "华东交付周报看板",
                              "华东区域在建项目的按期率、延期与风险项目，工作日早上自动刷新。",
                              files=[rec["fields"]["file"]], view={"plugin": "delivery-dashboard", "tab": "board"},
                              readers=["li@demo.local"], follow=True, actor=zhang)
    net.write_plugin_record(g.id, "delivery-dashboard", "dashboards", {"status": "已发布"}, rec["id"], actor=zhang)
    step("张工发布了公开的技能包和给李总的看板（交付件，带看板视图，跟随档案）")

    li_bot = net.assistant_of(li)
    net.set_subscription(fact.id, li_bot.id, True, actor=li)
    net.send_message(net.direct_chat(li_bot.id, actor=li).id, "华东这周交付情况怎么样？", actor=li, background=False)
    net.release_feedback(fact.id, "能不能再加一张：各项目经理名下的延期项目数？", actor=li)
    step("李总的助理订阅了看板并回答了一次；李总写了一条反馈，回到项目群聊")

    net.set_subscription(method.id, net.assistant_of(wang).id, True, actor=wang)
    step("王工的助理订阅了公开的技能包")

    net.store.save()
    server.shutdown()
    print(f"\n演示数据在 {data}。看效果：\n"
          f"  uv run python examples/delivery-dashboard/delivery_data_mcp.py --port {args.port}\n"
          f"  uv run matrix --runtime scripted --data {args.data} --connectors examples/delivery-dashboard/connectors.json "
          f"--plugins examples/delivery-dashboard/plugins.json\n"
          f"登录：zhang@demo.local / li@demo.local / wang@demo.local，密码 {PASSWORD}")


if __name__ == "__main__":
    main()
