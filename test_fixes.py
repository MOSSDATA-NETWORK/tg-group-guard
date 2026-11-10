"""回归验证脚本：覆盖本次修复的关键路径。"""
import asyncio
import json
import os
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

    # 地址族映射与 uvicorn 绑定逻辑一致:dual/:: → v6+v4,IPv6 → 仅 v6,其他 → 仅 v4
    import socket as _s

    from app.relauncher import _probe_targets

    assert _probe_targets("0.0.0.0") == [(_s.AF_INET, "0.0.0.0")]
    dual = _probe_targets("dual")
    assert (_s.AF_INET6, "::") in dual and (_s.AF_INET, "0.0.0.0") in dual and len(dual) == 2
    assert _probe_targets("::") == dual or _probe_targets("::") == _probe_targets("dual")
    assert _probe_targets("::1") == [(_s.AF_INET6, "::1")]
    print("PASS: relauncher 端口探测(空闲即过/占用等待/释放后恢复/地址族映射)正确")


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
            updater._restart_process(port=8000, host="dual")
        except SystemExit:
            pass
        assert len(spawned) == 1 and exited == [0]
        cmd = spawned[0]
        assert any("relauncher.py" in part for part in cmd), cmd
        assert "--port" in cmd and "8000" in cmd, cmd
        assert "--host" in cmd and "dual" in cmd, cmd
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


async def test_lift_restrictions_never_raises():
    """#3: lift_restrictions 对任意异常(含非 TelegramBadRequest)返回 False,不逃逸。"""
    from types import SimpleNamespace as _SN

    from app.bot_components.verification import lift_restrictions

    class _Bot:
        async def restrict_chat_member(self, **kwargs):
            raise RuntimeError("模拟网络/服务端异常")

    record = _SN(chat_id=-100, user_id=42)
    assert await lift_restrictions(_Bot(), record) is False
    print("PASS: lift_restrictions 全异常兜底返回 False(记录保持 pending 可重试)")


async def test_shutdown_hooks_run_with_timeout():
    """#4: 关闭钩子逐个执行,慢钩子被限时取消,不阻塞退出。"""
    from app import updater

    flags: list[str] = []

    async def _fast():
        flags.append("fast")

    async def _slow():
        await asyncio.sleep(10)

    real_hooks = updater._SHUTDOWN_HOOKS
    updater._SHUTDOWN_HOOKS = [_fast, _slow]
    try:
        start = datetime.now()
        await updater._run_shutdown_hooks(timeout=0.3)
        elapsed = (datetime.now() - start).total_seconds()
        assert flags == ["fast"], flags
        assert elapsed < 3, f"慢钩子未被限时取消,耗时 {elapsed:.2f}s"
    finally:
        updater._SHUTDOWN_HOOKS = real_hooks
    print("PASS: 关闭钩子执行 + 超时取消正常")


async def test_join_lock_eviction_keeps_locked():
    """#5: _JOIN_LOCKS 清理只踢空闲锁,在用锁必须保留。"""
    from app.bot_components import verification as ver

    real_locks = ver._JOIN_LOCKS
    ver._JOIN_LOCKS = {}
    try:
        held = await ver._acquire_join_lock(-100, 1)
        await held.acquire()  # 处于 locked 状态
        for i in range(10001):
            await ver._acquire_join_lock(-100, 1000 + i)
        assert (-100, 1) in ver._JOIN_LOCKS, "在用锁被误踢"
        assert ver._JOIN_LOCKS[(-100, 1)] is held
        held.release()
    finally:
        ver._JOIN_LOCKS = real_locks
    print("PASS: 入群锁清理跳过在用锁")


def test_supervisor_detection_extended():
    """#6: PROCESS_SUPERVISED 显式标记 + Docker 容器特征识别。"""
    import types

    from app import updater

    real_os = updater.os
    try:
        def _fake(getenv_map, *, exists_dockerenv=False, pid=123):
            return types.SimpleNamespace(
                name="posix",
                getenv=lambda key, default=None: getenv_map.get(key, default),
                path=types.SimpleNamespace(
                    exists=lambda p: p == "/.dockerenv" and exists_dockerenv
                ),
                getpid=lambda: pid,
            )

        updater.os = _fake({})
        assert updater._is_under_process_supervisor() is False
        updater.os = _fake({"PROCESS_SUPERVISED": "true"})
        assert updater._is_under_process_supervisor() is True
        updater.os = _fake({}, exists_dockerenv=True)
        assert updater._is_under_process_supervisor() is True
        updater.os = _fake({}, pid=1)
        assert updater._is_under_process_supervisor() is True
        updater.os = _fake({"INVOCATION_ID": "abc"})
        assert updater._is_under_process_supervisor() is True
    finally:
        updater.os = real_os
    print("PASS: 守护检测(PROCESS_SUPERVISED/.dockerenv/PID1/systemd)正确")


def test_single_instance_lock():
    """#6: 内核级文件锁互斥;持锁文件拒绝二次加锁;fd 关闭(模拟进程死亡)后可重新获取。"""
    from app import instance_lock as il

    with tempfile.TemporaryDirectory() as tmp:
        f1, f2 = Path(tmp) / "a.lock", Path(tmp) / "b.lock"
        il.ensure_single_instance(f1)  # 首次加锁成功
        fd1 = il._LOCK_FD
        assert fd1 is not None
        # 同一文件再次加锁(模拟第二实例) → SystemExit(3)
        try:
            il.ensure_single_instance(f1)
            raise AssertionError("持锁文件再次加锁应退出")
        except SystemExit as exc:
            assert exc.code == 3
        # 不同文件不受影响
        il.ensure_single_instance(f2)
        os.close(il._LOCK_FD)
        # 模拟进程死亡:关闭持锁 fd,内核放锁 → 可重新获取(无陈旧锁/PID 复用问题)
        os.close(fd1)
        il._LOCK_FD = None
        il.ensure_single_instance(f1)
        os.close(il._LOCK_FD)
        il._LOCK_FD = None
    print("PASS: 单实例内核文件锁(互斥/死亡即释放/无 PID 复用误判)正确")


def test_remove_stale_code():
    """#8: tarball 更新后删除"旧有新无"的 .py 及其 __pycache__,其他文件不动。"""
    import zipfile as _zf

    from app.updater import _remove_stale_code

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "src"
        (src / "app").mkdir(parents=True)
        (src / "app" / "x.py").write_text("x=1", encoding="utf-8")
        (src / "app" / "y.py").write_text("y=1", encoding="utf-8")

        snapshot = root / "snapshot.zip"
        with _zf.ZipFile(snapshot, "w") as zf:
            zf.writestr("app/x.py", "old-x")      # 新旧都有 → 保留
            zf.writestr("app/legacy.py", "old")   # 旧有新无 → 应删除
            zf.writestr("app/readme.txt", "txt")  # 非 .py → 不处理
            zf.writestr("../evil.py", "evil")     # 越界路径 → 跳过且不得删除

        dest = root / "dest"
        (dest / "app" / "__pycache__").mkdir(parents=True)
        (dest / "app" / "x.py").write_text("x=1", encoding="utf-8")
        (dest / "app" / "legacy.py").write_text("old", encoding="utf-8")
        (dest / "app" / "__pycache__" / "legacy.cpython-312.pyc").write_bytes(b"pyc")
        outside = root / "evil.py"  # dest_root 之外的同名文件,绝不能被动到
        outside.write_text("keep-me", encoding="utf-8")

        log: list[str] = []
        removed = _remove_stale_code(snapshot, src, log, dest_root=dest)
        assert removed == 1, (removed, log)
        assert not (dest / "app" / "legacy.py").exists()
        assert not (dest / "app" / "__pycache__" / "legacy.cpython-312.pyc").exists()
        assert outside.read_text(encoding="utf-8") == "keep-me"
        assert any("越界" in line for line in log), log
        assert (dest / "app" / "x.py").exists()
        assert any("清理 1 个" in line for line in log), log
    print("PASS: 废弃代码文件清理(.py + __pycache__,其他不动)正确")


