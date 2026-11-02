"""群消息行为风控：重复消息、短消息刷屏与发送频率检测。"""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from typing import Optional


class MessageBehaviorGuard:
    """按 (群, 用户) 保存有限时间窗内的消息事件，全部在内存中计算。"""

    def __init__(self, max_users: int = 10000) -> None:
        self._events: OrderedDict[tuple[int, int], deque[tuple[float, str]]] = OrderedDict()
        self._max_users = max_users

    def clear(self) -> None:
        self._events.clear()

    def check(
        self,
        *,
        chat_id: int,
        user_id: int,
        text: str,
        duplicate_threshold: int,
        duplicate_window_seconds: int,
        short_flood_threshold: int,
        short_flood_window_seconds: int,
        short_message_length: int,
        rate_limit: int,
        rate_window_seconds: int,
        now: Optional[float] = None,
    ) -> Optional[str]:
        """记录一条消息；命中规则时返回 rate / duplicate / short_flood。"""
        current = time.monotonic() if now is None else now
        normalized = " ".join(text.split()).casefold()
        windows = [1]
        for enabled, window in (
            (duplicate_threshold, duplicate_window_seconds),
            (short_flood_threshold, short_flood_window_seconds),
            (rate_limit, rate_window_seconds),
        ):
            if enabled > 0:
                windows.append(max(1, window))
        max_window = max(windows)

        key = (chat_id, user_id)
        events = self._events.get(key)
        if events is None:
            events = deque()
            self._events[key] = events
        while events and current - events[0][0] > max_window:
            events.popleft()
        events.append((current, normalized))
        self._events.move_to_end(key)
        while len(self._events) > self._max_users:
            self._events.popitem(last=False)

        if rate_limit > 0:
            rate_count = sum(
                1 for timestamp, _ in events
                if current - timestamp <= rate_window_seconds
            )
            if rate_count > rate_limit:
                return "rate"

        if duplicate_threshold > 0 and normalized:
            duplicate_count = sum(
                1 for timestamp, content in events
                if current - timestamp <= duplicate_window_seconds and content == normalized
            )
            if duplicate_count >= duplicate_threshold:
                return "duplicate"

        if (
            short_flood_threshold > 0
            and normalized
            and len(normalized) <= max(1, short_message_length)
        ):
            short_count = sum(
                1
                for timestamp, content in events
                if current - timestamp <= short_flood_window_seconds
                and content
                and len(content) <= max(1, short_message_length)
            )
            if short_count >= short_flood_threshold:
                return "short_flood"
        return None


__all__ = ["MessageBehaviorGuard"]
