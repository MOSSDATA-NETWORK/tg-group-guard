from __future__ import annotations

from urllib.parse import quote_plus

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..bot_components.messaging import send_message_with_ttl, schedule_message_auto_delete
from ..bot_components.permissions import is_authorized_admin
from ..services.dependencies import BotServices


def build_basic_router(services: BotServices) -> Router:
    router = Router(name="basic")
    settings = services.settings
    store = services.store

    async def handle_private_verification_start(message: Message, token: str) -> None:
        private_ttl = None if message.chat.type == "private" else settings.message_ttl_seconds

        if message.from_user is None or message.from_user.is_bot:
            await send_message_with_ttl(
                message.bot,
                chat_id=message.chat.id,
                text="无法识别用户信息，请使用个人账号。",
                ttl=private_ttl,
            )
            return

        record = await store.get(token)
        if record is None:
            await send_message_with_ttl(
                message.bot,
                chat_id=message.chat.id,
                text="验证链接已失效或已完成，请重新在群内获取。",
                ttl=private_ttl,
            )
            return

        if record.user_id != message.from_user.id:
            await send_message_with_ttl(
                message.bot,
                chat_id=message.chat.id,
                text="该验证链接不属于你，请使用自己的链接进行验证。",
                ttl=private_ttl,
            )
            return

        verification_link = f"{settings.verify_base_url}/verify/{token}"
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="点击完成验证 🔐", url=verification_link)]
            ]
        )
        await send_message_with_ttl(
            message.bot,
            chat_id=message.chat.id,
            text="请点击下方按钮完成验证：",
            ttl=private_ttl,
            reply_markup=markup,
            disable_web_page_preview=True,
        )

    @router.message(CommandStart())
    async def handle_start(message: Message, bot: Bot) -> None:
        if message.chat.type in {"group", "supergroup"}:
            schedule_message_auto_delete(bot, message, settings.message_ttl_seconds)

        payload = None
        if message.text:
            parts = message.text.strip().split(maxsplit=1)
            if len(parts) == 2:
                payload = parts[1].strip()

        if payload and payload.startswith("verify_"):
            token = payload.removeprefix("verify_")
            await handle_private_verification_start(message, token)
            return

        if message.chat.type == "private":
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="请在群聊中点击验证按钮获取最新的验证链接。",
                ttl=None,
            )
            return

        if not await is_authorized_admin(bot, settings, message):
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text="⚠️ 此机器人仅限管理员使用。",
                ttl=settings.message_ttl_seconds,
            )
            return

        await send_message_with_ttl(
            bot,
            chat_id=message.chat.id,
            text="我是KK的群管小助手",
            ttl=settings.message_ttl_seconds,
        )

    return router


__all__ = ["build_basic_router"]
