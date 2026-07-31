"""回归验证脚本：覆盖本次修复的关键路径。"""
import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.storage import VerificationStore
from app.state.ad_review import AdReviewStore, AdReviewContext
from app.bot_components.history import HistoryEntry
from app.metrics import NullMetrics, PrometheusMetrics
from app.keyword_replies import (
    KeywordRule,
    configure_keyword_replies,
    get_keyword_reply_config,
    save_keyword_rules,
    validate_keyword_rules_payload,
)


async def test_summarize_metrics_keyerror():
    """Bug #2: admin_tempban 事件不应再导致 KeyError。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = VerificationStore(Path(tmp) / "t.sqlite3")
        await store.connect()
        now = datetime.now(tz=timezone.utc)
        # 写入此前会触发 KeyError 的 admin_tempban 事件
        await store.record_verification_event(
            chat_id=-100, user_id=1, username="u", event="admin_tempban", created_at=now
        )
        await store.record_verification_event(
            chat_id=-100, user_id=2, username="v", event="joined", created_at=now
        )
        data = await store.summarize_metrics({-100})
        assert data["today"]["admin_tempban"] == 1, data["today"]
        assert data["today"]["joined"] == 1, data["today"]
        await store.close()
    print("PASS: summarize_metrics 不再因 admin_tempban 触发 KeyError，且已计入统计")


async def test_recent_events_default_30d():
    """Fix #3: 进群日志默认只扫描最近 30 天,显式 since 可查更早记录。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = VerificationStore(Path(tmp) / "t.sqlite3")
        await store.connect()
        now = datetime.now(tz=timezone.utc)
        old = now - timedelta(days=40)
        await store.record_verification_event(
            chat_id=-100, user_id=1, username="old", event="joined", created_at=old
        )
        await store.record_verification_event(
            chat_id=-100, user_id=2, username="new", event="joined", created_at=now
        )
        rows = await store.recent_verification_events({-100})
        assert len(rows) == 1 and rows[0]["user_id"] == 2, rows
        rows_all = await store.recent_verification_events(
            {-100}, since=int(old.timestamp()) - 10
        )
        assert len(rows_all) == 2, rows_all
        await store.close()
    print("PASS: 进群日志默认 30 天窗口生效,显式 since 可查更早记录")


async def test_ad_review_expire_cleanup():
    """Bug #6: case 过期时回调清理关联数据。"""
    cleaned = []

    async def on_expire(review_id: str):
        cleaned.append(review_id)

    store = AdReviewStore(on_expire=on_expire)
    ctx = AdReviewContext(
        chat_id=1, offender_id=2, offender_display_html="n", offender_name="n",
        original_html="x", history_entry=HistoryEntry(2, "n", "x", False),
        score_penalty=1, notice_chat_id=1, notice_message_id=1, confidence=None,
    )
    await store.put("rid1", ctx)
    store.schedule_expiry("rid1", 0)  # 立即过期
    await asyncio.sleep(0.05)
    assert await store.get("rid1") is None, "case 未被过期清理"
    assert cleaned == ["rid1"], f"on_expire 未回调: {cleaned}"
    print("PASS: 复核 case 过期后内存清理 + on_expire 回调均生效")


def test_metrics_expose():
    """Bug #1: PrometheusMetrics.expose() 能导出自定义 registry 中的 kkbot_* 指标。"""
    m = PrometheusMetrics()
    m.observe_llm_outcome(provider="ollama", flagged=True)
    payload = m.expose().decode("utf-8")
    assert "kkbot_llm_outcome_total" in payload, payload[:500]
    # web.py 的修复依赖 hasattr(metrics, "expose") 分支
    assert hasattr(m, "expose")
    assert not hasattr(NullMetrics(), "expose")  # NullMetrics 走全局 generate_latest 兜底
    print("PASS: metrics.expose() 输出包含 kkbot_* 指标，web 层分支条件成立")


def test_keyword_rule_matching():
    """关键词回复:any/all/大小写/正则 匹配语义。"""
    any_rule = KeywordRule(reply="r", keywords=("群规", "规则"))
    assert any_rule.matches("请看群规")
    assert any_rule.matches("这里的规则是什么")
    assert not any_rule.matches("今天天气不错")

    all_rule = KeywordRule(reply="r", keywords=("新人", "进群"), require_all=True)
    assert all_rule.matches("新人如何进群")
    assert not all_rule.matches("新人报道")

    case_rule = KeywordRule(reply="r", keywords=("VIP",), case_sensitive=True)
    assert case_rule.matches("开通VIP")
    assert not case_rule.matches("开通vip")

    lower_rule = KeywordRule(reply="r", keywords=("vip",))
    assert lower_rule.matches("开通VIP")

    regex_rule = KeywordRule(reply="r", pattern=__import__("re").compile("(?i)怎么.*验证"))
    assert regex_rule.matches("请问怎么完成验证")
    assert not regex_rule.matches("随便聊聊")

    print("PASS: 关键词规则 any/all/大小写/正则匹配语义正确")


