from __future__ import annotations

import asyncio
import logging
import uuid
from html import escape
from typing import Optional, Tuple

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Poll,
)

from ..ad_guard_rules import render_prompt_message
from ..bot_components.ad_guard import (
    check_advertisement,
    heuristic_detect_advertisement,
)
from ..bot_components.constants import ADMIN_STATUSES
from ..bot_components.history import (
    build_history_entry,
    format_context_for_prompt,
)
from ..bot_components.messaging import (
    send_message_with_ttl,
    truncate_for_logging,
)
from ..bot_components.moderation import handle_low_score_violation
from ..bot_components.permissions import is_authorized_admin
from ..services.ad_pipeline import AdPipeline
from ..services.dependencies import BotServices
from ..state.ad_review import AdReviewContext
from ..state.ad_vote import AdVoteContext

logger = logging.getLogger(__name__)


_PROVIDER_LABEL = {
    "ollama": "本地 Ollama",
    "openai": "OpenAI 兼容大模型",
}


def _provider_label(provider: str) -> str:
    return _PROVIDER_LABEL.get(provider, provider or "AI")


def build_ad_guard_router(services: BotServices, pipeline: AdPipeline) -> Router:
    router = Router(name="ad_guard")
    settings = services.settings
    store = services.store
    score_manager = services.score_manager
    history_store = services.history_store
    ad_review_store = services.ad_review_store
    ad_vote_store = services.ad_vote_store

    async def _record_ad_decision_safe(
        message: Message,
        text: str,
        *,
        source: str,
        flagged: bool,
        confidence: Optional[float],
        final_action: str,
        vote_used: bool = False,
        vote_adv: Optional[int] = None,
        vote_normal: Optional[int] = None,
    ) -> Optional[int]:
        if message.from_user is None:
            return None
        try:
            return await store.record_ad_decision(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                display_name=message.from_user.full_name
                or message.from_user.username
                or str(message.from_user.id),
                username=message.from_user.username,
                message_text=text,
                source=source,
                flagged=flagged,
                confidence=confidence,
                vote_used=vote_used,
                vote_adv=vote_adv,
                vote_normal=vote_normal,
                final_action=final_action,
            )
        except Exception as exc:
            logger.warning(
                "记录广告判断日志失败 chat_id=%s user_id=%s error=%r",
                message.chat.id,
                message.from_user.id,
                exc,
                exc_info=True,
            )
            return None

    async def _record_ban_event_safe(
        *,
        chat_id: int,
        user_id: int,
        display_name: Optional[str],
        operator_id: Optional[int],
        operator_name: Optional[str],
        reason: str,
        action: str = "ban",
        currently_banned: Optional[bool] = None,
    ) -> Optional[int]:
        try:
            return await store.record_ban_event(
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
                "记录封禁日志失败 chat_id=%s user_id=%s reason=%s error=%r",
                chat_id,
                user_id,
                reason,
                exc,
                exc_info=True,
            )
            return None

    async def _conduct_ad_vote(
        bot: Bot,
        message: Message,
        *,
        offender_id: int,
        offender_display_html: str,
        original_message_id: int,
        duration: int,
    ) -> Tuple[bool, int, int, bool, bool]:
        duration = max(duration, 1)
        question = f"⚖️ 检测到疑似广告，请在 {duration} 秒内投票"
        options = ["广告", "不是广告"]
        poll_kwargs: dict = {}
        thread_id = getattr(message, "message_thread_id", None)
        if thread_id is not None and getattr(message.chat, "is_forum", False):
            poll_kwargs["message_thread_id"] = thread_id

        vote_id = uuid.uuid4().hex
        vote_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="封禁并踢出 🚫",
                        callback_data=f"advote:ban:{vote_id}",
                    )
                ]
            ]
        )

        try:
            poll_message = await bot.send_poll(
                chat_id=message.chat.id,
                question=question,
                options=options,
                is_anonymous=False,
                allows_multiple_answers=False,
                reply_to_message_id=message.message_id,
                reply_markup=vote_keyboard,
                **poll_kwargs,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning(
                "发起广告投票失败 chat_id=%s msg_id=%s error=%s",
                message.chat.id,
                message.message_id,
                exc,
            )
            return (True, 0, 0, False, False)

        context = AdVoteContext(
            chat_id=message.chat.id,
            offender_id=offender_id,
            offender_display_html=offender_display_html,
            offender_message_id=original_message_id,
            poll_message_id=poll_message.message_id,
        )
        await ad_vote_store.put(vote_id, context)

        try:
            await asyncio.wait_for(context.event.wait(), timeout=duration)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            await ad_vote_store.pop(vote_id)
            raise

        forced_action = context.force_ban
        poll_result = context.poll_result

        if not forced_action:
            try:
                poll_result = await bot.stop_poll(message.chat.id, poll_message.message_id)
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                logger.warning(
                    "停止广告投票失败 chat_id=%s poll_msg_id=%s error=%s",
                    message.chat.id,
                    poll_message.message_id,
                    exc,
                )

        if poll_result is None:
            poll_result = getattr(poll_message, "poll", None)

        adv_votes = 0
        normal_votes = 0
        if poll_result and poll_result.options:
            adv_votes = poll_result.options[0].voter_count
            if len(poll_result.options) > 1:
                normal_votes = poll_result.options[1].voter_count

        # 投票结果判定：
        # - 管理员强制封禁：直接维持
        # - 无人参与（0:0）：不视为放行，按模型原判（flagged=True）保留判定，避免深夜/小群被 0 票漂白
        # - 广告票严格多于非广告票：维持
        # - 其他（含平票，但至少有一方投票）：放行
        if forced_action:
            final_flagged = True
        elif adv_votes == 0 and normal_votes == 0:
            final_flagged = True
        elif adv_votes > normal_votes:
            final_flagged = True
        else:
            final_flagged = False

        logger.info(
            "广告投票完成 chat_id=%s msg_id=%s adv_votes=%s normal_votes=%s final=%s forced=%s",
            message.chat.id,
            message.message_id,
            adv_votes,
            normal_votes,
            final_flagged,
            forced_action,
        )

        if not forced_action:
            try:
                await bot.delete_message(message.chat.id, poll_message.message_id)
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                logger.debug(
                    "删除广告投票消息失败 chat_id=%s poll_msg_id=%s error=%s",
                    message.chat.id,
                    poll_message.message_id,
                    exc,
                )

        await ad_vote_store.pop(vote_id)

        return (final_flagged, adv_votes, normal_votes, True, forced_action)

    async def _evaluate_ad(
        bot: Bot,
        message: Message,
        text: str,
        previous_entries,
        current_entry,
        is_user_forward: bool,
    ) -> None:
        chat_id = message.chat.id
        user_id = current_entry.user_id

        if await is_authorized_admin(bot, settings, message):
            if current_entry.text:
                history_store.get(chat_id).append(current_entry)
            return

        if text.startswith("/"):
            if current_entry.text:
                history_store.get(chat_id).append(current_entry)
            return

        # 入群认证未完成：不应发言，也不跑广告检测 / 不计有效发言。
        # （进群到禁言生效之间有竞态窗口，消息可能漏进来。）
        pending = await store.get_pending(chat_id, user_id)
        if pending is not None:
            logger.info(
                "待验证用户发言，跳过广告检测并删除 chat_id=%s user_id=%s msg_id=%s",
                chat_id,
                user_id,
                message.message_id,
            )
            try:
                await bot.delete_message(chat_id, message.message_id)
            except TelegramBadRequest as exc:
                logger.debug(
                    "删除待验证用户消息失败 chat_id=%s msg_id=%s error=%s",
                    chat_id,
                    message.message_id,
                    exc,
                )
            return

        # SQLite 永久合格用户：前 N 次有效发言通过后写入，之后不再检测。
        valid_count, is_qualified = await store.get_ad_qualification(chat_id, user_id)
        if is_qualified or valid_count >= settings.ad_guard_score_skip_threshold:
            if current_entry.text:
                history_store.get(chat_id).append(current_entry)
            logger.debug(
                "合格用户跳过广告检测 chat_id=%s user_id=%s valid_count=%s",
                chat_id,
                user_id,
                valid_count,
            )
            return

        # Redis 仅用于广告扣分/低分清理，与合格计数分离。
        current_score = await score_manager.get_score(chat_id, user_id)
        if current_score <= settings.ad_guard_score_ban_threshold:
            await handle_low_score_violation(
                bot,
                message,
                settings=settings,
                score_manager=score_manager,
                current_score=current_score,
                store=store,
            )
            return

        # 前 N 次有效发言内：全部检测，无长度豁免。
        if is_user_forward:
            logger.debug(
                "检测到用户转发消息 chat_id=%s user_id=%s msg_id=%s",
                chat_id,
                message.from_user.id,
                message.message_id,
            )

        source = "llm"
        if heuristic_detect_advertisement(text, previous_entries=previous_entries):
            logger.debug(
                "广告检测命中本地规则 chat_id=%s user_id=%s msg_id=%s",
                chat_id,
                message.from_user.id,
                message.message_id,
            )
            source = "rule"
            flagged, confidence = (True, 1.0)
        else:
            guard_payload = format_context_for_prompt(previous_entries, current_entry)
            logger.debug(
                "广告检测请求上下文 chat_id=%s user_id=%s msg_id=%s payload_length=%s payload_preview=%s",
                chat_id,
                message.from_user.id,
                message.message_id,
                len(guard_payload),
                truncate_for_logging(guard_payload),
            )
            flagged, confidence = await pipeline.check(guard_payload, message=message)

        logger.debug(
            "广告检测结果 chat_id=%s user_id=%s msg_id=%s flagged=%s confidence=%s length=%s context=%s",
            chat_id,
            message.from_user.id,
            message.message_id,
            flagged,
            confidence,
            len(text),
            len(previous_entries),
        )

        offender_display_html = escape(
            message.from_user.full_name
            or message.from_user.username
            or str(message.from_user.id)
        )

        final_flagged = flagged
        vote_used = False
        vote_adv_count = 0
        vote_normal_count = 0
        force_ban_triggered = False

        # 高把握度跳过投票：规则命中（confidence=1.0）或 LLM confidence ≥ 0.95 直接封禁
        FAST_BAN_CONFIDENCE = 0.95
        skip_vote_high_confidence = (
            flagged
            and (
                source == "rule"
                or (confidence is not None and confidence >= FAST_BAN_CONFIDENCE)
            )
        )

        if flagged and not skip_vote_high_confidence:
            (
                final_flagged,
                vote_adv_count,
                vote_normal_count,
                vote_used,
                force_ban_triggered,
            ) = await _conduct_ad_vote(
                bot,
                message,
                offender_id=message.from_user.id,
                offender_display_html=offender_display_html,
                original_message_id=message.message_id,
                duration=settings.ad_vote_duration_seconds,
            )
        elif skip_vote_high_confidence:
            # 不走投票，直接进入封禁分支：复用 force_ban_triggered 路径删消息+封禁
            final_flagged = True
            force_ban_triggered = True
            logger.info(
                "高把握度广告判定，跳过投票直接封禁 chat_id=%s msg_id=%s source=%s confidence=%s",
                chat_id,
                message.message_id,
                source,
                confidence,
            )
            try:
                await bot.delete_message(chat_id, message.message_id)
            except TelegramBadRequest as exc:
                logger.warning(
                    "高把握度广告消息删除失败 chat_id=%s msg_id=%s error=%s",
                    chat_id,
                    message.message_id,
                    exc,
                )
            try:
                await bot.ban_chat_member(chat_id, message.from_user.id)
                await _record_ban_event_safe(
                    chat_id=chat_id,
                    user_id=message.from_user.id,
                    display_name=message.from_user.full_name
                    or message.from_user.username
                    or str(message.from_user.id),
                    operator_id=None,
                    operator_name="system",
                    reason="ad_auto_high_conf",
                )
            except TelegramBadRequest as exc:
                logger.warning(
                    "高把握度广告封禁失败 chat_id=%s user_id=%s error=%s",
                    chat_id,
                    message.from_user.id,
                    exc,
                )
                # 封禁失败时回退到“走原投票+复核流程”，避免噤声
                force_ban_triggered = False

        if not final_flagged:
            if current_entry.text:
                history_store.get(chat_id).append(current_entry)
            new_count, qualified = await store.record_ad_valid_speech(
                chat_id=chat_id,
                user_id=user_id,
                threshold=settings.ad_guard_score_skip_threshold,
                display_name=message.from_user.full_name
                or message.from_user.username
                or str(message.from_user.id),
                username=message.from_user.username,
            )
            await _record_ad_decision_safe(
                message,
                text,
                source=source,
                flagged=flagged,
                confidence=confidence,
                vote_used=vote_used,
                vote_adv=vote_adv_count,
                vote_normal=vote_normal_count,
                final_action="none",
            )
            logger.debug(
                "广告检测通过 chat_id=%s user_id=%s valid_count=%s qualified=%s",
                chat_id,
                user_id,
                new_count,
                qualified,
            )
            return

        score_after_penalty = await score_manager.adjust_score(chat_id, user_id, -1)
        logger.debug(
            "广告判定扣分 chat_id=%s user_id=%s score=%s",
            chat_id,
            user_id,
            score_after_penalty,
        )
        score_display = score_after_penalty

        if force_ban_triggered:
            logger.debug(
                "广告消息已由管理员强制操作删除 chat_id=%s msg_id=%s",
                chat_id,
                message.message_id,
            )
        else:
            await asyncio.sleep(1)
            try:
                delete_success = await bot.delete_message(chat_id, message.message_id)
                if not delete_success:
                    logger.warning(
                        "广告消息删除返回 False chat_id=%s msg_id=%s",
                        chat_id,
                        message.message_id,
                    )
                    return
            except TelegramBadRequest as exc:
                logger.warning(
                    "广告消息删除失败 chat_id=%s msg_id=%s error=%s",
                    chat_id,
                    message.message_id,
                    exc,
                    exc_info=exc,
                )
                return

        action_suffix = ""
        ban_success = force_ban_triggered
        if ban_success:
            action_suffix = "（管理员已强制封禁）"
        elif settings.ad_guard_ban:
            if score_after_penalty <= settings.ad_guard_score_ban_threshold:
                try:
                    await bot.ban_chat_member(chat_id, message.from_user.id)
                    action_suffix = "（已封禁该用户）"
                    ban_success = True
                    await _record_ban_event_safe(
                        chat_id=chat_id,
                        user_id=message.from_user.id,
                        display_name=message.from_user.full_name
                        or message.from_user.username
                        or str(message.from_user.id),
                        operator_id=None,
                        operator_name="system",
                        reason="ad_auto",
                    )
                except TelegramBadRequest as exc:
                    logger.warning(
                        "广告封禁失败 chat_id=%s user_id=%s error=%s",
                        chat_id,
                        message.from_user.id,
                        exc,
                        exc_info=exc,
                    )
                    action_suffix = "（封禁失败，仅删除消息）"
            else:
                action_suffix = "（评分未到封禁阈值，仅删除消息）"

        if ban_success:
            await score_manager.reset_score(chat_id, message.from_user.id)
            try:
                await store.reset_ad_qualification(chat_id, message.from_user.id)
            except Exception as exc:
                logger.warning(
                    "封禁后重置合格状态失败 chat_id=%s user_id=%s error=%r",
                    chat_id,
                    message.from_user.id,
                    exc,
                    exc_info=True,
                )
            score_display = 0

        await _record_ad_decision_safe(
            message,
            text,
            source=source,
            flagged=flagged,
            confidence=confidence,
            vote_used=vote_used,
            vote_adv=vote_adv_count,
            vote_normal=vote_normal_count,
            final_action="banned" if ban_success else "deleted",
        )

        display_name = offender_display_html
        notice = (
            f"🚫 检测到疑似广告内容，已删除 "
            f"<a href='tg://user?id={message.from_user.id}'>{display_name}</a> 的消息"
        )
        if confidence is not None:
            notice += f"（模型判为广告，把握度 {confidence:.2f}，基于{_provider_label(settings.ad_guard_provider)}）"
        notice += f"（已扣1分，当前评分：{score_display}）"
        if vote_used and not force_ban_triggered:
            notice += f"（投票结果：广告 {vote_adv_count} 票 vs 非广告 {vote_normal_count} 票）"
        notice += action_suffix

        review_id: Optional[str] = None
        review_keyboard = None
        if not ban_success:
            review_id = uuid.uuid4().hex
            review_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="立即封禁 🚫",
                            callback_data=f"adreview:ban:{review_id}",
                        ),
                        InlineKeyboardButton(
                            text="这不是广告‼️",
                            callback_data=f"adreview:restore:{review_id}",
                        ),
                    ]
                ]
            )

        notice_message = await send_message_with_ttl(
            bot,
            chat_id=chat_id,
            text=notice,
            ttl=settings.message_ttl_seconds,
            disable_web_page_preview=True,
            reply_markup=review_keyboard,
        )

        if review_id is not None:
            original_html = message.html_text or escape(text)
            # 写一份快照到 store,供后续 restore 读取原文
            from datetime import datetime, timezone as _tz

            await store.record_ad_deletion(
                token=review_id,
                chat_id=chat_id,
                user_id=message.from_user.id,
                message_text=text,
                display_name=offender_display_html,
                confidence=confidence,
                deleted_at=datetime.now(tz=_tz.utc),
            )
            context = AdReviewContext(
                chat_id=chat_id,
                offender_id=message.from_user.id,
                offender_display_html=display_name,
                offender_name=message.from_user.full_name
                or message.from_user.username
                or str(message.from_user.id),
                original_html=original_html,
                history_entry=current_entry,
                score_penalty=1,
                notice_chat_id=notice_message.chat.id,
                notice_message_id=notice_message.message_id,
                confidence=confidence,
            )
            await ad_review_store.put(review_id, context)
            ttl_seconds = settings.message_ttl_seconds or 0
            if ttl_seconds > 0:
                ad_review_store.schedule_expiry(review_id, ttl_seconds + 5)

    @router.message(F.text | F.caption)
    async def handle_text_messages(message: Message, bot: Bot) -> None:
        if not settings.ad_guard_enabled:
            return

        sender_chat = message.sender_chat
        is_auto_forward = getattr(message, "is_automatic_forward", False)
        if sender_chat and sender_chat.type == "channel" and not is_auto_forward:
            logger.info(
                "检测到频道身份发言，准备删除 chat_id=%s sender_chat_id=%s msg_id=%s",
                message.chat.id,
                sender_chat.id,
                message.message_id,
            )
            try:
                await bot.delete_message(
                    chat_id=message.chat.id, message_id=message.message_id
                )
                await send_message_with_ttl(
                    bot,
                    chat_id=message.chat.id,
                    text="⚠️ 本群禁止使用频道身份发言，请改用个人账号。",
                    ttl=settings.message_ttl_seconds,
                )
            except TelegramBadRequest as exc:
                logger.warning(
                    "删除频道身份消息失败 chat_id=%s sender_chat_id=%s msg_id=%s error=%s",
                    message.chat.id,
                    sender_chat.id,
                    message.message_id,
                    exc,
                    exc_info=exc,
                )
            return

        if message.from_user is None or message.from_user.is_bot:
            return

        # 同时支持纯文本和带图/媒体的 caption（广告号常把文案塞进图片说明绕过检测）
        raw_text = message.text if message.text is not None else message.caption

        history = history_store.get(message.chat.id)
        if is_auto_forward:
            logger.debug(
                "关联频道自动转发消息跳过广告检测 chat_id=%s msg_id=%s",
                message.chat.id,
                message.message_id,
            )
            text_for_history = (raw_text or "").strip()
            if text_for_history:
                history.append(
                    build_history_entry(message, text_for_history, is_user_forward=True)
                )
            return

        if not raw_text:
            return
        text = raw_text.strip()
        if not text:
            return

        is_user_forward = any(
            (
                message.forward_from,
                message.forward_from_chat,
                getattr(message, "forward_sender_name", None),
            )
        )
        previous_entries = list(history)
        current_entry = build_history_entry(message, text, is_user_forward=is_user_forward)

        await _evaluate_ad(
            bot, message, text, previous_entries, current_entry, is_user_forward
        )

    @router.edited_message(F.text | F.caption)
    async def handle_edited_text_messages(message: Message, bot: Bot) -> None:
        # 编辑后内容若涉广告，复用同一套检测；防绕过手法：先发垃圾 → 编辑成广告
        await handle_text_messages(message, bot)

    @router.message(~F.text & ~F.caption)
    async def handle_non_text_messages(message: Message, bot: Bot) -> None:
        sender_chat = message.sender_chat
        is_auto_forward = getattr(message, "is_automatic_forward", False)
        if sender_chat and sender_chat.type == "channel" and not is_auto_forward:
            logger.info(
                "检测到频道身份非文本发言，准备删除 chat_id=%s sender_chat_id=%s msg_id=%s",
                message.chat.id,
                sender_chat.id,
                message.message_id,
            )
            try:
                await bot.delete_message(
                    chat_id=message.chat.id, message_id=message.message_id
                )
                await send_message_with_ttl(
                    bot,
                    chat_id=message.chat.id,
                    text="⚠️ 本群禁止使用频道身份发言，请改用个人账号。",
                    ttl=getattr(settings, "message_ttl_seconds", None),
                )
            except TelegramBadRequest as exc:
                logger.warning(
                    "删除频道身份非文本消息失败 chat_id=%s sender_chat_id=%s msg_id=%s error=%s",
                    message.chat.id,
                    sender_chat.id,
                    message.message_id,
                    exc,
                    exc_info=exc,
                )
            return

    @router.callback_query(lambda call: call.data and call.data.startswith("adreview:"))
    async def handle_ad_review_callback(callback: CallbackQuery, bot: Bot) -> None:
        if callback.message is None or callback.from_user is None:
            await callback.answer()
            return

        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("无效的指令", show_alert=True)
            return

        _, action, review_id = parts
        cases = ad_review_store.cases()
        lock = ad_review_store.lock()

        async with lock:
            case = cases.get(review_id)
            if case is None or case.resolved:
                await callback.answer("该操作已处理", show_alert=True)
                return
            if case.locked_by is not None and case.locked_by != callback.from_user.id:
                await callback.answer("其他管理员正在处理", show_alert=True)
                return
            case.locked_by = callback.from_user.id

        try:
            member = await bot.get_chat_member(case.chat_id, callback.from_user.id)
        except TelegramBadRequest as exc:
            logger.warning(
                "复核操作校验管理员失败 chat_id=%s operator_id=%s error=%s",
                case.chat_id,
                callback.from_user.id,
                exc,
                exc_info=exc,
            )
            async with lock:
                stored = cases.get(review_id)
                if stored is not None:
                    stored.locked_by = None
            await callback.answer("无法验证权限，请稍后再试", show_alert=True)
            return

        if member.status not in ADMIN_STATUSES:
            async with lock:
                stored = cases.get(review_id)
                if stored is not None:
                    stored.locked_by = None
            await callback.answer("仅管理员可操作", show_alert=True)
            return

        offender_link = f"<a href='tg://user?id={case.offender_id}'>{case.offender_display_html}</a>"
        manager_name = escape(
            callback.from_user.full_name
            or callback.from_user.username
            or str(callback.from_user.id)
        )

        if action == "ban":
            ban_error: Optional[Exception] = None
            try:
                await bot.ban_chat_member(case.chat_id, case.offender_id)
            except TelegramBadRequest as exc:
                if "USER_ALREADY_BANNED" not in str(exc):
                    ban_error = exc
                else:
                    logger.debug(
                        "目标已被封禁 chat_id=%s user_id=%s",
                        case.chat_id,
                        case.offender_id,
                    )
            if ban_error:
                logger.warning(
                    "复核封禁失败 chat_id=%s operator_id=%s target_id=%s error=%s",
                    case.chat_id,
                    callback.from_user.id,
                    case.offender_id,
                    ban_error,
                    exc_info=ban_error,
                )
                async with lock:
                    stored = cases.get(review_id)
                    if stored is not None:
                        stored.locked_by = None
                await callback.answer("封禁失败，请查看日志", show_alert=True)
                return

            try:
                await score_manager.reset_score(case.chat_id, case.offender_id)
            except Exception as exc:
                logger.warning(
                    "封禁后重置评分失败 chat_id=%s user_id=%s error=%r",
                    case.chat_id,
                    case.offender_id,
                    exc,
                    exc_info=True,
                )
            try:
                await store.reset_ad_qualification(case.chat_id, case.offender_id)
            except Exception as exc:
                logger.warning(
                    "封禁后重置合格状态失败 chat_id=%s user_id=%s error=%r",
                    case.chat_id,
                    case.offender_id,
                    exc,
                    exc_info=True,
                )

            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass

            await _record_ban_event_safe(
                chat_id=case.chat_id,
                user_id=case.offender_id,
                display_name=case.offender_name,
                operator_id=callback.from_user.id,
                operator_name=callback.from_user.full_name
                or callback.from_user.username
                or str(callback.from_user.id),
                reason="ad_review",
            )

            confirmation_text = (
                f"🚫 管理员 <a href='tg://user?id={callback.from_user.id}'>{manager_name}</a> "
                f"已确认并封禁 {offender_link}。"
            )
            await send_message_with_ttl(
                bot,
                chat_id=case.chat_id,
                text=confirmation_text,
                ttl=settings.message_ttl_seconds,
                disable_web_page_preview=True,
            )
            await store.delete_ad_deletion(review_id)
            await callback.answer("已封禁该用户")

            async with lock:
                stored = cases.pop(review_id, None)
                if stored is not None:
                    stored.resolved = True
                    stored.locked_by = callback.from_user.id
            return

        if action == "restore":
            try:
                await store.mark_ad_qualified(
                    chat_id=case.chat_id,
                    user_id=case.offender_id,
                    threshold=settings.ad_guard_score_skip_threshold,
                    display_name=case.offender_name,
                    username=None,
                )
            except Exception as exc:
                logger.warning(
                    "恢复并标记合格失败 chat_id=%s user_id=%s error=%r",
                    case.chat_id,
                    case.offender_id,
                    exc,
                    exc_info=True,
                )

            history_store.get(case.chat_id).append(case.history_entry)

            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass

            restored_payload = case.original_html.strip() or "（原消息为空）"
            restored_message = (
                f"✅ 管理员 <a href='tg://user?id={callback.from_user.id}'>{manager_name}</a> 判定 "
                f"{offender_link} 的消息不是广告，恢复内容如下：\n\n"
                f"<blockquote>{restored_payload}</blockquote>"
            )
            await bot.send_message(
                case.chat_id,
                restored_message,
                disable_web_page_preview=True,
            )
            try:
                await store.record_ad_decision(
                    chat_id=case.chat_id,
                    user_id=case.offender_id,
                    display_name=case.offender_name,
                    username=None,
                    message_text=case.history_entry.text,
                    source="review",
                    flagged=False,
                    confidence=case.confidence,
                    final_action="restored",
                )
            except Exception as exc:
                logger.warning(
                    "记录广告恢复日志失败 chat_id=%s user_id=%s error=%r",
                    case.chat_id,
                    case.offender_id,
                    exc,
                    exc_info=True,
                )
            await store.delete_ad_deletion(review_id)
            await callback.answer("已恢复消息")

            async with lock:
                stored = cases.pop(review_id, None)
                if stored is not None:
                    stored.resolved = True
                    stored.locked_by = callback.from_user.id
            return

        async with lock:
            stored = cases.get(review_id)
            if stored is not None:
                stored.locked_by = None
        await callback.answer("未知操作", show_alert=True)

    @router.callback_query(lambda call: call.data and call.data.startswith("advote:"))
    async def handle_ad_vote_callback(callback: CallbackQuery, bot: Bot) -> None:
        if callback.message is None or callback.from_user is None:
            await callback.answer()
            return

        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("无效的指令", show_alert=True)
            return

        _, action, vote_id = parts
        if action != "ban":
            await callback.answer("未知操作", show_alert=True)
            return

        context = await ad_vote_store.get(vote_id)
        if context is None or context.force_ban:
            await callback.answer("该投票已结束", show_alert=True)
            return

        try:
            member = await bot.get_chat_member(context.chat_id, callback.from_user.id)
        except TelegramBadRequest as exc:
            logger.warning(
                "广告投票强制封禁校验管理员失败 chat_id=%s operator_id=%s error=%s",
                context.chat_id,
                callback.from_user.id,
                exc,
                exc_info=exc,
            )
            await callback.answer("无法验证权限，请稍后再试", show_alert=True)
            return

        if member.status not in ADMIN_STATUSES:
            await callback.answer("仅管理员可操作", show_alert=True)
            return

        poll_result: Optional[Poll] = None
        try:
            poll_result = await bot.stop_poll(context.chat_id, context.poll_message_id)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.debug(
                "提前停止广告投票失败 chat_id=%s poll_msg_id=%s error=%s",
                context.chat_id,
                context.poll_message_id,
                exc,
            )

        ban_error: Optional[Exception] = None
        try:
            await bot.ban_chat_member(context.chat_id, context.offender_id)
        except TelegramBadRequest as exc:
            if "USER_ALREADY_BANNED" not in str(exc):
                ban_error = exc
        if ban_error:
            logger.warning(
                "广告投票强制封禁失败 chat_id=%s operator_id=%s target_id=%s error=%s",
                context.chat_id,
                callback.from_user.id,
                context.offender_id,
                ban_error,
                exc_info=ban_error,
            )
            await callback.answer("封禁失败，请查看日志", show_alert=True)
            return

        try:
            await bot.delete_message(context.chat_id, context.poll_message_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass

        try:
            await bot.delete_message(context.chat_id, context.offender_message_id)
        except TelegramBadRequest:
            pass

        await _record_ban_event_safe(
            chat_id=context.chat_id,
            user_id=context.offender_id,
            display_name=context.offender_display_html,
            operator_id=callback.from_user.id,
            operator_name=callback.from_user.full_name
            or callback.from_user.username
            or str(callback.from_user.id),
            reason="ad_vote_force",
        )

        context.force_ban = True
        context.poll_result = poll_result
        context.event.set()

        manager_name = escape(
            callback.from_user.full_name
            or callback.from_user.username
            or str(callback.from_user.id)
        )
        offender_link = (
            f"<a href='tg://user?id={context.offender_id}'>"
            f"{context.offender_display_html}</a>"
        )
        notice = (
            f"🚫 管理员 <a href='tg://user?id={callback.from_user.id}'>{manager_name}</a> "
            f"已强制封禁 {offender_link} 并终止广告投票。"
        )
        await send_message_with_ttl(
            bot,
            chat_id=context.chat_id,
            text=notice,
            ttl=settings.message_ttl_seconds,
            disable_web_page_preview=True,
        )
        await callback.answer("已封禁该用户")

    return router


__all__ = ["build_ad_guard_router"]
