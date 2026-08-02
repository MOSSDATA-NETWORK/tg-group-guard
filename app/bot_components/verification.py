from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from html import escape
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, User

from ..config import Settings
from ..chat_settings import resolve_chat
from ..storage import VerificationRecord, VerificationStore
from .constants import UTC
from .messaging import send_message_with_ttl

logger = logging.getLogger(__name__)


def _is_benign_ban_skip(exc: TelegramBadRequest) -> bool:
    """用户从未入群、已离开或 ID 对当前群无效时，封禁会返回此类错误，可安全忽略。"""
    text = str(exc).upper()
    return "PARTICIPANT_ID_INVALID" in text or "USER_NOT_PARTICIPANT" in text


async def ban_and_cleanup(
    bot: Bot,
    store: VerificationStore,
    record: VerificationRecord,
    *,
    reason: str | None = None,
    unban_after: bool = True,
) -> bool:
    try:
        await delete_prompt_message(bot, record)
        await bot.ban_chat_member(
            chat_id=record.chat_id,
            user_id=record.user_id,
            revoke_messages=True,
        )
        if unban_after:
            await bot.unban_chat_member(
                chat_id=record.chat_id,
                user_id=record.user_id,
                only_if_banned=True,
            )
        logger.info(
            "已移除待验证用户 chat_id=%s user_id=%s reason=%s",
            record.chat_id,
            record.user_id,
            reason or "unspecified",
        )
        await store.delete(record.token)
        return True
    except TelegramBadRequest as exc:
        if _is_benign_ban_skip(exc):
            logger.debug(
                "跳过封禁（用户非群成员或 ID 无效，仅清理记录） chat_id=%s user_id=%s reason=%s",
                record.chat_id,
                record.user_id,
                reason or "unspecified",
            )
            await store.delete(record.token)
            return True
        logger.warning(
            "移除待验证用户失败 chat_id=%s user_id=%s error=%s",
            record.chat_id,
            record.user_id,
            exc,
            exc_info=exc,
        )
        await store.delete(record.token)
        return False
    except Exception:
        await store.delete(record.token)
        raise


async def cleanup_expired_records(bot: Bot, store: VerificationStore, metrics=None) -> None:
    expired_records = await store.fetch_expired()
    if not expired_records:
        return
    logger.info("检测到 %d 条过期验证，执行封禁", len(expired_records))
    for record in expired_records:
        now = datetime.now(tz=UTC)
        updated = await store.mark_failed(record.token, now)
        if not updated:
            continue
        if metrics is not None:
            metrics.record_verification(result="expired")
        try:
            await store.record_verification_event(
                chat_id=record.chat_id,
                user_id=record.user_id,
                username=record.username,
                event="expired",
                created_at=now,
            )
        except Exception as exc:
            logger.warning(
                "记录过期事件失败 chat_id=%s user_id=%s error=%r",
                record.chat_id,
                record.user_id,
                exc,
                exc_info=True,
            )
        await delete_prompt_message(bot, record)
        await ban_and_cleanup(bot, store, record, reason="expired")


async def run_cleanup_scheduler(
    bot: Bot,
    store: VerificationStore,
    interval_seconds: int,
    metrics=None,
) -> None:
    try:
        while True:
            await cleanup_expired_records(bot, store, metrics=metrics)
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("清理任务异常", exc_info=exc)


# 同一用户进群事件串行化 + 提示重发抑制窗口。
# Telegram 对一次真实入群会同时推送 new_chat_members 服务消息和 chat_member 更新,
# 两个 handler 都会触发 process_new_member;没有抑制时用户会看到两条验证提示。
_JOIN_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}
_JOIN_LOCKS_GUARD = asyncio.Lock()
# 刚创建的 pending 记录在该窗口内不触发"重发提示",用于吞掉双事件;
# 窗口外的重进场景(用户退群后再次加入)仍会正常重发
_PROMPT_RESEND_SUPPRESS_SECONDS = 30


async def _acquire_join_lock(chat_id: int, user_id: int) -> asyncio.Lock:
    async with _JOIN_LOCKS_GUARD:
        lock = _JOIN_LOCKS.get((chat_id, user_id))
        if lock is None:
            lock = asyncio.Lock()
            _JOIN_LOCKS[(chat_id, user_id)] = lock
            # 兜底清理,防字典无限增长;只踢空闲锁,
            # 在用锁(可能正卡在网络 IO)若被踢掉会导致同一用户双 handler 并发
            if len(_JOIN_LOCKS) > 10000:
                for key in list(_JOIN_LOCKS)[:5000]:
                    candidate = _JOIN_LOCKS.get(key)
                    if candidate is not None and not candidate.locked():
                        _JOIN_LOCKS.pop(key, None)
        return lock


