"""管理操作的审计日志（docs/control-plane.md 第 8 节，C0）：后台的每次改动记一行，只追加。

`<数据目录>/audit.jsonl`，一行一条：{at, actor, actor_email, action, target, detail}。只记谁、什么时候、改了哪一类、
目标 id 与不含内容的摘要（例如“这几个供应商换了密钥”），不记密钥、不记内容。文件只追加，程序里没有改写或删除它的路径；
管理后台的“审计”页倒序读最近的若干条。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

# 动作 → 后台显示的类别
ACTIONS = {
    "user.reset_link": "重置密码",
    "models.save": "模型目录与供应商",
    "plugins.policy": "插件策略",
    "plugins.catalog.git": "插件目录",
    "plugins.catalog.upload": "插件目录",
    "plugins.catalog.refresh": "插件目录",
    "plugins.catalog.remove": "插件目录",
    "plugins.submission": "插件审核",
    "blueprints.upload": "模板",
    "im.create": "IM 机器人",
    "im.update": "IM 机器人",
    "im.delete": "IM 机器人",
}


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def record(self, actor: str, actor_email: str, action: str, target: str = "", **detail: Any) -> None:
        assert action in ACTIONS, action
        row = {"at": time.time(), "actor": actor, "actor_email": actor_email, "action": action,
               "category": ACTIONS[action], "target": target, "detail": detail}
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        """最近的 limit 条，新的在前。坏行跳过。"""
        if not self.path.is_file():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
            if len(out) >= limit:
                break
        return out
