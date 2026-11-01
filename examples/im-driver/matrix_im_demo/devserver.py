"""演示 IM 的本地服务：给驱动的测试用，也能在终端里扮演 IM 里的人，把驱动接进本地 Matrix 走一遍。

用法：

    python -m matrix_im_demo.devserver [--port 8790] [--secret demo-secret]

然后在终端里输入：
    alice: 你好                  alice 在和机器人的单聊里说“你好”
    #研发群 alice: @助理 看一下   alice 在群“研发群”里 @ 机器人
    /kick                        假装同一个应用在别处登录了（驱动应该报“在别处连上了”并停下）
机器人发出、改过的消息打印在终端里。

协议（JSON，一帧一个对象；这是虚构的，接你们的 IM 时换成你们的）：

  驱动 → 服务
    {"op": "auth", "id": "<请求号>", "app_id": "...", "app_secret": "..."}
    {"op": "ping", "id": "..."}
    {"op": "send", "id": "...", "chat": {"type": "single" | "group", "id": "..."}, "text": "...", "reply_to": "<msg_id，可省>"}
    {"op": "edit", "id": "...", "message_id": "...", "text": "..."}
  服务 → 驱动
    {"op": "ack", "id": "<请求号>", "ok": true, "message_id": "<send 才有>"}，失败时 {"op": "ack", "id": ..., "ok": false, "error": "..."}
    {"op": "message", "msg_id": "...", "chat": {"type": ..., "id": ...}, "from": "<用户标识>", "text": "..."}
    {"op": "kicked"}             同一个应用在别处登录了，这条连接随后被关掉

单聊的 chat.id 就是对方的用户标识；同一个 app_id 只保留最新的一条连接。
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
import threading
from typing import Any, Callable

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import ServerConnection, serve


class DevServer:
    """一个本地的演示 IM。sent 记下机器人发的 send / edit 帧，messages 是每条消息的当前文字。"""

    def __init__(self, port: int = 0, secret: str = "demo-secret", echo: Callable[[str], None] | None = None):
        self.secret = secret
        self.echo = echo
        self.sent: list[dict[str, Any]] = []
        self.messages: dict[str, dict[str, Any]] = {}   # message_id → {"chat", "text", "reply_to"}
        self.conns: dict[str, ServerConnection] = {}    # app_id → 当前连接
        self.logins: list[str] = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._srv = serve(self._handle, "127.0.0.1", port)
        self.port = self._srv.socket.getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    # ---- 扮演 IM 里的人 ----
    def say(self, sender: str, text: str, group: str | None = None) -> str:
        msg_id = f"m{next(self._ids)}"
        chat = {"type": "group", "id": group} if group else {"type": "single", "id": sender}
        self._push({"op": "message", "msg_id": msg_id, "chat": chat, "from": sender, "text": text})
        return msg_id

    def kick(self) -> None:
        self._push({"op": "kicked"})
        for ws in list(self.conns.values()):
            ws.close()

    def texts(self, chat_id: str | None = None) -> list[str]:
        """机器人发到某个会话（不给就是全部）的消息，按发出顺序，取当前（改过之后的）文字。"""
        with self._lock:
            return [m["text"] for m in self.messages.values() if chat_id is None or m["chat"]["id"] == chat_id]

    def close(self) -> None:
        self._srv.shutdown()

    # ---- 连接 ----
    def _push(self, frame: dict[str, Any]) -> None:
        for ws in list(self.conns.values()):
            ws.send(json.dumps(frame, ensure_ascii=False))

    def _handle(self, ws: ServerConnection) -> None:
        app_id = ""
        try:
            for raw in ws:
                f = json.loads(raw)
                op, rid = f.get("op"), f.get("id")
                if op == "auth":
                    if f.get("app_secret") != self.secret:
                        ws.send(json.dumps({"op": "ack", "id": rid, "ok": False, "error": "app_secret 不对"}))
                        continue
                    app_id = str(f.get("app_id") or "")
                    old = self.conns.get(app_id)
                    self.conns[app_id] = ws
                    self.logins.append(app_id)
                    if old is not None:  # 同一个应用只保留最新的连接
                        old.send(json.dumps({"op": "kicked"}))
                        old.close()
                    ws.send(json.dumps({"op": "ack", "id": rid, "ok": True}))
                elif not app_id:
                    ws.send(json.dumps({"op": "ack", "id": rid, "ok": False, "error": "还没登录"}))
                elif op == "ping":
                    ws.send(json.dumps({"op": "ack", "id": rid, "ok": True}))
                elif op == "send":
                    mid = f"b{next(self._ids)}"
                    with self._lock:
                        self.sent.append(f)
                        self.messages[mid] = {"chat": f["chat"], "text": f["text"], "reply_to": f.get("reply_to")}
                    self._show("→", f["chat"]["id"], f["text"])
                    ws.send(json.dumps({"op": "ack", "id": rid, "ok": True, "message_id": mid}))
                elif op == "edit":
                    with self._lock:
                        self.sent.append(f)
                        m = self.messages.get(f.get("message_id", ""))
                        if m is not None:
                            m["text"] = f["text"]
                    if m is None:
                        ws.send(json.dumps({"op": "ack", "id": rid, "ok": False, "error": "没有这条消息"}))
                        continue
                    self._show("✎", m["chat"]["id"], f["text"])
                    ws.send(json.dumps({"op": "ack", "id": rid, "ok": True}))
        except ConnectionClosed:
            pass
        finally:
            if app_id and self.conns.get(app_id) is ws:
                del self.conns[app_id]

    def _show(self, mark: str, chat_id: str, text: str) -> None:
        if self.echo:
            self.echo(f"[机器人 {mark} {chat_id}] {text}")


LINE = re.compile(r"^(?:#(?P<group>\S+)\s+)?(?P<sender>[^:：\s]+)\s*[:：]\s*(?P<text>.*)$")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="本地的演示 IM：在终端里扮演 IM 里的人")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--secret", default="demo-secret", help="驱动登录要用的 app_secret")
    args = ap.parse_args(argv)
    srv = DevServer(args.port, args.secret, echo=print)
    print(f"演示 IM 在 {srv.url}（app_secret：{args.secret}）。输入「alice: 你好」说话，「/kick」顶掉连接，Ctrl-D 退出。")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "/kick":
            srv.kick()
            continue
        m = LINE.match(line)
        if not m:
            print("格式：alice: 你好　或　#群名 alice: @助理 你好")
            continue
        if not srv.conns:
            print("（驱动还没连上）")
            continue
        srv.say(m["sender"], m["text"], group=m["group"])
    srv.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
