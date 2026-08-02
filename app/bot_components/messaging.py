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

# 注入的存储:启用 TTL 删除任务持久化(重启后可恢复);
# 未注入时退化为纯内存调度(旧行为)
_STORE: "VerificationStore | None" = None
# fire-and-forget 任务强引用集合,防 GC 提前回收(CPython 官方警告)
_TTL_TASKS: set[asyncio.Task] = set()


def configure_messaging_store(store: "VerificationStore | None") -> None:
    """注入存储,启用 TTL 删除任务持久化。启动时调用一次。"""
    global _STORE
    _STORE = store


async def _delete_message_later_tracked(
    bot: Bot, chat_id: int, message_id: int, delay: int
) -> None:
    """带持久化的延迟删除:先落库,删除成功后销记录;
    进程退出被中断(CancelledError)时保留记录,重启后恢复。"""
    if _STORE is not None:
        try:
            from datetime import datetime, timedelta, timezone

            await _STORE.schedule_message_deletion(
                chat_id,
                message_id,
                datetime.now(tz=timezone.utc) + timedelta(seconds=delay),
            )
        except Exception as exc:  # 持久化失败不阻断删除
            logger.warning("持久化删除任务失败 chat_id=%s msg_id=%s error=%r", chat_id, message_id, exc)
    try:
        await delete_message_later(bot, chat_id, message_id, delay)
    except asyncio.CancelledError:
        raise  # 保留持久化记录,重启后恢复
    if _STORE is not None:
        try:
            await _STORE.remove_scheduled_deletion(chat_id, message_id)
        except Exception as exc:
            logger.warning("移除删除任务记录失败 chat_id=%s msg_id=%s error=%r", chat_id, message_id, exc)


def _spawn_delete_task(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    task = asyncio.create_task(_delete_message_later_tracked(bot, chat_id, message_id, delay))
    _TTL_TASKS.add(task)
    task.add_done_callback(_TTL_TASKS.discard)


async def restore_scheduled_deletions(bot: Bot) -> int:
    """重启后恢复持久化的 TTL 删除任务;到期的立即删除。返回恢复条数。"""
    if _STORE is None:
        return 0
    try:
        rows = await _STORE.fetch_scheduled_deletions()
    except Exception as exc:
        logger.warning("读取持久化删除任务失败: %r", exc)
        return 0
    import time as _time

    now = int(_time.time())
    for row in rows:
        delay = max(0, int(row["delete_at"]) - now)
        _spawn_delete_task(bot, row["chat_id"], row["message_id"], delay)
    if rows:
        logger.info("已恢复 %s 条持久化的消息删除任务", len(rows))
    return len(rows)


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
            _spawn_delete_task(bot, chat_id, message.message_id, ttl)
        else:
            logger.debug(
                "跳过自动删除 chat_id=%s message_id=%s ttl=%s",
                chat_id,
                message.message_id,
                ttl,
            )
    elif delete_mode == "record":
        if store and token:
            try:
                await store.set_prompt_message(token, message.message_id)
            except Exception as exc:
                # 写库失败(如 SQLite 瞬时锁定)不能中断流程:若在这里抛出,
                # 下方 TTL 删除不会调度、prompt_message_id 也没落库,
                # 提示消息将成为群内永久孤儿
                logger.warning(
                    "记录验证提示消息 ID 失败(继续调度 TTL 删除) chat_id=%s msg_id=%s error=%r",
                    chat_id,
                    message.message_id,
                    exc,
                )
        if ttl and ttl > 0:
            logger.debug(
                "计划删除验证提示 chat_id=%s message_id=%s ttl=%s",
                chat_id,
                message.message_id,
                ttl,
            )
            _spawn_delete_task(bot, chat_id, message.message_id, ttl)
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
        _spawn_delete_task(bot, chat_id, message_id, ttl)


def truncate_for_logging(text: str, *, limit: int = MAX_LOG_PAYLOAD_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


__all__ = [
    "send_message_with_ttl",
    "delete_message_later",
    "schedule_message_auto_delete",
    "configure_messaging_store",
    "restore_scheduled_deletions",
    "truncate_for_logging",
]

