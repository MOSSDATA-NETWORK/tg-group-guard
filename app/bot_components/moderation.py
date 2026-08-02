from __future__ import annotations

import logging
import time
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from ..config import Settings
from ..chat_settings import resolve_chat
from .messaging import send_message_with_ttl
from .scoring import RedisDailyScoreManager

logger = logging.getLogger(__name__)

# 封禁失败冷却:机器人无权限或目标是管理员时,ban 会持续失败。
# 没有冷却的话,该用户每条消息都会重复触发"扣分 + 封禁尝试 + 公告",
# 评分无限下探且群内公告刷屏。冷却期内保持"禁言"效果(仍删消息),
# 但不再扣分、不再尝试封禁、不再发公告。
_BAN_FAILURE_COOLDOWN_SECONDS = 600
_ban_failure_cooldown: dict[tuple[int, int], float] = {}


def _prune_ban_failure_cooldown(now: float) -> None:
    if len(_ban_failure_cooldown) <= 10000:
        return
    expired = [
        key
        for key, ts in _ban_failure_cooldown.items()
        if now - ts >= _BAN_FAILURE_COOLDOWN_SECONDS
    ]
    for key in expired:
        _ban_failure_cooldown.pop(key, None)
    if len(_ban_failure_cooldown) > 10000:
        for key in list(_ban_failure_cooldown)[: len(_ban_failure_cooldown) // 2]:
            _ban_failure_cooldown.pop(key, None)


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

    now_mono = time.monotonic()
    _prune_ban_failure_cooldown(now_mono)
    last_failure = _ban_failure_cooldown.get((chat_id, user_id))
    if last_failure is not None and now_mono - last_failure < _BAN_FAILURE_COOLDOWN_SECONDS:
        # 冷却期:维持删除(禁言效果),跳过扣分/封禁/公告
        try:
            await bot.delete_message(chat_id, message.message_id)
        except TelegramBadRequest as exc:
            logger.debug(
                "冷却期删除低分用户消息失败 chat_id=%s msg_id=%s error=%s",
                chat_id,
                message.message_id,
                exc,
            )
        return

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
        # 封禁失败:回滚本次扣分(避免无法封禁的用户评分无限下探),
        # 并进入冷却(避免每条消息重复封禁尝试 + 公告刷屏)
        try:
            score_after = await score_manager.adjust_score(chat_id, user_id, 1)
        except Exception as rb_exc:
            logger.warning(
                "低评分封禁失败回滚扣分失败 chat_id=%s user_id=%s error=%r",
                chat_id,
                user_id,
                rb_exc,
            )
        _ban_failure_cooldown[(chat_id, user_id)] = now_mono
    else:
        _ban_failure_cooldown.pop((chat_id, user_id), None)

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
    if not ban_success:
        # ban_chat_member 失败时用户其实仍在群里,公告必须如实,
        # 否则管理员会误以为已处理完成(对齐广告封禁路径的做法)
        action_suffix = "移除失败（机器人权限不足或目标为管理员），请管理员手动处理。"
    elif kick_only:
        action_suffix = "已移出（未封禁，累计评分过低）。"
    else:
        action_suffix = "已移出并拉黑。"

    notice = (
        f"🚫 <a href='tg://user?id={user_id}'>{display_name}</a> 累计评分 {score_after} ≤ "
        f"{settings.ad_guard_score_ban_threshold}，{action_suffix}"
    )
    await send_message_with_ttl(
        bot,
        chat_id=chat_id,
        text=notice,
        ttl=resolve_chat(settings, chat_id, "message_ttl_seconds"),
        disable_web_page_preview=True,
    )


__all__ = ["handle_low_score_violation"]