async def test_regex_timeout_circuit_breaker():
    """#10: regex 引擎免疫经典灾难模式;超时熔断与禁用名单逻辑正确。"""
    import re as _re

    from app import keyword_replies as kw

    assert kw._regex_engine is not None, "测试环境需安装 regex 库"

    # ① regex 引擎对经典灾难模式微秒级返回(标准库 re 同输入需秒级且指数恶化)
    bad = kw.KeywordRule(reply="x", pattern=kw._regex_engine.compile(r"(a+)+$"))
    start = datetime.now()
    assert await kw.safe_rule_match(bad, "a" * 28 + "!") is False
    assert (datetime.now() - start).total_seconds() < 0.5, "regex 引擎应免疫经典灾难模式"

    # ② 超时被熔断:用假引擎模拟 TimeoutError,验证禁用名单逻辑
    class _FakePattern:
        pattern = "fake-redos"

        def search(self, text, timeout=None):
            raise TimeoutError()

    class _FakeEngine:
        Pattern = _FakePattern

    real_engine = kw._regex_engine
    kw._regex_engine = _FakeEngine
    try:
        rule = kw.KeywordRule(reply="x", pattern=_FakePattern())
        assert await kw.safe_rule_match(rule, "aaa", timeout=0.01) is False
        assert "fake-redos" in kw._DISABLED_PATTERNS
        # 熔断后立即返回,不再执行匹配
        start = datetime.now()
        assert await kw.safe_rule_match(rule, "aaa", timeout=0.01) is False
        assert (datetime.now() - start).total_seconds() < 0.1
    finally:
        kw._regex_engine = real_engine
        kw._DISABLED_PATTERNS.discard("fake-redos")

    # ③ 正常正则与纯关键词规则不受影响
    good = kw.KeywordRule(reply="x", pattern=kw._regex_engine.compile(r"hello"))
    assert await kw.safe_rule_match(good, "say hello") is True
    kw_rule = kw.KeywordRule(reply="x", keywords=("hi",))
    assert await kw.safe_rule_match(kw_rule, "hi there") is True
    print("PASS: regex 引擎免疫灾难模式 + 超时熔断 + 正常规则不受影响")


async def test_admin_group_requires_whitelist():
    """#1: 配置白名单时,非授权群管理员不能使用 admin 指令;未配置为开放模式。"""
    from types import SimpleNamespace as _SN

    from app.bot_components.permissions import is_authorized_admin

    async def _get_member(chat_id, user_id):
        return _SN(status="administrator")

    bot = _SN(get_chat_member=_get_member)
    settings = _SN(allowed_chat_ids={-100})
    msg_unauth = _SN(from_user=_SN(id=1), chat=_SN(id=-200, type="supergroup"))
    msg_auth = _SN(from_user=_SN(id=1), chat=_SN(id=-100, type="supergroup"))
    assert await is_authorized_admin(bot, settings, msg_unauth) is False
    assert await is_authorized_admin(bot, settings, msg_auth) is True
    # 开放模式(白名单为空):与 verify.py 入群处理语义一致,任意群管理员可用
    open_settings = _SN(allowed_chat_ids=set())
    assert await is_authorized_admin(bot, open_settings, msg_unauth) is True
    print("PASS: admin 指令群聊白名单校验(非授权拒绝/授权放行/空白名单开放)")


async def test_hourly_distribution_joined_only():
    """#2: 24h 进群分布只统计 joined 事件,不再被终态事件虚增。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = VerificationStore(Path(tmp) / "t.sqlite3")
        await store.connect()
        now = datetime.now(tz=timezone.utc)
        await store.record_verification_event(
            chat_id=-100, user_id=1, username="a", event="joined", created_at=now
        )
        await store.record_verification_event(
            chat_id=-100, user_id=2, username="b", event="joined", created_at=now
        )
        await store.record_verification_event(
            chat_id=-100, user_id=1, username="a", event="verified", created_at=now
        )
        await store.record_verification_event(
            chat_id=-100, user_id=2, username="b", event="expired", created_at=now
        )
        data = await store.summarize_metrics({-100})
        assert sum(data["hourly"]) == 2, data["hourly"]
        await store.close()
    print("PASS: 24h 进群分布只计 joined(4 条事件 → 分布合计 2)")


async def test_low_score_ban_failure_notice():
    """#3: 低分封禁失败时公告如实说明,不再谎称"已移出并拉黑"。"""
    from types import SimpleNamespace as _SN

    from aiogram.exceptions import TelegramBadRequest

    from app.bot_components.moderation import handle_low_score_violation

    sent: list[str] = []

    class _Bot:
        async def delete_message(self, *a, **k):
            return True

        async def ban_chat_member(self, *a, **k):
            raise TelegramBadRequest(method=None, message="not enough rights")

        async def send_message(self, chat_id, text, **kwargs):
            sent.append(text)
            return _SN(message_id=1, chat=_SN(id=chat_id))

    class _ScoreMgr:
        async def adjust_score(self, chat_id, user_id, delta):
            return -1

        async def reset_score(self, chat_id, user_id):
            raise AssertionError("封禁失败不应重置评分")

    message = _SN(
        from_user=_SN(id=42, full_name="User", username="u"),
        chat=_SN(id=-100),
        message_id=7,
    )
    settings = _SN(
        ad_guard_ban=True,
        ad_guard_score_ban_threshold=-3,
        message_ttl_seconds=None,
    )
    await handle_low_score_violation(
        _Bot(), message, settings=settings, score_manager=_ScoreMgr(), current_score=0, store=None
    )
    assert len(sent) == 1, sent
    assert "移除失败" in sent[0], sent[0]
    assert "已移出并拉黑" not in sent[0], sent[0]
    print("PASS: 低分封禁失败公告如实(不含虚假的『已移出并拉黑』)")


async def test_ttl_deletion_persistence_and_restore():
    """#4/#5: TTL 删除任务持久化存取 + 重启恢复(到期立即删 + 任务持强引用)。"""
    from types import SimpleNamespace as _SN

    from app.bot_components import messaging as msg

    with tempfile.TemporaryDirectory() as tmp:
        store = VerificationStore(Path(tmp) / "t.sqlite3")
        await store.connect()
        now = datetime.now(tz=timezone.utc)
        # 存取往返
        await store.schedule_message_deletion(-100, 1, now)
        await store.schedule_message_deletion(-100, 2, now + timedelta(hours=1))
        rows = await store.fetch_scheduled_deletions()
        assert len(rows) == 2, rows
        await store.remove_scheduled_deletion(-100, 1)
        assert len(await store.fetch_scheduled_deletions()) == 1

        deleted: list[tuple[int, int]] = []

        class _Bot:
            async def delete_message(self, chat_id, message_id, **k):
                deleted.append((chat_id, message_id))
                return True

        msg.configure_messaging_store(store)
        try:
            # 遗留一条已到期的任务,恢复后应立即删除并销记录
            await store.schedule_message_deletion(-100, 99, now - timedelta(seconds=5))
            restored = await msg.restore_scheduled_deletions(_Bot())
            assert restored == 2, restored  # 1 条一小时后 + 1 条已到期
            await asyncio.sleep(0.3)
            assert (-100, 99) in deleted, deleted
            remaining = await store.fetch_scheduled_deletions()
            assert len(remaining) == 1 and remaining[0]["message_id"] == 2, remaining
            assert len(msg._TTL_TASKS) >= 1, "未到期任务应持有强引用"
        finally:
            for task in list(msg._TTL_TASKS):
                task.cancel()
            msg._TTL_TASKS.clear()
            msg.configure_messaging_store(None)
        await store.close()
    print("PASS: TTL 删除持久化(存取/到期即删/记录清理/强引用)正确")


