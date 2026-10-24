"""信息只流向当时在场的人（不变量 22）：私有目录里每个文件记下它是在哪个场合写的，网络据此决定挂载与放行。

参照情境完整性（research: information-flow-control#contextual-integrity）：隐私问题出在信息越出了它原本所属的场合；
管住“说出去”而不是“知道什么”，参照 CaMeL / Dual LLM（research: information-flow-control#camel、
information-flow-control#dual-llm）：判断是普通代码按来源标签做的集合比较，不用模型、不看内容。

- 场合（venue）：项目 id，或 `ME`（私聊、项目会话、助理对话：只有所有者在场）。
- 场合在场的人：项目现在的全部参与者，加上 Agent 的所有者（Agent 只代表它在场）；`ME` 只有所有者。
- 文件标签：写下它的那些运行的场合与来源（`venues`），以及写的时候有没有别人的输入（`mixed`）。
  文件能给谁看 = 各场合在场的人的交集。没有标签的老文件按 `ME`，最保守。
- 一次运行的输出能到达的人（reach）：在项目里（群聊、任务、项目会话）是项目的全部参与者，私聊里只有所有者。
  文件在场的人包含 reach，或者每个场合都有授权，就挂载；其余的只列名字，用 `open_note` 读。
- 读了别处的笔记，那个场合记进这个模型会话的来源（`session_sources`）；输出给一组人时，每个来源都要
  在场的人包含这组人、或者有所有者的授权，否则拦下交给所有者确认。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

ME = "me"
SKILLS_PREFIX = "skills/"
# 读了别处来源的会话不能再有的能力：都能把内容带出网络（联网、执行命令、外部服务）
EXFIL = ("web.research", "shell.exec", "connector.use")

Present = Callable[[str], set]
Labels = dict[str, dict[str, Any]]


def load(path: Path) -> Labels:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def save(path: Path, labels: Labels) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(labels, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def venues_of(labels: Labels, rel: str) -> list[str]:
    return list(labels.get(rel, {}).get("venues") or [ME])


def is_mixed(labels: Labels, rel: str) -> bool:
    return bool(labels.get(rel, {}).get("mixed"))


def passes(venues: Iterable[str], audience: set, present: Present, granted: Iterable[str] = ()) -> list[str]:
    """把来自 venues 的信息给 audience 看：返回越界的场合（空表示放行）。"""
    ok = set(granted)
    return sorted({v for v in venues if v not in ok and not audience <= present(v)})


def mountable(rel: str, labels: Labels, reach: set, present: Present, granted: Iterable[str] = ()) -> bool:
    if rel.startswith(SKILLS_PREFIX):
        return True  # 技能是做法，可以带到任何场合（有别人的场合里只读，见 engine._run）
    return not passes(venues_of(labels, rel), reach, present, granted)


def relabel(labels: Labels, written: Iterable[str], deleted: Iterable[str], venue: str,
            sources: Iterable[str], mixed: bool, existed: Iterable[str] = ()) -> Labels:
    """一次运行写回之后：写过的文件并入这次的场合与来源（只会收窄），删掉的文件去掉标签。
    existed：写之前就在的文件；其中没有标签的是老文件，先按 ME 算。"""
    out = dict(labels)
    add = [venue, *sources]
    before = set(existed)
    for rel in written:
        old = out.get(rel)
        prior = (old.get("venues") or []) if old else ([ME] if rel in before else [])
        venues = list(dict.fromkeys([*prior, *add]))
        out[rel] = {"venues": venues, "mixed": bool(mixed or (old or {}).get("mixed"))}
    for rel in deleted:
        out.pop(rel, None)
    return out
