from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Tuple

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatMember, Message

from .constants import ADMIN_STATUSES

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)


async def is_authorized_admin(bot: Bot, settings: "Settings", message: Message) -> bool:
    user = message.from_user
    if user is None:
        return False

    chat = message.chat
    if chat.type in {"group", "supergroup"}:
        try:
            member = await bot.get_chat_member(chat.id, user.id)
        except (TelegramBadRequest, TelegramForbiddenError):
            return False
        return member.status in ADMIN_STATUSES

    if not settings.allowed_chat_ids:
        return False

    for chat_id in settings.allowed_chat_ids:
        try:
            member = await bot.get_chat_member(chat_id, user.id)
        except (TelegramBadRequest, TelegramForbiddenError):
            continue
        if member.status in ADMIN_STATUSES:
            return True
    return False


def _chat_display_name(chat) -> str:
    display = getattr(chat, "full_name", None)
    if display:
        return display
    username = getattr(chat, "username", None)
    if username:
        return f"@{username}"
    first_name = getattr(chat, "first_name", None)
    last_name = getattr(chat, "last_name", None)
    if first_name or last_name:
        return " ".join(filter(None, [first_name, last_name]))
    return str(getattr(chat, "id", "unknown"))


async def resolve_target_user(message: Message, bot: Bot) -> Optional[Tuple[int, str]]:
    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        display = user.full_name or user.username or str(user.id)
        return (user.id, display)

    if not message.text:
        return None

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return None

    arg = parts[1].strip()
    if not arg:
        return None

    if arg.startswith('@'):
        username = arg.lstrip('@')
        logger.debug("忽略用户名参数 username=%s，指令仅支持回复或用户ID", username)
        return None

    try:
        user_id = int(arg)
    except ValueError:
        return None

    return (user_id, str(user_id))


async def resolve_target_member(message: Message, bot: Bot) -> Optional[ChatMember]:
    chat = message.chat
    if chat.type not in {"group", "supergroup"}:
        logger.debug(
            "resolve_target_member 跳过非群聊环境 chat_id=%s message_id=%s",
            getattr(chat, "id", None),
            message.message_id,
        )
        return None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        try:
            member = await bot.get_chat_member(chat.id, target_id)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.debug(
                "通过 reply 解析目标成员失败 chat_id=%s target_id=%s error=%s",
                chat.id,
                target_id,
                exc,
            )
            return None
        return member

    target = await resolve_target_user(message, bot)
    if target is None:
        logger.debug(
            "解析命令参数失败 chat_id=%s message_id=%s raw_text=%r",
            chat.id,
            message.message_id,
            message.text,
        )
        return None

    target_id, _ = target
    try:
        return await bot.get_chat_member(chat.id, target_id)
    except TelegramBadRequest as exc:
        logger.debug(
            "获取目标成员信息失败 chat_id=%s target_id=%s error=%s",
            chat.id,
            target_id,
            exc,
        )
    except TelegramForbiddenError as exc:
        logger.debug(
            "Bot 无权限获取成员信息 chat_id=%s target_id=%s error=%s",
            chat.id,
            target_id,
            exc,
        )
    return None


__all__ = ["is_authorized_admin", "resolve_target_user", "resolve_target_member"]

