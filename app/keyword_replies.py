from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Optional, Pattern

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message, ReplyParameters

from .bot_components.messaging import send_message_with_ttl

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class KeywordRule:
    """单条关键词回复规则。keywords 与 pattern 二选一,同时配置时 pattern 优先。"""

    reply: str
    keywords: tuple[str, ...] = ()
    require_all: bool = False
    case_sensitive: bool = False
    pattern: Optional[Pattern[str]] = None

    def matches(self, text: str) -> bool:
        if self.pattern is not None:
            return bool(self.pattern.search(text))
        if not self.keywords:
            return False
        haystack = text if self.case_sensitive else text.lower()
        if self.require_all:
            return all(k in haystack for k in self.keywords)
        return any(k in haystack for k in self.keywords)


def _parse_rule(raw: object, index: int) -> Optional[KeywordRule]:
    if not isinstance(raw, dict):
        logger.warning("关键词规则 #%s 不是对象,已忽略", index)
        return None
    reply = raw.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        logger.warning("关键词规则 #%s 缺少有效的 reply 字段,已忽略", index)
        return None
    case_sensitive = bool(raw.get("case_sensitive", False))
    require_all = str(raw.get("match", "any")).strip().lower() == "all"

    pattern: Optional[Pattern[str]] = None
    keywords: tuple[str, ...] = ()

    pattern_raw = raw.get("pattern")
    keywords_raw = raw.get("keywords")
    if isinstance(pattern_raw, str) and pattern_raw.strip():
        try:
            pattern = re.compile(pattern_raw)
        except re.error as exc:
            logger.warning("关键词规则 #%s 正则无效 %r: %s,已忽略", index, pattern_raw, exc)
            return None
    elif isinstance(keywords_raw, (list, tuple)):
        words: list[str] = []
        for item in keywords_raw:
            if not isinstance(item, str) or not item.strip():
                logger.warning("关键词规则 #%s 含无效关键词,已忽略整条规则", index)
                return None
            cleaned = item.strip()
            words.append(cleaned if case_sensitive else cleaned.lower())
        keywords = tuple(words)
    elif keywords_raw is not None:
        logger.warning("关键词规则 #%s 的 keywords 必须是字符串数组,已忽略", index)
        return None

    if pattern is None and not keywords:
        logger.warning("关键词规则 #%s 需要配置 keywords 或 pattern,已忽略", index)
        return None
    return KeywordRule(
        reply=reply,
        keywords=keywords,
        require_all=require_all,
        case_sensitive=case_sensitive,
        pattern=pattern,
    )


