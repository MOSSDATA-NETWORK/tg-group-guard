from __future__ import annotations

import asyncio
import logging
from typing import Optional, TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message

from .constants import MAX_LOG_PAYLOAD_LENGTH

if TYPE_CHECKING:
    from ..storage import VerificationStore


logger = logging.getLogger(__name__)


async def send_message_with_ttl(
    bot: Bot,
    chat_id: int,
    text: str,
    ttl: Optional[int],
    *,
    store: "VerificationStore" | None = None,
    token: str | None = None,
    delete_mode: str = "auto",
    **kwargs,
) -> Message:
    message = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    if delete_mode == "auto":
        if ttl and ttl > 0:
            logger.debug(
                "计划自动删除消息 chat_id=%s message_id=%s ttl=%s",
                chat_id,
                message.message_id,
                ttl,
            )
            asyncio.create_task(
                delete_message_later(bot, chat_id, message.message_id, ttl)
            )
        else:
            logger.debug(
                "跳过自动删除 chat_id=%s message_id=%s ttl=%s",
                chat_id,
                message.message_id,
                ttl,
            )
    elif delete_mode == "record":
        if store and token:
            await store.set_prompt_message(token, message.message_id)
        if ttl and ttl > 0:
            logger.debug(
                "计划删除验证提示 chat_id=%s message_id=%s ttl=%s",
                chat_id,
                message.message_id,
                ttl,
            )
            asyncio.create_task(
                delete_message_later(bot, chat_id, message.message_id, ttl)
            )
        else:
            logger.debug(
                "验证提示不自动删除 chat_id=%s message_id=%s ttl=%s",
                chat_id,
                message.message_id,
                ttl,
            )
    return message


async def delete_message_later(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.debug(
            "已自动删除消息 chat_id=%s message_id=%s 延迟=%s",
            chat_id,
            message_id,
            delay,
        )
    except asyncio.CancelledError:
        raise
    except TelegramBadRequest as exc:
        logger.debug(
            "延迟删除消息失败 chat_id=%s message_id=%s error=%s",
            chat_id,
            message_id,
            exc,
        )
    except TelegramForbiddenError as exc:
        logger.warning(
            "无权限删除消息 chat_id=%s message_id=%s error=%s",
            chat_id,
            message_id,
            exc,
        )
    except Exception as exc:  # pragma: no cover - 兜底日志
        logger.warning(
            "删除消息时出现未知错误 chat_id=%s message_id=%s error=%r",
            chat_id,
            message_id,
            exc,
            exc_info=True,
        )


def schedule_message_auto_delete(bot: Bot, message: Message, ttl: Optional[int]) -> None:
    # 私聊场景不调度自动删除,避免无意义任务堆积,且用户能在私聊回看历史指令
    chat_type = getattr(getattr(message, "chat", None), "type", None)
    if chat_type == "private":
        return
    if ttl and ttl > 0:
        chat_id = message.chat.id
        message_id = message.message_id
        logger.debug(
            "计划删除指令消息 chat_id=%s message_id=%s ttl=%s",
            chat_id,
            message_id,
            ttl,
        )
        asyncio.create_task(delete_message_later(bot, chat_id, message_id, ttl))


def truncate_for_logging(text: str, *, limit: int = MAX_LOG_PAYLOAD_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


__all__ = [
    "send_message_with_ttl",
    "delete_message_later",
    "schedule_message_auto_delete",
    "truncate_for_logging",
]

