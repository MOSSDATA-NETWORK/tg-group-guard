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
    """Bug #1: PrometheusMetrics.expose() 能导出自定义 registry 中的 telegram_group_guard_bot_* 指标。"""
    m = PrometheusMetrics()
    m.observe_llm_outcome(provider="ollama", flagged=True)
    payload = m.expose().decode("utf-8")
    assert "telegram_group_guard_bot_llm_outcome_total" in payload, payload[:500]
    # web.py 的修复依赖 hasattr(metrics, "expose") 分支
    assert hasattr(m, "expose")
    assert not hasattr(NullMetrics(), "expose")  # NullMetrics 走全局 generate_latest 兜底
    print("PASS: metrics.expose() 输出包含 telegram_group_guard_bot_* 指标，web 层分支条件成立")


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


async def test_join_prompt_sent_once():
    """入群双事件:同一用户并发/连续触发 process_new_member,验证提示只发一次。"""
    from types import SimpleNamespace

    from app.bot_components.verification import process_new_member

    class FakeBot:
        def __init__(self):
            self.sent = []
            self.restricted = []

        async def restrict_chat_member(self, **kwargs):
            self.restricted.append(kwargs)

        async def send_message(self, chat_id, text, **kwargs):
            self.sent.append(text)
            return SimpleNamespace(message_id=len(self.sent), chat=SimpleNamespace(id=chat_id))

    with tempfile.TemporaryDirectory() as tmp:
        store = VerificationStore(Path(tmp) / "t.sqlite3")
        await store.connect()
        bot = FakeBot()
        settings = SimpleNamespace(
            message_ttl_seconds=None,
            verification_timeout_seconds=0,  # 避免调度后台删除任务
            bot_username="dummybot",
        )
        member = SimpleNamespace(id=42, username="u", full_name="User U")

        # ① 模拟 Telegram 对一次入群同时推送 new_chat_members + chat_member 双事件
        await asyncio.gather(
            process_new_member(bot, store, settings, chat_id=-100, chat_title="测试群", member=member),
            process_new_member(bot, store, settings, chat_id=-100, chat_title="测试群", member=member),
        )
        assert len(bot.sent) == 1, f"并发双事件应只发 1 条提示,实际 {len(bot.sent)} 条"

        # ② 抑制窗口内串行再触发(第二个事件晚到)也不应重发
        await process_new_member(bot, store, settings, chat_id=-100, chat_title="测试群", member=member)
        assert len(bot.sent) == 1, f"窗口内重复触发不应重发,实际 {len(bot.sent)} 条"

        # ③ 窗口外(用户退群重进,pending 记录已存在超 30s)仍应正常重发提示
        old_ts = (datetime.now(tz=timezone.utc) - timedelta(seconds=60)).timestamp()
        async with store._lock:
            await store._db.execute(
                "UPDATE verifications SET created_at = ? WHERE chat_id = ? AND user_id = ?",
                (old_ts, -100, 42),
            )
            await store._db.commit()
        await process_new_member(bot, store, settings, chat_id=-100, chat_title="测试群", member=member)
        assert len(bot.sent) == 2, f"窗口外重进应重发提示,实际 {len(bot.sent)} 条"
        await store.close()
    print("PASS: 入群双事件只发一次验证提示,窗口外重进正常重发")


def test_relauncher_wait_for_port_free():
    """Bug 修复:relauncher 能正确判断端口是否已释放(防 Windows 重启端口竞争)。"""
    import socket as _socket

    from app.relauncher import wait_for_port_free

    # 空闲端口:立即返回 True
    probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    assert wait_for_port_free(free_port, timeout=2.0, interval=0.1) is True

    # 占用中的端口:超时后返回 False,而不是盲目启动
    holder = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    holder.bind(("0.0.0.0", 0))
    busy_port = holder.getsockname()[1]
    try:
        start = datetime.now()
        assert wait_for_port_free(busy_port, timeout=0.8, interval=0.2) is False
        elapsed = (datetime.now() - start).total_seconds()
        assert elapsed >= 0.7, f"应等到超时,实际 {elapsed:.2f}s 就返回了"
    finally:
        holder.close()
    # 占用释放后:恢复可 bind
    assert wait_for_port_free(busy_port, timeout=2.0, interval=0.1) is True
    print("PASS: relauncher 端口探测(空闲即过/占用等待/释放后恢复)正确")


