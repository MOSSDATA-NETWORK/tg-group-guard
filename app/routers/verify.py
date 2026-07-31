from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from urllib.parse import quote_plus

from ..bot_components.verification import process_new_member
from ..services.dependencies import BotServices

logger = logging.getLogger(__name__)


def build_verify_router(services: BotServices) -> Router:
    router = Router(name="verify")
    settings = services.settings
    store = services.store

    @router.message(lambda message: bool(message.new_chat_members))
    async def handle_new_members(message: Message, bot: Bot) -> None:
        chat_id = message.chat.id
        if settings.allowed_chat_ids and chat_id not in settings.allowed_chat_ids:
            logger.debug("忽略非授权群的新成员事件 chat_id=%s", chat_id)
            return
        for member in message.new_chat_members:
            if member.is_bot:
                continue
            logger.info("检测到新成员加入 chat_id=%s user_id=%s", chat_id, member.id)
            await process_new_member(
                bot=bot,
                store=store,
                settings=settings,
                chat_id=chat_id,
                chat_title=message.chat.title,
                member=member,
            )

    @router.chat_member()
    async def handle_member_update(event: ChatMemberUpdated, bot: Bot) -> None:
        user = event.new_chat_member.user
        if user.is_bot:
            return
        chat_id = event.chat.id
        if settings.allowed_chat_ids:
            if chat_id not in settings.allowed_chat_ids:
                logger.debug("忽略非授权群的成员更新 chat_id=%s", chat_id)
                return
        old_status = event.old_chat_member.status
        new_status = event.new_chat_member.status
        if old_status in {"left", "kicked"} and new_status in {"member", "restricted"}:
            logger.info(
                "chat_member 事件捕获新成员 chat_id=%s user_id=%s old=%s new=%s",
                chat_id,
                user.id,
                old_status,
                new_status,
            )
            await process_new_member(
                bot=bot,
                store=store,
                settings=settings,
                chat_id=chat_id,
                chat_title=getattr(event.chat, "title", None),
                member=user,
            )

    @router.callback_query(lambda call: call.data and call.data.startswith("verify:"))
    async def handle_verify_callback(callback: CallbackQuery) -> None:
        if callback.message is None:
            await callback.answer()
            return

        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("无效的指令", show_alert=True)
            return

        _, _action, token = parts
        token = token.strip()
        record = await store.get(token)
        if record is None:
            await callback.answer("验证链接已失效，请重新请求。", show_alert=True)
            return

        if callback.from_user.id != record.user_id:
            await callback.answer("仅待验证成员可使用此按钮。", show_alert=True)
            return

        bot_username = getattr(settings, "bot_username", "").strip()
        if not bot_username:
            logger.error("缺少 TELEGRAM_BOT_USERNAME 配置，无法生成深度链接。")
            await callback.answer("配置缺失，请联系管理员。", show_alert=True)
            return

        deep_link_token = quote_plus(token)
        start_url = f"https://t.me/{bot_username}?start=verify_{deep_link_token}"
        await callback.answer(
            "正在跳转到机器人，请点击 Start 后继续验证。", url=start_url
        )
        logger.info(
            "待验证成员点击按钮获取链接 chat_id=%s user_id=%s token=%s",
            record.chat_id,
            record.user_id,
            token,
        )

    @router.callback_query(lambda call: call.data and call.data.startswith("admin:"))
    async def handle_admin_actions(callback: CallbackQuery, bot: Bot) -> None:
        if callback.message is None:
            await callback.answer()
            return

        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("无效的指令", show_alert=True)
            return

        _, action, token = parts
        token = token.strip()
        record = await store.get(token)
        if record is None:
            await callback.answer("链接已失效", show_alert=True)
            return
        if record.status != "pending":
            await callback.answer("该验证已处理", show_alert=True)
            return

        try:
            member = await bot.get_chat_member(record.chat_id, callback.from_user.id)
        except TelegramBadRequest as exc:
            logger.warning(
                "无法获取操作者身份 chat_id=%s operator_id=%s error=%s",
                record.chat_id,
                callback.from_user.id,
                exc,
                exc_info=exc,
            )
            await callback.answer("无法验证权限，请稍后再试", show_alert=True)
            return

        from ..bot_components.constants import ADMIN_STATUSES

        if member.status not in ADMIN_STATUSES:
            await callback.answer("仅管理员可操作", show_alert=True)
            return

        if action == "skip":
            from ..bot_components.verification import (
                delete_prompt_message,
                lift_restrictions,
            )

            await lift_restrictions(bot, record)
            await delete_prompt_message(bot, record)
            try:
                await store.record_verification_event(
                    chat_id=record.chat_id,
                    user_id=record.user_id,
                    username=record.username,
                    event="admin_skip",
                )
            except Exception as exc:
                logger.warning(
                    "记录管理员跳过事件失败 chat_id=%s user_id=%s error=%r",
                    record.chat_id,
                    record.user_id,
                    exc,
                    exc_info=True,
                )
            await store.delete(token)
            await callback.answer("已跳过验证并解除限制")
        elif action == "tempban":
            from ..bot_components.verification import delete_prompt_message

            until_date = datetime.now(tz=timezone.utc) + timedelta(hours=1)
            await delete_prompt_message(bot, record)
            try:
                await bot.ban_chat_member(
                    record.chat_id,
                    record.user_id,
                    until_date=until_date,
                    revoke_messages=True,
                )
            except TelegramBadRequest as exc:
                logger.warning(
                    "临时封禁失败 chat_id=%s user_id=%s operator=%s error=%s",
                    record.chat_id,
                    record.user_id,
                    callback.from_user.id,
                    exc,
                    exc_info=exc,
                )
                await callback.answer("临时封禁失败，请查看日志", show_alert=True)
                return
            try:
                await store.record_verification_event(
                    chat_id=record.chat_id,
                    user_id=record.user_id,
                    username=record.username,
                    event="admin_tempban",
                )
                await store.record_ban_event(
                    chat_id=record.chat_id,
                    user_id=record.user_id,
                    display_name=record.username or str(record.user_id),
                    operator_id=callback.from_user.id,
                    operator_name=callback.from_user.full_name
                    or callback.from_user.username
                    or str(callback.from_user.id),
                    reason="verify_admin_tempban_1h",
                    action="ban",
                    currently_banned=True,
                )
            except Exception as exc:
                logger.warning(
                    "记录临时封禁事件失败 chat_id=%s user_id=%s error=%r",
                    record.chat_id,
                    record.user_id,
                    exc,
                    exc_info=True,
                )
            await store.delete(token)
            await callback.answer("已临时封禁 1 小时")
        elif action == "ban":
            from ..bot_components.verification import (
                ban_and_cleanup,
                delete_prompt_message,
            )

            await delete_prompt_message(bot, record)
            await ban_and_cleanup(
                bot,
                store,
                record,
                reason=f"admin:{callback.from_user.id}",
                unban_after=False,
            )
            try:
                await store.record_verification_event(
                    chat_id=record.chat_id,
                    user_id=record.user_id,
                    username=record.username,
                    event="admin_ban",
                )
                await store.record_ban_event(
                    chat_id=record.chat_id,
                    user_id=record.user_id,
                    display_name=record.username or str(record.user_id),
                    operator_id=callback.from_user.id,
                    operator_name=callback.from_user.full_name
                    or callback.from_user.username
                    or str(callback.from_user.id),
                    reason="verify_admin_ban",
                    action="ban",
                    currently_banned=True,
                )
            except Exception as exc:
                logger.warning(
                    "记录管理员封禁事件失败 chat_id=%s user_id=%s error=%r",
                    record.chat_id,
                    record.user_id,
                    exc,
                    exc_info=True,
                )
            await callback.answer("已封禁该用户")
        else:
            await callback.answer("未知操作", show_alert=True)

    return router


__all__ = ["build_verify_router"]
