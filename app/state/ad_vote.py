from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from aiogram.types import Poll


@dataclass(slots=True)
class AdVoteContext:
    chat_id: int
    offender_id: int
    offender_display_html: str
    offender_message_id: int
    poll_message_id: int
    event: asyncio.Event = field(default_factory=asyncio.Event)
    force_ban: bool = False
    poll_result: Optional[Poll] = None


class AdVoteStore:
    """广告投票 case 存储,封装原先模块级的 _ad_vote_cases + 锁。"""

    def __init__(self) -> None:
        self._cases: dict[str, AdVoteContext] = {}
        self._lock = asyncio.Lock()

    async def put(self, vote_id: str, context: AdVoteContext) -> None:
        async with self._lock:
            self._cases[vote_id] = context

    async def pop(self, vote_id: str) -> Optional[AdVoteContext]:
        async with self._lock:
            return self._cases.pop(vote_id, None)

    async def get(self, vote_id: str) -> Optional[AdVoteContext]:
        async with self._lock:
            return self._cases.get(vote_id)

    def lock(self) -> asyncio.Lock:
        return self._lock

    def cases(self) -> dict[str, AdVoteContext]:
        return self._cases


__all__ = ["AdVoteContext", "AdVoteStore"]
