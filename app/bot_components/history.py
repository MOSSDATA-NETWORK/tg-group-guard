from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Deque, Sequence, TYPE_CHECKING

from .constants import MESSAGE_HISTORY_LIMIT, MAX_HISTORY_TEXT_LENGTH

if TYPE_CHECKING:
    from aiogram.types import Message

# 跟踪的群数量上限,防止开放授权模式下字典无界增长(慢内存泄漏)。
# 超出时按 LRU 淘汰最久未访问的群;被淘汰群的旧 deque 引用仍可用,只是不再共享。
MAX_HISTORY_CHATS = 10000


@dataclass(slots=True)
class HistoryEntry:
    user_id: int
    display_name: str
    text: str
    is_forward: bool


_message_histories: "OrderedDict[int, Deque[HistoryEntry]]" = OrderedDict()


def get_message_history(chat_id: int) -> Deque[HistoryEntry]:
    history = _message_histories.get(chat_id)
    if history is not None:
        _message_histories.move_to_end(chat_id)
        return history
    history = deque(maxlen=MESSAGE_HISTORY_LIMIT)
    _message_histories[chat_id] = history
    while len(_message_histories) > MAX_HISTORY_CHATS:
        _message_histories.popitem(last=False)
    return history


def normalize_history_text(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) > MAX_HISTORY_TEXT_LENGTH:
        cleaned = cleaned[: MAX_HISTORY_TEXT_LENGTH - 3].rstrip() + "..."
    return cleaned


def build_history_entry(
    message: Message,
    text: str,
    *,
    is_user_forward: bool,
) -> HistoryEntry:
    user = message.from_user
    if user is None:
        raise ValueError("message.from_user is required for history entry")
    display_name = (user.full_name or user.username or str(user.id)).strip()
    if not display_name:
        display_name = str(user.id)
    normalized_text = normalize_history_text(text)
    return HistoryEntry(
        user_id=user.id,
        display_name=display_name,
        text=normalized_text,
        is_forward=is_user_forward,
    )


def format_context_for_prompt(
    previous_entries: Sequence[HistoryEntry],
    current_entry: HistoryEntry,
) -> str:
    lines: list[str] = []
    lines.append("【最近群聊上下文】")
    context_entries = previous_entries[-MESSAGE_HISTORY_LIMIT:]
    if context_entries:
        for idx, entry in enumerate(context_entries, start=1):
            prefix = f"[{idx}] 用户ID={entry.user_id} 昵称={entry.display_name}"
            if entry.is_forward:
                prefix = prefix + "（转发）"
            lines.append(f"{prefix}\n内容：{entry.text}")
            lines.append("------")
        if lines[-1] == "------":
            lines.pop()
    else:
        lines.append("无")

    current_prefix = f"用户ID={current_entry.user_id} 昵称={current_entry.display_name}"
    if current_entry.is_forward:
        current_prefix = current_prefix + "（转发）"
    lines.append("")
    lines.append("【待判定消息】")
    lines.append(current_prefix)
    lines.append(f"内容：{current_entry.text}")
    return "\n".join(lines)


__all__ = [
    "HistoryEntry",
    "get_message_history",
    "normalize_history_text",
    "build_history_entry",
    "format_context_for_prompt",
]

