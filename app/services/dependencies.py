from __future__ import annotations

from dataclasses import dataclass

from ..bot_components.scoring import RedisDailyScoreManager
from ..config import Settings
from ..state.ad_review import AdReviewStore
from ..state.ad_vote import AdVoteStore
from ..state.message_history import MessageHistoryStore
from ..storage import VerificationStore


@dataclass(slots=True)
class BotServices:
    """单个 bot 实例共享的服务对象集合。

    替代原先散落在 bot.py 闭包里、被所有 handler 隐式捕获的可变状态。
    每个 dispatcher 创建时构造一份,所有 router 通过闭包持有引用。
    Bot 实例本身由 aiogram 通过 handler 的 bot 参数注入,不要放在这里。
    """

    settings: Settings
    store: VerificationStore
    score_manager: RedisDailyScoreManager
    history_store: MessageHistoryStore
    ad_review_store: AdReviewStore
    ad_vote_store: AdVoteStore
    # 可选指标对象（NullMetrics/PrometheusMetrics），未注入时为 None
    metrics: object = None


__all__ = ["BotServices"]
