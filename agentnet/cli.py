"""命令行：在群组中与某个 Agent 对话一轮（无 UI），打印事件流与回复。

    uv run python -m agentnet.cli "我要研究一个新的 AI 产品机会。" [--agent "Product Strategy Agent"] [--runtime scripted]

DSH 运行时下网络工具经 MCP 回调 HTTP 端点，所以 CLI 会在后台起一个仅本机的服务。
"""

from __future__ import annotations

import argparse
import threading

from .server import build_network, create_app

SHOW = {"runtime.invoke", "tool.call", "task.created", "task.updated", "knowledge.shared", "authority.violation"}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("text")
    p.add_argument("--agent", default="Product Strategy Agent")
    p.add_argument("--group", default=None, help="群组名称；默认取该 Agent 所在的第一个群组")
    p.add_argument("--runtime", choices=["dsh", "scripted"], default="dsh")
    p.add_argument("--data", default=".agentnet")
    p.add_argument("--port", type=int, default=8766)
    args = p.parse_args()

    net = build_network(args.runtime, args.data, args.port)
    if args.runtime == "dsh":
        import uvicorn
        server = uvicorn.Server(uvicorn.Config(create_app(net), host="127.0.0.1", port=args.port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()

    agent = net.store.agent_by_name(args.agent)
    if agent is None:
        raise SystemExit(f"unknown agent {args.agent}")
    groups = net.store.groups_of(agent.id)
    group = next((g for g in groups if g.name == args.group), None) if args.group else (groups[0] if groups else None)
    conv = net.create_conversation(agent.id, group.id if group else None)
    name = lambda i: net.store.agents[i].name if i in net.store.agents else i  # noqa: E731

    seq = net.bus.last_seq
    net.send_message(conv.id, args.text)
    while conv.status == "thinking":
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
            else:
                msg = str(d)[:200]
            print(f"[{ev.seq:04d}] {ev.type:20s} {msg}", flush=True)
    print("\n==== REPLY ====")
    print(conv.messages[-1].text)
    print("\nconversation:", conv.id)


if __name__ == "__main__":
    main()