def test_ad_guard_enabled_recompute():
    """#2: ad_guard_enabled 有效值随意图 + AI 开关 + 端点/密钥统一重算。"""
    from types import SimpleNamespace as _SN

    from app.runtime_settings import _FIELD_BY_KEY, _apply_field

    settings = _SN(
        ai_enabled=True,
        ad_guard_enabled=True,
        ad_guard_enabled_intent=True,
        ad_guard_provider="ollama",
        ollama_endpoint="http://127.0.0.1:11434",
        openai_api_key=None,
    )

    def apply(key, raw):
        _apply_field(settings, _FIELD_BY_KEY[key], raw)

    # AI 关→开:有效值应随意图恢复(修复前永远卡在 False)
    apply("AI_ENABLED", "false")
    assert settings.ad_guard_enabled is False
    apply("AI_ENABLED", "true")
    assert settings.ad_guard_enabled is True, "AI 重新开启后广告守卫未恢复"

    # 清空端点应立即关闭;补回应恢复(修复前不重算)
    apply("OLLAMA_ENDPOINT", "")
    assert settings.ad_guard_enabled is False, "清空端点后仍显示开启"
    apply("OLLAMA_ENDPOINT", "http://127.0.0.1:11434")
    assert settings.ad_guard_enabled is True, "补回端点后未恢复"

    # 切 provider 到 openai 且无 key 应关闭;配上 key 应开启
    apply("AD_GUARD_PROVIDER", "openai")
    assert settings.ad_guard_enabled is False
    apply("OPENAI_API_KEY", "sk-test")
    assert settings.ad_guard_enabled is True

    # 意图关闭时即使端点齐备也关闭;意图恢复后重算
    apply("AD_GUARD_ENABLED", "false")
    assert settings.ad_guard_enabled is False
    assert settings.ad_guard_enabled_intent is False
    apply("AD_GUARD_ENABLED", "true")
    assert settings.ad_guard_enabled is True
    print("PASS: ad_guard_enabled 意图/有效值统一重算(AI 开关/端点/密钥/provider 全路径)")


def test_rules_file_path_hot_switch():
    """#3: 修改规则文件路径后,规则缓存立即切换到新文件。"""
    from types import SimpleNamespace as _SN

    from app.ad_guard_rules import configure_ad_guard_rules, get_heuristic_pattern
    from app.runtime_settings import apply_hot_values

    with tempfile.TemporaryDirectory() as tmp:
        f1 = Path(tmp) / "rules_a.json"
        f2 = Path(tmp) / "rules_b.json"
        f1.write_text(json.dumps({"pattern": "aaa_unique_one"}), encoding="utf-8")
        f2.write_text(json.dumps({"pattern": "bbb_unique_two"}), encoding="utf-8")

        settings = _SN(ad_guard_rules_file=None)
        try:
            apply_hot_values(settings, {"AD_GUARD_RULES_FILE": str(f1)})
            p1 = get_heuristic_pattern()
            assert p1 is not None and "aaa_unique_one" in p1.pattern

            apply_hot_values(settings, {"AD_GUARD_RULES_FILE": str(f2)})
            p2 = get_heuristic_pattern()
            assert p2 is not None and "bbb_unique_two" in p2.pattern, "改路径后规则缓存未切换"
        finally:
            configure_ad_guard_rules(None)
    print("PASS: 规则文件路径修改后热生效(缓存即时切换)")


def test_heuristic_rules_signature_reload():
    """#6+#7: 同秒二次保存可检测 / 坏文件不阻断修复后加载 / 启发式正则有超时防护。"""
    import time

    from app.ad_guard_rules import (
        configure_ad_guard_rules,
        get_heuristic_pattern,
        heuristic_pattern_search,
    )

    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "rules.json"
        f.write_text(json.dumps({"pattern": "sig_v1_token"}), encoding="utf-8")
        try:
            configure_ad_guard_rules(f)
            assert get_heuristic_pattern().pattern == "sig_v1_token"

            # 同一秒内二次保存(长度不同)也必须被检测为变更(修复前 mtime<= 直接跳过)
            f.write_text(json.dumps({"pattern": "sig_v2_token_longer"}), encoding="utf-8")
            assert get_heuristic_pattern().pattern == "sig_v2_token_longer", "同秒二次保存未触发重载"

            # 坏 JSON 不推进加载签名,沿用旧规则;修复后(签名变化)立即恢复加载
            f.write_text("{broken json", encoding="utf-8")
            p = get_heuristic_pattern()
            assert p is not None and p.pattern == "sig_v2_token_longer", "坏文件应沿用旧规则"
            f.write_text(json.dumps({"pattern": "sig_v3_fixed"}), encoding="utf-8")
            assert get_heuristic_pattern().pattern == "sig_v3_fixed", "修复后未重新加载"

            # 灾难性 pattern 搜索必须快速返回,不卡事件循环
            f.write_text(json.dumps({"pattern": r"(a+)+$"}), encoding="utf-8")
            pattern = get_heuristic_pattern()
            start = time.monotonic()
            result = heuristic_pattern_search(pattern, "a" * 5000 + "b")
            elapsed = time.monotonic() - start
            assert result is False and elapsed < 2.0, f"灾难性 pattern 未在时限内返回: {elapsed:.2f}s"
        finally:
            configure_ad_guard_rules(None)
    print("PASS: 规则热重载签名检测(同秒变更/坏文件恢复) + 启发式正则超时防护")


def test_message_history_lru_cap():
    """#5: 消息历史字典有界,超限按 LRU 淘汰,访问提升为最近使用。"""
    from app.bot_components import history as history_mod

    original = history_mod._message_histories
    history_mod._message_histories = history_mod.OrderedDict()
    try:
        cap = history_mod.MAX_HISTORY_CHATS
        for chat_id in range(cap + 10):
            history_mod.get_message_history(chat_id)
        assert len(history_mod._message_histories) == cap, "超出上限未淘汰"
        assert 0 not in history_mod._message_histories, "最早的条目未被 LRU 淘汰"
        assert (cap + 9) in history_mod._message_histories
        # 访问后提升为最近使用,再插入新条目时先淘汰其他旧条目
        history_mod.get_message_history(20)
        history_mod.get_message_history(cap + 100)
        assert 20 in history_mod._message_histories, "LRU 访问提升失败"
        assert len(history_mod._message_histories) == cap
    finally:
        history_mod._message_histories = original
    print("PASS: 消息历史字典 LRU 上限(超限淘汰/访问提升/有界)")


