"""群消息发送频率风控。"""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from typing import Optional


class MessageBehaviorGuard:
    """按 (群, 用户) 保存有限时间窗内的消息时间，全部在内存中计算。"""

    def __init__(self, max_users: int = 10000) -> None:
        self._events: OrderedDict[tuple[int, int], deque[float]] = OrderedDict()
        self._max_users = max_users

    def clear(self) -> None:
        self._events.clear()

    def check(
        self,
        *,
        chat_id: int,
        user_id: int,
        rate_limit: int,
        rate_window_seconds: int,
        now: Optional[float] = None,
    ) -> Optional[str]:
        """记录一条消息；超过发送频率时返回 rate，否则返回 None。"""
        if rate_limit <= 0:
            return None

        current = time.monotonic() if now is None else now
        window = max(1, rate_window_seconds)
        key = (chat_id, user_id)
        events = self._events.get(key)
        if events is None:
            events = deque()
            self._events[key] = events
        while events and current - events[0] > window:
            events.popleft()
        events.append(current)
        self._events.move_to_end(key)
        while len(self._events) > self._max_users:
            self._events.popitem(last=False)

        if len(events) > rate_limit:
            return "rate"
        return None


__all__ = ["MessageBehaviorGuard"]
