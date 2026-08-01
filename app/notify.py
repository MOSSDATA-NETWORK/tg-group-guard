"""管理事件 Telegram 通知。

配置保存、更新、回滚、关停等管理操作发生时，向所有授权群发送通知，
让群管理员能及时感知后台变更。发送失败只记日志，不阻断主流程。
"""
from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from .config import Settings

logger = logging.getLogger(__name__)


async def notify_admins(bot, settings: Settings, text: str) -> None:
    if bot is None or not settings.allowed_chat_ids:
        return
    for chat_id in settings.allowed_chat_ids:
        try:
            await bot.send_message(chat_id, text)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning("管理通知发送失败 chat_id=%s error=%r", chat_id, exc)
        except Exception as exc:  # pragma: no cover - 网络异常兜底
            logger.warning("管理通知发送异常 chat_id=%s error=%r", chat_id, exc)