async def test_cleanup_serialized_with_admin_via_token_lock():
    """#1: 清理器与管理员操作经 token 锁串行;管理员先放行(删记录)后清理器不得再封。"""
    from app.bot_components.verification import cleanup_expired_records
    from app.storage import VerificationRecord

    store = VerificationStore(Path(tempfile.mkdtemp()) / "t.db")
    now = datetime.now(tz=timezone.utc)
    token = "tok_race"
    await store.create(
        VerificationRecord(
            token=token,
            chat_id=-100,
            user_id=42,
            username="u",
            status="pending",
            created_at=now - timedelta(hours=2),
            expire_at=now - timedelta(hours=1),
            verified_at=None,
            prompt_message_id=None,
        )
    )

    class _Bot:
        ban_calls = 0

        async def ban_chat_member(self, **kwargs):
            type(self).ban_calls += 1

        async def unban_chat_member(self, **kwargs):
            pass

    # 外部持有 token 锁,模拟管理员回调正在处理
    held = await store.acquire_token_lock(token)
    await held.acquire()
    task = asyncio.create_task(cleanup_expired_records(_Bot(), store))
    await asyncio.sleep(0.2)
    assert not task.done(), "清理器未等待 token 锁"
    # 管理员放行:删除记录
    await store.delete(token)
    held.release()
    await store.release_token_lock(token)
    await asyncio.wait_for(task, timeout=5)
    assert _Bot.ban_calls == 0, "管理员放行后清理器仍执行了封禁"
    assert await store.get(token) is None
    await store.close()
    print("PASS: 清理器与管理员操作 token 锁串行(放行后不再封禁)")


async def test_openai_empty_endpoint_uses_official():
    """#1: openai provider 端点留空时默认官方地址,不再每条消息静默跳过。"""
    from types import SimpleNamespace as _SN

    from app.bot_components import ad_guard as ag

    captured: dict[str, str] = {}

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self):
            return {"choices": [{"message": {"content": '{"advertisement": false, "confidence": 0.1}'}}]}

        async def text(self):
            return ""

    class _Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            captured["url"] = url
            return _Resp()

    real = ag.aiohttp
    ag.aiohttp = _SN(ClientSession=_Session, ClientTimeout=lambda **kw: None, ClientError=Exception)
    try:
        settings = _SN(
            openai_api_key="sk-x",
            openai_endpoint=None,
            openai_model="gpt-4o-mini",
            openai_timeout_seconds=30,
            ad_guard_threshold=0.5,
        )
        flagged, _conf = await ag._check_advertisement_openai("hello", settings)
        assert captured.get("url") == "https://api.openai.com/chat/completions", captured
        assert flagged is False

        # 自定义端点行为不变
        settings.openai_endpoint = "https://oneapi.example.com/"
        await ag._check_advertisement_openai("hello", settings)
        assert captured["url"] == "https://oneapi.example.com/chat/completions"
    finally:
        ag.aiohttp = real
    print("PASS: openai provider 端点留空默认官方地址(自定义端点不受影响)")


def test_heuristic_timeout_circuit_breaker():
    """#3: 启发式正则超时一次即永久熔断,后续调用不再执行灾难 pattern。"""
    from types import SimpleNamespace as _SN

    from app import ad_guard_rules as agr

    calls = {"n": 0}

    class _FakePattern:
        pattern = "(evil)+$"

        def search(self, text, timeout=None):
            calls["n"] += 1
            raise TimeoutError()

    real_engine = agr._regex_engine
    agr._regex_engine = _SN(Pattern=_FakePattern)
    try:
        p = _FakePattern()
        assert agr.heuristic_pattern_search(p, "x" * 100) is False
        assert p.pattern in agr._DISABLED_PATTERNS
        assert agr.heuristic_pattern_search(p, "x" * 100) is False
        assert calls["n"] == 1, "熔断后仍在重复执行灾难 pattern"
    finally:
        agr._regex_engine = real_engine
        agr._DISABLED_PATTERNS.discard("(evil)+$")
    print("PASS: 启发式正则超时熔断(一次超时永久禁用,零持续开销)")


def test_settings_api_exposes_ad_guard_effective():
    """#2: 设置 API 对广告守卫开关同时暴露意图值与实际生效状态。"""
    from types import SimpleNamespace as _SN

    from app.runtime_settings import FIELDS, describe_for_api

    settings = _SN(**{f.attr: None for f in FIELDS})
    settings.allowed_chat_ids = set()
    settings.ad_guard_enabled_intent = True
    settings.ad_guard_enabled = False

    data = describe_for_api(settings)
    item = next(
        f for g in data["groups"] for f in g["fields"] if f["key"] == "AD_GUARD_ENABLED"
    )
    assert item["value"] == "true", "开关应显示管理员意图"
    assert item["effective"] is False, "API 应额外暴露实际生效状态"
    print("PASS: 设置 API 暴露 ad_guard 意图与实际生效状态")


def test_settings_save_has_no_group_notifications():
    """全局与按群配置保存只写日志，不向授权群发送通知。"""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/web.py").read_text(encoding="utf-8"))
    targets = {"admin_api_settings_put", "admin_api_chat_settings_put"}
    found = set()
    notify_calls = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in targets:
            found.add(node.name)
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "notify_admins"
                ):
                    notify_calls.append(node.name)
    assert found == targets, "未找到两个配置保存端点"
    assert not notify_calls, f"配置保存仍会通知群组: {notify_calls}"
    print("PASS: 全局/按群配置保存均不发送群通知")


def test_extract_media_payload():
    """#1: 媒体快照提取(取最大尺寸/各类型/无媒体)。"""
    from types import SimpleNamespace as _SN

    from app.routers.ad_guard import _extract_media_payload

    base = dict(photo=None, video=None, document=None, animation=None, audio=None, voice=None)
    msg = _SN(**{**base, "photo": [_SN(file_id="small"), _SN(file_id="big")]})
    assert _extract_media_payload(msg) == ("photo", "big"), "应取最大尺寸照片"
    msg = _SN(**{**base, "video": _SN(file_id="v1")})
    assert _extract_media_payload(msg) == ("video", "v1")
    msg = _SN(**{**base, "document": _SN(file_id="d1")})
    assert _extract_media_payload(msg) == ("document", "d1")
    msg = _SN(**{**base, "animation": _SN(file_id="g1")})
    assert _extract_media_payload(msg) == ("animation", "g1")
    msg = _SN(**base)
    assert _extract_media_payload(msg) == (None, None)
    print("PASS: 媒体快照提取(最大尺寸/多类型/无媒体)正确")


async def test_prompt_record_failure_still_schedules_ttl():
    """#3: set_prompt_message 写库失败时,TTL 删除仍被调度,提示消息不成孤儿。"""
    from types import SimpleNamespace as _SN

    from app.bot_components import messaging as msg

    spawned: list[tuple] = []

    class _Store:
        async def set_prompt_message(self, token, message_id):
            raise RuntimeError("database is locked")

    class _Bot:
        async def send_message(self, chat_id, text, **kwargs):
            return _SN(message_id=555, chat=_SN(id=chat_id))

    real_spawn = msg._spawn_delete_task
    msg._spawn_delete_task = lambda bot, chat_id, message_id, delay: spawned.append(
        (chat_id, message_id, delay)
    )
    try:
        await msg.send_message_with_ttl(
            _Bot(), -100, "验证提示", ttl=60, store=_Store(), token="tok", delete_mode="record"
        )
        assert spawned == [(-100, 555, 60)], f"写库失败导致 TTL 未调度: {spawned}"
    finally:
        msg._spawn_delete_task = real_spawn
    print("PASS: 验证提示写库失败仍调度 TTL 删除(不成孤儿)")


