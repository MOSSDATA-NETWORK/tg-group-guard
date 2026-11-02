"""群组封禁同步到关联频道。

Telegram 的封禁是 per-chat 的:群里 ban 用户不影响其在关联频道(linked_chat_id)
的状态。本模块在群组封禁成功后,若群绑定了关联频道且 bot 在频道有权限,
按相同语义同步处理(永久封禁 / 踢出 / 临时封禁)。

任何失败(无关联频道、bot 非频道管理员、目标是频道管理员等)都只记日志,
绝不阻断群内主流程。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

logger = logging.getLogger(__name__)


async def sync_ban_to_linked_channel(
    bot: Bot,
    chat_id: int,
    user_id: int,
    *,
    unban_after: bool = False,
    until_date: Optional[datetime] = None,
) -> None:
    """把群组封禁同步到关联频道。

    unban_after=True 对应群组"踢出"语义(ban+unban),频道侧同步踢出;
    until_date 用于临时封禁,频道侧使用相同时长。
    """
    try:
        chat = await bot.get_chat(chat_id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.debug("同步频道封禁:获取群信息失败 chat_id=%s error=%s", chat_id, exc)
        return
    linked_id = getattr(chat, "linked_chat_id", None)
    if not linked_id:
        return
    try:
        await bot.ban_chat_member(linked_id, user_id, until_date=until_date)
        if unban_after:
            await bot.unban_chat_member(linked_id, user_id, only_if_banned=True)
        logger.info(
            "已同步封禁到关联频道 chat_id=%s linked_chat_id=%s user_id=%s unban_after=%s",
            chat_id,
            linked_id,
            user_id,
            unban_after,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        # 常见原因:bot 不是频道管理员/用户不在频道/目标是频道管理员——不影响群内封禁
        logger.debug(
            "同步关联频道封禁未完成(通常因 bot 无频道权限) chat_id=%s linked_chat_id=%s user_id=%s error=%s",
            chat_id,
            linked_id,
            user_id,
            exc,
        )
    except Exception as exc:  # 兜底:同步逻辑任何异常都不允许外溢
        logger.warning(
            "同步关联频道封禁异常 chat_id=%s linked_chat_id=%s user_id=%s error=%r",
            chat_id,
            linked_id,
            user_id,
            exc,
        )


__all__ = ["sync_ban_to_linked_channel"]
