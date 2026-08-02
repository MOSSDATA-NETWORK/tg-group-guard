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
from aiogram.types import Message

logger = logging.getLogger(__name__)

# ===== 正则 ReDoS 防护(与 keyword_replies 同款机制) =====
# regex 库引擎内建规避经典灾难模式,并对残余病态模式提供原生 timeout;
# 标准库 re 无超时且回溯失控时持有 GIL,线程池无法真正解救。
try:
    import regex as _regex_engine
except ImportError:
    _regex_engine = None

if _regex_engine is None:
    logger.warning("未安装 regex 库(pip install regex),关键词删除正则将无超时防护")

_PATTERN_ERROR_TYPES = (re.error,) if _regex_engine is None else (re.error, _regex_engine.error)
_REGEX_TIMEOUT_SECONDS = 0.5
_DISABLED_PATTERNS: set[str] = set()


def _compile_pattern(pattern_raw: str):
    return (_regex_engine or re).compile(pattern_raw)


async def safe_rule_match(rule: DeletionRule, text: str, timeout: float = _REGEX_TIMEOUT_SECONDS) -> bool:
    """带超时与熔断的规则匹配。纯关键词规则走快路径;regex 库缺席时退回无防护匹配。"""
    pattern = rule.pattern
    if pattern is None:
        return rule.matches(text)
    if pattern.pattern in _DISABLED_PATTERNS:
        return False
    if _regex_engine is not None and isinstance(pattern, _regex_engine.Pattern):
        try:
            return bool(pattern.search(text, timeout=timeout))
        except TimeoutError:
            _DISABLED_PATTERNS.add(pattern.pattern)
            logger.warning(
                "关键词删除规则正则执行超时(>%.1fs),已熔断禁用 pattern=%r",
                timeout,
                pattern.pattern,
            )
            return False
    return rule.matches(text)


@dataclass(slots=True)
class DeletionRule:
    """单条关键词删除规则。keywords 与 pattern 二选一,同时配置时 pattern 优先。"""

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


def _parse_rule(raw: object, index: int) -> Optional[DeletionRule]:
    if not isinstance(raw, dict):
        logger.warning("关键词删除规则 #%s 不是对象,已忽略", index)
        return None
    case_sensitive = bool(raw.get("case_sensitive", False))
    require_all = str(raw.get("match", "any")).strip().lower() == "all"

    pattern: Optional[Pattern[str]] = None
    keywords: tuple[str, ...] = ()

    pattern_raw = raw.get("pattern")
    keywords_raw = raw.get("keywords")
    if isinstance(pattern_raw, str) and pattern_raw.strip():
        try:
            pattern = _compile_pattern(pattern_raw)
        except _PATTERN_ERROR_TYPES as exc:
            logger.warning("关键词删除规则 #%s 正则无效 %r: %s,已忽略", index, pattern_raw, exc)
            return None
    elif isinstance(keywords_raw, (list, tuple)):
        words: list[str] = []
        for item in keywords_raw:
            if not isinstance(item, str) or not item.strip():
                logger.warning("关键词删除规则 #%s 含无效关键词,已忽略整条规则", index)
                return None
            cleaned = item.strip()
            words.append(cleaned if case_sensitive else cleaned.lower())
        keywords = tuple(words)
    elif keywords_raw is not None:
        logger.warning("关键词删除规则 #%s 的 keywords 必须是字符串数组,已忽略", index)
        return None

    if pattern is None and not keywords:
        logger.warning("关键词删除规则 #%s 需要配置 keywords 或 pattern,已忽略", index)
        return None
    return DeletionRule(
        keywords=keywords,
        require_all=require_all,
        case_sensitive=case_sensitive,
        pattern=pattern,
    )


