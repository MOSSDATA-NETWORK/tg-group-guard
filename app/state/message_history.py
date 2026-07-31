from __future__ import annotations

from collections import deque
from typing import Deque, Dict

from ..bot_components.constants import MESSAGE_HISTORY_LIMIT
from ..bot_components.history import HistoryEntry


class MessageHistoryStore:
    """每个群的消息历史,替换原先模块级的 _message_histories。"""

    def __init__(self) -> None:
        self._histories: Dict[int, Deque[HistoryEntry]] = {}

    def get(self, chat_id: int) -> Deque[HistoryEntry]:
        history = self._histories.get(chat_id)
        if history is None:
            history = deque(maxlen=MESSAGE_HISTORY_LIMIT)
            self._histories[chat_id] = history
        return history


__all__ = ["MessageHistoryStore"]
