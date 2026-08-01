from __future__ import annotations

import asyncio
import logging
from html import escape
from typing import Optional

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import Message

from ..bot_components.constants import ADMIN_STATUSES
from ..bot_components.messaging import send_message_with_ttl, schedule_message_auto_delete
from ..bot_components.permissions import (
    is_authorized_admin,
    resolve_target_member,
    resolve_target_user,
)
from ..bot_components.tgdc import lookup_chat_dc, lookup_user_dc
from ..chat_settings import resolve_chat
from ..services.dependencies import BotServices

logger = logging.getLogger(__name__)


def build_admin_commands_router(services: BotServices) -> Router:
    router = Router(name="admin_commands")
    settings = services.settings
    store = services.store
    score_manager = services.score_manager

    async def _record_ban_event_safe(
        *,
        chat_id: int,
        user_id: int,
        display_name: Optional[str],
        operator_id: Optional[int],
        operator_name: Optional[str],
        reason: str,
        action: str,
        currently_banned: Optional[bool] = None,
    ) -> None:
        try:
            await store.record_ban_event(
                chat_id=chat_id,
                user_id=user_id,
                display_name=display_name,
                operator_id=operator_id,
                operator_name=operator_name,
                reason=reason,
                action=action,
                currently_banned=currently_banned,
            )
        except Exception as exc:
            logger.warning(
                "记录封禁日志失败 chat_id=%s user_id=%s reason=%s action=%s error=%r",
                chat_id,
                user_id,
                reason,
                action,
                exc,
                exc_info=True,
            )

    @router.message(Command("id"))
    async def handle_id_command(message: Message, bot: Bot) -> None:
        schedule_message_auto_delete(bot, message, settings.message_ttl_seconds)
        chat = message.chat
        if chat.type not in {"group", "supergroup"}:
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="请在群聊中使用该命令。",
                ttl=settings.message_ttl_seconds,
            )
            return
        if not await is_authorized_admin(bot, settings, message):
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="⚠️ 仅管理员可以使用 /id 命令。",
                ttl=settings.message_ttl_seconds,
            )
            return

        target_user = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user = message.reply_to_message.from_user
        else:
            target_user = message.from_user

        if target_user is None:
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="无法识别目标用户信息。",
                ttl=settings.message_ttl_seconds,
            )
            return

        score = await score_manager.get_score(chat.id, target_user.id)
        valid_count, is_qualified = await store.get_ad_qualification(chat.id, target_user.id)
        user_dc_task = asyncio.create_task(lookup_user_dc(bot, target_user.id))
        chat_dc_task = asyncio.create_task(lookup_chat_dc(bot, chat))
        warning_task = asyncio.create_task(store.count_warnings_in_month(chat.id, target_user.id))
        user_dc_result, chat_dc_result, warning_count = await asyncio.gather(
            user_dc_task, chat_dc_task, warning_task
        )
        qualify_label = (
            f"已合格（{valid_count}/{settings.ad_guard_score_skip_threshold}）"
            if is_qualified
            else f"{valid_count}/{settings.ad_guard_score_skip_threshold}"
        )

        lines = [
            "📊 当前信息：",
            f"用户昵称：{escape(target_user.full_name or target_user.username or str(target_user.id))}",
            f"用户 ID：{target_user.id}",
            f"群组 ID：{chat.id}",
            f"广告合格进度：{qualify_label}",
            f"广告扣分：{score}",
            f"本月警告次数：{warning_count}",
            f"用户 DC：{user_dc_result.describe()}",
            f"群组 DC：{chat_dc_result.describe()}",
        ]
        await send_message_with_ttl(
            bot,
            chat_id=message.chat.id,
            text="\n".join(lines),
            ttl=settings.message_ttl_seconds,
            disable_web_page_preview=True,
        )

    @router.message(Command("re"))
    async def handle_relay_command(message: Message, bot: Bot) -> None:
        schedule_message_auto_delete(bot, message, settings.message_ttl_seconds)
        chat = message.chat
        if chat.type not in {"group", "supergroup"}:
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text="⚠️ 请在群聊中使用 /re 命令。",
                ttl=settings.message_ttl_seconds,
            )
            return
        if not await is_authorized_admin(bot, settings, message):
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text="⚠️ 仅管理员可以使用 /re 命令。",
                ttl=settings.message_ttl_seconds,
            )
            return
        if message.reply_to_message is None:
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text="⚠️ 请先回复需要转发的消息后再使用 /re。",
                ttl=settings.message_ttl_seconds,
            )
            return
        target_message = message.reply_to_message
        if target_message.message_id is None:
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text="⚠️ 无法识别要转发的消息。",
                ttl=settings.message_ttl_seconds,
            )
            return
        source_chat = getattr(target_message, "chat", None)
        source_chat_id = source_chat.id if source_chat else chat.id
        forward_kwargs = {
            "chat_id": chat.id,
            "from_chat_id": source_chat_id,
            "message_id": target_message.message_id,
        }
        thread_id = getattr(message, "message_thread_id", None)
        if thread_id is not None and getattr(chat, "is_forum", False):
            forward_kwargs["message_thread_id"] = thread_id
        try:
            await bot.forward_message(**forward_kwargs)
            try:
                await bot.delete_message(chat.id, message.message_id)
            except TelegramBadRequest as exc:
                logger.debug(
                    "/re 删除指令消息失败 chat_id=%s message_id=%s error=%s",
                    chat.id,
                    message.message_id,
                    exc,
                )
        except TelegramBadRequest as exc:
            logger.warning(
                "/re 转发失败 chat_id=%s msg_id=%s error=%s",
                chat.id,
                target_message.message_id,
                exc,
                exc_info=exc,
            )
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text=f"⚠️ 转发失败：{escape(str(exc))}",
                ttl=settings.message_ttl_seconds,
            )

    @router.message(Command("warn"))
    async def handle_warn_command(message: Message, bot: Bot) -> None:
        schedule_message_auto_delete(bot, message, settings.message_ttl_seconds)
        chat = message.chat
        issuer = message.from_user
        if issuer is None:
            return
        if chat.type not in {"group", "supergroup"}:
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text="⚠️ 请在群聊中使用 /warn 命令。",
                ttl=settings.message_ttl_seconds,
            )
            return
        if not await is_authorized_admin(bot, settings, message):
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text="⚠️ 仅管理员可以使用 /warn 命令。",
                ttl=settings.message_ttl_seconds,
            )
            return
        if message.reply_to_message is None or message.reply_to_message.from_user is None:
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text="⚠️ 请回复需要警告的成员消息后再发送指令。",
                ttl=settings.message_ttl_seconds,
            )
            return

        target_user = message.reply_to_message.from_user
        try:
            target_member = await bot.get_chat_member(chat.id, target_user.id)
        except TelegramBadRequest as exc:
            logger.warning(
                "/warn 获取目标成员失败 chat_id=%s target_id=%s error=%s",
                chat.id,
                target_user.id,
                exc,
                exc_info=exc,
            )
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text="⚠️ 无法获取目标成员信息，请稍后再试。",
                ttl=settings.message_ttl_seconds,
            )
            return

        if target_member.status in ADMIN_STATUSES:
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text="⚠️ 管理员不可被警告。",
                ttl=settings.message_ttl_seconds,
            )
            return

        reason_text = ""
        if message.text:
            parts = message.text.strip().split(maxsplit=1)
            if len(parts) == 2:
                reason_text = parts[1].strip()

        warn_limit = resolve_chat(settings, chat.id, "warn_limit")
        warning_count = await store.add_warning(
            chat_id=chat.id,
            user_id=target_user.id,
            issued_by=issuer.id,
            reason=reason_text or None,
        )

        display_name = escape(target_user.full_name or target_user.username or str(target_user.id))
        notice_lines = [
            f"⚠️ 已警告 <a href='tg://user?id={target_user.id}'>{display_name}</a>。",
            f"本月累计：{warning_count}/{warn_limit} 次。",
        ]
        if reason_text:
            notice_lines.append(f"原因：{escape(reason_text)}")

        exceeded = warning_count >= warn_limit
        if exceeded:
            try:
                await bot.ban_chat_member(chat.id, target_user.id)
                await score_manager.reset_score(chat.id, target_user.id)
                await store.reset_ad_qualification(chat.id, target_user.id)
                await _record_ban_event_safe(
                    chat_id=chat.id,
                    user_id=target_user.id,
                    display_name=target_user.full_name
                    or target_user.username
                    or str(target_user.id),
                    operator_id=issuer.id,
                    operator_name=issuer.full_name or issuer.username or str(issuer.id),
                    reason="warn_limit",
                    action="ban",
                    currently_banned=True,
                )
                notice_lines.append("🚫 已达到警告上限，自动封禁并移出本群。")
            except TelegramBadRequest as exc:
                logger.warning(
                    "/warn 自动封禁失败 chat_id=%s target_id=%s error=%s",
                    chat.id,
                    target_user.id,
                    exc,
                    exc_info=exc,
                )
                notice_lines.append(f"⚠️ 达到上限但封禁失败：{escape(str(exc))}")

        await send_message_with_ttl(
            bot,
            chat_id=chat.id,
            text="\n".join(notice_lines),
            ttl=settings.message_ttl_seconds,
            disable_web_page_preview=True,
        )

    @router.message(Command("sb"))
    async def handle_sb_command(message: Message, bot: Bot) -> None:
        schedule_message_auto_delete(bot, message, settings.message_ttl_seconds)
        if message.reply_to_message is None:
            return
        if not await is_authorized_admin(bot, settings, message):
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="⚠️ 仅管理员可以使用 /sb 命令。",
                ttl=settings.message_ttl_seconds,
            )
            return

        target_message = message.reply_to_message
        target_user = target_message.from_user
        if target_user is None:
            return
        try:
            target_member = await bot.get_chat_member(message.chat.id, target_user.id)
        except TelegramBadRequest as exc:
            logger.warning(
                "获取被封禁成员信息失败 chat_id=%s target_id=%s error=%s",
                message.chat.id,
                target_user.id,
                exc,
                exc_info=exc,
            )
            return

        if target_member.status in ADMIN_STATUSES:
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="⚠️ 不能封禁管理员。",
                ttl=settings.message_ttl_seconds,
            )
            return

        if target_user.id == message.from_user.id:
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="⚠️ 不能封禁自己。",
                ttl=settings.message_ttl_seconds,
            )
            return

        try:
            await bot.delete_message(message.chat.id, target_message.message_id)
        except TelegramBadRequest as exc:
            logger.warning(
                "删除指定消息失败 chat_id=%s msg_id=%s error=%s",
                message.chat.id,
                target_message.message_id,
                exc,
                exc_info=exc,
            )
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="⚠️ 无法删除该消息，可能已经被删除。",
                ttl=settings.message_ttl_seconds,
            )
            return

        try:
            await bot.ban_chat_member(message.chat.id, target_user.id)
            operator = message.from_user
            await _record_ban_event_safe(
                chat_id=message.chat.id,
                user_id=target_user.id,
                display_name=target_user.full_name
                or target_user.username
                or str(target_user.id),
                operator_id=operator.id if operator else None,
                operator_name=(operator.full_name or operator.username or str(operator.id))
                if operator
                else None,
                reason="admin_cmd",
                action="ban",
                currently_banned=True,
            )
            notice = (
                f"🚫 <a href='tg://user?id={target_user.id}'>"
                f"{escape(target_user.full_name or str(target_user.id))}</a> 已被封禁。"
            )
        except TelegramBadRequest as exc:
            logger.warning(
                "/sb 封禁失败 chat_id=%s target_id=%s error=%s",
                message.chat.id,
                target_user.id,
                exc,
                exc_info=exc,
            )
            notice = f"⚠️ 封禁失败：{escape(str(exc))}"
        await send_message_with_ttl(
            bot,
            chat_id=message.chat.id,
            text=notice,
            ttl=settings.message_ttl_seconds,
            disable_web_page_preview=True,
        )

    @router.message(Command("ban"))
    async def handle_ban_command(message: Message, bot: Bot) -> None:
        schedule_message_auto_delete(bot, message, settings.message_ttl_seconds)
        if message.chat.type not in {"group", "supergroup"}:
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="请在群聊中使用该命令。",
                ttl=settings.message_ttl_seconds,
            )
            return
        if not await is_authorized_admin(bot, settings, message):
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="⚠️ 仅管理员可以使用 /ban 命令。",
                ttl=settings.message_ttl_seconds,
            )
            return

        target_member = await resolve_target_member(message, bot)
        if target_member is None:
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="⚠️ 请回复目标消息，或提供用户 ID（指令不支持 @用户名）。",
                ttl=settings.message_ttl_seconds,
            )
            return

        target_user = target_member.user
        if target_user is None:
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="⚠️ 无法识别目标用户。",
                ttl=settings.message_ttl_seconds,
            )
            return

        if target_user.id == message.from_user.id:
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="⚠️ 不能封禁自己。",
                ttl=settings.message_ttl_seconds,
            )
            return

        if target_member.status in ADMIN_STATUSES:
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="⚠️ 不能封禁管理员。",
                ttl=settings.message_ttl_seconds,
            )
            return

        try:
            await bot.ban_chat_member(message.chat.id, target_user.id)
            await score_manager.reset_score(message.chat.id, target_user.id)
            await store.reset_ad_qualification(message.chat.id, target_user.id)
            operator = message.from_user
            await _record_ban_event_safe(
                chat_id=message.chat.id,
                user_id=target_user.id,
                display_name=target_user.full_name
                or target_user.username
                or str(target_user.id),
                operator_id=operator.id if operator else None,
                operator_name=(operator.full_name or operator.username or str(operator.id))
                if operator
                else None,
                reason="admin_cmd",
                action="ban",
                currently_banned=True,
            )
        except TelegramBadRequest as exc:
            logger.warning(
                "/ban 封禁失败 chat_id=%s target_id=%s error=%s",
                message.chat.id,
                target_user.id,
                exc,
                exc_info=exc,
            )
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text=f"⚠️ 封禁失败：{escape(str(exc))}",
                ttl=settings.message_ttl_seconds,
            )
            return

        display_name = escape(target_user.full_name or str(target_user.id))
        await send_message_with_ttl(
            bot,
            chat_id=message.chat.id,
            text=f"🚫 已封禁 <a href='tg://user?id={target_user.id}'>{display_name}</a>。",
            ttl=settings.message_ttl_seconds,
            disable_web_page_preview=True,
        )

    @router.message(Command("unban"))
    async def handle_unban_command(message: Message, bot: Bot) -> None:
        schedule_message_auto_delete(bot, message, settings.message_ttl_seconds)
        if message.chat.type not in {"group", "supergroup"}:
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="请在群聊中使用该命令。",
                ttl=settings.message_ttl_seconds,
            )
            return
        if not await is_authorized_admin(bot, settings, message):
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="⚠️ 仅管理员可以使用 /unban 命令。",
                ttl=settings.message_ttl_seconds,
            )
            return

        target_member = await resolve_target_member(message, bot)
        target_user_id: Optional[int] = None
        target_display: Optional[str] = None

        if target_member is not None and target_member.user is not None:
            target_user_id = target_member.user.id
            target_display = target_member.user.full_name or str(target_user_id)
        else:
            target = await resolve_target_user(message, bot)
            if target is None:
                await send_message_with_ttl(
                    bot,
                    chat_id=message.chat.id,
                    text="⚠️ 请回复目标消息，或提供用户 ID（指令不支持 @用户名）。",
                    ttl=settings.message_ttl_seconds,
                )
                return
            target_user_id, target_display = target

        try:
            await bot.unban_chat_member(message.chat.id, target_user_id, only_if_banned=False)
        except TelegramBadRequest as exc:
            logger.warning(
                "/unban 解封失败 chat_id=%s target_id=%s error=%s",
                message.chat.id,
                target_user_id,
                exc,
                exc_info=exc,
            )
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text=f"⚠️ 解封失败：{escape(str(exc))}",
                ttl=settings.message_ttl_seconds,
            )
            return

        display_name = escape(target_display or str(target_user_id))
        operator = message.from_user
        await _record_ban_event_safe(
            chat_id=message.chat.id,
            user_id=target_user_id,
            display_name=target_display or str(target_user_id),
            operator_id=operator.id if operator else None,
            operator_name=(operator.full_name or operator.username or str(operator.id))
            if operator
            else None,
            reason="admin_cmd",
            action="unban",
            currently_banned=False,
        )
        await send_message_with_ttl(
            bot,
            chat_id=message.chat.id,
            text=f"✅ 已解封 <a href='tg://user?id={target_user_id}'>{display_name}</a>。",
            ttl=settings.message_ttl_seconds,
            disable_web_page_preview=True,
        )

    @router.message(Command("up"))
    async def handle_up_command(message: Message, bot: Bot) -> None:
        schedule_message_auto_delete(bot, message, settings.message_ttl_seconds)
        chat = message.chat
        if chat.type not in {"group", "supergroup"}:
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text="⚠️ 请在群聊中使用 /up 命令。",
                ttl=settings.message_ttl_seconds,
            )
            return

        if message.from_user is None:
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text="⚠️ 无法识别指令发起人。",
                ttl=settings.message_ttl_seconds,
            )
            return

        try:
            issuer_member = await bot.get_chat_member(chat.id, message.from_user.id)
        except TelegramBadRequest as exc:
            logger.warning(
                "/up 查询执行者身份失败 chat_id=%s user_id=%s error=%s",
                chat.id,
                message.from_user.id,
                exc,
                exc_info=exc,
            )
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text=f"⚠️ 查询执行者身份失败：{escape(str(exc))}",
                ttl=settings.message_ttl_seconds,
            )
            return

        if issuer_member.status != "creator":
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text="⚠️ 仅群主可以使用 /up 命令。",
                ttl=settings.message_ttl_seconds,
            )
            return

        args_text = ""
        if message.text:
            parts = message.text.strip().split(maxsplit=1)
            if len(parts) == 2:
                args_text = parts[1].strip()

        target_member = None
        title: Optional[str] = None

        if message.reply_to_message and message.reply_to_message.from_user:
            title = args_text
            try:
                target_member = await bot.get_chat_member(
                    chat.id, message.reply_to_message.from_user.id
                )
            except TelegramBadRequest as exc:
                logger.warning(
                    "/up 获取目标成员失败 chat_id=%s target_id=%s error=%s",
                    chat.id,
                    message.reply_to_message.from_user.id,
                    exc,
                    exc_info=exc,
                )
                await send_message_with_ttl(
                    bot,
                    chat_id=chat.id,
                    text=f"⚠️ 无法获取目标成员信息：{escape(str(exc))}",
                    ttl=settings.message_ttl_seconds,
                )
                return
        else:
            if not args_text:
                await send_message_with_ttl(
                    bot,
                    chat_id=chat.id,
                    text="⚠️ 请回复目标消息，或提供用户 ID 与头衔，例如 /up 123456789 传奇管理员。",
                    ttl=settings.message_ttl_seconds,
                )
                return
            id_and_title = args_text.split(maxsplit=1)
            if len(id_and_title) < 2:
                await send_message_with_ttl(
                    bot,
                    chat_id=chat.id,
                    text="⚠️ 请同时提供用户 ID 和管理员头衔，例如 /up 123456789 传奇管理员。",
                    ttl=settings.message_ttl_seconds,
                )
                return
            user_id_text, title = id_and_title
            try:
                target_id = int(user_id_text)
            except ValueError:
                await send_message_with_ttl(
                    bot,
                    chat_id=chat.id,
                    text="⚠️ 用户 ID 必须为数字。",
                    ttl=settings.message_ttl_seconds,
                )
                return
            try:
                target_member = await bot.get_chat_member(chat.id, target_id)
            except TelegramBadRequest as exc:
                logger.warning(
                    "/up 获取目标成员失败 chat_id=%s target_id=%s error=%s",
                    chat.id,
                    target_id,
                    exc,
                    exc_info=exc,
                )
                await send_message_with_ttl(
                    bot,
                    chat_id=chat.id,
                    text=f"⚠️ 无法获取目标成员信息：{escape(str(exc))}",
                    ttl=settings.message_ttl_seconds,
                )
                return

        if target_member is None or target_member.user is None:
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text="⚠️ 无法识别目标用户。",
                ttl=settings.message_ttl_seconds,
            )
            return

        title = title.strip() if title else ""
        if not title:
            title = "管理员"
        if len(title) > 16:
            await send_message_with_ttl(
                bot,
                chat_id=chat.id,
                text="⚠️ 管理员头衔不能超过 16 个字符。",
                ttl=settings.message_ttl_seconds,
            )
            return

        target_user = target_member.user

        needs_promotion = target_member.status not in ADMIN_STATUSES
        if needs_promotion:
            try:
                await bot.promote_chat_member(
                    chat_id=chat.id,
                    user_id=target_user.id,
                    can_manage_chat=False,
                    can_delete_messages=True,
                    can_change_info=False,
                    can_invite_users=False,
                    can_restrict_members=True,
                    can_pin_messages=False,
                    can_promote_members=False,
                    can_manage_video_chats=False,
                    can_edit_messages=False,
                    can_post_messages=False,
                    can_manage_topics=False,
                    is_anonymous=False,
                )
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                logger.warning(
                    "/up 提升管理员失败 chat_id=%s target_id=%s error=%s",
                    chat.id,
                    target_user.id,
                    exc,
                    exc_info=exc,
                )
                await send_message_with_ttl(
                    bot,
                    chat_id=chat.id,
                    text="⚠️ 提升管理员失败，请确认机器人拥有管理员权限并允许管理成员。",
                    ttl=settings.message_ttl_seconds,
                )
                return

        if chat.type == "supergroup":
            try:
                await bot.set_chat_administrator_custom_title(chat.id, target_user.id, title)
            except TelegramBadRequest as exc:
                logger.warning(
                    "/up 设置管理员头衔失败 chat_id=%s target_id=%s error=%s",
                    chat.id,
                    target_user.id,
                    exc,
                    exc_info=exc,
                )
                await send_message_with_ttl(
                    bot,
                    chat_id=chat.id,
                    text=f"⚠️ 已授予管理员权限，但设置头衔失败：{escape(str(exc))}",
                    ttl=settings.message_ttl_seconds,
                )
                return

        display_name = escape(
            target_user.full_name or target_user.username or str(target_user.id)
        )
        if needs_promotion:
            result_text = (
                f"✅ 已将 <a href='tg://user?id={target_user.id}'>{display_name}</a> "
                f"提升为管理员，头衔为「{escape(title)}」。"
            )
        else:
            result_text = (
                f"✅ 已更新 <a href='tg://user?id={target_user.id}'>{display_name}</a> "
                f"的管理员头衔为「{escape(title)}」。"
            )
        await send_message_with_ttl(
            bot,
            chat_id=chat.id,
            text=result_text,
            ttl=settings.message_ttl_seconds,
            disable_web_page_preview=True,
        )

    return router


__all__ = ["build_admin_commands_router"]
