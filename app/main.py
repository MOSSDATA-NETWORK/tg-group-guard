from __future__ import annotations
import asyncio
import copy
from contextlib import suppress
import logging
import uvicorn
import redis.asyncio as aioredis
from redis.exceptions import RedisError
from uvicorn.config import LOGGING_CONFIG as UVICORN_LOGGING_CONFIG
from aiogram.exceptions import TelegramNetworkError

from .bot import create_bot, create_dispatcher
from .bot_commands import sync_bot_commands
from .bot_components.scoring import RedisDailyScoreManager
from .bot_components.verification import run_cleanup_scheduler
from .config import describe_effective_config, load_settings
from .instance_lock import ensure_single_instance
from .metrics import build_metrics
from .runtime_settings import apply_overrides
from .storage import VerificationStore
from .updater import check_latest_release, register_shutdown_hook
from .web import create_web_app


def _build_uvicorn_logging(level_name: str) -> dict:
    config = copy.deepcopy(UVICORN_LOGGING_CONFIG)
    root_cfg = config.get("root")
    if root_cfg is not None:
        root_cfg["level"] = level_name
    else:
        handlers = config.get("handlers", {})
        default_handler = "default" if "default" in handlers else None
        new_root = {"level": level_name}
        if default_handler is not None:
            new_root["handlers"] = [default_handler]
        config["root"] = new_root
    loggers_cfg = config.get("loggers", {})
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger_cfg = loggers_cfg.get(logger_name)
        if logger_cfg is not None:
            logger_cfg["level"] = level_name
    return config


logger = logging.getLogger(__name__)


