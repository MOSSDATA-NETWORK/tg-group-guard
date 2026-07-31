from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from ..config import Settings
from .messaging import send_message_with_ttl
from .scoring import RedisDailyScoreManager

logger = logging.getLogger(__name__)


async def handle_low_score_violation(
    bot: Bot,
    message: Message,
    *,
    settings: Settings,
    score_manager: RedisDailyScoreManager,
    current_score: int,
    store=None,
) -> None:
    if message.from_user is None:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    logger.info(
        "触发低评分处理 chat_id=%s user_id=%s score=%s",
        chat_id,
        user_id,
        current_score,
    )
    score_after = await score_manager.adjust_score(chat_id, user_id, -1)

    try:
        await bot.delete_message(chat_id, message.message_id)
    except TelegramBadRequest as exc:
        logger.warning(
            "低评分封禁前删除消息失败 chat_id=%s msg_id=%s error=%s",
            chat_id,
            message.message_id,
            exc,
            exc_info=exc,
        )

    kick_only = not settings.ad_guard_ban

    ban_success = False
    try:
        await bot.ban_chat_member(chat_id, user_id)
        if kick_only:
            await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        ban_success = True
    except TelegramBadRequest as exc:
        logger.warning(
            "低评分移除失败 chat_id=%s user_id=%s error=%s",
            chat_id,
            user_id,
            exc,
            exc_info=exc,
        )

    if ban_success:
        await score_manager.reset_score(chat_id, user_id)
        score_after = 0
        if store is not None:
            try:
                await store.reset_ad_qualification(chat_id, user_id)
            except Exception as exc:
                logger.warning(
                    "低评分封禁后重置合格状态失败 chat_id=%s user_id=%s error=%r",
                    chat_id,
                    user_id,
                    exc,
                    exc_info=True,
                )
            try:
                await store.record_ban_event(
                    chat_id=chat_id,
                    user_id=user_id,
                    display_name=message.from_user.full_name
                    or message.from_user.username
                    or str(user_id),
                    operator_id=None,
                    operator_name="system",
                    reason="low_score",
                    action="kick" if kick_only else "ban",
                    currently_banned=not kick_only,
                )
            except Exception as exc:
                logger.warning(
                    "记录低评分封禁日志失败 chat_id=%s user_id=%s error=%r",
                    chat_id,
                    user_id,
                    exc,
                    exc_info=True,
                )

    display_name = escape(message.from_user.full_name or str(user_id))
    if kick_only:
        action_suffix = "已移出（未封禁，今日评分过低）。"
    else:
        action_suffix = "已移出并拉黑。"

    notice = (
        f"🚫 <a href='tg://user?id={user_id}'>{display_name}</a> 今日评分 {score_after} ≤ "
        f"{settings.ad_guard_score_ban_threshold}，{action_suffix}"
    )
    await send_message_with_ttl(
        bot,
        chat_id=chat_id,
        text=notice,
        ttl=settings.message_ttl_seconds,
        disable_web_page_preview=True,
    )


__all__ = ["handle_low_score_violation"]

