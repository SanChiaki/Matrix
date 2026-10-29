"""搭一个“项目接入已有看板系统”的演示数据目录（脚本运行时，离线），然后打开看效果。

    uv run python examples/dashboard-system/setup_demo.py

它会临时起三个服务把流程跑一遍：交付数据（MCP）、看板系统（公开 HTTP API）、看板系统的 MCP 包装层。
看效果时按打印出的命令把这三个服务和 Matrix 常驻起来。

跑完的状态：张工的“看板 Agent”用他的交付数据授权查数，在看板系统里建了「华东交付周报」草稿、把链接加进项目资料；
张工说“发布”，网络先弹确认卡（参数原样），他同意后才发布，资料里的链接更新为“已发布 v1”；
张工把这条链接设成项目页上的标签，看板系统的页面直接嵌在项目页里。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parents[1]), str(ROOT), str(ROOT.parent / "delivery-dashboard")]

from matrix import auth  # noqa: E402
from matrix.connector_catalog import ConnectorCatalog  # noqa: E402
from matrix.engine import Network  # noqa: E402
from matrix.runtime import ScriptedRuntime  # noqa: E402
from matrix.scripted import scenario_handler  # noqa: E402

import board_api  # noqa: E402
import board_mcp  # noqa: E402
import delivery_data_mcp  # noqa: E402

PASSWORD = "demo-pass-123"


def main() -> None:
    ap = argparse.ArgumentParser(description="搭“接入已有看板系统”的演示数据")
    ap.add_argument("--data", default=".matrix-board-demo")
    ap.add_argument("--fresh", action="store_true", help="先删掉已有的数据目录")
    args = ap.parse_args()
    data = Path(args.data).resolve()
    if data.exists():
        if not args.fresh:
            sys.exit(f"{data} 已经存在：加 --fresh 重建")
        shutil.rmtree(data)
    data.mkdir(parents=True)
    store = data / "board-system.json"  # 看板系统自己的数据（演示用，放在一起方便清理）

    servers = [delivery_data_mcp.serve(8790), board_api.serve(8791, store), board_mcp.serve(8792, "http://127.0.0.1:8791")]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()

    net = Network(ScriptedRuntime(scenario_handler), data, seed=False, connectors=ConnectorCatalog(ROOT / "connectors.json"))
    zhang = auth.register(net.store, "zhang@demo.local", PASSWORD)[0].id
    net.ensure_assistant(zhang)
    dash = net.create_agent("看板 Agent", "在公司的看板系统里做看板：查数、建草稿、给链接；发布前请所有者确认",
                            ["看板", "查数"], ["connector.use"], actor=zhang)
    net.authorize_connector("delivery-data", {"token": "demo-huadong"}, actor=zhang, agent_ids=[dash.id])
    net.authorize_connector("boards", {}, actor=zhang, agent_ids=[dash.id])
    g = net.create_project("华东交付", "华东区域的项目交付：看板在公司的看板系统里", [dash.id], actor=zhang)
    net.set_brief(g.id, "看板做在公司已有的看板系统里（公开，发布后任何人可见）：Agent 建草稿、给链接，人说发布才发布，"
                        "发布前所有者在确认卡上确认。数据用交付数据连接器，各自用自己的授权。", actor=zhang)
    chat = net.project_chat(g.id, actor=zhang)
    print("· 张工授权了交付数据（只能看华东）和看板系统（公开服务，不用填），勾给看板 Agent", flush=True)

    net.send_message(chat.id, "@看板 Agent 在看板系统里做一个华东交付周报看板", actor=zhang, background=False)
    print("· 看板 Agent 建了草稿，链接进了项目资料", flush=True)

    t = threading.Thread(target=net.send_message, args=(chat.id, "@看板 Agent 发布这个看板"),
                         kwargs={"actor": zhang, "background": False})
    t.start()
    for _ in range(500):
        pending = [x for x in net.pending_approvals(zhang) if x.kind == "connector_write"]
        if pending:
            break
        time.sleep(0.02)
    else:
        sys.exit("没有等到发布前的确认卡")
    print(f"· 发布前弹出确认卡：{pending[0].reason} 参数 {pending[0].args}", flush=True)
    net.decide_approval(pending[0].id, True, actor=zhang, note="演示：替张工点了“发出这一次”")
    t.join(10)
    print("· 张工确认后发布了，资料里的链接更新为“已发布 v1”", flush=True)
    net.set_ref_tab(g.id, g.refs[0].id, True, actor=zhang)  # 部署方允许嵌入看板系统（connectors.json 的 embed）
    print("· 张工把看板设成了项目页上的标签：在 Matrix 里直接看看板系统的页面", flush=True)

    net.store.save()
    for s in servers:
        s.shutdown()
    print(f"""
演示数据在 {data}。看效果（四个终端，或都放后台）：
  uv run python examples/delivery-dashboard/delivery_data_mcp.py --port 8790
  uv run python examples/dashboard-system/board_api.py --port 8791 --store {store}
  uv run python examples/dashboard-system/board_mcp.py --port 8792 --api http://127.0.0.1:8791
  uv run matrix --runtime scripted --data {args.data} --connectors examples/dashboard-system/connectors.json
登录 zhang@demo.local，密码 {PASSWORD}""")


if __name__ == "__main__":
    main()
