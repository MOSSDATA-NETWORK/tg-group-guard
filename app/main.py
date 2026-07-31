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
from .bot_components.scoring import RedisDailyScoreManager
from .bot_components.verification import run_cleanup_scheduler
from .config import describe_effective_config, load_settings
from .metrics import build_metrics
from .storage import VerificationStore
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
    level_name = settings.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format='%(asctime)s %(levelname)s [%(name)s] %(message)s')
    logging.getLogger().setLevel(level)

    # 启动时打印一份脱敏的 effective config,帮助排查"配了但不知道生效没"
    logger.info("启动配置快照：%s", describe_effective_config(settings))

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
    score_manager = RedisDailyScoreManager(redis_client, settings.redis_score_prefix)

    metrics = build_metrics(settings.enable_metrics)

    bot = create_bot(settings, store)
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
    cleanup_task = asyncio.create_task(run_cleanup_scheduler(bot, store, settings.cleanup_interval_seconds))
    try:
        await asyncio.gather(bot_task, web_task)
    except asyncio.CancelledError:
        pass
    finally:
        pass
    bot_task.cancel()
    web_task.cancel()
    cleanup_task.cancel()
    with suppress(asyncio.CancelledError):
        await bot_task
    with suppress(asyncio.CancelledError):
        await web_task
    with suppress(asyncio.CancelledError):
        await cleanup_task
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