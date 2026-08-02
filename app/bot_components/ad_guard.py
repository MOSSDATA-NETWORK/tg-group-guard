from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, Optional, Sequence, Tuple, TYPE_CHECKING

import aiohttp

from ..ad_guard_rules import (
    get_heuristic_pattern,
    heuristic_pattern_search,
    render_prompt_message,
)
from ..chat_settings import resolve_chat

from .constants import MESSAGE_HISTORY_LIMIT
from .history import HistoryEntry

if TYPE_CHECKING:
    from ..config import Settings


logger = logging.getLogger(__name__)


def heuristic_detect_advertisement(
    text: str,
    *,
    previous_entries: Sequence[HistoryEntry],
) -> bool:
    if _heuristic_match_text(text):
        return True
    if not previous_entries:
        return False

    context_segments = [entry.text for entry in previous_entries if entry.text]
    if not context_segments:
        return False

    context_tail = context_segments[-(MESSAGE_HISTORY_LIMIT - 1) :]
    combined_context = "\n".join([*context_tail, text])
    if _heuristic_match_text(combined_context):
        return True

    max_window = min(4, len(context_tail))
    for window_size in range(1, max_window + 1):
        snippet = "\n".join([*context_tail[-window_size:], text])
        if _heuristic_match_text(snippet):
            return True
    return False


def _heuristic_match_text(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    pattern = get_heuristic_pattern()
    if pattern is None:
        return False
    return heuristic_pattern_search(pattern, normalized)


async def check_advertisement(
    content: str,
    settings: "Settings",
    chat_id: Optional[int] = None,
) -> Tuple[bool, Optional[float]]:
    if chat_id is not None:
        if not resolve_chat(settings, chat_id, "ad_guard_enabled"):
            return (False, None)
    elif not settings.ad_guard_enabled:
        return (False, None)
    provider = getattr(settings, "ad_guard_provider", "ollama")
    if provider == "openai":
        return await _check_advertisement_openai(content, settings, chat_id=chat_id)
    return await _check_advertisement_ollama(content, settings, chat_id=chat_id)


def _effective_threshold(settings: "Settings", chat_id: Optional[int]) -> float:
    if chat_id is None:
        return settings.ad_guard_threshold
    return resolve_chat(settings, chat_id, "ad_guard_threshold")


async def _check_advertisement_ollama(
    content: str,
    settings: "Settings",
    chat_id: Optional[int] = None,
) -> Tuple[bool, Optional[float]]:
    if not settings.ollama_endpoint:
        logger.warning("启用了 Ollama 守卫但未配置 OLLAMA_ENDPOINT")
        return (False, None)

    prompt = render_prompt_message(content.strip())
    logger.debug("广告检测提示词 provider=ollama length=%s\n%s", len(prompt), prompt)

    payload = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0,
            "top_p": 0.1,
            "num_predict": 256,
        },
    }

    url = f"{settings.ollama_endpoint.rstrip('/')}/api/generate"
    timeout = aiohttp.ClientTimeout(total=settings.ollama_timeout_seconds)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Ollama 返回状态码 %s，响应：%s", resp.status, body)
                    return (False, None)
                data = await resp.json()
    except asyncio.TimeoutError:
        logger.warning(
            "Ollama 请求超时（%s 秒），可调大 OLLAMA_TIMEOUT_SECONDS",
            settings.ollama_timeout_seconds,
        )
        return (False, None)
    except aiohttp.ClientError as exc:
        logger.warning("Ollama 请求异常: %r", exc)
        return (False, None)
    except Exception:
        raise

    response_text = data.get("response", "").strip()
    thinking_trace = data.get("thinking", "").strip()
    if thinking_trace:
        logger.info("Ollama 推理思考内容：%s", thinking_trace)
    if not response_text:
        logger.warning("Ollama 响应为空: %s", data)
        return (False, None)

    parsed = _parse_json_response(response_text)
    if not parsed:
        logger.warning("Ollama 响应无法解析: %s; 原始数据: %s", response_text, data)
        return (False, None)

    flagged = bool(parsed.get("advertisement"))
    confidence_raw = parsed.get("confidence")
    confidence: Optional[float] = None
    try:
        if confidence_raw is not None:
            confidence = float(confidence_raw)
            confidence = max(0, min(1, confidence))
    except (TypeError, ValueError):
        confidence = None

    if confidence is not None and confidence < _effective_threshold(settings, chat_id):
        flagged = False

    return (flagged, confidence)


