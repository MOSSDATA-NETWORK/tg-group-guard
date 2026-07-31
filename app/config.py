from __future__ import annotations

import importlib
import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _maybe_load_dotenv() -> None:
    if importlib.util.find_spec("dotenv") is None:
        return
    dotenv = importlib.import_module("dotenv")
    dotenv.load_dotenv()  # type: ignore[attr-defined]


_maybe_load_dotenv()


@dataclass(slots=True)
class Settings:
    """应用运行时配置。"""

    bot_token: str
    bot_username: str
    verify_base_url: str
    web_host: str
    web_port: int
    database_path: Path
    verification_timeout_seconds: int
    cleanup_interval_seconds: int
    allowed_chat_ids: set[int]
    ssl_cert_file: Optional[Path]
    ssl_key_file: Optional[Path]
    ssl_ca_file: Optional[Path]
    message_ttl_seconds: Optional[int]
    ai_enabled: bool
    ad_guard_enabled: bool
    ad_guard_rules_file: Optional[Path]
    ad_guard_provider: str
    ad_guard_threshold: float
    ollama_endpoint: Optional[str]
    ollama_model: str
    ollama_timeout_seconds: int
    ad_guard_ban: bool
    openai_endpoint: Optional[str]
    openai_model: str
    openai_api_key: Optional[str]
    openai_timeout_seconds: int
    ad_guard_min_length: int
    log_level: str
    redis_url: str
    redis_score_prefix: str
    ad_guard_score_skip_threshold: int
    ad_guard_score_ban_threshold: int
    warn_limit: int
    ad_vote_duration_seconds: int
    telegram_proxy: Optional[str]
    enable_metrics: bool
    ad_guard_llm_concurrency: int
    admin_web_enabled: bool
    admin_session_ttl_seconds: int
    admin_max_sessions_per_user: int
    admin_rate_limit_per_min: int
    admin_behind_proxy: bool
    admin_auth_age_seconds: int
    keyword_reply_enabled: bool
    keyword_reply_rules_file: Optional[Path]
    keyword_reply_cooldown_seconds: int
    keyword_deletion_enabled: bool
    keyword_deletion_rules_file: Optional[Path]


def _mask_secret(value: Optional[str]) -> str:
    if not value:
        return "<unset>"
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def describe_effective_config(settings: "Settings") -> dict:
    """返回一份脱敏的 effective config 快照,供启动日志输出。

    所有 token / api_key 字段都会被掩码,不会泄露真实密钥。
    """
    return {
        "bot_username": settings.bot_username or "<unset>",
        "verify_base_url": settings.verify_base_url,
        "web": {"host": settings.web_host, "port": settings.web_port},
        "database_path": str(settings.database_path),
        "verification_timeout_seconds": settings.verification_timeout_seconds,
        "cleanup_interval_seconds": settings.cleanup_interval_seconds,
        "allowed_chat_ids": sorted(settings.allowed_chat_ids),
        "message_ttl_seconds": settings.message_ttl_seconds,
        "ai_enabled": settings.ai_enabled,
        "ad_guard": {
            "enabled": settings.ad_guard_enabled,
            "provider": settings.ad_guard_provider,
            "threshold": settings.ad_guard_threshold,
            "min_length": settings.ad_guard_min_length,
            "ban": settings.ad_guard_ban,
            "score_skip_threshold": settings.ad_guard_score_skip_threshold,
            "score_ban_threshold": settings.ad_guard_score_ban_threshold,
            "vote_duration_seconds": settings.ad_vote_duration_seconds,
            "llm_concurrency": settings.ad_guard_llm_concurrency,
            "rules_file": str(settings.ad_guard_rules_file) if settings.ad_guard_rules_file else None,
        },
        "ollama": {
            "endpoint": settings.ollama_endpoint or "<unset>",
            "model": settings.ollama_model,
            "timeout_seconds": settings.ollama_timeout_seconds,
        },
        "openai": {
            "endpoint": settings.openai_endpoint or "<unset>",
            "model": settings.openai_model,
            "api_key": _mask_secret(settings.openai_api_key),
            "timeout_seconds": settings.openai_timeout_seconds,
        },
        "redis": {
            "url": settings.redis_url,
            "score_prefix": settings.redis_score_prefix,
        },
        "telegram_proxy": "set" if settings.telegram_proxy else "<unset>",
        "ssl": {
            "cert_file": str(settings.ssl_cert_file) if settings.ssl_cert_file else None,
            "key_file": str(settings.ssl_key_file) if settings.ssl_key_file else None,
            "ca_file": str(settings.ssl_ca_file) if settings.ssl_ca_file else None,
        },
        "metrics_enabled": settings.enable_metrics,
        "keyword_reply": {
            "enabled": settings.keyword_reply_enabled,
            "rules_file": str(settings.keyword_reply_rules_file)
            if settings.keyword_reply_rules_file
            else None,
            "cooldown_seconds": settings.keyword_reply_cooldown_seconds,
        },
        "keyword_deletion": {
            "enabled": settings.keyword_deletion_enabled,
            "rules_file": str(settings.keyword_deletion_rules_file)
            if settings.keyword_deletion_rules_file
            else None,
        },
        "admin_web": {
            "enabled": settings.admin_web_enabled,
            "session_ttl_seconds": settings.admin_session_ttl_seconds,
            "max_sessions_per_user": settings.admin_max_sessions_per_user,
            "rate_limit_per_min": settings.admin_rate_limit_per_min,
            "behind_proxy": settings.admin_behind_proxy,
            "auth_age_seconds": settings.admin_auth_age_seconds,
        },
        "log_level": settings.log_level,
        "warn_limit": settings.warn_limit,
        "bot_token": _mask_secret(settings.bot_token),
    }