async def test_low_score_ban_failure_rollback_and_cooldown():
    """#4: ban 失败回滚扣分;冷却期内静默删除,不再扣分/封禁/公告。"""
    from types import SimpleNamespace as _SN

    from aiogram.exceptions import TelegramBadRequest

    from app.bot_components import moderation as mod

    sent: list[str] = []

    class _Score:
        def __init__(self):
            self.score = -10
            self.calls: list[int] = []

        async def adjust_score(self, chat_id, user_id, delta):
            self.calls.append(delta)
            self.score += delta
            return self.score

        async def reset_score(self, chat_id, user_id):
            raise AssertionError("封禁失败不应重置评分")

    class _Bot:
        def __init__(self):
            self.ban_attempts = 0
            self.deleted: list[int] = []

        async def delete_message(self, chat_id, message_id, **k):
            self.deleted.append(message_id)
            return True

        async def ban_chat_member(self, *a, **k):
            self.ban_attempts += 1
            raise TelegramBadRequest(method=None, message="not enough rights")

        async def send_message(self, chat_id, text, **kwargs):
            sent.append(text)
            return _SN(message_id=1, chat=_SN(id=chat_id))

    message = _SN(
        from_user=_SN(id=777, full_name="U", username=None),
        chat=_SN(id=-200),
        message_id=7,
    )
    settings = _SN(ad_guard_ban=True, ad_guard_score_ban_threshold=-3, message_ttl_seconds=None)

    mod._ban_failure_cooldown.clear()
    try:
        score, bot = _Score(), _Bot()
        await mod.handle_low_score_violation(
            bot, message, settings=settings, score_manager=score, current_score=-10
        )
        assert score.calls == [-1, 1], f"ban 失败未回滚扣分: {score.calls}"
        assert score.score == -10, "回滚后评分应回到原值"
        assert bot.ban_attempts == 1 and len(sent) == 1

        # 冷却期内再次触发:静默删除,不再扣分/封禁/公告
        await mod.handle_low_score_violation(
            bot, message, settings=settings, score_manager=score, current_score=-10
        )
        assert bot.ban_attempts == 1, "冷却期内重复尝试封禁"
        assert score.calls == [-1, 1], "冷却期内重复扣分"
        assert len(sent) == 1, "冷却期内重复公告"
        assert bot.deleted == [7, 7], "冷却期内仍应删除消息保持禁言效果"
    finally:
        mod._ban_failure_cooldown.clear()
    print("PASS: 低分 ban 失败回滚扣分 + 冷却防刷屏(静默删除保留)")


def test_bot_message_ad_scan_gate():
    """bot 广告识别:其他 bot 消息默认纳入检测,自身豁免,开关可回退旧行为。"""
    from types import SimpleNamespace as _SN

    from app.routers.ad_guard import _should_skip_bot_message

    human = _SN(from_user=_SN(id=1001, is_bot=False))
    own_bot = _SN(from_user=_SN(id=999, is_bot=True))
    other_bot = _SN(from_user=_SN(id=555, is_bot=True))
    anonymous = _SN(from_user=None)

    # 其他 bot:开启时纳入检测(不跳过),关闭时跳过(旧行为)
    assert _should_skip_bot_message(other_bot, 999, True) is False, "bot 广告未被纳入检测"
    assert _should_skip_bot_message(other_bot, 999, False) is True
    # 自身始终豁免
    assert _should_skip_bot_message(own_bot, 999, True) is True
    assert _should_skip_bot_message(own_bot, 999, False) is True
    # 人类/匿名不受该门控影响
    assert _should_skip_bot_message(human, 999, True) is False
    assert _should_skip_bot_message(anonymous, 999, True) is False

    # 配置默认值:Settings 新字段存在且默认 true
    import inspect

    from app.config import Settings

    assert "ad_guard_include_bots" in inspect.signature(Settings).parameters
    print("PASS: bot 消息广告检测门控(纳入/自身豁免/开关回退)正确")


def test_channel_sender_chat_block_gate():
    """频道马甲开关:开启拦截频道身份,关闭放行,自动转发始终放行。"""
    from types import SimpleNamespace as _SN

    from app.routers.ad_guard import _should_block_channel_sender_chat

    channel_message = _SN(sender_chat=_SN(type="channel"), is_automatic_forward=False)
    auto_forward = _SN(sender_chat=_SN(type="channel"), is_automatic_forward=True)
    group_identity = _SN(sender_chat=_SN(type="supergroup"), is_automatic_forward=False)
    personal_message = _SN(sender_chat=None, is_automatic_forward=False)

    assert _should_block_channel_sender_chat(channel_message, True) is True
    assert _should_block_channel_sender_chat(channel_message, False) is False
    assert _should_block_channel_sender_chat(auto_forward, True) is False
    assert _should_block_channel_sender_chat(group_identity, True) is False
    assert _should_block_channel_sender_chat(personal_message, True) is False
    print("PASS: 禁止频道马甲发言开关(开关/自动转发/身份类型)正确")


def test_registered_command_gate():
    """仅已注册命令豁免,未知斜杠前缀不得绕过广告检测。"""
    from app.routers.ad_guard import _is_registered_command_text

    assert _is_registered_command_text("/start verify_abc") is True
    assert _is_registered_command_text("/ID@my_bot") is True
    assert _is_registered_command_text("/news 推广链接和返利") is False
    assert _is_registered_command_text("/id_extra 广告") is False
    assert _is_registered_command_text("普通文本") is False
    print("PASS: 斜杠命令白名单(未知命令不豁免)正确")


def test_short_message_valid_speech_gate():
    """短消息跳过 LLM 或编辑消息时,均不得累计免检资格。"""
    from app.routers.ad_guard import _should_count_valid_speech

    assert _should_count_valid_speech(count_valid_speech=True, short_message_skipped=False) is True
    assert _should_count_valid_speech(count_valid_speech=True, short_message_skipped=True) is False
    assert _should_count_valid_speech(count_valid_speech=False, short_message_skipped=False) is False
    print("PASS: 短消息不累计免检资格门控正确")


def test_extract_message_ad_text():
    """提取 text_link、Caption Entities、按钮 URL、投票与清单文本。"""
    from types import SimpleNamespace as _SN

    from app.routers.ad_guard import _extract_message_ad_text

    def message(**overrides):
        data = dict(
            text=None,
            caption=None,
            entities=None,
            caption_entities=None,
            reply_markup=None,
            poll=None,
            checklist=None,
        )
        data.update(overrides)
        return _SN(**data)

    markup = _SN(inline_keyboard=[[
        _SN(url="https://button.example", login_url=None, web_app=None),
        _SN(url=None, login_url=_SN(url="https://login.example"), web_app=None),
        _SN(url=None, login_url=None, web_app=_SN(url="https://app.example")),
    ]])
    msg = message(
        text="点击查看",
        entities=[_SN(url="https://hidden.example")],
        reply_markup=markup,
    )
    text = _extract_message_ad_text(msg)
    for expected in (
        "点击查看",
        "https://hidden.example",
        "https://button.example",
        "https://login.example",
        "https://app.example",
    ):
        assert expected in text

    caption_msg = message(
        caption="图片说明",
        caption_entities=[_SN(url="https://caption.example")],
    )
    assert _extract_message_ad_text(caption_msg) == "图片说明\nhttps://caption.example"

    poll = _SN(
        question="投票问题",
        question_entities=[_SN(url="https://poll.example")],
        options=[_SN(text="选项一", text_entities=None), _SN(text="选项二", text_entities=None)],
        explanation="投票解释",
        explanation_entities=None,
    )
    poll_text = _extract_message_ad_text(message(poll=poll))
    assert poll_text.splitlines() == [
        "投票问题", "https://poll.example", "选项一", "选项二", "投票解释"
    ]

    checklist = _SN(
        title="清单标题",
        title_entities=None,
        tasks=[_SN(text="任务一", text_entities=None), _SN(text="任务二", text_entities=None)],
    )
    assert _extract_message_ad_text(message(checklist=checklist)).splitlines() == [
        "清单标题", "任务一", "任务二"
    ]

    related = message(
        text="当前正文",
        quote=_SN(text="引用内容", entities=[_SN(url="https://quote.example")]),
        reply_to_message=message(
            caption="回复图片说明",
            caption_entities=[_SN(url="https://reply.example")],
            poll=poll,
        ),
        external_reply=_SN(checklist=checklist),
    )
    related_text = _extract_message_ad_text(related)
    for expected in (
        "当前正文",
        "引用内容",
        "https://quote.example",
        "回复图片说明",
        "https://reply.example",
        "投票问题",
        "清单标题",
        "任务二",
    ):
        assert expected in related_text, (expected, related_text)

    from app.routers.ad_guard import _is_user_forward_message

    assert _is_user_forward_message(message(forward_origin=_SN(type="user"))) is True
    assert _is_user_forward_message(message(forward_origin=None)) is False
    print("PASS: 隐藏链接/按钮/投票/清单/引用回复广告文本提取正确")


