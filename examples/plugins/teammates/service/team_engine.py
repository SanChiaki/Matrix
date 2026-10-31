"""「队友协作（Raft 式）」插件服务：频道被动投递 + 任务板与认领锁（stdio MCP，仅标准库，不调模型）。

照 Raft 的协作方式（research: raft#inbox-wake、research: raft#tasks-claim），放在 Matrix 的规则里：

- 被动投递：频道里人发的每条消息都请所有频道成员看一下（route 的 wake），不需要 @；成员自己判断是不是自己的事，
  不是就只回“（略过）”，网络不发进群聊。Agent 的发言只叫被点到的成员（任务负责人、名字或能力被提到的），
  免得成员之间互相叫醒。一条人发的消息最多叫醒几个由网络的预算决定（传进来的 relay.budget）。
- 任务就是工作项，认领是并发锁：先到先得、只有一个负责人。状态 todo / in_progress / in_review / done / closed，
  转移照 Raft 的表；Agent 最多把任务推到 in_review，done、closed 由人在群聊里说（“#3 完成”“#3 关闭”“#3 重开”“#3 退回”）。
- 谁在调用由网络认定：工具调用的 _meta["matrix/caller"]（agent / user / network），不信参数里自报的名字。
  人在群聊里的指令经 route 进来，route 的参数 message.author_kind 也由网络填。

数据：工作目录（Matrix 托管时是这个项目的插件数据目录）里的 tasks.json。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

STATE = Path("tasks.json")
STATUSES = ("todo", "in_progress", "in_review", "done", "closed")
CN = {"todo": "待认领", "in_progress": "进行中", "in_review": "待验收", "done": "已完成", "closed": "已关闭"}
# 照 Raft 的 VALID_TRANSITIONS（research: raft#tasks-claim）
TRANSITIONS = {
    "todo": {"in_progress", "closed"},
    "in_progress": {"in_review", "done", "closed"},
    "in_review": {"done", "in_progress", "closed"},
    "done": {"todo", "in_progress", "in_review", "closed"},
    "closed": {"todo", "in_progress"},
}
HUMAN_ONLY = {"done", "closed"}  # Agent 做完只能交到 in_review，等人点完成
COMMANDS = {"完成": "done", "通过": "done", "关闭": "closed", "重开": "todo", "退回": "in_progress"}
CREATE = re.compile(r"(?:建任务|新任务)[:：]\s*(.+)")
COMMAND = re.compile(r"#(\d+)\s*(完成|通过|关闭|重开|退回)")
REF = re.compile(r"#(\d+)")


def load() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tasks"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"next": 1, "tasks": []}


def save(data: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def who(caller: dict) -> str:
    return caller.get("name") or caller.get("email") or caller.get("kind", "?")


def find(data: dict, tid: object) -> dict:
    try:
        n = int(str(tid).lstrip("#"))
    except ValueError as e:
        raise ValueError(f"任务编号无效：{tid!r}") from e
    t = next((t for t in data["tasks"] if t["id"] == n), None)
    if t is None:
        raise ValueError(f"没有任务 #{n}")
    return t


def line(t: dict) -> str:
    return f"#{t['id']} [{CN[t['status']]}] {t['title']} —— 负责人：{t['assignee'] or '无'}"


def log(t: dict, by: str, change: str) -> None:
    t["history"].append({"at": time.strftime("%Y-%m-%d %H:%M"), "by": by, "change": change})
    t["history"] = t["history"][-50:]


def new_task(data: dict, title: str, creator: str) -> dict:
    title = " ".join(title.split())[:120]
    if not title:
        raise ValueError("title 不能为空")
    t = {"id": data["next"], "title": title, "status": "todo", "assignee": None, "creator": creator, "history": []}
    data["next"] += 1
    data["tasks"].append(t)
    log(t, creator, "创建")
    return t


def move(t: dict, to: str, by: str, human: bool) -> None:
    if to not in STATUSES:
        raise ValueError(f"status 只能是 {'/'.join(STATUSES)}")
    if to in HUMAN_ONLY and not human:
        raise ValueError(f"{CN[to]}要由人在群聊里说（如“#{t['id']} 完成”）：做完了就交到 in_review")
    if to == t["status"]:
        return
    if to not in TRANSITIONS[t["status"]]:
        raise ValueError(f"#{t['id']} 现在是{CN[t['status']]}，不能直接改成{CN[to]}")
    if to == "todo":
        t["assignee"] = None  # 重开：重新等人认领
    log(t, by, f"{CN[t['status']]} → {CN[to]}")
    t["status"] = to


# ---- 给 Agent 的工具 ----
def t_tasks(a: dict, caller: dict) -> str:
    data = load()
    rows = [t for t in data["tasks"] if (not a.get("status") or t["status"] == a["status"])
            and (not a.get("mine") or t["assignee"] == who(caller))]
    if a.get("format") == "json":
        return json.dumps(rows, ensure_ascii=False)
    return "\n".join(line(t) for t in rows) or "任务板上没有符合条件的任务。"


def t_task_create(a: dict, caller: dict) -> str:
    data = load()
    t = new_task(data, str(a.get("title") or ""), who(caller))
    if a.get("note"):
        log(t, who(caller), f"备注：{str(a['note'])[:300]}")
    if a.get("claim") and caller.get("kind") == "agent":
        t["assignee"] = who(caller)
        move(t, "in_progress", who(caller), False)
    save(data)
    return f"已创建 {line(t)}"


def t_task_claim(a: dict, caller: dict) -> str:
    if caller.get("kind") != "agent":
        raise ValueError("认领要由成员 Agent 来做")
    data = load()
    t = find(data, a.get("id"))
    me = who(caller)
    if t["status"] == "closed":
        raise ValueError(f"#{t['id']} 已关闭：要人先说“#{t['id']} 重开”")
    if t["status"] == "done":
        raise ValueError(f"#{t['id']} 已完成")
    if t["assignee"] == me:
        raise ValueError(f"#{t['id']} 已经是你认领的了")
    if t["assignee"]:
        raise ValueError(f"#{t['id']} 已经归 @{t['assignee']}：别重复做，需要配合就在群聊里说")
    t["assignee"] = me
    log(t, me, "认领")
    if t["status"] == "todo":
        move(t, "in_progress", me, False)
    save(data)
    return f"认领成功：{line(t)}"


def t_task_update(a: dict, caller: dict) -> str:
    data = load()
    t = find(data, a.get("id"))
    me = who(caller)
    if caller.get("kind") == "agent" and t["assignee"] != me:
        raise ValueError(f"#{t['id']} 归 @{t['assignee'] or '无人'}，只有负责人能改它；没人认领就先 task_claim")
    if a.get("status"):
        move(t, str(a["status"]), me, caller.get("kind") == "user")
    if a.get("note"):
        log(t, me, f"备注：{str(a['note'])[:500]}")
    save(data)
    return f"已更新 {line(t)}"


# ---- route：被动投递与人在群聊里的指令 ----
def route_suggest(a: dict, caller: dict) -> dict:
    msg = a.get("message") or {}
    text = str(msg.get("text") or "")
    human = msg.get("author_kind") == "user"
    author = str(msg.get("author") or "")
    relay = a.get("relay") or {}
    budget = int(relay.get("budget") or 0)
    involved = set(relay.get("involved") or [])
    members = [m for m in a.get("members") or [] if isinstance(m, dict) and m.get("name") and not m.get("assistant")]
    data = load()
    done: list[str] = []
    refs: list[dict] = []
    if human:  # 人在群聊里的指令：网络认定的作者，Agent 发同样的话不算
        for m in CREATE.finditer(text):
            t = new_task(data, m.group(1).split("\n")[0], author)
            refs.append(t)
            done.append(f"创建 #{t['id']}")
        for n, word in COMMAND.findall(text):
            try:
                t = find(data, n)
                move(t, COMMANDS[word], author, True)
                refs.append(t)
                done.append(f"#{n} {word}")
            except ValueError as e:
                done.append(str(e))
        save(data)
    for n in REF.findall(text):
        t = next((t for t in data["tasks"] if t["id"] == int(n)), None)
        if t is not None and t not in refs:
            refs.append(t)
    command_only = human and bool(done) and not CREATE.search(text) and not COMMAND.sub("", text).strip(" ，,。")

    wakes: list[dict] = []

    def add(name: str, reason: str) -> None:
        if name and name != author and name not in involved and name not in {w["agent"] for w in wakes} \
                and any(m["name"] == name for m in members):
            wakes.append({"agent": name, "reason": reason})
    for t in refs:  # 先叫被提到的任务的负责人；Agent 顺口提到已经完成的任务，不为它占一次唤醒
        if t["assignee"] and (human or t["status"] not in ("done", "closed")):
            add(t["assignee"], f"#{t['id']} 的负责人")
    if human:  # 人确认完成了上一棒：叫后续任务（标题里引用了 #N、还没完成的）的负责人；没人认领的，按标题前缀找岗位
        finished = {t["id"] for t in refs if t["status"] == "done"}
        for t in data["tasks"]:
            cited = {int(n) for n in REF.findall(t["title"])} & finished
            if not cited or t["status"] in ("done", "closed") or t["id"] in finished:
                continue
            if t["assignee"]:
                add(t["assignee"], f"#{min(cited)} 完成了，接着做 #{t['id']}")
                continue
            head = re.split(r"[:：]", t["title"], maxsplit=1)[0] if re.search(r"[:：]", t["title"]) else ""
            for m in members:
                if head and head in [m["name"], *(m.get("roles") or [])]:
                    add(m["name"], f"#{min(cited)} 完成了，#{t['id']}（{head}）还没人认领")
    for m in members:  # 再叫名字、岗位或能力被提到的
        hits = [c for c in [m["name"], *(m.get("roles") or []), *(m.get("capabilities") or [])] if c and str(c) in text]
        if hits:
            add(m["name"], f"消息里提到「{hits[0]}」")
    if human and not command_only:  # 人的发言：被动投递给所有频道成员（Raft 的做法），剩下的按顺序
        for m in members:
            add(m["name"], "频道成员：人在群聊里发言")
    return {"wake": wakes[:max(budget, 0)], "done": done}


def t_route_suggest(a: dict, caller: dict) -> str:
    if caller.get("kind") != "network":
        raise ValueError("route_suggest 只给网络的 route 挂点调")
    return json.dumps(route_suggest(a, caller), ensure_ascii=False)


TOOLS = {
    "tasks": (t_tasks, "看任务板（status：只看某个状态；mine：true 只看自己认领的）。", {
        "status": {"type": "string", "description": "todo / in_progress / in_review / done / closed，可选"},
        "mine": {"type": "boolean", "description": "只看自己认领的"},
        "format": {"type": "string", "description": "json：给页面用"}}, [], True),
    "task_create": (t_task_create, "新建一个任务（title；claim：true 时直接由你认领、进行中；note：备注）。", {
        "title": {"type": "string", "description": "任务标题，一句话"},
        "claim": {"type": "boolean", "description": "直接认领"},
        "note": {"type": "string", "description": "备注，可选"}}, ["title"], False),
    "task_claim": (t_task_claim, "认领一个任务（id）。先到先得：别人已经认领的会失败，失败就别做。", {
        "id": {"type": "integer", "description": "任务编号"}}, ["id"], False),
    "task_update": (t_task_update, "更新自己认领的任务（id；status：in_progress / in_review；note：进展或交付说明）。完成、关闭由人在群聊里说。", {
        "id": {"type": "integer", "description": "任务编号"},
        "status": {"type": "string", "description": "in_progress / in_review"},
        "note": {"type": "string", "description": "进展、交付在哪"}}, ["id"], False),
    "route_suggest": (t_route_suggest, "（只给网络的 route 挂点用）被动投递与群聊指令。",
                      {"message": {"type": "object"}, "members": {"type": "array"}, "may": {"type": "array"},
                       "relay": {"type": "object"}}, [], True),
}


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        mid, method, p = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            continue
        if method == "initialize":
            result: dict = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                            "serverInfo": {"name": "matrix-teammates", "version": "0.1.0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": n, "description": d,
                                 "inputSchema": {"type": "object", "properties": props, "required": req},
                                 "annotations": {"readOnlyHint": ro}} for n, (_, d, props, req, ro) in TOOLS.items()]}
        elif method == "tools/call":
            spec = TOOLS.get(str(p.get("name")))
            caller = ((p.get("_meta") or {}).get("matrix/caller") or {"kind": "unknown"})
            try:
                if spec is None:
                    raise ValueError(f"没有工具 {p.get('name')}")
                text, err = spec[0](p.get("arguments") or {}, caller), False
            except (ValueError, OSError) as e:
                text, err = str(e), True
            result = {"content": [{"type": "text", "text": text}], "isError": err}
        elif method == "ping":
            result = {}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