def _read_env(key: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(key, default)
    if required and (value is None or value.strip() == ""):
        raise RuntimeError(f"环境变量 {key} 未设置")
    if value is None:
        return ""
    return value


def _read_bool(value: str, default: bool = False) -> bool:
    if value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_allowed_chat_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError as exc:  # pragma: no cover - 配置错误时直接报错
            raise RuntimeError(f"ALLOWED_CHAT_IDS 包含无效的数字：{item}") from exc
    return ids


def _resolve_optional_path(raw: str) -> Optional[Path]:
    raw = raw.strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path.resolve(strict=False)


def load_settings() -> Settings:
    bot_token = _read_env("TELEGRAM_BOT_TOKEN", required=True)
    verify_base_url = _read_env("VERIFY_BASE_URL", "http://localhost:8000")
    web_host = _read_env("WEB_HOST", "0.0.0.0")
    web_port = int(_read_env("WEB_PORT", "8000"))
    database_path = Path(_read_env("DATABASE_PATH", "data/verifications.sqlite3")).resolve()
    verification_timeout_seconds = int(
        _read_env("VERIFICATION_TIMEOUT_SECONDS", "600")
    )
    cleanup_interval_seconds = int(
        _read_env("CLEANUP_INTERVAL_SECONDS", "300")
    )
    allowed_chat_ids = _parse_allowed_chat_ids(_read_env("ALLOWED_CHAT_IDS", ""))
    ssl_cert_file = _resolve_optional_path(_read_env("SSL_CERT_FILE", ""))
    ssl_key_file = _resolve_optional_path(_read_env("SSL_KEY_FILE", ""))
    ssl_ca_file = _resolve_optional_path(_read_env("SSL_CA_FILE", ""))
    message_ttl_raw = _read_env("MESSAGE_TTL_SECONDS", "")
    message_ttl_seconds = int(message_ttl_raw) if message_ttl_raw.strip() else None
    ai_enabled = _read_bool(_read_env("AI_ENABLED", "true"))
    ad_guard_enabled_raw = _read_bool(_read_env("AD_GUARD_ENABLED", "false"))
    ad_guard_rules_file_raw = _read_env("AD_GUARD_RULES_FILE", "config/ad_guard_rules.json")
    ad_guard_rules_file = _resolve_optional_path(ad_guard_rules_file_raw)
    ad_guard_provider = _read_env("AD_GUARD_PROVIDER", "ollama").strip().lower() or "ollama"
    # 兼容旧的 "hunyuan" 值,统一归一化为 "openai"
    if ad_guard_provider == "hunyuan":
        ad_guard_provider = "openai"
    if ad_guard_provider not in {"ollama", "openai"}:
        raise RuntimeError("AD_GUARD_PROVIDER 仅支持 'ollama' 或 'openai'")
    ad_guard_threshold = float(_read_env("AD_GUARD_THRESHOLD", "0.8"))
    # 兼容旧配置；广告检测已不再按长度跳过
    ad_guard_min_length = max(int(_read_env("AD_GUARD_MIN_LENGTH", "0")), 0)
    log_level = _read_env("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    ollama_endpoint = _read_env("OLLAMA_ENDPOINT", "") or None
    ollama_model = _read_env("OLLAMA_MODEL", "qwen3:0.6b")
    ollama_timeout_seconds_raw = int(_read_env("OLLAMA_TIMEOUT_SECONDS", "30"))
    ollama_timeout_seconds = max(ollama_timeout_seconds_raw, 30)
    ad_guard_ban = _read_bool(_read_env("AD_GUARD_BAN", "false"))
    # OpenAI 兼容端点;旧名 HUNYUAN_* 仍兼容读取,但优先用 OPENAI_*
    openai_endpoint = (
        _read_env("OPENAI_ENDPOINT", "") or _read_env("HUNYUAN_ENDPOINT", "")
    ) or None
    openai_model = (
        _read_env("OPENAI_MODEL", "") or _read_env("HUNYUAN_MODEL", "")
    ) or "gpt-4o-mini"
    openai_api_key = (
        _read_env("OPENAI_API_KEY", "") or _read_env("HUNYUAN_API_KEY", "")
    ) or None
    openai_timeout_seconds_raw_str = (
        _read_env("OPENAI_TIMEOUT_SECONDS", "")
        or _read_env("HUNYUAN_TIMEOUT_SECONDS", "30")
    )
    openai_timeout_seconds = max(int(openai_timeout_seconds_raw_str), 30)
    redis_url = _read_env("REDIS_URL", "redis://localhost:6379/0")
    redis_score_prefix = _read_env("REDIS_SCORE_PREFIX", "kkbot:adscore") or "kkbot:adscore"
    ad_guard_score_skip_threshold = int(_read_env("AD_GUARD_SCORE_SKIP_THRESHOLD", "3"))
    ad_guard_score_ban_threshold = int(_read_env("AD_GUARD_SCORE_BAN_THRESHOLD", "-10"))
    warn_limit = max(int(_read_env("WARN_LIMIT", "3")), 1)
    ad_vote_duration_seconds = max(int(_read_env("AD_VOTE_DURATION_SECONDS", "30")), 1)
    telegram_proxy_raw = _read_env("TELEGRAM_PROXY", "").strip()
    telegram_proxy = telegram_proxy_raw or None
    enable_metrics = _read_bool(_read_env("ENABLE_METRICS", "false"))
    ad_guard_llm_concurrency = max(int(_read_env("AD_GUARD_LLM_CONCURRENCY", "4")), 1)
    admin_web_enabled = _read_bool(_read_env("ADMIN_WEB_ENABLED", "true"), True)
    admin_session_ttl_seconds = max(int(_read_env("ADMIN_SESSION_TTL_SECONDS", "28800")), 300)
    admin_max_sessions_per_user = max(int(_read_env("ADMIN_MAX_SESSIONS_PER_USER", "5")), 1)
    admin_rate_limit_per_min = max(int(_read_env("ADMIN_RATE_LIMIT_PER_MIN", "60")), 5)
    admin_behind_proxy = _read_bool(_read_env("ADMIN_BEHIND_PROXY", "false"), False)
    admin_auth_age_seconds = max(int(_read_env("ADMIN_AUTH_AGE_SECONDS", "300")), 60)
    keyword_reply_enabled = _read_bool(_read_env("KEYWORD_REPLY_ENABLED", "false"))
    keyword_reply_rules_file = _resolve_optional_path(
        _read_env("KEYWORD_REPLY_RULES_FILE", "config/keyword_replies.json")
    )
    keyword_reply_cooldown_seconds = max(
        int(_read_env("KEYWORD_REPLY_COOLDOWN_SECONDS", "60")), 0
    )
    keyword_deletion_enabled = _read_bool(_read_env("KEYWORD_DELETION_ENABLED", "false"))
    keyword_deletion_rules_file = _resolve_optional_path(
        _read_env("KEYWORD_DELETION_RULES_FILE", "config/keyword_deletions.json")
    )

    database_path.parent.mkdir(parents=True, exist_ok=True)

    effective_ad_guard_enabled = False
    if ai_enabled and ad_guard_enabled_raw:
        if ad_guard_provider == "ollama":
            effective_ad_guard_enabled = bool(ollama_endpoint)
        else:
            effective_ad_guard_enabled = bool(openai_api_key)

    bot_username = _read_env("TELEGRAM_BOT_USERNAME", required=True).lstrip("@")

    # 显式配置校验:用户配置了 ad_guard_enabled=true 但缺少关键依赖时,
    # 启动阶段就 fail,避免运行时静默跳过让用户误以为在保护
    if ad_guard_enabled_raw and ai_enabled and not effective_ad_guard_enabled:
        missing = (
            "OLLAMA_ENDPOINT" if ad_guard_provider == "ollama" else "OPENAI_API_KEY"
        )
        raise RuntimeError(
            f"AD_GUARD_ENABLED=true 且 AI_ENABLED=true,但未配置 {missing},"
            f"广告守卫不会生效。请补齐配置或将 AD_GUARD_ENABLED 设为 false。"
        )

    return Settings(
        bot_token=bot_token,
        bot_username=bot_username,
        verify_base_url=verify_base_url.rstrip("/"),
        web_host=web_host,
        web_port=web_port,
        database_path=database_path,
        verification_timeout_seconds=verification_timeout_seconds,
        cleanup_interval_seconds=cleanup_interval_seconds,
        allowed_chat_ids=allowed_chat_ids,
        ssl_cert_file=ssl_cert_file,
        ssl_key_file=ssl_key_file,
        ssl_ca_file=ssl_ca_file,
        message_ttl_seconds=message_ttl_seconds,
        ai_enabled=ai_enabled,
        ad_guard_enabled=effective_ad_guard_enabled,
        ad_guard_rules_file=ad_guard_rules_file,
        ad_guard_provider=ad_guard_provider,
        ad_guard_threshold=ad_guard_threshold,
        ollama_endpoint=ollama_endpoint,
        ollama_model=ollama_model,
        ollama_timeout_seconds=ollama_timeout_seconds,
        ad_guard_ban=ad_guard_ban,
        openai_endpoint=openai_endpoint,
        openai_model=openai_model,
        openai_api_key=openai_api_key,
        openai_timeout_seconds=openai_timeout_seconds,
        ad_guard_min_length=ad_guard_min_length,
        log_level=log_level,
        redis_url=redis_url,
        redis_score_prefix=redis_score_prefix,
        ad_guard_score_skip_threshold=ad_guard_score_skip_threshold,
        ad_guard_score_ban_threshold=ad_guard_score_ban_threshold,
        warn_limit=warn_limit,
        ad_vote_duration_seconds=ad_vote_duration_seconds,
        telegram_proxy=telegram_proxy,
        enable_metrics=enable_metrics,
        ad_guard_llm_concurrency=ad_guard_llm_concurrency,
        admin_web_enabled=admin_web_enabled,
        admin_session_ttl_seconds=admin_session_ttl_seconds,
        admin_max_sessions_per_user=admin_max_sessions_per_user,
        admin_rate_limit_per_min=admin_rate_limit_per_min,
        admin_behind_proxy=admin_behind_proxy,
        admin_auth_age_seconds=admin_auth_age_seconds,
        keyword_reply_enabled=keyword_reply_enabled,
        keyword_reply_rules_file=keyword_reply_rules_file,
        keyword_reply_cooldown_seconds=keyword_reply_cooldown_seconds,
        keyword_deletion_enabled=keyword_deletion_enabled,
        keyword_deletion_rules_file=keyword_deletion_rules_file,
    )
