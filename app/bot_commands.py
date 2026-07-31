from __future__ import annotations

import logging
from typing import List

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeDefault

logger = logging.getLogger(__name__)

# 全局命令列表（所有聊天中输入 / 时可见）
# 实际权限在 handler 中校验，这里仅做命令描述同步
_BOT_COMMANDS: List[BotCommand] = [
    BotCommand(command="start", description="开始 / 验证入口"),
    BotCommand(command="id", description="查询用户信息（管理员）"),
    BotCommand(command="warn", description="警告成员，回复消息 + 可选原因（管理员）"),
    BotCommand(command="ban", description="封禁成员，回复消息或提供用户ID（管理员）"),
    BotCommand(command="unban", description="解封成员，回复消息或提供用户ID（管理员）"),
    BotCommand(command="sb", description="删除消息并封禁，回复消息（管理员）"),
    BotCommand(command="re", description="转发消息到当前群，回复消息（管理员）"),
    BotCommand(command="up", description="提升管理员/设头衔，仅群主可用"),
]


async def sync_bot_commands(bot: Bot) -> None:
    """将命令列表同步到 Telegram BotFather，使用户在输入 / 时能看到命令菜单。"""
    try:
        await bot.set_my_commands(
            commands=_BOT_COMMANDS,
            scope=BotCommandScopeDefault(),
        )
        logger.info("已同步 %s 条 Bot 命令到 Telegram", len(_BOT_COMMANDS))
    except Exception as exc:
        logger.warning("同步 Bot 命令失败: %s", exc)


__all__ = ["sync_bot_commands", "_BOT_COMMANDS"]