async def process_new_member(
    bot: Bot,
    store: VerificationStore,
    settings: Settings,
    *,
    chat_id: int,
    chat_title: Optional[str],
    member: User,
) -> None:
    # 串行化同一用户的进群处理,避免双事件并发时各自创建记录/发提示
    lock = await _acquire_join_lock(chat_id, member.id)
    async with lock:
        await _process_new_member_inner(
            bot,
            store,
            settings,
            chat_id=chat_id,
            chat_title=chat_title,
            member=member,
        )


async def _process_new_member_inner(
    bot: Bot,
    store: VerificationStore,
    settings: Settings,
    *,
    chat_id: int,
    chat_title: Optional[str],
    member: User,
) -> None:
    message_ttl = resolve_chat(settings, chat_id, "message_ttl_seconds")
    prompt_ttl = settings.verification_timeout_seconds
    existing = await store.get_pending(chat_id, member.id)
    if existing is not None:
        age_seconds = (datetime.now(tz=UTC) - existing.created_at).total_seconds()
        if age_seconds < _PROMPT_RESEND_SUPPRESS_SECONDS:
            logger.info(
                "待验证记录刚创建(%.1fs),判定为入群双事件重复触发,跳过重发提示 chat_id=%s user_id=%s token=%s",
                age_seconds,
                chat_id,
                member.id,
                existing.token,
            )
            return
        logger.info(
            "检测到已有待验证记录，重发提示 chat_id=%s user_id=%s token=%s",
            chat_id,
            member.id,
            existing.token,
        )
        await restrict_pending_member(bot, chat_id, member.id)
        prompt_message_id = await send_group_prompt(
            bot=bot,
            chat_id=chat_id,
            chat_title=chat_title,
            member=member,
            settings=settings,
            token=existing.token,
            ttl=prompt_ttl,
            store=store,
        )
        if prompt_message_id:
            existing.prompt_message_id = prompt_message_id
        return

    if await store.delete_if_exists(chat_id, member.id):
        logger.info("清理旧验证记录 chat_id=%s user_id=%s，重新创建", chat_id, member.id)

    token = secrets.token_urlsafe(32)
    now = datetime.now(tz=UTC)
    expire_at = now + timedelta(seconds=settings.verification_timeout_seconds)
    record = VerificationRecord(
        token=token,
        chat_id=chat_id,
        user_id=member.id,
        username=member.username,
        status="pending",
        created_at=now,
        expire_at=expire_at,
        verified_at=None,
        prompt_message_id=None,
    )
    await store.create(record)
    try:
        await store.record_verification_event(
            chat_id=chat_id,
            user_id=member.id,
            username=member.username,
            event="joined",
            created_at=now,
        )
    except Exception as exc:
        logger.warning(
            "记录入群事件失败 chat_id=%s user_id=%s error=%r",
            chat_id,
            member.id,
            exc,
            exc_info=True,
        )
    logger.info("创建验证记录 chat_id=%s user_id=%s token=%s", chat_id, member.id, token)
    await restrict_pending_member(bot, chat_id, member.id)
    prompt_message_id = await send_group_prompt(
        bot=bot,
        chat_id=chat_id,
        chat_title=chat_title,
        member=member,
        settings=settings,
        token=token,
        ttl=prompt_ttl,
        store=store,
    )
    if prompt_message_id:
        record.prompt_message_id = prompt_message_id


async def restrict_pending_member(bot: Bot, chat_id: int, user_id: int) -> None:
    permissions = ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=permissions,
            use_independent_chat_permissions=True,
        )
        logger.info("已临时限制新成员 chat_id=%s user_id=%s", chat_id, user_id)
    except TelegramBadRequest as exc:
        logger.warning(
            "限制新成员失败 chat_id=%s user_id=%s error=%s",
            chat_id,
            user_id,
            exc,
            exc_info=exc,
        )