async def main() -> None:
    settings = load_settings()
    # 应用管理后台保存的运行时配置覆盖（data/admin_overrides.json）
    skipped_overrides = apply_overrides(settings)
    if skipped_overrides:
        logger.warning("以下覆盖配置无效已被忽略：%s", ", ".join(skipped_overrides))
    level_name = settings.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format='%(asctime)s %(levelname)s [%(name)s] %(message)s')
    logging.getLogger().setLevel(level)

    # 启动时打印一份脱敏的 effective config,帮助排查"配了但不知道生效没"
    logger.info("启动配置快照：%s", describe_effective_config(settings))

    # 单实例锁:守护漏判/重启接力竞态导致双实例时,后到者直接退出
    ensure_single_instance(settings.database_path.parent / "bot.lock")

    if settings.message_ttl_seconds and settings.message_ttl_seconds > 0:
        logger.info("消息自动删除TTL=%s秒", settings.message_ttl_seconds)
    else:
        logger.info("消息自动删除TTL未启用或≤0，所有提示消息将保留")
    if settings.telegram_proxy:
        logger.info("已设置 TELEGRAM_PROXY，访问 Bot API 将经代理转发")
    store = VerificationStore(settings.database_path)
    await store.connect()
    redis_client = aioredis.from_url(settings.redis_url, encoding='utf-8', decode_responses=True)
    try:
        await redis_client.ping()
    except RedisError as exc:
        raise RuntimeError(f'无法连接 Redis: {exc}') from exc

    metrics = build_metrics(settings.enable_metrics)
    score_manager = RedisDailyScoreManager(
        redis_client, settings.redis_score_prefix, metrics=metrics
    )

    bot = create_bot(settings, store)
    await sync_bot_commands(bot)

    # 注册关闭钩子:os._exit/execv 重启或关停前限时执行,
    # 弥补直接终止绕过下方 finally 收尾的问题(降低数据丢失窗口)
    register_shutdown_hook(store.close)
    register_shutdown_hook(bot.session.close)
    register_shutdown_hook(redis_client.aclose)

    dispatcher = create_dispatcher(
        settings,
        store,
        score_manager,
        metrics=metrics,
    )
    web_app = create_web_app(settings, store, bot)
    web_app.state.redis_client = redis_client
    web_app.state.score_manager = score_manager
    web_app.state.metrics = metrics if settings.enable_metrics else None
    web_app.state.version_info = None
    web_app.state.update_status = {"state": "idle", "log": [], "error": None}

    async def run_update_checker() -> None:
        """启动后立即检查一次，之后按配置间隔定时与 GitHub Release 比对版本。"""
        while True:
            try:
                info = await check_latest_release()
                web_app.state.version_info = info
                if info.get("error"):
                    logger.warning("版本检查失败：%s", info["error"])
                elif info.get("update_available"):
                    logger.info(
                        "发现新版本：%s（当前 %s），可在管理后台「系统设置」页一键更新",
                        info["latest"],
                        info["current"],
                    )
            except Exception as exc:  # pragma: no cover - 防御性兜底
                logger.warning("版本检查异常：%r", exc)
            await asyncio.sleep(settings.update_check_interval_seconds)

    async def run_web_server(host: str) -> None:
        ssl_kwargs = {}
        if settings.ssl_cert_file and settings.ssl_key_file:
            ssl_kwargs['ssl_certfile'] = str(settings.ssl_cert_file)
            ssl_kwargs['ssl_keyfile'] = str(settings.ssl_key_file)
        elif settings.ssl_cert_file or settings.ssl_key_file:
            print('⚠️ SSL 配置不完整：SSL_CERT_FILE 与 SSL_KEY_FILE 需同时设置。服务将以纯 HTTP 运行。')
        if settings.ssl_ca_file:
            ssl_kwargs['ssl_ca_certs'] = str(settings.ssl_ca_file)
        uvicorn_log_level = settings.log_level.lower()
        if uvicorn_log_level not in {'critical', 'error', 'warning', 'info', 'debug', 'trace'}:
            uvicorn_log_level = 'info'
        config = uvicorn.Config(
            web_app,
            host=host,
            port=settings.web_port,
            loop='asyncio',
            log_level=uvicorn_log_level,
            log_config=_build_uvicorn_logging(level_name),
            **ssl_kwargs,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = False
        await server.serve()

    async def run_web() -> None:
        hosts = [settings.web_host]
        if settings.web_host in {'dual', '::'}:
            hosts = ['::', '0.0.0.0']
        tasks = [asyncio.create_task(run_web_server(host)) for host in hosts]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
    async def run_polling_with_retry() -> None:
        delay_seconds = 1.0
        max_delay_seconds = 120.0
        while True:
            try:
                web_app.state.polling_alive = True
                # 显式追加 edited_message,确保 TG 推送编辑事件给 bot
                allowed_updates = sorted(
                    set(dispatcher.resolve_used_update_types()) | {"edited_message"}
                )
                await dispatcher.start_polling(
                    bot,
                    allowed_updates=allowed_updates,
                )
                return
            except asyncio.CancelledError:
                raise
            except TelegramNetworkError as exc:
                web_app.state.polling_alive = False
                logger.warning(
                    "访问 Telegram API 网络失败，%.0f 秒后重试：%s",
                    delay_seconds,
                    exc,
                )
                await asyncio.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, max_delay_seconds)

    bot_task = asyncio.create_task(run_polling_with_retry())
    web_task = asyncio.create_task(run_web())
    cleanup_task = asyncio.create_task(
        run_cleanup_scheduler(bot, store, settings.cleanup_interval_seconds, metrics=metrics)
    )
    update_task = (
        asyncio.create_task(run_update_checker())
        if settings.update_check_enabled
        else None
    )
    try:
        await asyncio.gather(bot_task, web_task)
    except asyncio.CancelledError:
        pass
    finally:
        pass
    bot_task.cancel()
    web_task.cancel()
    cleanup_task.cancel()
    if update_task is not None:
        update_task.cancel()
    with suppress(asyncio.CancelledError):
        await bot_task
    with suppress(asyncio.CancelledError):
        await web_task
    with suppress(asyncio.CancelledError):
        await cleanup_task
    if update_task is not None:
        with suppress(asyncio.CancelledError):
            await update_task
    await store.close()
    await bot.session.close()
    await redis_client.aclose()
    with suppress(asyncio.CancelledError):
        await web_task
    with suppress(asyncio.CancelledError):
        await cleanup_task
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
