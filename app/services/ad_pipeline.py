from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple

from aiogram.types import Message

from ..bot_components.ad_guard import check_advertisement

logger = logging.getLogger(__name__)


class AdPipeline:
    """广告检测流水线,封装 LLM 并发限流 + 可观测 metric 占位。

    现状:每个 handler 直接 await check_advertisement,LLM 慢的时候
    会阻塞 aiogram 协程 30 秒。这里加一个全局 Semaphore 限流。
    """

    def __init__(self, settings, llm_concurrency: int = 4, metrics=None) -> None:
        self._settings = settings
        self._semaphore = asyncio.Semaphore(max(1, llm_concurrency))
        self._metrics = metrics
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def check(
        self,
        content: str,
        *,
        message: Optional[Message] = None,
    ) -> Tuple[bool, Optional[float]]:
        """对内容做 LLM 广告检测。

        - 用 Semaphore 限流并发,避免单进程打爆本地 Ollama
        - 失败时打 metric(如果有)
        - 返回 (flagged, confidence),与 check_advertisement 同签名
        """
        import time

        async with self._semaphore:
            self._in_flight += 1
            if self._metrics is not None:
                self._metrics.set_llm_in_flight(self._in_flight)
            start = time.monotonic()
            try:
                chat_id = message.chat.id if message is not None else None
                flagged, confidence = await check_advertisement(
                    content, self._settings, chat_id=chat_id
                )
            finally:
                elapsed = time.monotonic() - start
                self._in_flight -= 1
                if self._metrics is not None:
                    self._metrics.set_llm_in_flight(self._in_flight)
                    self._metrics.observe_llm_latency(
                        provider=self._settings.ad_guard_provider, seconds=elapsed
                    )

            if self._metrics is not None:
                self._metrics.observe_llm_outcome(
                    provider=self._settings.ad_guard_provider,
                    flagged=bool(flagged),
                )

            return flagged, confidence


__all__ = ["AdPipeline"]
