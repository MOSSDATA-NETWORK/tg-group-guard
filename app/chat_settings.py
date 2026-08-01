"""按群差异化配置覆盖层。

存储 data/chat_overrides.json：{ "<chat_id>": { "ENV_KEY": "raw value" } }。
运行时通过 resolve_chat(settings, chat_id, attr) 读取：有覆盖用覆盖，
否则回退到全局 Settings 值。文件改动按 mtime 热重载，保存即生效。

第一版支持按群覆盖的参数（均为群消息路径上的热配置）见 CHAT_FIELDS。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from .config import Settings

logger = logging.getLogger(__name__)

CHAT_OVERRIDES_PATH = Path("data/chat_overrides.json")


class ChatField:
    __slots__ = ("key", "attr", "kind", "label", "help", "min_value", "max_value", "nullable")

    def __init__(self, key, attr, kind, label, help, min_value=None, max_value=None, nullable=False):
        self.key = key
        self.attr = attr
        self.kind = kind          # bool / int / float
        self.label = label
        self.help = help
        self.min_value = min_value
        self.max_value = max_value
        self.nullable = nullable


CHAT_FIELDS: tuple[ChatField, ...] = (
    ChatField("AD_GUARD_ENABLED", "ad_guard_enabled", "bool",
              "广告守卫开关", "关闭后本群不再进行 LLM 广告检测（不影响关键词功能）"),
    ChatField("AD_GUARD_THRESHOLD", "ad_guard_threshold", "float",
              "判定置信度阈值", "0~1，越高越保守，越低越激进", min_value=0.0, max_value=1.0),
    ChatField("AD_GUARD_BAN", "ad_guard_ban", "bool",
              "命中广告即封禁", "关闭时本群命中广告只踢出不封禁"),
    ChatField("KEYWORD_REPLY_ENABLED", "keyword_reply_enabled", "bool",
              "关键词自动回复", "控制本群是否触发关键词回复（规则本身全局共享）"),
    ChatField("KEYWORD_DELETION_ENABLED", "keyword_deletion_enabled", "bool",
              "关键词自动删除", "控制本群是否触发关键词删除（规则本身全局共享）"),
    ChatField("MESSAGE_TTL_SECONDS", "message_ttl_seconds", "int",
              "提示消息自动删除（秒）", "留空表示跟随全局；0 表示本群不自动删除",
              min_value=0, nullable=True),
    ChatField("WARN_LIMIT", "warn_limit", "int",
              "警告封禁阈值", "本群本月警告达此次数自动封禁", min_value=1),
)

_FIELD_BY_KEY = {f.key: f for f in CHAT_FIELDS}
_FIELD_BY_ATTR = {f.attr: f for f in CHAT_FIELDS}

_cache: dict[str, Any] = {"mtime": None, "data": {}}


def _path() -> Path:
    return CHAT_OVERRIDES_PATH.resolve()


def _load_cached() -> dict[str, dict[str, str]]:
    p = _path()
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        _cache["mtime"] = None
        _cache["data"] = {}
        return _cache["data"]
    if _cache["mtime"] != mtime:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("读取按群配置覆盖文件失败 %s: %s", p, exc)
            raw = {}
        data: dict[str, dict[str, str]] = {}
        if isinstance(raw, dict):
            for chat_key, values in raw.items():
                if not isinstance(values, dict):
                    continue
                cleaned = {
                    str(k): str(v)
                    for k, v in values.items()
                    if k in _FIELD_BY_KEY
                }
                if cleaned:
                    data[str(chat_key)] = cleaned
        _cache["mtime"] = mtime
        _cache["data"] = data
    return _cache["data"]


def load_all() -> dict[str, dict[str, str]]:
    return {k: dict(v) for k, v in _load_cached().items()}


def save_all(data: dict[str, dict[str, str]]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(p)
    _cache["mtime"] = None  # 强制下次读取时重载


def _parse(spec: ChatField, raw: str) -> Any:
    raw = raw.strip()
    if spec.kind == "bool":
        if raw == "" and spec.nullable:
            return None
        return raw.lower() in {"1", "true", "yes", "on"}
    if raw == "" and spec.nullable:
        return None
    try:
        value: Any = int(raw) if spec.kind == "int" else float(raw)
    except ValueError as exc:
        raise ValueError(f"{spec.label} 必须是数字") from exc
    if spec.min_value is not None:
        value = max(value, spec.min_value)
    if spec.max_value is not None:
        value = min(value, spec.max_value)
    return value


def resolve_chat(settings: Settings, chat_id: int, attr: str) -> Any:
    """读取某 attr 在该群的生效值：群覆盖 > 全局 Settings。"""
    spec = _FIELD_BY_ATTR.get(attr)
    if spec is None:
        return getattr(settings, attr)
    raw = _load_cached().get(str(chat_id), {}).get(spec.key)
    if raw is None:
        return getattr(settings, attr)
    try:
        return _parse(spec, raw)
    except ValueError:
        logger.warning("按群配置 %s(%s) 无效，回退全局值 chat_id=%s", spec.key, raw, chat_id)
        return getattr(settings, attr)


def describe_for_chat(settings: Settings, chat_id: int) -> list[dict[str, Any]]:
    """输出某群各字段：覆盖值(可空) + 全局值 + 生效值。"""
    overrides = _load_cached().get(str(chat_id), {})
    items: list[dict[str, Any]] = []
    for spec in CHAT_FIELDS:
        raw = overrides.get(spec.key)
        global_value = getattr(settings, spec.attr)
        items.append(
            {
                "key": spec.key,
                "kind": spec.kind,
                "label": spec.label,
                "help": spec.help,
                "nullable": spec.nullable,
                "override": raw,  # None = 跟随全局
                "global_value": (
                    ""
                    if global_value is None
                    else ("true" if global_value is True else "false" if global_value is False else str(global_value))
                ),
            }
        )
    return items


def validate_chat_values(values: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """校验提交的按群覆盖值；value 为 None 或空串表示清除覆盖（跟随全局）。"""
    cleaned: dict[str, str] = {}
    errors: list[str] = []
    if not isinstance(values, dict):
        return cleaned, ["提交格式不正确"]
    for key, raw_value in values.items():
        spec = _FIELD_BY_KEY.get(key)
        if spec is None:
            continue
        if raw_value is None or str(raw_value).strip() == "":
            continue  # 清除覆盖
        raw = str(raw_value).strip()
        try:
            parsed = _parse(spec, raw)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if spec.kind == "bool":
            cleaned[key] = "true" if parsed else "false"
        else:
            cleaned[key] = str(parsed)
    return cleaned, errors


def set_chat_overrides(chat_id: int, values: dict[str, str]) -> dict[str, dict[str, str]]:
    """整体替换某群的覆盖集（空 dict = 删除该群所有覆盖）。"""
    data = load_all()
    key = str(chat_id)
    if values:
        data[key] = dict(values)
    else:
        data.pop(key, None)
    save_all(data)
    return data
