from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from ..bot_components.history import HistoryEntry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AdReviewContext:
    chat_id: int
    offender_id: int
    offender_display_html: str
    offender_name: str
    original_html: str
    history_entry: HistoryEntry
    score_penalty: int
    notice_chat_id: int
    notice_message_id: int
    confidence: Optional[float]
    locked_by: Optional[int] = None
    resolved: bool = False


class AdReviewStore:
    """广告复核 case 存储,封装原先模块级的 _ad_review_cases + 锁 + 过期任务。"""

    def __init__(self) -> None:
        self._cases: dict[str, AdReviewContext] = {}
        self._lock = asyncio.Lock()

    async def get(self, review_id: str) -> Optional[AdReviewContext]:
        async with self._lock:
            return self._cases.get(review_id)

    async def put(self, review_id: str, context: AdReviewContext) -> None:
        async with self._lock:
            self._cases[review_id] = context

    async def pop(self, review_id: str) -> Optional[AdReviewContext]:
        async with self._lock:
            return self._cases.pop(review_id, None)

    def lock(self) -> asyncio.Lock:
        return self._lock

    def cases(self) -> dict[str, AdReviewContext]:
        return self._cases

    def schedule_expiry(self, review_id: str, delay_seconds: int) -> asyncio.Task:
        return asyncio.create_task(self._expire(review_id, delay_seconds))

    async def _expire(self, review_id: str, delay_seconds: int) -> None:
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            raise
        async with self._lock:
            case = self._cases.get(review_id)
            if case and not case.resolved:
                self._cases.pop(review_id, None)
                logger.debug("广告复核任务已过期 review_id=%s", review_id)


__all__ = ["AdReviewContext", "AdReviewStore"]
