from __future__ import annotations

from datetime import timedelta, timezone

UTC = timezone.utc
CHINA_TZ = timezone(timedelta(hours=8))

ADMIN_STATUSES = {"administrator", "creator"}

MESSAGE_HISTORY_LIMIT = 10
MAX_HISTORY_TEXT_LENGTH = 400
MAX_LOG_PAYLOAD_LENGTH = 2000

__all__ = [
    "UTC",
    "CHINA_TZ",
    "ADMIN_STATUSES",
    "MESSAGE_HISTORY_LIMIT",
    "MAX_HISTORY_TEXT_LENGTH",
    "MAX_LOG_PAYLOAD_LENGTH",
]

