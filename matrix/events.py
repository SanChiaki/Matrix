"""网络事件总线：一条全局事件流，供可视化与审计（不持久化，持久状态见 store.py）。"""

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

    @property
    def last_seq(self) -> int:
        with self._cond:
            return self._offset + len(self._events)

    def emit(self, type_: str, **data: Any) -> Event:
        with self._cond:
            ev = Event(seq=self._offset + len(self._events) + 1, type=type_, data=data)
            self._events.append(ev)
            if len(self._events) > self._keep:
                drop = len(self._events) - self._keep
                del self._events[:drop]
                self._offset += drop
            self._cond.notify_all()
            return ev

    def since(self, seq: int) -> list[Event]:
        with self._cond:
            return self._events[max(0, seq - self._offset):]

    def wait(self, seq: int, timeout: float) -> list[Event]:
        with self._cond:
            if self._offset + len(self._events) <= seq and not self.closed:
                self._cond.wait(timeout)
            return self._events[max(0, seq - self._offset):]

    def close(self) -> None:
        """停服务时唤醒所有等待中的事件流，让它们立即结束。"""
        with self._cond:
            self.closed = True
            self._cond.notify_all()