class _KeywordDeletionCache:
    """关键词删除规则缓存,按文件 mtime 热重载。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._path: Path | None = None
        self._mtime: float | None = None
        self._rules: list[DeletionRule] = []

    def configure(self, path: Path | None) -> None:
        with self._lock:
            self._path = path
            self._mtime = None
            self._rules = []
            self._ensure_loaded(force=True)

    def get_rules(self) -> list[DeletionRule]:
        with self._lock:
            self._ensure_loaded()
            return self._rules.copy()

    def _ensure_loaded(self, force: bool = False) -> None:
        path = self._path
        if path is None:
            return
        try:
            stat = path.stat()
        except FileNotFoundError:
            if force:
                logger.info("关键词删除配置文件不存在,功能将不生效:%s", path)
            self._mtime = None
            return
        except OSError as exc:
            if force:
                logger.warning("读取关键词删除配置文件信息失败 %s: %s", path, exc)
            return
        if not force and self._mtime is not None and stat.st_mtime <= self._mtime:
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("读取关键词删除配置文件失败 %s: %s", path, exc)
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("关键词删除配置 JSON 解析失败 %s: %s", path, exc)
            self._mtime = stat.st_mtime
            return
        if not isinstance(payload, dict):
            logger.warning("关键词删除配置根节点必须是对象:%s", path)
            self._mtime = stat.st_mtime
            return

        rules: list[DeletionRule] = []
        raw_rules = payload.get("rules", [])
        if isinstance(raw_rules, list):
            for idx, item in enumerate(raw_rules):
                rule = _parse_rule(item, idx)
                if rule is not None:
                    rules.append(rule)
        else:
            logger.warning("关键词删除配置 rules 必须是数组,已忽略全部规则")

        self._rules = rules
        self._mtime = stat.st_mtime
        logger.info(
            "关键词删除规则已加载 count=%s file=%s",
            len(rules),
            path,
        )


_CACHE = _KeywordDeletionCache()

# 删除冷却:(chat_id, user_id) -> 上次删除的 monotonic 时间
# 防止同一用户短时间内被连续删除（误删保护）
_deletion_cooldowns: dict[tuple[int, int], float] = {}
_deletion_cooldown_guard = asyncio.Lock()
_DELETION_COOLDOWN_SECONDS = 3


def configure_keyword_deletions(path: Path | None) -> None:
    """配置关键词删除规则文件路径。传入 None 表示禁用文件规则。"""
    _CACHE.configure(path)


def get_keyword_deletion_rules() -> list[DeletionRule]:
    return _CACHE.get_rules()


async def try_keyword_deletion(
    bot: Bot,
    message: Message,
    *,
    rules: list[DeletionRule],
) -> bool:
    """命中第一条匹配规则即删除消息并返回 True;未命中返回 False。

    - 命令(/开头)不触发
    - 同一用户 3 秒内最多删除一次（防误删保护）
    """
    if not rules:
        return False
    raw = message.text if message.text is not None else message.caption
    text = (raw or "").strip()
    if not text or text.startswith("/"):
        return False

    for idx, rule in enumerate(rules):
        if not await safe_rule_match(rule, text):
            continue

        # 冷却检查
        if message.from_user is not None:
            key = (message.chat.id, message.from_user.id)
            now = time.monotonic()
            async with _deletion_cooldown_guard:
                last = _deletion_cooldowns.get(key)
                if last is not None and now - last < _DELETION_COOLDOWN_SECONDS:
                    logger.debug(
                        "关键词删除冷却中 chat_id=%s user_id=%s rule=%s",
                        message.chat.id,
                        message.from_user.id,
                        idx,
                    )
                    return True
                _deletion_cooldowns[key] = now
                # 兜底清理
                if len(_deletion_cooldowns) > 10000:
                    cutoff = now - max(_DELETION_COOLDOWN_SECONDS, 3600)
                    for stale_key in [k for k, ts in _deletion_cooldowns.items() if ts < cutoff]:
                        _deletion_cooldowns.pop(stale_key, None)

        try:
            await bot.delete_message(message.chat.id, message.message_id)
            logger.info(
                "关键词删除已执行 chat_id=%s msg_id=%s rule=%s user_id=%s",
                message.chat.id,
                message.message_id,
                idx,
                message.from_user.id if message.from_user else None,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning(
                "关键词删除失败 chat_id=%s msg_id=%s error=%s",
                message.chat.id,
                message.message_id,
                exc,
            )
        return True
    return False


_MAX_RULES = 100
_MAX_KEYWORDS_PER_RULE = 20
_MAX_KEYWORD_LEN = 100


def validate_keyword_deletion_payload(payload: object) -> tuple[dict, list[str]]:
    """校验后台提交的关键词删除配置。

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

        case_sensitive = bool(item.get("case_sensitive", False))
        require_all = str(item.get("match", "any")).strip().lower() == "all"

        pattern_raw = item.get("pattern")
        keywords_raw = item.get("keywords")
        cleaned: dict = {}

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
            if case_sensitive:
                cleaned["case_sensitive"] = True
        elif pattern_raw is not None or keywords_raw is not None:
            errors.append(f"{label}: pattern 必须是字符串，keywords 必须是数组")
            continue
        else:
            errors.append(f"{label}: 需要配置 keywords 或 pattern")
            continue
        cleaned_rules.append(cleaned)

    normalized: dict = {"rules": cleaned_rules}

    if errors:
        return {}, errors
    return normalized, []


def save_keyword_deletion_rules(path: Path, payload: dict) -> None:
    """原子写入规则文件;保存后缓存按 mtime 自动热重载。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


__all__ = [
    "DeletionRule",
    "configure_keyword_deletions",
    "get_keyword_deletion_rules",
    "save_keyword_deletion_rules",
    "try_keyword_deletion",
    "validate_keyword_deletion_payload",
]
