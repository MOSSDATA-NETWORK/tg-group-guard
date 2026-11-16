"""广告守卫低误判策略的回归测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from app.bot_components.history import HistoryEntry, build_history_entry, format_context_for_prompt
from app.routers import ad_guard as router_module
from app.state.ad_review import AdReviewStore
from app.state.ad_vote import AdVoteStore
from app.state.message_history import MessageHistoryStore


class _Store:
    def __init__(self):
        self.decisions = []
        self.deletions = {}

    async def get_pending(self, chat_id, user_id):
        return None

    async def get_ad_qualification(self, chat_id, user_id):
        return 0, False

    async def record_ad_valid_speech(self, **kwargs):
        return 1, False

    async def record_ad_decision(self, **kwargs):
        self.decisions.append(kwargs)
        return len(self.decisions)

    async def record_ban_event(self, **kwargs):
        return 1

    async def reset_ad_qualification(self, chat_id, user_id):
        return None

    async def record_ad_deletion(self, **kwargs):
        self.deletions[kwargs["token"]] = kwargs

    async def delete_ad_deletion(self, token):
        self.deletions.pop(token, None)

    async def mark_ad_qualified(self, **kwargs):
        return None


class _Score:
    def __init__(self, value=0):
        self.value = value
        self.adjustments = []

    async def get_score(self, chat_id, user_id):
        return self.value

    async def adjust_score(self, chat_id, user_id, delta):
        self.adjustments.append(delta)
        self.value += delta
        return self.value

    async def reset_score(self, chat_id, user_id):
        self.value = 0


class _Bot:
    id = 999

    def __init__(self, vote_counts=(0, 0)):
        self.vote_counts = vote_counts
        self.bans = []
        self.unbans = []
        self.deleted = []
        self.sent = []

    async def get_chat_member(self, chat_id, user_id):
        return NS(status="administrator" if user_id == 99 else "member")

    async def get_chat(self, chat_id):
        return NS(linked_chat_id=None)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True

    async def ban_chat_member(self, chat_id, user_id):
        self.bans.append((chat_id, user_id))

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=False):
        self.unbans.append((chat_id, user_id))

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((text, kwargs))
        return NS(message_id=1000 + len(self.sent), chat=NS(id=chat_id))

    async def send_poll(self, chat_id, **kwargs):
        poll = NS(options=[NS(voter_count=self.vote_counts[0]), NS(voter_count=self.vote_counts[1])])
        return NS(message_id=2000, chat=NS(id=chat_id), poll=poll)

    async def stop_poll(self, chat_id, message_id):
        return NS(options=[NS(voter_count=self.vote_counts[0]), NS(voter_count=self.vote_counts[1])])


class _CallbackMessage:
    async def edit_reply_markup(self, reply_markup=None):
        return None


def _message(text="这是一条用于测试的正常讨论消息"):
    return NS(
        chat=NS(id=-991000, type="supergroup", is_forum=False),
        from_user=NS(id=42, full_name="User", username="user", is_bot=False),
        sender_chat=None,
        is_automatic_forward=False,
        message_id=7,
        message_thread_id=None,
        text=text,
        html_text=text,
        caption=None,
        entities=None,
        caption_entities=None,
        reply_markup=None,
        poll=None,
        checklist=None,
        quote=None,
        reply_to_message=None,
        external_reply=None,
        photo=None,
        video=None,
        document=None,
        animation=None,
        audio=None,
        voice=None,
        contact=None,
        story=None,
        forward_origin=None,
        forward_from=None,
        forward_from_chat=None,
        forward_sender_name=None,
    )


def _settings(*, ban=False, vote_counts=(0, 0)):
    return NS(
        allowed_chat_ids=set(),
        keyword_reply_enabled=False,
        keyword_deletion_enabled=False,
        keyword_reply_cooldown_seconds=60,
        ad_guard_enabled=True,
        ad_guard_provider="ollama",
        ad_guard_image_enabled=False,
        ad_guard_include_bots=True,
        ad_guard_block_sender_chat=False,
        ad_guard_ban_sender_chat=False,
        ad_guard_message_rate_limit=0,
        ad_guard_message_rate_window_seconds=60,
        ad_guard_min_length=0,
        ad_guard_score_skip_threshold=10,
        ad_guard_score_ban_threshold=-10,
        ad_guard_ban=ban,
        ad_vote_duration_seconds=1,
        contact_message_policy="off",
        story_message_policy="off",
        voice_message_policy="off",
        message_ttl_seconds=None,
    )


class _Pipeline:
    def __init__(self, confidence):
        self.confidence = confidence

    async def check(self, content, *, message=None, image=None):
        return True, self.confidence


def _router(settings, score, store, bot, confidence):
    review_store = AdReviewStore()
    review_store.schedule_expiry = lambda *args: None
    services = NS(
        settings=settings,
        store=store,
        score_manager=score,
        history_store=MessageHistoryStore(),
        ad_review_store=review_store,
        ad_vote_store=AdVoteStore(),
        metrics=None,
    )
    router = router_module.build_ad_guard_router(services, _Pipeline(confidence))
    handlers = {}
    for handler in router.message.handlers:
        handlers[handler.callback.__name__] = handler.callback
    for handler in router.callback_query.handlers:
        handlers[handler.callback.__name__] = handler.callback
    return handlers, review_store


class AdGuardFalsePositiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        router_module._BEHAVIOR_GUARD.clear()

    async def asyncTearDown(self):
        router_module._BEHAVIOR_GUARD.clear()

    async def test_llm_high_confidence_does_not_ban_and_ignores_no_switch(self):
        for ban_enabled in (False, True):
            with self.subTest(ban_enabled=ban_enabled):
                bot, score, store = _Bot(), _Score(), _Store()
                handlers, review_store = _router(_settings(ban=ban_enabled), score, store, bot, 0.99)
                await handlers["handle_text_messages"](_message(), bot)
                self.assertEqual(bot.bans, [], "单次 LLM 高置信判定不得直接永久封禁")
                self.assertEqual(score.adjustments, [-1])
                self.assertEqual(len(review_store.cases()), 1, "必须保留误判复核入口")

    async def test_tied_or_empty_vote_is_review_only_without_penalty(self):
        for votes in ((0, 0), (1, 1)):
            with self.subTest(votes=votes):
                bot, score, store = _Bot(votes), _Score(), _Store()
                handlers, review_store = _router(_settings(ban=True), score, store, bot, 0.8)
                await handlers["handle_text_messages"](_message(), bot)
                self.assertEqual(score.adjustments, [], "无明确投票结论时不得扣分")
                self.assertEqual(bot.bans, [], "无明确投票结论时不得封禁")
                self.assertEqual(store.decisions[-1]["final_action"], "review")
                self.assertEqual(len(review_store.cases()), 1)

    async def test_vote_creation_failure_is_review_only_without_penalty(self):
        from aiogram.exceptions import TelegramBadRequest

        class BrokenPollBot(_Bot):
            async def send_poll(self, chat_id, **kwargs):
                raise TelegramBadRequest(method=None, message="poll unavailable")

        bot, score, store = BrokenPollBot(), _Score(), _Store()
        handlers, review_store = _router(_settings(ban=True), score, store, bot, 0.8)
        await handlers["handle_text_messages"](_message(), bot)
        self.assertEqual(score.adjustments, [], "投票创建失败时不得按模型原判扣分")
        self.assertEqual(bot.bans, [])
        self.assertEqual(store.decisions[-1]["final_action"], "review")
        self.assertEqual(len(review_store.cases()), 1)

    async def test_banned_decision_can_restore_and_unban(self):
        bot, score, store = _Bot(), _Score(), _Store()
        settings = _settings(ban=True)
        settings.ad_guard_score_ban_threshold = -1
        handlers, review_store = _router(settings, score, store, bot, 0.99)
        await handlers["handle_text_messages"](_message(), bot)
        self.assertEqual(bot.bans, [(-991000, 42)])
        markup = bot.sent[-1][1].get("reply_markup")
        self.assertIsNotNone(markup, "已封禁路径也必须提供误伤恢复按钮")
        restore_data = next(
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("adreview:restore:")
        )
        callback = NS(
            data=restore_data,
            from_user=NS(id=99, full_name="Admin", username="admin"),
            message=_CallbackMessage(),
            answer=_async_noop,
        )
        await handlers["handle_ad_review_callback"](callback, bot)
        self.assertEqual(bot.unbans, [(-991000, 42)], "误判恢复必须同步解除群组封禁")
        self.assertEqual(score.value, 0, "封禁时已重置评分，恢复不得再重复加分")

    async def test_local_keyword_rule_does_not_skip_vote_as_certain_ad(self):
        from app import ad_guard_rules as rules

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps({"pattern": "跑分|拉新"}), encoding="utf-8")
            rules.configure_ad_guard_rules(path)
            try:
                bot, score, store = _Bot((0, 0)), _Score(), _Store()
                handlers, review_store = _router(_settings(ban=True), score, store, bot, 0.1)
                await handlers["handle_text_messages"](_message("跑分是什么意思"), bot)
                self.assertEqual(store.decisions[-1]["source"], "rule")
                self.assertEqual(store.decisions[-1]["final_action"], "review")
                self.assertEqual(score.adjustments, [], "单个关键词命中不得直接当成确定广告扣分")
                self.assertEqual(bot.bans, [])
            finally:
                rules.configure_ad_guard_rules(None)

    async def test_quote_reply_and_external_content_have_distinct_roles(self):
        msg = _message("这个抽奖文案太夸张了")
        msg.quote = NS(text="百分百返利，点击链接", entities=None)
        msg.reply_to_message = NS(text="原价回收账号", caption=None, entities=None, caption_entities=None, reply_markup=None, poll=None, checklist=None)
        msg.external_reply = NS(text="外部推广原文", caption=None, entities=None, caption_entities=None, reply_markup=None, poll=None, checklist=None)
        entry = build_history_entry(
            msg,
            "这个抽奖文案太夸张了",
            is_user_forward=False,
            quote_text="百分百返利，点击链接",
            reply_text="原价回收账号",
            external_reply_text="外部推广原文",
        )
        payload = format_context_for_prompt([], entry)
        self.assertIn("【本人正文】", payload)
        self.assertIn("【Quote 引用内容（仅作背景）】", payload)
        self.assertIn("【回复原消息（仅作背景）】", payload)
        self.assertIn("【外部回复内容（仅作背景）】", payload)


class AdGuardPromptAndRuleTests(unittest.TestCase):
    def test_prompt_does_not_turn_malformed_input_into_advertisement(self):
        from app import ad_guard_rules as rules

        rules.configure_ad_guard_rules(None)
        prompt = rules.render_prompt_message("正常消息")
        self.assertNotIn('输出 {"advertisement": true, "confidence": 0.99}', prompt)
        self.assertIn("引用、回复、转发来源和最近群聊只作为背景", prompt)
        self.assertIn("信息不足、内容异常或无法确定时", prompt)
        config_prompt = json.loads(Path("config/ad_guard_rules.json").read_text(encoding="utf-8"))["prompt_template"]
        self.assertNotIn('输出 {"advertisement": true, "confidence": 0.99}', config_prompt)

    def test_review_and_restore_events_have_chinese_labels(self):
        from app.storage import AD_FINAL_ACTION_LABELS, BAN_REASON_LABELS

        self.assertEqual(AD_FINAL_ACTION_LABELS["review"], "待管理员复核")
        self.assertEqual(BAN_REASON_LABELS["ad_review_restore"], "广告误判恢复")

    def test_historical_ad_does_not_make_normal_current_message_match(self):
        from app import ad_guard_rules as rules
        from app.bot_components.ad_guard import heuristic_detect_advertisement

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps({"pattern": "跑分|拉新"}), encoding="utf-8")
            rules.configure_ad_guard_rules(path)
            try:
                previous = [HistoryEntry(7, "other", "专业跑分，欢迎联系", False)]
                self.assertFalse(
                    heuristic_detect_advertisement("昨天那件事确实很离谱", previous_entries=previous),
                    "历史消息只能提供语境，不得成为当前消息的命中证据",
                )
            finally:
                rules.configure_ad_guard_rules(None)


async def _async_noop(*args, **kwargs):
    return None


if __name__ == "__main__":
    unittest.main()