async def send_group_prompt(
    *,
    bot: Bot,
    chat_id: int,
    chat_title: Optional[str],
    member: User,
    settings: Settings,
    token: str,
    ttl: Optional[int],
    store: VerificationStore,
) -> Optional[int]:
    timeout_seconds = settings.verification_timeout_seconds
    if timeout_seconds >= 60 and timeout_seconds % 60 == 0:
        minutes = timeout_seconds // 60
        timeout_display = f"{minutes}分钟⏱" if minutes > 1 else "60秒⏱"
    else:
        timeout_display = f"{timeout_seconds}秒⏱"

    display_name = escape(member.full_name)
    if chat_title:
        chat_prefix = f"欢迎来到 {escape(chat_title)}！"
    else:
        chat_prefix = "欢迎加入！"

    text = (
        f"{chat_prefix}\n欢迎👋<a href='tg://user?id={member.id}'>{display_name}</a> "
        f"请在 {timeout_display} 内点击下方按钮完成验证 🔐"
    )

    bot_username = getattr(settings, "bot_username", "").strip()
    if not bot_username:
        raise RuntimeError("未配置 TELEGRAM_BOT_USERNAME，无法生成验证链接。")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="验证 🚪", callback_data=f"verify:start:{token}")],
            [
                InlineKeyboardButton(
                    text="跳过验证 🔐 (管理员)", callback_data=f"admin:skip:{token}"
                ),
                InlineKeyboardButton(
                    text="临时封禁 1h ⏳ (管理员)", callback_data=f"admin:tempban:{token}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="永久封禁 🤐 (管理员)", callback_data=f"admin:ban:{token}"
                ),
            ],
        ]
    )

    try:
        message = await send_message_with_ttl(
            bot,
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
            ttl=ttl,
            store=store,
            token=token,
            delete_mode="record",
        )
        logger.info("已发送群内验证提示 chat_id=%s user_id=%s", chat_id, member.id)
        return message.message_id
    except TelegramBadRequest as exc:
        logger.error(
            "发送群内验证提示失败 chat_id=%s user_id=%s error=%s",
            chat_id,
            member.id,
            exc,
            exc_info=True,
        )
        return None


async def lift_restrictions(bot: Bot, record: VerificationRecord) -> bool:
    permissions = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
    )
    try:
        await bot.restrict_chat_member(
            chat_id=record.chat_id,
            user_id=record.user_id,
            permissions=permissions,
            use_independent_chat_permissions=True,
        )
        return True
    except TelegramBadRequest as exc:
        logger.warning("解除限制失败 chat_id=%s user_id=%s error=%s", record.chat_id, record.user_id, exc)
        return False
    except Exception as exc:
        # 网络/服务端异常同样视为失败:记录保持 pending,用户可重试(解禁幂等)
        logger.warning(
            "解除限制异常 chat_id=%s user_id=%s error=%r",
            record.chat_id, record.user_id, exc, exc_info=exc,
        )
        return False


async def notify_verification_success(bot: Bot, record: VerificationRecord) -> None:
    _ = (bot, record)


def _mention_html(user_id: int, username: Optional[str]) -> str:
    """有 username 用原生 @mention；否则退回 tg://user?id=（受对方隐私设置影响）。"""
    if username:
        return f"@{escape(username.lstrip('@'))}"
    return f'<a href="tg://user?id={user_id}">{user_id}</a>'


async def announce_group_success(
    bot: Bot,
    record: VerificationRecord,
    ttl: Optional[int],
) -> None:
    message = f"✅ {_mention_html(record.user_id, record.username)} 已完成验证，欢迎正式加入！"
    try:
        await send_message_with_ttl(
            bot,
            chat_id=record.chat_id,
            text=message,
            disable_web_page_preview=True,
            ttl=ttl,
        )
    except TelegramBadRequest as exc:
        logger.warning(
            "群内验证成功提示发送失败 chat_id=%s user_id=%s error=%s",
            record.chat_id,
            record.user_id,
            exc,
            exc_info=exc,
        )


async def delete_prompt_message(bot: Bot, record: VerificationRecord) -> None:
    if not record.prompt_message_id:
        return
    try:
        await bot.delete_message(record.chat_id, record.prompt_message_id)
    except TelegramBadRequest:
        pass
    record.prompt_message_id = None


__all__ = [
    "announce_group_success",
    "ban_and_cleanup",
    "cleanup_expired_records",
    "delete_prompt_message",
    "lift_restrictions",
    "notify_verification_success",
    "process_new_member",
    "restrict_pending_member",
    "run_cleanup_scheduler",
    "send_group_prompt",
]

