"""本地测试用的假 `demo-agent`：照驱动开头写的协议回一段话。真的 Agent 不需要这个文件。

    python -m matrix_device_demo.fake_agent run --workdir <目录>
"""

from __future__ import annotations

import json
import sys
import uuid


def main() -> None:
    if sys.argv[1:2] != ["run"]:
        print("用法：demo-agent run --workdir <目录>", file=sys.stderr)
        raise SystemExit(2)
    req = json.loads(sys.stdin.read() or "{}")
    prompt = str(req.get("prompt") or "")
    if "失败" in prompt:
        print("演示：按要求失败", file=sys.stderr)
        raise SystemExit(3)
    session = req.get("session_id") or uuid.uuid4().hex[:8]
    for ev in ({"type": "text", "text": "收到，处理中…"}, {"type": "tool", "name": "查单据"},
               {"type": "done", "text": f"已处理：{prompt}", "session_id": session}):
        print(json.dumps(ev, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
