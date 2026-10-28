"""命令行：在群聊里 @ 某个 Agent 发一条消息（无 UI），打印事件流与回复，直到所有委托都有结果。

    uv run python -m matrix.cli "我要研究一个新的 AI 产品机会。" [--agent "Product Strategy Agent"] [--runtime scripted]

DSH 运行时下网络工具经 MCP 回调 HTTP 端点，所以 CLI 会在后台起一个仅本机的服务。
"""

from __future__ import annotations

import argparse
import threading

from . import compat
from .server import build_network, create_app

SHOW = {"runtime.invoke", "tool.call", "task.created", "task.updated", "knowledge.shared", "authority.violation",
        "run.retry", "proposal.updated", "message.added"}


def main() -> None:
    from . import utf8_stdio
    utf8_stdio()
    p = argparse.ArgumentParser()
    p.add_argument("text")
    p.add_argument("--agent", default="Product Strategy Agent")
    p.add_argument("--project", default=None, help="项目名称；默认取该 Agent 所在的第一个项目")
    p.add_argument("--runtime", choices=["dsh", "scripted"], default="dsh")
    p.add_argument("--data", default=None, help="数据目录；默认同服务（.matrix，只有改名前的 .agentnet 时沿用它）")
    p.add_argument("--port", type=int, default=8766)
    args = p.parse_args()
    args.data = args.data or compat.data_dir()

    net = build_network(args.runtime, args.data, args.port, seed=True)
    if args.runtime == "dsh":
        import uvicorn
        server = uvicorn.Server(uvicorn.Config(create_app(net), host="127.0.0.1", port=args.port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()

    agent = net.store.agent_by_name(args.agent)
    if agent is None:
        raise SystemExit(f"unknown agent {args.agent}")
    projects = net.store.projects_of(agent.id)
    project = next((g for g in projects if g.name == args.project), None) if args.project \
        else (projects[0] if projects else None)
    # 在项目里：在项目群聊里 @ 这个 Agent；不在项目里：私聊
    conv = net.create_conversation(project.id) if project else net.create_conversation(agent_id=agent.id)
    name = lambda i: net.store.agents[i].name if i in net.store.agents else i  # noqa: E731

    seq = net.bus.last_seq
    net.send_message(conv.id, args.text, mentions=[agent.id] if project else None)
    # 委托是异步的：一直跑到对话回到 idle（没有在跑的轮次，也没有进行中的委托）
    while True:
        with net.store.lock:
            quiet = not net._running and conv.status == "idle"
        if quiet:
            break
        for ev in net.bus.wait(seq, 2):
            seq = ev.seq
            if ev.type not in SHOW:
                continue
            d = ev.data
            if ev.type.startswith("task."):
                t = d["task"]
                msg = f"{name(t['delegator_id'])} -> {name(t['delegatee_id'])}: {t['status']} granted={t['granted_authority']}" \
                      + (f" ({t['error']})" if t["error"] else "")
            elif ev.type == "tool.call":
                msg = f"{name(d['agent_id'])} {d['tool']} {d['input'][:100]}"
            elif ev.type == "message.added":
                m = d["message"]
                msg = f"[{m['role']}] {m['text'][:160]!r}"
            else:
                msg = str(d)[:200]
            print(f"[{ev.seq:04d}] {ev.type:20s} {msg}", flush=True)
    pending = [p for p in net.store.proposals.values() if p.conversation_id == conv.id and p.status == "pending"]
    print("\n==== REPLY ====")
    print(conv.messages[-1].text)
    if pending:
        print("\n待审核的 Agent 提议（在 UI 里批准或拒绝）：", ", ".join(p.name for p in pending))
    print("\nconversation:", conv.id)


if __name__ == "__main__":
    main()