async def _check_advertisement_openai(
    content: str,
    settings: "Settings",
    chat_id: Optional[int] = None,
) -> Tuple[bool, Optional[float]]:
    if not settings.openai_api_key:
        logger.warning("启用了 OpenAI 兼容守卫但未配置 OPENAI_API_KEY")
        return (False, None)

    base_url = settings.openai_endpoint.rstrip("/") if settings.openai_endpoint else None
    if not base_url:
        logger.warning("启用了 OpenAI 兼容守卫但未配置 OPENAI_ENDPOINT")
        return (False, None)

    prompt = render_prompt_message(content.strip())
    logger.debug("广告检测提示词 provider=openai length=%s\n%s", len(prompt), prompt)

    payload = {
        "model": settings.openai_model,
        "messages": [{"role": "user", "content": prompt}],
        "enable_enhancement": True,
    }
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=settings.openai_timeout_seconds)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("OpenAI 兼容端点返回状态码 %s，响应：%s", resp.status, body)
                    return (False, None)
                data = await resp.json()
    except asyncio.TimeoutError:
        logger.warning(
            "OpenAI 兼容请求超时（%s 秒），可调大 OPENAI_TIMEOUT_SECONDS",
            settings.openai_timeout_seconds,
        )
        return (False, None)
    except aiohttp.ClientError as exc:
        logger.warning("OpenAI 兼容请求异常: %r", exc)
        return (False, None)
    except Exception:
        raise

    choices = data.get("choices")
    if not choices:
        logger.warning("OpenAI 兼容响应缺少 choices 字段: %s", data)
        return (False, None)

    choice0 = choices[0]
    if not isinstance(choice0, dict):
        logger.warning("OpenAI 兼容响应 JSON 类型异常: %s", type(choice0).__name__)
        return (False, None)

    message_payload = choice0.get("message")
    if not isinstance(message_payload, dict):
        logger.warning("OpenAI 兼容响应缺少 message 字段: %s", data)
        return (False, None)

    response_text = message_payload.get("content", "").strip()
    if not response_text:
        logger.warning("OpenAI 兼容响应内容为空: %s", data)
        return (False, None)

    parsed = _parse_json_response(response_text)
    if not parsed:
        logger.warning("OpenAI 兼容响应无法解析: %s", response_text)
        return (False, None)

    flagged = bool(parsed.get("advertisement"))
    confidence_raw = parsed.get("confidence")
    confidence: Optional[float] = None
    try:
        if confidence_raw is not None:
            confidence = float(confidence_raw)
            confidence = max(0, min(1, confidence))
    except (TypeError, ValueError):
        confidence = None

    if confidence is not None and confidence < _effective_threshold(settings, chat_id):
        flagged = False

    return (flagged, confidence)


def _parse_json_response(response_text: str) -> Optional[Dict[str, Any]]:
    text = response_text.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed_any = json.loads(text)
    except json.JSONDecodeError:
        return _parse_fallback(text)

    if isinstance(parsed_any, dict):
        return parsed_any
    if isinstance(parsed_any, list):
        for item in parsed_any:
            if isinstance(item, dict) and "advertisement" in item:
                return item
            if isinstance(item, dict):
                for value in item.values():
                    if isinstance(value, str):
                        try:
                            nested = json.loads(value)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(nested, dict) and "advertisement" in nested:
                            return nested
        return None
    return None


def _parse_fallback(response_text: str) -> Optional[Dict[str, Any]]:
    adv_match = re.search(
        r"[\"']advertisement[\"']\s*:\s*(true|false|1|0)",
        response_text,
        flags=re.IGNORECASE,
    )
    if not adv_match:
        return None

    adv_raw = adv_match.group(1).lower()
    flagged = adv_raw in {"true", "1"}
    conf_match = re.search(r"[\"']confidence[\"']\s*:\s*([-+]?\d*\.?\d+)", response_text)
    try:
        confidence = float(conf_match.group(1)) if conf_match else None
    except (TypeError, ValueError):
        confidence = None
    return {"advertisement": flagged, "confidence": confidence}


__all__ = [
    "heuristic_detect_advertisement",
    "check_advertisement",
]

