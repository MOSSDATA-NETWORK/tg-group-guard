"""管理后台「系统设置」的运行时配置覆盖层。

设计：
- 启动时 config.load_settings() 先从环境变量 / .env 构建 Settings，
  然后调用 apply_overrides() 把 data/admin_overrides.json 中的覆盖值套上去。
- 管理后台保存时写入同一个 JSON，并把"热生效"字段直接应用到运行中的
  Settings 对象（各组件在使用时才读属性，改动立即生效）；
  标注 restart=True 的字段只落盘，下次重启才生效。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .config import Settings

logger = logging.getLogger(__name__)

OVERRIDES_PATH = Path("data/admin_overrides.json")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    key: str            # 环境变量名（覆盖文件里的键）
    attr: str           # Settings 属性名
    kind: str           # bool / int / float / str / secret / chat_ids / select
    group: str          # 设置页分组名
    label: str          # 中文名称
    help: str           # 中文说明
    restart: bool       # True = 需重启生效
    choices: tuple[str, ...] = ()
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    nullable: bool = False  # True = 允许留空（表示不启用/不设置）


GROUP_ORDER = (
    "入群验证",
    "广告守卫",
    "关键词功能",
    "Admin 后台",
    "Web 服务",
    "存储",
    "网络与代理",
    "可观测性",
    "核心凭证",
)

FIELDS: tuple[FieldSpec, ...] = (
    # ===== 入群验证 =====
    FieldSpec("VERIFY_BASE_URL", "verify_base_url", "str", "入群验证",
              "验证页面基址", "生成 /verify 链接使用的外网可达地址，例如 https://example.com", False),
    FieldSpec("VERIFICATION_TIMEOUT_SECONDS", "verification_timeout_seconds", "int", "入群验证",
              "验证有效期（秒）", "入群验证 token 的有效时长", False, min_value=30),
    FieldSpec("CLEANUP_INTERVAL_SECONDS", "cleanup_interval_seconds", "int", "入群验证",
              "过期清理间隔（秒）", "清理过期验证记录的后台任务间隔", True, min_value=30),
    FieldSpec("ALLOWED_CHAT_IDS", "allowed_chat_ids", "chat_ids", "入群验证",
              "授权群 ID 列表", "逗号分隔的群 ID；留空表示不限制", False, nullable=True),
    FieldSpec("MESSAGE_TTL_SECONDS", "message_ttl_seconds", "int", "入群验证",
              "提示消息自动删除（秒）", "留空或 0 表示不自动删除群内提示消息", False, nullable=True, min_value=0),
    FieldSpec("WARN_LIMIT", "warn_limit", "int", "入群验证",
              "警告封禁阈值", "本月累计警告达到该次数后自动封禁", False, min_value=1),
    # ===== 广告守卫 =====
    FieldSpec("AI_ENABLED", "ai_enabled", "bool", "广告守卫",
              "AI 总开关", "关闭后所有依赖 LLM 的检测都不再执行", False),
    FieldSpec("AD_GUARD_ENABLED", "ad_guard_enabled", "bool", "广告守卫",
              "广告守卫开关", "开启需同时配置对应的 LLM 端点/密钥，否则保存后不生效", False),
    FieldSpec("AD_GUARD_PROVIDER", "ad_guard_provider", "select", "广告守卫",
              "LLM 提供方", "ollama = 本地模型；openai = OpenAI 兼容端点", False,
              choices=("ollama", "openai")),
    FieldSpec("AD_GUARD_THRESHOLD", "ad_guard_threshold", "float", "广告守卫",
              "判定置信度阈值", "0~1，越高越保守（漏判多），越低越激进（误判多）", False,
              min_value=0.0, max_value=1.0),
    FieldSpec("AD_GUARD_BAN", "ad_guard_ban", "bool", "广告守卫",
              "命中广告即封禁", "关闭时命中广告只踢出不封禁", False),
    FieldSpec("AD_GUARD_SCORE_SKIP_THRESHOLD", "ad_guard_score_skip_threshold", "int", "广告守卫",
              "免检通过次数", "新成员通过广告检测达到该次数后永久免检", False, min_value=1),
    FieldSpec("AD_GUARD_SCORE_BAN_THRESHOLD", "ad_guard_score_ban_threshold", "int", "广告守卫",
              "评分封禁阈值", "违规评分低于等于该值时触发低分封禁", False),
    FieldSpec("AD_VOTE_DURATION_SECONDS", "ad_vote_duration_seconds", "int", "广告守卫",
              "广告投票时长（秒）", "群成员投票判定广告的持续时间", False, min_value=1),
    FieldSpec("AD_GUARD_LLM_CONCURRENCY", "ad_guard_llm_concurrency", "int", "广告守卫",
              "LLM 并发上限", "防止并发请求打爆本地模型服务", True, min_value=1),
    FieldSpec("OLLAMA_ENDPOINT", "ollama_endpoint", "str", "广告守卫",
              "Ollama 端点", "例如 http://127.0.0.1:11434；留空则 ollama 提供方不可用", False, nullable=True),
    FieldSpec("OLLAMA_MODEL", "ollama_model", "str", "广告守卫",
              "Ollama 模型", "例如 qwen3:0.6b", False),
    FieldSpec("OLLAMA_TIMEOUT_SECONDS", "ollama_timeout_seconds", "int", "广告守卫",
              "Ollama 超时（秒）", "实际下限为 30 秒", False, min_value=30),
    FieldSpec("OPENAI_ENDPOINT", "openai_endpoint", "str", "广告守卫",
              "OpenAI 兼容端点", "适用于混元 / OneAPI / vLLM 等 OpenAI 协议端点，可留空用官方", False, nullable=True),
    FieldSpec("OPENAI_MODEL", "openai_model", "str", "广告守卫",
              "OpenAI 模型", "例如 gpt-4o-mini", False),
    FieldSpec("OPENAI_API_KEY", "openai_api_key", "secret", "广告守卫",
              "OpenAI API Key", "留空表示保持现有密钥不变", False, nullable=True),
    FieldSpec("OPENAI_TIMEOUT_SECONDS", "openai_timeout_seconds", "int", "广告守卫",
              "OpenAI 超时（秒）", "实际下限为 30 秒", False, min_value=30),
    FieldSpec("AD_GUARD_RULES_FILE", "ad_guard_rules_file", "str", "广告守卫",
              "启发式规则文件", "广告判定启发式规则 JSON 路径，支持热重载", False, nullable=True),
    # ===== 关键词功能 =====
    FieldSpec("KEYWORD_REPLY_ENABLED", "keyword_reply_enabled", "bool", "关键词功能",
              "关键词自动回复", "命中规则时自动回复，规则在「关键词回复」页维护", False),
    FieldSpec("KEYWORD_REPLY_COOLDOWN_SECONDS", "keyword_reply_cooldown_seconds", "int", "关键词功能",
              "回复默认冷却（秒）", "同一群同一规则的默认冷却，可被规则覆盖", False, min_value=0),
    FieldSpec("KEYWORD_DELETION_ENABLED", "keyword_deletion_enabled", "bool", "关键词功能",
              "关键词自动删除", "命中规则时自动删除消息，规则在「关键词删除」页维护", False),
    FieldSpec("KEYWORD_REPLY_RULES_FILE", "keyword_reply_rules_file", "str", "关键词功能",
              "回复规则文件", "关键词回复规则 JSON 路径", False, nullable=True),
    FieldSpec("KEYWORD_DELETION_RULES_FILE", "keyword_deletion_rules_file", "str", "关键词功能",
              "删除规则文件", "关键词删除规则 JSON 路径", False, nullable=True),
    # ===== Admin 后台 =====
    FieldSpec("ADMIN_WEB_ENABLED", "admin_web_enabled", "bool", "Admin 后台",
              "启用 Admin 后台", "关闭后 /admin 全部不可用，请谨慎操作", False),
    FieldSpec("ADMIN_SESSION_TTL_SECONDS", "admin_session_ttl_seconds", "int", "Admin 后台",
              "会话有效期（秒）", "登录态保持时长，下限 300 秒", False, min_value=300),
    FieldSpec("ADMIN_MAX_SESSIONS_PER_USER", "admin_max_sessions_per_user", "int", "Admin 后台",
              "每用户并发会话数", "超出时挤掉最旧的登录", False, min_value=1),
    FieldSpec("ADMIN_RATE_LIMIT_PER_MIN", "admin_rate_limit_per_min", "int", "Admin 后台",
              "每 IP 每分钟限流", "保护登录回调与管理接口，下限 5", False, min_value=5),
    FieldSpec("ADMIN_BEHIND_PROXY", "admin_behind_proxy", "bool", "Admin 后台",
              "位于反向代理之后", "仅在确有 Nginx/Caddy/CF 反代时开启；直连部署必须关闭，否则限流可被绕过", False),
    FieldSpec("ADMIN_AUTH_AGE_SECONDS", "admin_auth_age_seconds", "int", "Admin 后台",
              "登录签名有效期（秒）", "Telegram 登录签名允许的最大年龄，下限 60", False, min_value=60),
    # ===== Web 服务 =====
    FieldSpec("WEB_HOST", "web_host", "str", "Web 服务",
              "监听地址", "0.0.0.0 / :: / dual 等", True),
    FieldSpec("WEB_PORT", "web_port", "int", "Web 服务",
              "监听端口", "HTTP 服务端口", True, min_value=1, max_value=65535),
    FieldSpec("SSL_CERT_FILE", "ssl_cert_file", "str", "Web 服务",
              "SSL 证书文件", "HTTPS 证书路径，与私钥需同时设置；留空为纯 HTTP", True, nullable=True),
    FieldSpec("SSL_KEY_FILE", "ssl_key_file", "str", "Web 服务",
              "SSL 私钥文件", "HTTPS 私钥路径", True, nullable=True),
    FieldSpec("SSL_CA_FILE", "ssl_ca_file", "str", "Web 服务",
              "SSL CA 文件", "客户端证书校验用 CA，通常留空", True, nullable=True),
    # ===== 存储 =====
    FieldSpec("DATABASE_PATH", "database_path", "str", "存储",
              "SQLite 数据库路径", "验证记录数据库文件位置", True),
    FieldSpec("REDIS_URL", "redis_url", "str", "存储",
              "Redis 地址", "例如 redis://localhost:6379/0", True),
    FieldSpec("REDIS_SCORE_PREFIX", "redis_score_prefix", "str", "存储",
              "Redis 评分键前缀", "广告评分在 Redis 中的 key 前缀", True),
    # ===== 网络与代理 =====
    FieldSpec("TELEGRAM_PROXY", "telegram_proxy", "str", "网络与代理",
              "Telegram 代理", "socks5/http 代理地址，留空为直连", True, nullable=True),
    # ===== 可观测性 =====
    FieldSpec("ENABLE_METRICS", "enable_metrics", "bool", "可观测性",
              "Prometheus 指标", "开启 /metrics 端点", True),
    FieldSpec("LOG_LEVEL", "log_level", "select", "可观测性",
              "日志级别", "DEBUG / INFO / WARNING / ERROR", False,
              choices=("DEBUG", "INFO", "WARNING", "ERROR")),
    # ===== 核心凭证 =====
    FieldSpec("TELEGRAM_BOT_TOKEN", "bot_token", "secret", "核心凭证",
              "Bot Token", "从 @BotFather 获取；留空表示保持现有值不变", True, nullable=True),
    FieldSpec("TELEGRAM_BOT_USERNAME", "bot_username", "str", "核心凭证",
              "Bot 用户名", "不带 @，用于生成深链与登录组件", True),
)

_FIELD_BY_KEY = {f.key: f for f in FIELDS}


def _resolve_path() -> Path:
    return OVERRIDES_PATH.resolve()


def load_overrides(path: Optional[Path] = None) -> dict[str, str]:
    p = (path or _resolve_path())
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("读取配置覆盖文件失败 %s: %s", p, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if k in _FIELD_BY_KEY}


def save_overrides(overrides: dict[str, str], path: Optional[Path] = None) -> None:
    p = (path or _resolve_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(overrides, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(p)


def _to_raw(spec: FieldSpec, value: Any) -> str:
    if value is None:
        return ""
    if spec.kind == "bool":
        return "true" if value else "false"
    if spec.kind == "chat_ids":
        return ",".join(str(i) for i in sorted(value))
    if spec.kind == "select":
        return str(value)
    if spec.attr.endswith("_file") or spec.attr.startswith("ssl_"):
        return str(value)
    return str(value)


def _parse_value(spec: FieldSpec, raw: str) -> Any:
    raw = raw.strip()
    if spec.kind == "bool":
        return raw.lower() in {"1", "true", "yes", "on"}
    if spec.kind == "chat_ids":
        ids: set[int] = set()
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                ids.add(int(item))
            except ValueError as exc:
                raise ValueError(f"{spec.key} 包含无效数字：{item}") from exc
        return ids
    if raw == "" and spec.nullable:
        return None
    if spec.kind == "int":
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{spec.label} 必须是整数") from exc
        if spec.min_value is not None:
            value = max(value, int(spec.min_value))
        if spec.max_value is not None:
            value = min(value, int(spec.max_value))
        return value
    if spec.kind == "float":
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"{spec.label} 必须是数字") from exc
        if spec.min_value is not None:
            value = max(value, spec.min_value)
        if spec.max_value is not None:
            value = min(value, spec.max_value)
        return value
    if spec.kind == "select":
        value = raw.lower() if spec.key != "LOG_LEVEL" else raw.upper()
        if spec.key == "AD_GUARD_PROVIDER" and value == "hunyuan":
            value = "openai"
        if value not in spec.choices:
            raise ValueError(f"{spec.label} 仅支持：{' / '.join(spec.choices)}")
        return value
    # str / secret
    if spec.attr.endswith("_file") or spec.attr.startswith("ssl_"):
        if not raw:
            return None
        return Path(raw).expanduser().resolve(strict=False)
    if spec.key == "VERIFY_BASE_URL":
        return raw.rstrip("/")
    if spec.key == "TELEGRAM_BOT_USERNAME":
        return raw.lstrip("@")
    return raw


def recompute_ad_guard_enabled(settings: Settings) -> None:
    """由管理员的开启意图 + AI 总开关 + 端点/密钥是否齐备，重算广告守卫有效值。

    任何相关字段（AD_GUARD_ENABLED / AI_ENABLED / AD_GUARD_PROVIDER /
    OLLAMA_ENDPOINT / OPENAI_API_KEY）变更后都必须调用，
    否则会出现"UI 显示开启但实际不检测"或"补齐端点后仍不生效"。
    """
    effective = False
    if settings.ad_guard_enabled_intent and settings.ai_enabled:
        if settings.ad_guard_provider == "ollama":
            effective = bool(settings.ollama_endpoint)
        else:
            effective = bool(settings.openai_api_key)
    settings.ad_guard_enabled = effective


def _apply_field(settings: Settings, spec: FieldSpec, raw: str) -> None:
    value = _parse_value(spec, raw)
    if spec.key == "AD_GUARD_ENABLED":
        settings.ad_guard_enabled_intent = bool(value)
        recompute_ad_guard_enabled(settings)
        return
    if spec.key == "AI_ENABLED":
        settings.ai_enabled = bool(value)
        # AI 总开关变化后（无论开还是关），广告守卫有效值都要重算
        recompute_ad_guard_enabled(settings)
        return
    if spec.key == "AD_GUARD_PROVIDER":
        setattr(settings, spec.attr, value)
        recompute_ad_guard_enabled(settings)
        return
    if spec.key in {"OLLAMA_ENDPOINT", "OPENAI_API_KEY"}:
        setattr(settings, spec.attr, value or None)
        # 端点/密钥变化会影响广告守卫有效值（补齐应开启，清空应关闭）
        recompute_ad_guard_enabled(settings)
        return
    if spec.key == "AD_GUARD_RULES_FILE":
        setattr(settings, spec.attr, value)
        # 规则文件路径变更需同步切换规则缓存，否则 UI 显示新路径但实际仍用旧规则
        from .ad_guard_rules import configure_ad_guard_rules

        configure_ad_guard_rules(value)
        return
    if spec.key == "KEYWORD_REPLY_RULES_FILE":
        setattr(settings, spec.attr, value)
        from .keyword_replies import configure_keyword_replies

        configure_keyword_replies(value)
        return
    if spec.key == "KEYWORD_DELETION_RULES_FILE":
        setattr(settings, spec.attr, value)
        from .keyword_deletions import configure_keyword_deletions

        configure_keyword_deletions(value)
        return
    setattr(settings, spec.attr, value)


def apply_overrides(settings: Settings, overrides: Optional[dict[str, str]] = None) -> list[str]:
    """把覆盖值套到 Settings 上；返回应用过程中被忽略的键（解析失败）。"""
    data = overrides if overrides is not None else load_overrides()
    skipped: list[str] = []
    for key, raw in data.items():
        spec = _FIELD_BY_KEY.get(key)
        if spec is None:
            continue
        try:
            _apply_field(settings, spec, raw)
        except ValueError as exc:
            logger.warning("覆盖配置 %s 无效，已忽略：%s", key, exc)
            skipped.append(key)
    return skipped


def _display_value(settings: Settings, spec: FieldSpec) -> Any:
    """字段对外的"当前值"。AD_GUARD_ENABLED 显示管理员意图而非有效值，
    避免端点缺失时 UI 开关自动回弹成关闭、覆盖文件里的 true 被误判为已变更。"""
    if spec.key == "AD_GUARD_ENABLED":
        return settings.ad_guard_enabled_intent
    return getattr(settings, spec.attr)


def describe_for_api(settings: Settings) -> dict[str, Any]:
    """输出给设置页的当前值快照；secret 字段脱敏，只回 is_set。"""
    groups: list[dict[str, Any]] = []
    for group in GROUP_ORDER:
        fields = []
        for spec in FIELDS:
            if spec.group != group:
                continue
            value = _display_value(settings, spec)
            item: dict[str, Any] = {
                "key": spec.key,
                "kind": spec.kind,
                "label": spec.label,
                "help": spec.help,
                "restart": spec.restart,
                "nullable": spec.nullable,
                "choices": list(spec.choices),
            }
            if spec.kind == "secret":
                item["value"] = ""
                item["is_set"] = bool(value)
            else:
                item["value"] = _to_raw(spec, value)
                item["is_set"] = True
            if spec.key == "AD_GUARD_ENABLED":
                # 开关显示意图值;额外暴露实际生效状态,便于前端提示"已开启但未生效"
                item["effective"] = bool(settings.ad_guard_enabled)
            fields.append(item)
        if fields:
            groups.append({"name": group, "fields": fields})
    return {"groups": groups}


def validate_and_split(
    settings: Settings, values: dict[str, Any]
) -> tuple[dict[str, str], dict[str, str], list[str], list[str]]:
    """校验前端提交的值。

    返回 (hot_values, restart_values, errors, changed_keys)。
    hot/restart 按字段是否需要重启分组；值统一为 raw 字符串。
    """
    hot: dict[str, str] = {}
    restart: dict[str, str] = {}
    errors: list[str] = []
    changed: list[str] = []
    if not isinstance(values, dict):
        return hot, restart, ["提交格式不正确"], changed
    for key, raw_value in values.items():
        spec = _FIELD_BY_KEY.get(key)
        if spec is None:
            continue  # 忽略未知键，防止前端带脏数据
        raw = "" if raw_value is None else str(raw_value)
        if spec.kind == "secret" and raw.strip() == "":
            continue  # 留空 = 保持不变
        try:
            parsed = _parse_value(spec, raw)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        # 与当前值相同则跳过，避免无谓的覆盖与重启提示
        current_raw = _to_raw(spec, _display_value(settings, spec))
        if spec.kind != "secret" and raw.strip() == current_raw.strip():
            continue
        # 归一化后再存，保证覆盖文件里的值就是解析后的语义
        normalized = _to_raw(spec, parsed)
        (restart if spec.restart else hot)[key] = normalized
        changed.append(key)
    return hot, restart, errors, changed


def apply_hot_values(settings: Settings, hot: dict[str, str]) -> None:
    for key, raw in hot.items():
        spec = _FIELD_BY_KEY.get(key)
        if spec is None:
            continue
        _apply_field(settings, spec, raw)


def merge_into_overrides(new_values: dict[str, str], path: Optional[Path] = None) -> dict[str, str]:
    """把新值合并进覆盖文件（先读旧文件，避免并发/历史值丢失），返回合并结果。"""
    merged = load_overrides(path)
    merged.update(new_values)
    save_overrides(merged, path)
    return merged