def test_message_behavior_guard():
    """重复与连续短消息均不拦截；仅保留发送频率、窗口过期与 0 关闭语义。"""
    from app.bot_components.behavior import MessageBehaviorGuard

    def check(guard, now, **overrides):
        kwargs = dict(
            chat_id=-100,
            user_id=42,
            rate_limit=0,
            rate_window_seconds=60,
            now=now,
        )
        kwargs.update(overrides)
        return guard.check(**kwargs)

    guard = MessageBehaviorGuard()
    for index in range(20):
        assert check(guard, index) is None, "重复消息不得再触发删除"

    guard.clear()
    for index in range(20):
        assert check(guard, index) is None, "连续短消息不得再触发删除"

    guard.clear()
    for index in range(15):
        assert check(guard, index, rate_limit=15) is None
    assert check(guard, 15, rate_limit=15) == "rate"

    guard.clear()
    assert check(guard, 0, rate_limit=2, rate_window_seconds=10) is None
    assert check(guard, 1, rate_limit=2, rate_window_seconds=10) is None
    assert check(guard, 2, rate_limit=2, rate_window_seconds=10) == "rate"
    assert check(guard, 12, rate_limit=2, rate_window_seconds=10) is None
    print("PASS: 重复/短消息不拦截，发送频率阈值与窗口过期语义正确")


async def test_multimodal_provider_payloads():
    """OpenAI 与 Ollama 图片均按各自协议编码，纯文本请求保持原格式。"""
    from types import SimpleNamespace as _SN

    from app.bot_components import ad_guard as ag

    captured = []

    class _Resp:
        status = 200

        def __init__(self, ollama):
            self.ollama = ollama

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self):
            if self.ollama:
                return {"response": '{"advertisement": false, "confidence": 0.1}'}
            return {"choices": [{"message": {"content": '{"advertisement": false, "confidence": 0.1}'}}]}

        async def text(self):
            return ""

    class _Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            captured.append((url, json))
            return _Resp(url.endswith("/api/generate"))

    settings = _SN(
        openai_api_key="sk-x",
        openai_endpoint="https://openai.example",
        openai_model="gpt-4o-mini",
        openai_timeout_seconds=30,
        ollama_endpoint="http://ollama.example",
        ollama_model="qwen-vl",
        ollama_timeout_seconds=30,
        ad_guard_threshold=0.5,
    )
    real = ag.aiohttp
    ag.aiohttp = _SN(ClientSession=_Session, ClientTimeout=lambda **kw: None, ClientError=Exception)
    try:
        await ag._check_advertisement_openai("检查图片", settings, image=b"\x00\x01")
        await ag._check_advertisement_ollama("检查图片", settings, image=b"\x00\x01")
        await ag._check_advertisement_openai("纯文本", settings)
    finally:
        ag.aiohttp = real

    openai_content = captured[0][1]["messages"][0]["content"]
    assert openai_content[0] == {"type": "text", "text": ag.render_prompt_message("检查图片")}
    assert openai_content[1]["image_url"]["url"] == "data:image/jpeg;base64,AAE="
    assert captured[1][1]["images"] == ["AAE="]
    assert isinstance(captured[2][1]["messages"][0]["content"], str)
    print("PASS: OpenAI/Ollama 多模态图片 payload 与纯文本兼容格式正确")


async def test_image_download_limits():
    """图片预检、下载后大小限制及正常下载。"""
    import io
    from types import SimpleNamespace as _SN

    from app.routers.ad_guard import _MAX_IMAGE_BYTES, _download_message_image

    class _Bot:
        def __init__(self, data=b"image"):
            self.data = data
            self.get_file_calls = 0

        async def get_file(self, file_id):
            self.get_file_calls += 1
            return _SN(file_path="photos/image.jpg", file_size=None)

        async def download_file(self, file_path):
            return io.BytesIO(self.data)

    oversized = _SN(message_id=1, photo=[_SN(file_id="big", file_size=_MAX_IMAGE_BYTES + 1)])
    bot = _Bot()
    assert await _download_message_image(bot, oversized) is None
    assert bot.get_file_calls == 0

    normal = _SN(message_id=2, photo=[_SN(file_id="ok", file_size=None)])
    assert await _download_message_image(bot, normal) == b"image"

    bot_too_large = _Bot(data=b"x" * (_MAX_IMAGE_BYTES + 1))
    assert await _download_message_image(bot_too_large, normal) is None
    print("PASS: 图片下载预检/下载后 10 MiB 限制与正常读取正确")


async def test_special_message_policies():
    """特殊消息 off/new_member/all，以及合格用户和管理员豁免。"""
    from types import SimpleNamespace as _SN

    from app.routers.ad_guard import _enforce_special_message_policy, _special_message_policy

    class _Store:
        valid_count = 0
        qualified = False

        async def get_ad_qualification(self, chat_id, user_id):
            return self.valid_count, self.qualified

    class _Bot:
        def __init__(self):
            self.deleted = []
            self.admin = False

        async def get_chat_member(self, chat_id, user_id):
            return _SN(status="administrator" if self.admin else "member")

        async def delete_message(self, chat_id, message_id):
            self.deleted.append(message_id)

        async def send_message(self, chat_id, text, **kwargs):
            return _SN(message_id=999, chat=_SN(id=chat_id))

    settings = _SN(
        allowed_chat_ids=set(),
        contact_message_policy="off",
        story_message_policy="off",
        voice_message_policy="off",
        ad_guard_score_skip_threshold=3,
        message_ttl_seconds=None,
    )
    message = _SN(
        chat=_SN(id=-100, type="group"),
        from_user=_SN(id=42),
        message_id=1,
        contact=_SN(phone_number="123"),
        story=None,
        voice=None,
        audio=None,
    )
    store, bot = _Store(), _Bot()
    assert _special_message_policy(message, settings) == ("contact_message_policy", "off")
    assert await _enforce_special_message_policy(bot, message, settings, store) is False

    settings.contact_message_policy = "new_member"
    assert await _enforce_special_message_policy(bot, message, settings, store) is True
    assert bot.deleted == [1]
    store.valid_count, store.qualified = 3, True
    assert await _enforce_special_message_policy(bot, message, settings, store) is False

    settings.contact_message_policy = "all"
    assert await _enforce_special_message_policy(bot, message, settings, store) is True
    assert bot.deleted == [1, 1]
    bot.admin = True
    assert await _enforce_special_message_policy(bot, message, settings, store) is False
    assert bot.deleted == [1, 1]

    message.contact = None
    message.voice = _SN(file_id="voice")
    assert _special_message_policy(message, settings) == ("voice_message_policy", "off")
    print("PASS: 联系人/Story/语音策略及新成员、合格用户、管理员豁免正确")


