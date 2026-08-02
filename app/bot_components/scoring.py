from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Tuple

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RedisDailyScoreManager:
    """群内用户有效发言计数（永久保留，不过期）。

    语义：通过广告检测的发言 +1；判为广告 -1。
    达到跳过阈值后永久免检。Redis 为权威源，进程内缓存作故障降级。
    """

    client: Redis
    key_prefix: str
    # 可选指标对象（NullMetrics/PrometheusMetrics），用于暴露 Redis 降级状态
    metrics: object = None
    # 进程内缓存,key=(chat_id, user_id),value=score
    # 仅在 Redis 写入成功时更新,故障期间不更新
    _cache: Dict[Tuple[int, int], int] = field(default_factory=dict, repr=False)
    _degraded_logged: bool = field(default=False, repr=False)

    async def get_score(self, chat_id: int, user_id: int) -> int:
        key = self._build_key(chat_id, user_id)
        try:
            raw = await self.client.get(key)
        except RedisError as exc:
            self._mark_degraded(
                "获取 Redis 评分失败 chat_id=%s user_id=%s error=%s",
                chat_id,
                user_id,
                exc,
            )
            return self._cache_fallback(chat_id, user_id)
        self._clear_degraded_flag()
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Redis 评分格式异常 chat_id=%s user_id=%s raw=%r",
                chat_id,
                user_id,
                raw,
            )
            return 0

    async def adjust_score(self, chat_id: int, user_id: int, delta: int) -> int:
        key = self._build_key(chat_id, user_id)
        try:
            new_score = await self.client.incrby(key, delta)
            # 永久保留，不设 TTL
            self._cache[(chat_id, user_id)] = int(new_score)
            logger.debug(
                "Redis评分更新 chat_id=%s user_id=%s delta=%s score=%s",
                chat_id,
                user_id,
                delta,
                new_score,
            )
            return int(new_score)
        except RedisError as exc:
            self._mark_degraded(
                "调整 Redis 评分失败 chat_id=%s user_id=%s delta=%s error=%s",
                chat_id,
                user_id,
                delta,
                exc,
            )
            # 故障期间无法写,返回缓存里的旧值作为 best-effort 读
            return self._cache_fallback(chat_id, user_id)

    async def reset_score(self, chat_id: int, user_id: int) -> None:
        key = self._build_key(chat_id, user_id)
        try:
            await self.client.delete(key)
            self._cache.pop((chat_id, user_id), None)
            logger.debug(
                "Redis评分清零 chat_id=%s user_id=%s",
                chat_id,
                user_id,
            )
        except RedisError as exc:
            self._mark_degraded(
                "Redis 评分清零失败 chat_id=%s user_id=%s error=%s",
                chat_id,
                user_id,
                exc,
            )

    def _cache_fallback(self, chat_id: int, user_id: int) -> int:
        cached = self._cache.get((chat_id, user_id))
        if cached is None:
            return 0
        logger.warning(
            "Redis 不可用,使用缓存评分 chat_id=%s user_id=%s cached_score=%s",
            chat_id,
            user_id,
            cached,
        )
        return cached

    def _mark_degraded(self, msg: str, *args) -> None:
        # 同一类故障在短时间内不刷屏
        if not self._degraded_logged:
            logger.warning(msg, *args)
            self._degraded_logged = True
            if self.metrics is not None:
                self.metrics.set_score_redis_degraded(1)

    def _clear_degraded_flag(self) -> None:
        if self._degraded_logged and self.metrics is not None:
            self.metrics.set_score_redis_degraded(0)
        self._degraded_logged = False

    def _build_key(self, chat_id: int, user_id: int) -> str:
        # perm 与旧的按日 key（prefix:YYYYMMDD:chat:user）隔离，避免串读
        prefix = self.key_prefix.rstrip(":") if self.key_prefix else "telegram_group_guard_bot:score"
        return f"{prefix}:perm:{chat_id}:{user_id}"


__all__ = ["RedisDailyScoreManager"]
