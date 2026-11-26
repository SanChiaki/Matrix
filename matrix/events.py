"""网络事件总线：一条全局事件流，供可视化与审计。

内存里留最近 `keep` 条。用数据库存储时（M7）另外落进 event 表（`attach`）：序号接着库里的往下数，
重启后网页凭 Last-Event-ID 续上，内存里没有的从库里补。用 state.json 时不持久化，重启后序号从 0 重新计数。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    seq: int
    type: str
    data: dict[str, Any]
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "ts": self.ts, "data": self.data}


class EventBus:
    def __init__(self, keep: int = 5000) -> None:
        self._events: list[Event] = []
        self._offset = 0  # 已丢弃的事件数
        self._keep = keep
        self._cond = threading.Condition()
        self.closed = False
        self._sink: Any = None   # db.EventLog：落库

    def attach(self, sink: Any) -> None:
        """接上事件表：序号从库里最大的往下数，之后每个事件也写进去。要在第一个事件之前接。"""
        with self._cond:
            self._sink = sink
            self._offset = max(self._offset + len(self._events), sink.last_seq())
            self._events = []

    def _older(self, seq: int) -> list[Event]:
        """调用方持有 _cond。内存里已经没有的、序号在 seq 之后的事件：从库里补（没接库就没有）。"""
        if self._sink is None or seq >= self._offset:
            return []
        return [Event(seq=s, type=t, data=d, ts=ts) for s, ts, t, d in self._sink.since(seq)
                if s <= self._offset]

    @property
    def last_seq(self) -> int:
        with self._cond:
            return self._offset + len(self._events)

    def emit(self, type_: str, **data: Any) -> Event:
        with self._cond:
            ev = Event(seq=self._offset + len(self._events) + 1, type=type_, data=data)
            self._events.append(ev)
            if self._sink is not None:
                self._sink.put(ev.seq, ev.ts, ev.type, ev.data)
            if len(self._events) > self._keep:
                drop = len(self._events) - self._keep
                del self._events[:drop]
                self._offset += drop
            self._cond.notify_all()
            return ev

    def since(self, seq: int) -> list[Event]:
        with self._cond:
            return self._older(seq) + self._events[max(0, seq - self._offset):]

    def wait(self, seq: int, timeout: float) -> list[Event]:
        with self._cond:
            if self._offset + len(self._events) <= seq and not self.closed:
                self._cond.wait(timeout)
            return self._older(seq) + self._events[max(0, seq - self._offset):]

    def close(self) -> None:
        """停服务时唤醒所有等待中的事件流，让它们立即结束。"""
        with self._cond:
            self.closed = True
            self._cond.notify_all()