async def test_media_poll_and_special_handlers_reach_pipeline():
    """真实路由：无 Caption 图片和投票进入检测，类型策略在 AI 关闭时仍生效。"""
    import io
    from types import SimpleNamespace as _SN

    from app.routers import ad_guard as router_module
    from app.state.message_history import MessageHistoryStore

    class _Store:
        async def get_pending(self, chat_id, user_id):
            return None

        async def get_ad_qualification(self, chat_id, user_id):
            return 0, False

        async def record_ad_valid_speech(self, **kwargs):
            return 1, False

        async def record_ad_decision(self, **kwargs):
            return 1

    class _Score:
        async def get_score(self, chat_id, user_id):
            return 0

    class _Pipeline:
        def __init__(self):
            self.calls = []

        async def check(self, content, *, message=None, image=None):
            self.calls.append((content, image))
            return False, 0.1

    class _Bot:
        id = 999

        def __init__(self):
            self.deleted = []

        async def get_chat_member(self, chat_id, user_id):
            return _SN(status="member")

        async def get_file(self, file_id):
            return _SN(file_path="photos/image.jpg", file_size=5)

        async def download_file(self, file_path):
            return io.BytesIO(b"image")

        async def delete_message(self, chat_id, message_id):
            self.deleted.append(message_id)

        async def send_message(self, chat_id, text, **kwargs):
            return _SN(message_id=999, chat=_SN(id=chat_id))

    settings = _SN(
        allowed_chat_ids=set(),
        ad_guard_enabled=True,
        ad_guard_image_enabled=True,
        ad_guard_include_bots=True,
        ad_guard_block_sender_chat=False,
        ad_guard_ban_sender_chat=False,
        ad_guard_message_rate_limit=0,
        ad_guard_message_rate_window_seconds=60,
        ad_guard_min_length=20,
        ad_guard_score_skip_threshold=3,
        ad_guard_score_ban_threshold=-10,
        contact_message_policy="off",
        story_message_policy="off",
        voice_message_policy="off",
        message_ttl_seconds=None,
    )
    pipeline = _Pipeline()
    services = _SN(
        settings=settings,
        store=_Store(),
        score_manager=_Score(),
        history_store=MessageHistoryStore(),
        ad_review_store=None,
        ad_vote_store=None,
        metrics=None,
    )
    router = router_module.build_ad_guard_router(services, pipeline)
    handlers = {handler.callback.__name__: handler.callback for handler in router.message.handlers}
    handle_non_text = handlers["handle_non_text_messages"]

    def message(message_id, **overrides):
        data = dict(
            chat=_SN(id=-100, type="group"),
            from_user=_SN(id=42, full_name="User", username=None, is_bot=False),
            sender_chat=None,
            is_automatic_forward=False,
            message_id=message_id,
            photo=None,
            poll=None,
            contact=None,
            story=None,
            voice=None,
            audio=None,
        )
        data.update(overrides)
        return _SN(**data)

    bot = _Bot()
    router_module._BEHAVIOR_GUARD.clear()
    try:
        await handle_non_text(
            message(1, photo=[_SN(file_id="photo", file_size=5)]), bot
        )
        assert len(pipeline.calls) == 1
        assert "图片消息，无文字说明" in pipeline.calls[0][0]
        assert pipeline.calls[0][1] == b"image"

        poll_question = "本周日晚上我们一起到市中心聚餐吗大家觉得怎么样"
        poll = _SN(
            question=poll_question,
            question_entities=None,
            options=[_SN(text="参加", text_entities=None)],
            explanation=None,
            explanation_entities=None,
        )
        await handle_non_text(message(2, poll=poll), bot)
        assert len(pipeline.calls) == 2
        assert poll_question in pipeline.calls[1][0]

        # 四张无 Caption 图片不能被共用占位文本误判成重复消息。
        for message_id in range(10, 14):
            await handle_non_text(
                message(message_id, photo=[_SN(file_id=f"photo-{message_id}", file_size=5)]),
                bot,
            )
        assert len(pipeline.calls) == 6
        assert not bot.deleted

        settings.ad_guard_image_enabled = False
        await handle_non_text(
            message(3, photo=[_SN(file_id="photo", file_size=5)]), bot
        )
        assert len(pipeline.calls) == 6

        settings.ad_guard_enabled = False
        settings.contact_message_policy = "all"
        await handle_non_text(message(4, contact=_SN(phone_number="123")), bot)
        assert bot.deleted == [4]
        assert len(pipeline.calls) == 6
    finally:
        router_module._BEHAVIOR_GUARD.clear()
    print("PASS: 无 Caption 图片/相册/投票真实路由及 AI 关闭时特殊消息策略正确")


async def test_sync_unban_to_linked_channel():
    """手动解封同步到关联频道，遵循开关且频道异常不影响群组解封。"""
    from types import SimpleNamespace as _SN

    from aiogram.exceptions import TelegramBadRequest

    from app.bot_components import linked

    sync_unban = getattr(linked, "sync_unban_to_linked_channel", None)
    assert callable(sync_unban), "缺少关联频道同步解封函数"

    class _Bot:
        def __init__(self, linked_id=-300, fail=False):
            self.linked_id = linked_id
            self.fail = fail
            self.get_chat_calls = []
            self.unbans = []

        async def get_chat(self, chat_id):
            self.get_chat_calls.append(chat_id)
            return _SN(linked_chat_id=self.linked_id)

        async def unban_chat_member(self, chat_id, user_id, only_if_banned=False):
            if self.fail:
                raise TelegramBadRequest(method=None, message="not enough rights")
            self.unbans.append((chat_id, user_id, only_if_banned))

    linked.configure_linked_sync(True)
    try:
        bot = _Bot()
        await sync_unban(bot, -100, 42)
        assert bot.get_chat_calls == [-100]
        assert bot.unbans == [(-300, 42, False)]

        bot = _Bot(linked_id=None)
        await sync_unban(bot, -100, 42)
        assert not bot.unbans

        bot = _Bot(fail=True)
        await sync_unban(bot, -100, 42)  # 频道无权限不得外抛

        linked.configure_linked_sync(False)
        bot = _Bot()
        await sync_unban(bot, -100, 42)
        assert not bot.get_chat_calls and not bot.unbans
    finally:
        linked.configure_linked_sync(True)
    print("PASS: 关联频道同步解封(开关/无频道/异常静默)正确")


async def test_unban_entrypoints_sync_linked_channel():
    """群指令与 Admin API 两个手动解封入口都同步解除关联频道封禁。"""
    from types import SimpleNamespace as _SN

    from app.bot_components import linked
    from app.routers import admin_cmds
    from app import web

    assert callable(getattr(linked, "sync_unban_to_linked_channel", None))

    operator = _SN(id=7, full_name="Admin", username="admin")
    target = _SN(id=42, full_name="Target", username="target")

    class _Bot:
        def __init__(self):
            self.unbans = []

        async def get_chat_member(self, chat_id, user_id):
            if user_id == operator.id:
                return _SN(status="administrator", user=operator)
            return _SN(status="member", user=target)

        async def get_chat(self, chat_id):
            return _SN(linked_chat_id=-300)

        async def unban_chat_member(self, chat_id, user_id, only_if_banned=False):
            self.unbans.append((chat_id, user_id, only_if_banned))

        async def send_message(self, chat_id, text, **kwargs):
            return _SN(message_id=999, chat=_SN(id=chat_id))

    class _Store:
        async def record_ban_event(self, **kwargs):
            return 1

        async def mark_unbanned(self, *args, **kwargs):
            return 1

        async def reset_ad_qualification(self, chat_id, user_id):
            return None

    settings = _SN(allowed_chat_ids=set(), message_ttl_seconds=None)
    services = _SN(settings=settings, store=_Store(), score_manager=_SN())
    router = admin_cmds.build_admin_commands_router(services)
    handlers = {handler.callback.__name__: handler.callback for handler in router.message.handlers}
    message = _SN(
        chat=_SN(id=-100, type="group"),
        from_user=operator,
        message_id=1,
        text="/unban",
        reply_to_message=_SN(from_user=target),
    )
    bot = _Bot()
    await handlers["handle_unban_command"](message, bot)
    assert bot.unbans == [(-100, 42, False), (-300, 42, False)], bot.unbans

    app = web.create_web_app(_SN(admin_behind_proxy=False), _Store(), bot := _Bot())
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/admin/api/unban"
    )
    session = {
        "admin_chat_ids": [-100],
        "user_id": str(operator.id),
        "name": operator.full_name,
    }

    class _Request:
        async def json(self):
            return {"chat_id": -100, "user_id": 42}

    async def fake_require_session(request, api=False):
        return session

    async def fake_admin_chat_ids(bot, chat_ids, user_id):
        return set(chat_ids)

    original_session = web._require_admin_session
    original_csrf = web._require_csrf
    original_admin_ids = web._admin_chat_ids_for_user
    web._require_admin_session = fake_require_session
    web._require_csrf = lambda request, current_session: None
    web._admin_chat_ids_for_user = fake_admin_chat_ids
    try:
        response = await endpoint(_Request())
    finally:
        web._require_admin_session = original_session
        web._require_csrf = original_csrf
        web._admin_chat_ids_for_user = original_admin_ids
    assert response.status_code == 200
    assert bot.unbans == [(-100, 42, False), (-300, 42, False)], bot.unbans
    print("PASS: /unban 与 Admin API 均同步解除群组和关联频道封禁")