def test_keyword_config_load_and_hotreload():
    """关键词回复:配置文件加载、无效规则剔除、热重载、cooldown 覆盖。"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "kw.json"
        cfg.write_text(json.dumps({
            "cooldown_seconds": 120,
            "rules": [
                {"keywords": ["群规"], "reply": "看置顶"},
                {"reply": "缺关键词"},          # 无效:无 keywords/pattern
                {"keywords": "不是数组"},        # 无效:类型错误
                {"pattern": "[", "reply": "x"},  # 无效:坏正则
            ],
        }, ensure_ascii=False), encoding="utf-8")
        configure_keyword_replies(cfg)
        rules, cooldown = get_keyword_reply_config()
        assert cooldown == 120, cooldown
        assert len(rules) == 1 and rules[0].matches("群规在哪"), rules

        # 热重载:修改文件后无需重启即生效
        cfg.write_text(json.dumps({
            "rules": [{"keywords": ["新词"], "reply": "新回复"}],
        }, ensure_ascii=False), encoding="utf-8")
        import os, time as _t
        _t.sleep(0.02)
        os.utime(cfg, None)
        rules2, cooldown2 = get_keyword_reply_config()
        assert cooldown2 is None  # 未配置时回退到环境变量
        assert len(rules2) == 1 and rules2[0].matches("包含新词"), rules2
        assert not rules2[0].matches("群规在哪")
    print("PASS: 关键词配置加载、无效规则剔除、热重载均正常")


def test_keyword_validate_payload():
    """后台接口校验:规范化、错误收集、cooldown 边界。"""
    ok_payload, errors = validate_keyword_rules_payload({
        "cooldown_seconds": 90,
        "rules": [
            {"keywords": ["群规"], "match": "any", "reply": "看置顶"},
            {"keywords": ["新人", "进群"], "match": "all", "case_sensitive": True, "reply": "r"},
            {"pattern": "(?i)abc", "reply": "r"},
        ],
    })
    assert errors == [], errors
    assert ok_payload["cooldown_seconds"] == 90
    assert ok_payload["rules"][0] == {"keywords": ["群规"], "reply": "看置顶"}
    assert ok_payload["rules"][1]["match"] == "all"
    assert ok_payload["rules"][1]["case_sensitive"] is True
    assert ok_payload["rules"][2] == {"pattern": "(?i)abc", "reply": "r"}

    bad_cases = [
        ({"rules": "不是数组"}, "rules 必须是数组"),
        ({"rules": [{"keywords": [], "reply": "r"}]}, "空数组"),
        ({"rules": [{"keywords": ["a"], "reply": ""}]}, "reply 不能为空"),
        ({"rules": [{"keywords": ["a"], "match": "bad", "reply": "r"}]}, "match"),
        ({"rules": [{"pattern": "[", "reply": "r"}]}, "正则无效"),
        ({"rules": [{"keywords": ["a"], "reply": "r"}], "cooldown_seconds": -1}, "0-86400"),
        ({"rules": [{"keywords": ["a"], "reply": "r"}], "cooldown_seconds": "abc"}, "整数"),
    ]
    for payload, expect in bad_cases:
        _, errs = validate_keyword_rules_payload(payload)
        assert errs and any(expect in e for e in errs), (payload, errs)

    # 未提供 cooldown 时不出现在规范化结果中
    normalized, errs = validate_keyword_rules_payload(
        {"rules": [{"keywords": ["a"], "reply": "r"}]}
    )
    assert errs == [] and "cooldown_seconds" not in normalized
    print("PASS: 后台关键词配置校验(规范化/错误收集/边界)正确")


def test_keyword_save_and_reload():
    """后台保存路径:save_keyword_rules 原子写入后缓存热重载生效。"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "kw.json"
        payload, errors = validate_keyword_rules_payload({
            "cooldown_seconds": 45,
            "rules": [{"keywords": ["你好"], "reply": "你好呀"}],
        })
        assert errors == []
        save_keyword_rules(cfg, payload)
        configure_keyword_replies(cfg)
        rules, cooldown = get_keyword_reply_config()
        assert cooldown == 45
        assert len(rules) == 1 and rules[0].matches("说你好")
        # 文件内容可被 json 解析且不含临时文件残留
        assert json.loads(cfg.read_text(encoding="utf-8"))["cooldown_seconds"] == 45
        assert not (cfg.parent / "kw.json.tmp").exists()
    print("PASS: 规则保存-热重载闭环正常,无临时文件残留")


async def main():
    await test_summarize_metrics_keyerror()
    await test_recent_events_default_30d()
    await test_ad_review_expire_cleanup()
    test_metrics_expose()
    test_keyword_rule_matching()
    test_keyword_config_load_and_hotreload()
    test_keyword_validate_payload()
    test_keyword_save_and_reload()
    print("\n全部回归验证通过")


if __name__ == "__main__":
    asyncio.run(main())
