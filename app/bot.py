from __future__ import annotations

import logging
import socket

try:
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.enums import ParseMode
except ModuleNotFoundError as exc:
    raise RuntimeError("运行 KKBot 需要先安装 aiogram 依赖") from exc


from .ad_guard_rules import configure_ad_guard_rules
from .keyword_replies import configure_keyword_replies
from .bot_components.scoring import RedisDailyScoreManager
from .config import Settings
from .routers import (
    build_ad_guard_router,
    build_admin_commands_router,
    build_basic_router,
    build_verify_router,
)
from .services.ad_pipeline import AdPipeline
from .services.dependencies import BotServices
from .state import AdReviewStore, AdVoteStore, MessageHistoryStore
from .storage import VerificationStore

logger = logging.getLogger(__name__)


class _IPv4AiohttpSession(AiohttpSession):
    """仅通过 IPv4 连接 Telegram Bot API，避免 IPv6 路由异常或被重置。"""

    def __init__(self, proxy=None, limit: int = 100, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(proxy=proxy, limit=limit, **kwargs)
        self._connector_init["family"] = socket.AF_INET


def create_bot(settings: Settings, store: VerificationStore) -> Bot:
    if settings.telegram_proxy:
        session = _IPv4AiohttpSession(proxy=settings.telegram_proxy)
    else:
        session = _IPv4AiohttpSession()
    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher(
    settings: Settings,
    store: VerificationStore,
    score_manager: RedisDailyScoreManager,
    *,
    pipeline: AdPipeline | None = None,
    metrics=None,
) -> Dispatcher:
    """构造 dispatcher,装配各 router。

    pipeline / metrics 可选,用于注入可观测性能力。
    """
    dp = Dispatcher()
    configure_ad_guard_rules(settings.ad_guard_rules_file)
    configure_keyword_replies(settings.keyword_reply_rules_file)

    services = BotServices(
        settings=settings,
        store=store,
        score_manager=score_manager,
        history_store=MessageHistoryStore(),
        ad_review_store=AdReviewStore(on_expire=store.delete_ad_deletion),
        ad_vote_store=AdVoteStore(),
    )

    if pipeline is None:
        llm_concurrency = getattr(settings, "ad_guard_llm_concurrency", 4)
        pipeline = AdPipeline(settings, llm_concurrency=llm_concurrency, metrics=metrics)

    # Router 顺序:basic(命令) > admin_commands(命令) > verify(事件+回调)
    # > ad_guard(普通文本消息兜底,必须放最后,F.text 兜底所有文本)
    dp.include_router(build_basic_router(services))
    dp.include_router(build_admin_commands_router(services))
    dp.include_router(build_verify_router(services))
    dp.include_router(build_ad_guard_router(services, pipeline))

    # workflow_data 让每个 handler 都能通过 services 参数注入
    dp.workflow_data.update(services=services, pipeline=pipeline, metrics=metrics)

    return dp


__all__ = ["create_bot", "create_dispatcher"]
