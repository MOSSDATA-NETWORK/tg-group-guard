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

# OPENAI_ENDPOINT 留空时的官方默认端点(字段说明承诺"可留空用官方")
_OFFICIAL_OPENAI_ENDPOINT = "https://api.openai.com"


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

    # 端点留空时使用官方地址(与字段说明"可留空用官方"一致);
    # 修复前端点留空会每条消息静默跳过,守卫形同虚设但 UI 显示开启
    base_url = (
        settings.openai_endpoint.rstrip("/")
        if settings.openai_endpoint
        else _OFFICIAL_OPENAI_ENDPOINT
    )

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


class ModelProbeError(Exception):
    """拉取模型列表失败。message 会原样显示给后台管理员，要写成人话。"""


# 列目录几秒就该回。复用 OPENAI_TIMEOUT_SECONDS 会让一个打不开的端点
# 把后台按钮卡上好几分钟——那个超时是留给推理的。
_MODEL_PROBE_TIMEOUT_SECONDS = 15
# OneAPI 这类聚合网关能返回上千个模型，全塞进下拉框会把页面拖垮
_MODEL_PROBE_LIMIT = 500
# 端点是管理员随手填的，填错时对面可能是任何服务。不设上限地整个读进内存，
# 一个指向大文件或日志流的地址就能把 bot 进程撑爆。上千个模型也就几百 KB。
_MODEL_PROBE_MAX_BYTES = 2 * 1024 * 1024


def _brief(text: str, limit: int = 160) -> str:
    cleaned = " ".join(text.split())
    return (cleaned[:limit] + "…") if len(cleaned) > limit else cleaned


def _extract_model_ids(data: Any) -> list[str]:
    """OpenAI 官方是 {"data": [{"id": ...}]}；部分聚合网关直接返回数组或用 name。"""
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ModelProbeError("响应里找不到模型列表，请确认端点是否为 OpenAI 兼容根路径")
    ids: list[str] = []
    for item in items:
        raw = (item.get("id") or item.get("name")) if isinstance(item, dict) else item
        if isinstance(raw, str) and raw.strip():
            ids.append(raw.strip())
    if not ids:
        raise ModelProbeError("端点没有返回任何模型")
    return sorted(set(ids))[:_MODEL_PROBE_LIMIT]


async def list_openai_models(
    endpoint: Optional[str],
    api_key: Optional[str],
) -> list[str]:
    """向 OpenAI 兼容端点要一份可用模型清单，供后台「获取」按钮使用。"""
    if not api_key:
        raise ModelProbeError("未配置 OpenAI API Key")
    base = endpoint.rstrip("/") if endpoint else _OFFICIAL_OPENAI_ENDPOINT
    if not base.startswith(("http://", "https://")):
        raise ModelProbeError("端点必须以 http:// 或 https:// 开头")

    url = f"{base}/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = aiohttp.ClientTimeout(total=_MODEL_PROBE_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                # 不能用 resp.text()：它会把整个响应读进内存，没有上限。
                # 这里边读边数，超了立刻断开。
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > _MODEL_PROBE_MAX_BYTES:
                        raise ModelProbeError(
                            "端点返回的内容过大，请确认地址是否为 OpenAI 兼容根路径"
                        )
                    chunks.append(chunk)
                # 二进制服务的响应解出来是替换字符，后面 json.loads 会挡住
                body = b"".join(chunks).decode("utf-8", errors="replace")
                if resp.status != 200:
                    logger.warning(
                        "拉取模型列表失败 status=%s url=%s body=%s",
                        resp.status,
                        url,
                        _brief(body, 500),
                    )
                    raise ModelProbeError(f"端点返回 {resp.status}：{_brief(body)}")
                data = json.loads(body)
    except asyncio.TimeoutError as exc:
        raise ModelProbeError(f"请求超时（{_MODEL_PROBE_TIMEOUT_SECONDS} 秒）") from exc
    except aiohttp.ClientError as exc:
        raise ModelProbeError(f"无法连接端点：{exc}") from exc
    # 捕 ValueError 而不是 JSONDecodeError：解码兜底改成 errors="replace" 之后
    # 走的仍是 JSON 解析失败，但留宽一档，免得哪天换回严格解码就漏出去变成 500，
    # 管理员那边只能看到一句"服务器错误"。
    except ValueError as exc:
        raise ModelProbeError("端点返回的不是 JSON，请确认地址是否为 OpenAI 兼容根路径") from exc
    return _extract_model_ids(data)


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
    "list_openai_models",
    "ModelProbeError",
]

