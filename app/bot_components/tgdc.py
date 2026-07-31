from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass
from typing import Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Chat, ChatPhoto, PhotoSize, UserProfilePhotos

_DC_LOCATION_HINTS: dict[int, str] = {
    1: "迈阿密（美国）",
    2: "阿姆斯特丹（荷兰）",
    3: "迈阿密（美国）",
    4: "阿姆斯特丹（荷兰）",
    5: "新加坡",
}


@dataclass(slots=True)
class DcLookupResult:
    dc_id: int | None
    error: str | None = None

    def describe(self) -> str:
        if self.dc_id is None:
            return "未知"
        hint = _DC_LOCATION_HINTS.get(self.dc_id)
        if hint:
            return f"DC{self.dc_id} · {hint}"
        return f"DC{self.dc_id}"


async def lookup_user_dc(bot: Bot, user_id: int) -> DcLookupResult:
    try:
        photos = await bot.get_user_profile_photos(user_id=user_id, limit=1)
    except (TelegramBadRequest, TelegramForbiddenError):
        return DcLookupResult(dc_id=None, error="无法获取头像")

    file_id = _pick_first_photo_file_id(photos)
    if not file_id:
        return DcLookupResult(dc_id=None, error="未找到公开头像")

    dc_id = _decode_dc_id(file_id)
    if dc_id is None:
        return DcLookupResult(dc_id=None, error="头像 file_id 无法解码")
    return DcLookupResult(dc_id=dc_id)


async def lookup_chat_dc(bot: Bot, chat: Chat) -> DcLookupResult:
    file_id = _chat_photo_file_id(chat.photo)
    fetched_chat = chat

    if not file_id:
        try:
            fetched_chat = await bot.get_chat(chat.id)
        except (TelegramBadRequest, TelegramForbiddenError):
            return DcLookupResult(dc_id=None, error="无法获取群头像")
        file_id = _chat_photo_file_id(fetched_chat.photo)

    if not file_id:
        return DcLookupResult(dc_id=None, error="未设置群头像")

    dc_id = _decode_dc_id(file_id)
    if dc_id is None:
        return DcLookupResult(dc_id=None, error="群头像 file_id 无法解码")
    return DcLookupResult(dc_id=dc_id)


def _chat_photo_file_id(photo: ChatPhoto | None) -> str | None:
    if photo is None:
        return None
    return photo.big_file_id or photo.small_file_id


def _pick_first_photo_file_id(photos: UserProfilePhotos) -> str | None:
    for album in photos.photos:
        candidate = _select_largest_size(album)
        if candidate:
            return candidate.file_id
    return None


def _select_largest_size(sizes: Sequence[PhotoSize]) -> PhotoSize | None:
    best: PhotoSize | None = None
    best_score = -1
    for size in sizes:
        if not size.file_id:
            continue
        score = size.file_size or (size.width * size.height)
        if score > best_score:
            best = size
            best_score = score
    return best


def _decode_dc_id(file_id: str) -> int | None:
    try:
        decoded = _rle_decode(_urlsafe_b64decode(file_id))
    except (binascii.Error, ValueError):
        return None
    if not decoded:
        return None

    major = decoded[-1]
    trailer = 1 if major < 4 else 2
    if len(decoded) <= trailer:
        return None
    payload = decoded[:-trailer]
    if len(payload) < 8:
        return None

    _, dc_id = struct.unpack_from("<ii", payload, 0)
    return dc_id


def _urlsafe_b64decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded)


def _rle_decode(data: bytes) -> bytes:
    output = bytearray()
    expecting_zeros = False
    for byte in data:
        if expecting_zeros:
            output.extend(b"\x00" * byte)
            expecting_zeros = False
            continue
        if byte == 0:
            expecting_zeros = True
            continue
        output.append(byte)
    return bytes(output)


__all__ = ["DcLookupResult", "lookup_user_dc", "lookup_chat_dc"]