class _KeywordReplyCache:
    """关键词规则缓存,按文件 mtime 热重载(与广告规则同一模式)。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._path: Path | None = None
        self._mtime: float | None = None
        self._rules: list[KeywordRule] = []
        self._cooldown_seconds: Optional[int] = None

    def configure(self, path: Path | None) -> None:
        with self._lock:
            self._path = path
            self._mtime = None
            self._rules = []
            self._cooldown_seconds = None
            self._ensure_loaded(force=True)

    def get_config(self) -> tuple[list[KeywordRule], Optional[int]]:
        with self._lock:
            self._ensure_loaded()
            return self._rules, self._cooldown_seconds

    def _ensure_loaded(self, force: bool = False) -> None:
        path = self._path
        if path is None:
            return
        try:
            stat = path.stat()
        except FileNotFoundError:
            if force:
                logger.info("关键词回复配置文件不存在,功能将不生效:%s", path)
            self._mtime = None
            return
        except OSError as exc:
            if force:
                logger.warning("读取关键词回复配置文件信息失败 %s: %s", path, exc)
            return
        if not force and self._mtime is not None and stat.st_mtime <= self._mtime:
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("读取关键词回复配置文件失败 %s: %s", path, exc)
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("关键词回复配置 JSON 解析失败 %s: %s", path, exc)
            self._mtime = stat.st_mtime
            return
        if not isinstance(payload, dict):
            logger.warning("关键词回复配置根节点必须是对象:%s", path)
            self._mtime = stat.st_mtime
            return

        rules: list[KeywordRule] = []
        raw_rules = payload.get("rules", [])
        if isinstance(raw_rules, list):
            for idx, item in enumerate(raw_rules):
                rule = _parse_rule(item, idx)
                if rule is not None:
                    rules.append(rule)
        else:
            logger.warning("关键词回复配置 rules 必须是数组,已忽略全部规则")

        cooldown_val: Optional[int] = None
        cooldown_raw = payload.get("cooldown_seconds")
        if cooldown_raw is not None:
            try:
                cooldown_val = max(int(cooldown_raw), 0)
            except (TypeError, ValueError):
                logger.warning("关键词回复配置 cooldown_seconds 无效,已忽略")

        self._rules = rules
        self._cooldown_seconds = cooldown_val
        self._mtime = stat.st_mtime
        logger.info(
            "关键词回复规则已加载 count=%s cooldown=%s file=%s",
            len(rules),
            cooldown_val,
            path,
        )


_CACHE = _KeywordReplyCache()

# 冷却状态:(chat_id, 规则序号) -> 上次回复的 monotonic 时间
_cooldowns: dict[tuple[int, int], float] = {}
_cooldown_guard = asyncio.Lock()


def configure_keyword_replies(path: Path | None) -> None:
    """配置关键词回复规则文件路径。传入 None 表示禁用文件规则。"""
    _CACHE.configure(path)


def get_keyword_reply_config() -> tuple[list[KeywordRule], Optional[int]]:
    return _CACHE.get_config()


async def try_keyword_reply(
    bot: Bot,
    message: Message,
    *,
    rules: list[KeywordRule],
    cooldown_seconds: int,
    ttl: Optional[int],
) -> bool:
    """命中第一条匹配规则即回复并返回 True;未命中返回 False。

    - 命令(/开头)不触发
    - 同一群同一规则在 cooldown_seconds 内只回复一次
    """
    if not rules:
        return False
    raw = message.text if message.text is not None else message.caption
    text = (raw or "").strip()
    if not text or text.startswith("/"):
        return False

    for idx, rule in enumerate(rules):
        if not rule.matches(text):
            continue
        if cooldown_seconds > 0:
            key = (message.chat.id, idx)
            now = time.monotonic()
            async with _cooldown_guard:
                last = _cooldowns.get(key)
                if last is not None and now - last < cooldown_seconds:
                    logger.debug(
                        "关键词回复冷却中 chat_id=%s rule=%s", message.chat.id, idx
                    )
                    return True
                _cooldowns[key] = now
                # 兜底清理,防止字典无限增长
                if len(_cooldowns) > 10000:
                    cutoff = now - max(cooldown_seconds, 3600)
                    for stale_key in [k for k, ts in _cooldowns.items() if ts < cutoff]:
                        _cooldowns.pop(stale_key, None)
        try:
            await send_message_with_ttl(
                bot,
                chat_id=message.chat.id,
                text=rule.reply,
                ttl=ttl,
                disable_web_page_preview=True,
                reply_parameters=ReplyParameters(message_id=message.message_id),
            )
            logger.info(
                "关键词回复已发送 chat_id=%s rule=%s user_id=%s",
                message.chat.id,
                idx,
                message.from_user.id if message.from_user else None,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning(
                "关键词回复发送失败 chat_id=%s rule=%s error=%s",
                message.chat.id,
                idx,
                exc,
            )
        return True
    return False


_MAX_RULES = 100
_MAX_KEYWORDS_PER_RULE = 20
_MAX_KEYWORD_LEN = 100
_MAX_REPLY_LEN = 2000


def validate_keyword_rules_payload(payload: object) -> tuple[dict, list[str]]:
    """校验后台提交的关键词配置。

    返回 (规范化后可写入文件的 payload, 错误列表)。errors 非空时不应落盘。
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return {}, ["配置必须是 JSON 对象"]

    rules_raw = payload.get("rules", [])
    if not isinstance(rules_raw, list):
        return {}, ["rules 必须是数组"]
    if len(rules_raw) > _MAX_RULES:
        return {}, [f"规则数量不能超过 {_MAX_RULES} 条（当前 {len(rules_raw)} 条）"]

    cleaned_rules: list[dict] = []
    for idx, item in enumerate(rules_raw):
        label = f"规则 #{idx + 1}"
        if not isinstance(item, dict):
            errors.append(f"{label}: 必须是对象")
            continue
        reply = item.get("reply")
        if not isinstance(reply, str) or not reply.strip():
            errors.append(f"{label}: reply 不能为空")
            continue
        if len(reply) > _MAX_REPLY_LEN:
            errors.append(f"{label}: reply 超过 {_MAX_REPLY_LEN} 字")
            continue

        pattern_raw = item.get("pattern")
        keywords_raw = item.get("keywords")
        cleaned: dict = {"reply": reply}

        if isinstance(pattern_raw, str) and pattern_raw.strip():
            try:
                re.compile(pattern_raw)
            except re.error as exc:
                errors.append(f"{label}: 正则无效（{exc}）")
                continue
            cleaned["pattern"] = pattern_raw
        elif isinstance(keywords_raw, list):
            if not keywords_raw:
                errors.append(f"{label}: keywords 不能为空数组")
                continue
            if len(keywords_raw) > _MAX_KEYWORDS_PER_RULE:
                errors.append(f"{label}: 关键词数量超过 {_MAX_KEYWORDS_PER_RULE} 个")
                continue
            words: list[str] = []
            invalid = False
            for word in keywords_raw:
                if not isinstance(word, str) or not word.strip():
                    errors.append(f"{label}: 含空白或非字符串关键词")
                    invalid = True
                    break
                if len(word.strip()) > _MAX_KEYWORD_LEN:
                    errors.append(f"{label}: 关键词超过 {_MAX_KEYWORD_LEN} 字")
                    invalid = True
                    break
                words.append(word.strip())
            if invalid:
                continue
            cleaned["keywords"] = words
            match_raw = str(item.get("match", "any")).strip().lower()
            if match_raw not in {"any", "all"}:
                errors.append(f"{label}: match 只能是 any 或 all")
                continue
            if match_raw == "all":
                cleaned["match"] = "all"
            if bool(item.get("case_sensitive", False)):
                cleaned["case_sensitive"] = True
        elif pattern_raw is not None or keywords_raw is not None:
            errors.append(f"{label}: pattern 必须是字符串，keywords 必须是数组")
            continue
        else:
            errors.append(f"{label}: 需要配置 keywords 或 pattern")
            continue
        cleaned_rules.append(cleaned)

    normalized: dict = {"rules": cleaned_rules}
    cooldown_raw = payload.get("cooldown_seconds")
    if cooldown_raw is not None:
        try:
            cooldown_val = int(cooldown_raw)
        except (TypeError, ValueError):
            errors.append("cooldown_seconds 必须是整数")
        else:
            if not 0 <= cooldown_val <= 86400:
                errors.append("cooldown_seconds 需在 0-86400 之间")
            else:
                normalized["cooldown_seconds"] = cooldown_val

    if errors:
        return {}, errors
    return normalized, []


def save_keyword_rules(path: Path, payload: dict) -> None:
    """原子写入规则文件;保存后缓存按 mtime 自动热重载。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


__all__ = [
    "KeywordRule",
    "configure_keyword_replies",
    "get_keyword_reply_config",
    "save_keyword_rules",
    "try_keyword_reply",
    "validate_keyword_rules_payload",
]