async def test_sync_ban_to_linked_channel():
    """关联频道同步封禁:有频道同步/踢出语义/临时时长/无频道跳过/异常静默。"""
    from types import SimpleNamespace as _SN

    from aiogram.exceptions import TelegramBadRequest

    from app.bot_components.linked import sync_ban_to_linked_channel

    class _Bot:
        def __init__(self, linked_id, fail_ban=False):
            self.linked_id = linked_id
            self.fail_ban = fail_ban
            self.bans: list[tuple] = []
            self.unbans: list[tuple] = []

        async def get_chat(self, chat_id):
            return _SN(linked_chat_id=self.linked_id)

        async def ban_chat_member(self, chat_id, user_id, until_date=None, **kw):
            if self.fail_ban:
                raise TelegramBadRequest(method=None, message="not enough rights")
            self.bans.append((chat_id, user_id, until_date))

        async def unban_chat_member(self, chat_id, user_id, only_if_banned=False):
            self.unbans.append((chat_id, user_id))

    # 有频道:永久封禁同步
    bot = _Bot(linked_id=-300)
    await sync_ban_to_linked_channel(bot, -100, 42)
    assert bot.bans == [(-300, 42, None)] and not bot.unbans

    # 踢出语义:频道侧同样 ban+unban
    bot = _Bot(linked_id=-300)
    await sync_ban_to_linked_channel(bot, -100, 42, unban_after=True)
    assert bot.bans == [(-300, 42, None)] and bot.unbans == [(-300, 42)]

    # 临时封禁:时长透传
    from datetime import datetime, timezone

    until = datetime.now(tz=timezone.utc)
    bot = _Bot(linked_id=-300)
    await sync_ban_to_linked_channel(bot, -100, 42, until_date=until)
    assert bot.bans == [(-300, 42, until)]

    # 无关联频道:不调用任何封禁
    bot = _Bot(linked_id=None)
    await sync_ban_to_linked_channel(bot, -100, 42)
    assert not bot.bans and not bot.unbans

    # 频道侧失败:静默处理不外抛
    bot = _Bot(linked_id=-300, fail_ban=True)
    await sync_ban_to_linked_channel(bot, -100, 42)  # 不应抛异常
    print("PASS: 关联频道同步封禁(同步/踢出/时长/无频道/异常静默)正确")


async def test_linked_sync_toggle():
    """关联频道同步开关:关闭时不访问群信息,开启后恢复同步。"""
    from types import SimpleNamespace as _SN

    from app.bot_components.linked import configure_linked_sync, sync_ban_to_linked_channel

    class _Bot:
        def __init__(self):
            self.get_chat_calls: list[int] = []
            self.bans: list[tuple] = []

        async def get_chat(self, chat_id):
            self.get_chat_calls.append(chat_id)
            return _SN(linked_chat_id=-300)

        async def ban_chat_member(self, chat_id, user_id, until_date=None, **kw):
            self.bans.append((chat_id, user_id, until_date))

    try:
        configure_linked_sync(False)
        bot = _Bot()
        await sync_ban_to_linked_channel(bot, -100, 42)
        assert not bot.get_chat_calls and not bot.bans

        configure_linked_sync(True)
        await sync_ban_to_linked_channel(bot, -100, 42)
        assert bot.get_chat_calls == [-100]
        assert bot.bans == [(-300, 42, None)]
    finally:
        configure_linked_sync(True)
    print("PASS: 关联频道同步封禁开关(关闭跳过/开启恢复)正确")


async def test_ban_sender_chat_quiet():
    """频道身份封禁:成功调用 + 异常静默不阻断。"""
    from aiogram.exceptions import TelegramBadRequest

    from app.routers.ad_guard import _ban_sender_chat_quiet

    calls: list[tuple] = []

    class _Bot:
        def __init__(self, fail=False):
            self.fail = fail

        async def ban_sender_chat(self, chat_id, sender_chat_id):
            if self.fail:
                raise TelegramBadRequest(method=None, message="not enough rights")
            calls.append((chat_id, sender_chat_id))

    await _ban_sender_chat_quiet(_Bot(), -100, -500)
    assert calls == [(-100, -500)]
    await _ban_sender_chat_quiet(_Bot(fail=True), -100, -500)  # 不应抛异常
    print("PASS: 频道身份 ban_sender_chat(成功/异常静默)正确")


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
    await test_lift_restrictions_never_raises()
    await test_shutdown_hooks_run_with_timeout()
    await test_join_lock_eviction_keeps_locked()
    test_supervisor_detection_extended()
    test_single_instance_lock()
    test_remove_stale_code()
    await test_regex_timeout_circuit_breaker()
    await test_admin_group_requires_whitelist()
    await test_hourly_distribution_joined_only()
    await test_low_score_ban_failure_notice()
    await test_ttl_deletion_persistence_and_restore()
    test_ad_guard_enabled_recompute()
    test_rules_file_path_hot_switch()
    test_heuristic_rules_signature_reload()
    test_message_history_lru_cap()
    await test_cleanup_serialized_with_admin_via_token_lock()
    await test_openai_empty_endpoint_uses_official()
    test_heuristic_timeout_circuit_breaker()
    test_settings_api_exposes_ad_guard_effective()
    test_settings_save_has_no_group_notifications()
    test_extract_media_payload()
    await test_prompt_record_failure_still_schedules_ttl()
    await test_low_score_ban_failure_rollback_and_cooldown()
    test_bot_message_ad_scan_gate()
    test_channel_sender_chat_block_gate()
    test_registered_command_gate()
    test_short_message_valid_speech_gate()
    test_extract_message_ad_text()
    test_message_behavior_guard()
    await test_multimodal_provider_payloads()
    await test_image_download_limits()
    await test_special_message_policies()
    await test_media_poll_and_special_handlers_reach_pipeline()
    await test_sync_unban_to_linked_channel()
    await test_unban_entrypoints_sync_linked_channel()
    await test_sync_ban_to_linked_channel()
    await test_linked_sync_toggle()
    await test_ban_sender_chat_quiet()
    print("\n全部回归验证通过")


if __name__ == "__main__":
    asyncio.run(main())