def test_restart_process_windows_uses_relauncher():
    """Bug 修复:Windows 裸跑重启必须经 relauncher 接力,守护场景直接退出。"""
    import types

    from app import updater

    spawned: list[list[str]] = []
    exited: list[int] = []

    def _fake_exit(code: int = 0):
        exited.append(code)
        raise SystemExit(code)  # 真实 os._exit 不会返回,用异常模拟进程终止

    real_os = updater.os
    real_spawn = updater._spawn_detached
    try:
        fake_os = types.SimpleNamespace(
            name="nt",
            getenv=lambda key, default=None: default,
            execv=lambda *a: (_ for _ in ()).throw(AssertionError("不应走 execv")),
            _exit=_fake_exit,
        )
        updater.os = fake_os
        updater._spawn_detached = spawned.append
        try:
            updater._restart_process(port=8000)
        except SystemExit:
            pass
        assert len(spawned) == 1 and exited == [0]
        cmd = spawned[0]
        assert any("relauncher.py" in part for part in cmd), cmd
        assert "--port" in cmd and "8000" in cmd, cmd
        assert "--" in cmd and cmd.index("--") < len(cmd) - 1, cmd
    finally:
        updater.os = real_os
        updater._spawn_detached = real_spawn
    print("PASS: Windows 裸跑重启经 relauncher 接力(含端口等待)")


def test_restart_process_windows_supervised_exits():
    """Bug 修复:Windows 守护场景直接 _exit,不启动接力器也不 execv。"""
    import types

    from app import updater

    spawned: list[list[str]] = []
    exited: list[int] = []

    def _fake_exit(code: int = 0):
        exited.append(code)
        raise SystemExit(code)

    real_os = updater.os
    real_spawn = updater._spawn_detached
    try:
        fake_os = types.SimpleNamespace(
            name="nt",
            getenv=lambda key, default=None: "1" if key == "INVOCATION_ID" else default,
            execv=lambda *a: (_ for _ in ()).throw(AssertionError("不应走 execv")),
            _exit=_fake_exit,
        )
        updater.os = fake_os
        updater._spawn_detached = spawned.append
        try:
            updater._restart_process(port=8000)
        except SystemExit:
            pass
        assert spawned == [] and exited == [0], (spawned, exited)
    finally:
        updater.os = real_os
        updater._spawn_detached = real_spawn
    print("PASS: Windows 守护场景重启直接退出交由守护进程拉起")


async def test_run_updater_task_guard():
    """Bug 修复:更新/回滚任务异常不再让 state 卡死导致 409 永久拒绝。"""
    from app.web import _run_updater_task

    # 异常路径:state 落为 failed 并带错误信息
    status_info = {"state": "pulling", "log": [], "error": None}

    async def _boom():
        raise RuntimeError("模拟 git 不存在 FileNotFoundError 之类的逃逸异常")

    await _run_updater_task(_boom(), status_info, "更新")
    assert status_info["state"] == "failed", status_info
    assert "更新任务内部错误" in (status_info["error"] or ""), status_info
    assert any("内部错误" in line for line in status_info["log"]), status_info

    # 正常路径:不干预业务自己的状态推进
    status_ok = {"state": "pulling", "log": [], "error": None}

    async def _fine():
        status_ok["state"] = "ready_to_restart"

    await _run_updater_task(_fine(), status_ok, "回滚")
    assert status_ok["state"] == "ready_to_restart" and status_ok["error"] is None
    print("PASS: 更新/回滚任务异常兜底(异常落 failed,正常不干预)")


async def main():
    await test_summarize_metrics_keyerror()
    await test_recent_events_default_30d()
    await test_ad_review_expire_cleanup()
    test_metrics_expose()
    test_keyword_rule_matching()
    test_keyword_config_load_and_hotreload()
    test_keyword_validate_payload()
    test_keyword_save_and_reload()
    await test_join_prompt_sent_once()
    test_relauncher_wait_for_port_free()
    test_restart_process_windows_uses_relauncher()
    test_restart_process_windows_supervised_exits()
    await test_run_updater_task_guard()
    print("\n全部回归验证通过")


if __name__ == "__main__":
    asyncio.run(main())
