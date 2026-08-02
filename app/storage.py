from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

try:  # pragma: no cover - 运行时需安装依赖
    import aiosqlite  # type: ignore[import]
except ModuleNotFoundError as exc:  # pragma: no cover
    raise RuntimeError("运行 Telegram-group-guard-bot 需要安装 aiosqlite 依赖") from exc


from .bot_components.constants import CHINA_TZ


UTC = timezone.utc


AD_SOURCE_LABELS = {
    "rule": "规则命中",
    "llm": "AI 判定",
    "review": "管理员复核",
}

AD_FINAL_ACTION_LABELS = {
    "none": "已通过",
    "deleted": "已删除",
    "banned": "已封禁",
    "restored": "已恢复",
}

BAN_REASON_LABELS = {
    "ad_auto": "广告自动封禁",
    "ad_review": "广告复核封禁",
    "ad_vote_force": "广告强制封禁",
    "low_score": "低分用户清理",
    "admin_cmd": "管理员命令",
    "warn_limit": "警告上限",
    "verify_admin_ban": "入群验证封禁",
    "verify_admin_tempban_1h": "入群验证临时封禁 1h",
    "web_unban": "后台解封",
}

BAN_ACTION_LABELS = {
    "ban": "封禁",
    "kick": "踢出",
    "unban": "解封",
}

VERIFICATION_EVENT_LABELS = {
    "joined": "新成员",
    "pending": "待验证",
    "verified": "已通过",
    "expired": "已超时",
    "failed": "失败",
    "admin_skip": "管理员放行",
    "admin_ban": "管理员封禁",
    "admin_tempban": "管理员临时封禁 1h",
}


@dataclass(slots=True)
class VerificationRecord:
    token: str
    chat_id: int
    user_id: int
    username: Optional[str]
    status: str
    created_at: datetime
    expire_at: datetime
    verified_at: Optional[datetime]
    prompt_message_id: Optional[int]


class VerificationStore:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        # token 级串行化锁,防止同一 token 被并发请求重复处理
        # (虽然 mark_verified 有 WHERE status='pending' 保护,但
        # lift_restrictions/announce_group_success 仍可能执行两次)
        # 值 = [锁, 引用计数]；只有引用归零且锁空闲时才从字典移除,
        # 避免释放时 pop 掉仍有等待者的锁导致并发穿越（TOCTOU）
        self._token_locks: dict[str, list] = {}
        self._token_locks_guard = asyncio.Lock()

    async def acquire_token_lock(self, token: str) -> asyncio.Lock:
        async with self._token_locks_guard:
            entry = self._token_locks.get(token)
            if entry is None:
                entry = [asyncio.Lock(), 0]
                self._token_locks[token] = entry
            entry[1] += 1
            return entry[0]

    async def release_token_lock(self, token: str) -> None:
        async with self._token_locks_guard:
            entry = self._token_locks.get(token)
            if entry is None:
                return
            entry[1] -= 1
            if entry[1] <= 0 and not entry[0].locked():
                self._token_locks.pop(token, None)

    async def connect(self) -> None:
        if self._db is not None:
            return

        self._db = await aiosqlite.connect(self._database_path)
        self._db.row_factory = aiosqlite.Row

        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA foreign_keys=ON;")
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS verifications (
                token TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expire_at INTEGER NOT NULL,
                verified_at INTEGER,
                prompt_message_id INTEGER
            );
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                issued_by INTEGER NOT NULL,
                reason TEXT,
                created_at INTEGER NOT NULL
            );
            """
        )
        await self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_warnings_chat_user
            ON warnings(chat_id, user_id, created_at);
            """
        )
        # 被广告流程删除的消息快照,用于管理员后续 restore
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS ad_deletions (
                token TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_text TEXT,
                display_name TEXT,
                confidence REAL,
                deleted_at INTEGER NOT NULL,
                restore_eligible_until INTEGER
            );
            """
        )
        await self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ad_deletions_chat_user
            ON ad_deletions(chat_id, user_id, deleted_at);
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS ad_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT,
                username TEXT,
                message_text TEXT,
                source TEXT NOT NULL,
                flagged INTEGER NOT NULL,
                confidence REAL,
                vote_used INTEGER NOT NULL DEFAULT 0,
                vote_adv INTEGER,
                vote_normal INTEGER,
                final_action TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        await self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ad_decisions_chat_created
            ON ad_decisions(chat_id, created_at DESC);
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS ban_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT,
                operator_id INTEGER,
                operator_name TEXT,
                reason TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                currently_banned INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        await self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ban_events_chat_created
            ON ban_events(chat_id, created_at DESC);
            """
        )
        await self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ban_events_chat_user
            ON ban_events(chat_id, user_id, created_at DESC);
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS verification_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                event TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        await self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_verification_events_chat_created
            ON verification_events(chat_id, created_at DESC);
            """
        )
        await self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_verification_events_event_created
            ON verification_events(event, created_at DESC);
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS ad_qualified_users (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                valid_count INTEGER NOT NULL DEFAULT 0,
                qualified INTEGER NOT NULL DEFAULT 0,
                display_name TEXT,
                username TEXT,
                qualified_at INTEGER,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );
            """
        )
        await self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ad_qualified_users_qualified
            ON ad_qualified_users(chat_id, qualified);
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_deletions (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                delete_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
            """
        )
        await self._db.commit()

        try:
            await self._db.execute(
                "ALTER TABLE verifications ADD COLUMN prompt_message_id INTEGER"
            )
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def create(self, record: VerificationRecord) -> None:
        await self._ensure_connected()
        async with self._lock:
            await self._db.execute(
                """
                INSERT OR REPLACE INTO verifications (
                    token, chat_id, user_id, username, status, created_at, expire_at, verified_at, prompt_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.token,
                    record.chat_id,
                    record.user_id,
                    record.username,
                    record.status,
                    int(record.created_at.timestamp()),
                    int(record.expire_at.timestamp()),
                    int(record.verified_at.timestamp()) if record.verified_at else None,
                    record.prompt_message_id,
                ),
            )
            await self._db.commit()

    async def get(self, token: str) -> Optional[VerificationRecord]:
        await self._ensure_connected()
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT * FROM verifications WHERE token = ?", (token,)
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None
        return self._row_to_record(row)

    async def get_pending(self, chat_id: int, user_id: int) -> Optional[VerificationRecord]:
        await self._ensure_connected()
        async with self._lock:
            cursor = await self._db.execute(
                """
                SELECT * FROM verifications
                WHERE chat_id = ? AND user_id = ? AND status = ?
                """,
                (chat_id, user_id, "pending"),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None
        return self._row_to_record(row)

    async def mark_verified(self, token: str, verified_at: datetime) -> bool:
        await self._ensure_connected()
        async with self._lock:
            result = await self._db.execute(
                """
                UPDATE verifications
                SET status = ?, verified_at = ?
                WHERE token = ? AND status = ?
                """,
                (
                    "verified",
                    int(verified_at.timestamp()),
                    token,
                    "pending",
                ),
            )
            await self._db.commit()
            return result.rowcount > 0

    async def mark_failed(self, token: str, failed_at: datetime) -> bool:
        await self._ensure_connected()
        async with self._lock:
            result = await self._db.execute(
                """
                UPDATE verifications
                SET status = ?, verified_at = ?
                WHERE token = ? AND status = ?
                """,
                (
                    "failed",
                    int(failed_at.timestamp()),
                    token,
                    "pending",
                ),
            )
            await self._db.commit()
            return result.rowcount > 0

    async def delete(self, token: str) -> None:
        await self._ensure_connected()
        async with self._lock:
            await self._db.execute("DELETE FROM verifications WHERE token = ?", (token,))
            await self._db.commit()

    async def delete_if_exists(self, chat_id: int, user_id: int) -> bool:
        await self._ensure_connected()
        async with self._lock:
            result = await self._db.execute(
                "DELETE FROM verifications WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            await self._db.commit()
            return bool(result.rowcount)

    # ===== TTL 消息删除任务持久化（重启后恢复） =====

    async def schedule_message_deletion(
        self, chat_id: int, message_id: int, delete_at: datetime
    ) -> None:
        await self._ensure_connected()
        async with self._lock:
            await self._db.execute(
                """
                INSERT OR REPLACE INTO scheduled_deletions (chat_id, message_id, delete_at)
                VALUES (?, ?, ?)
                """,
                (chat_id, message_id, int(delete_at.timestamp())),
            )
            await self._db.commit()

    async def remove_scheduled_deletion(self, chat_id: int, message_id: int) -> None:
        await self._ensure_connected()
        async with self._lock:
            await self._db.execute(
                "DELETE FROM scheduled_deletions WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            )
            await self._db.commit()

    async def fetch_scheduled_deletions(self) -> list[dict[str, int]]:
        await self._ensure_connected()
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT chat_id, message_id, delete_at FROM scheduled_deletions"
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [dict(row) for row in rows]

    async def add_warning(
        self,
        chat_id: int,
        user_id: int,
        issued_by: int,
        reason: Optional[str] = None,
        issued_at: Optional[datetime] = None,
    ) -> int:
        await self._ensure_connected()
        issued_at = issued_at or datetime.now(tz=UTC)
        timestamp = int(issued_at.timestamp())
        async with self._lock:
            await self._db.execute(
                """
                INSERT INTO warnings (chat_id, user_id, issued_by, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, user_id, issued_by, reason, timestamp),
            )
            count = await self._count_warnings_locked(chat_id, user_id, issued_at)
            await self._db.commit()
        return count

    async def count_warnings_in_month(
        self,
        chat_id: int,
        user_id: int,
        reference: Optional[datetime] = None,
    ) -> int:
        await self._ensure_connected()
        reference = reference or datetime.now(tz=UTC)
        async with self._lock:
            return await self._count_warnings_locked(chat_id, user_id, reference)

    async def fetch_expired(self, reference: Optional[datetime] = None) -> list[VerificationRecord]:
        await self._ensure_connected()
        if reference is None:
            reference = datetime.now(tz=UTC)
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT * FROM verifications WHERE status = ? AND expire_at <= ?",
                ("pending", int(reference.timestamp())),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._row_to_record(row) for row in rows]

    async def set_prompt_message(self, token: str, message_id: Optional[int]) -> None:
        await self._ensure_connected()
        async with self._lock:
            await self._db.execute(
                "UPDATE verifications SET prompt_message_id = ? WHERE token = ?",
                (message_id, token),
            )
            await self._db.commit()

    async def record_ad_deletion(
        self,
        *,
        token: str,
        chat_id: int,
        user_id: int,
        message_text: str,
        display_name: Optional[str],
        confidence: Optional[float],
        deleted_at: datetime,
        restore_eligible_until: Optional[datetime] = None,
    ) -> None:
        """记录一条广告删除快照,供 adreview:restore 流程读取原文。"""
        await self._ensure_connected()
        async with self._lock:
            await self._db.execute(
                """
                INSERT OR REPLACE INTO ad_deletions (
                    token, chat_id, user_id, message_text, display_name,
                    confidence, deleted_at, restore_eligible_until
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    chat_id,
                    user_id,
                    message_text,
                    display_name,
                    confidence,
                    int(deleted_at.timestamp()),
                    int(restore_eligible_until.timestamp())
                    if restore_eligible_until
                    else None,
                ),
            )
            await self._db.commit()

    async def delete_ad_deletion(self, token: str) -> None:
        await self._ensure_connected()
        async with self._lock:
            await self._db.execute(
                "DELETE FROM ad_deletions WHERE token = ?",
                (token,),
            )
            await self._db.commit()

    async def get_ad_qualification(
        self, chat_id: int, user_id: int
    ) -> Tuple[int, bool]:
        """返回 (valid_count, qualified)。无记录时为 (0, False)。"""
        await self._ensure_connected()
        async with self._lock:
            cursor = await self._db.execute(
                """
                SELECT valid_count, qualified
                FROM ad_qualified_users
                WHERE chat_id = ? AND user_id = ?
                """,
                (chat_id, user_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return (0, False)
        return (int(row["valid_count"]), bool(row["qualified"]))

    async def record_ad_valid_speech(
        self,
        *,
        chat_id: int,
        user_id: int,
        threshold: int,
        display_name: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Tuple[int, bool]:
        """通过广告检测后累加有效发言；达到 threshold 后永久合格。

        返回 (valid_count, qualified)。已合格用户不再累加。
        """
        await self._ensure_connected()
        now_ts = int(datetime.now(tz=UTC).timestamp())
        threshold = max(int(threshold), 1)
        async with self._lock:
            cursor = await self._db.execute(
                """
                SELECT valid_count, qualified
                FROM ad_qualified_users
                WHERE chat_id = ? AND user_id = ?
                """,
                (chat_id, user_id),
            )
            row = await cursor.fetchone()
            if row is None:
                valid_count = 1
                qualified = 1 if valid_count >= threshold else 0
                await self._db.execute(
                    """
                    INSERT INTO ad_qualified_users (
                        chat_id, user_id, valid_count, qualified,
                        display_name, username, qualified_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chat_id,
                        user_id,
                        valid_count,
                        qualified,
                        display_name,
                        username,
                        now_ts if qualified else None,
                        now_ts,
                    ),
                )
            elif int(row["qualified"]):
                valid_count = int(row["valid_count"])
                qualified = 1
            else:
                valid_count = int(row["valid_count"]) + 1
                qualified = 1 if valid_count >= threshold else 0
                await self._db.execute(
                    """
                    UPDATE ad_qualified_users
                    SET valid_count = ?,
                        qualified = ?,
                        display_name = COALESCE(?, display_name),
                        username = COALESCE(?, username),
                        qualified_at = CASE
                            WHEN ? = 1 AND qualified_at IS NULL THEN ?
                            ELSE qualified_at
                        END,
                        updated_at = ?
                    WHERE chat_id = ? AND user_id = ?
                    """,
                    (
                        valid_count,
                        qualified,
                        display_name,
                        username,
                        qualified,
                        now_ts,
                        now_ts,
                        chat_id,
                        user_id,
                    ),
                )
            await self._db.commit()
        return (valid_count, bool(qualified))

    async def mark_ad_qualified(
        self,
        *,
        chat_id: int,
        user_id: int,
        threshold: int,
        display_name: Optional[str] = None,
        username: Optional[str] = None,
    ) -> None:
        """直接标记为永久合格（如管理员复核恢复）。"""
        await self._ensure_connected()
        now_ts = int(datetime.now(tz=UTC).timestamp())
        threshold = max(int(threshold), 1)
        async with self._lock:
            await self._db.execute(
                """
                INSERT INTO ad_qualified_users (
                    chat_id, user_id, valid_count, qualified,
                    display_name, username, qualified_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    valid_count = MAX(ad_qualified_users.valid_count, excluded.valid_count),
                    qualified = 1,
                    display_name = COALESCE(excluded.display_name, ad_qualified_users.display_name),
                    username = COALESCE(excluded.username, ad_qualified_users.username),
                    qualified_at = COALESCE(ad_qualified_users.qualified_at, excluded.qualified_at),
                    updated_at = excluded.updated_at
                """,
                (
                    chat_id,
                    user_id,
                    threshold,
                    display_name,
                    username,
                    now_ts,
                    now_ts,
                ),
            )
            await self._db.commit()

    async def reset_ad_qualification(self, chat_id: int, user_id: int) -> None:
        await self._ensure_connected()
        async with self._lock:
            await self._db.execute(
                "DELETE FROM ad_qualified_users WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            await self._db.commit()

    async def record_ad_decision(
        self,
        *,
        chat_id: int,
        user_id: int,
        display_name: Optional[str],
        username: Optional[str],
        message_text: str,
        source: str,
        flagged: bool,
        confidence: Optional[float],
        final_action: str,
        created_at: Optional[datetime] = None,
        vote_used: bool = False,
        vote_adv: Optional[int] = None,
        vote_normal: Optional[int] = None,
    ) -> int:
        await self._ensure_connected()
        created_at = created_at or datetime.now(tz=UTC)
        async with self._lock:
            cursor = await self._db.execute(
                """
                INSERT INTO ad_decisions (
                    chat_id, user_id, display_name, username, message_text, source,
                    flagged, confidence, vote_used, vote_adv, vote_normal,
                    final_action, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    user_id,
                    display_name,
                    username,
                    message_text,
                    source,
                    1 if flagged else 0,
                    confidence,
                    1 if vote_used else 0,
                    vote_adv,
                    vote_normal,
                    final_action,
                    int(created_at.timestamp()),
                ),
            )
            await self._db.commit()
            return int(cursor.lastrowid)

    async def recent_ad_decisions(
        self,
        chat_ids: set[int],
        *,
        limit: int = 50,
        offset: int = 0,
        chat_id: Optional[int] = None,
        flagged_only: bool = False,
        user_id: Optional[int] = None,
        since: Optional[int] = None,
    ) -> list[dict]:
        await self._ensure_connected()
        scoped_ids = set(chat_ids)
        if chat_id is not None:
            if chat_id not in scoped_ids:
                return []
            scoped_ids = {chat_id}
        if not scoped_ids:
            return []

        placeholders = ",".join("?" for _ in scoped_ids)
        where = [f"chat_id IN ({placeholders})"]
        params: list[object] = list(scoped_ids)
        if flagged_only:
            where.append("flagged = 1")
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if since is not None:
            where.append("created_at >= ?")
            params.append(int(since))
        query = f"""
            SELECT *
            FROM ad_decisions
            WHERE {" AND ".join(where)}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
        """
        params.extend([max(1, min(limit, 200)), max(offset, 0)])

        async with self._lock:
            cursor = await self._db.execute(query, tuple(params))
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._enrich_ad_decision(self._row_to_dict(row)) for row in rows]

    async def record_ban_event(
        self,
        *,
        chat_id: int,
        user_id: int,
        display_name: Optional[str],
        operator_id: Optional[int],
        operator_name: Optional[str],
        reason: str,
        action: str,
        created_at: Optional[datetime] = None,
        currently_banned: Optional[bool] = None,
    ) -> int:
        await self._ensure_connected()
        created_at = created_at or datetime.now(tz=UTC)
        is_banned = currently_banned if currently_banned is not None else action == "ban"
        async with self._lock:
            cursor = await self._db.execute(
                """
                INSERT INTO ban_events (
                    chat_id, user_id, display_name, operator_id, operator_name,
                    reason, action, created_at, currently_banned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    user_id,
                    display_name,
                    operator_id,
                    operator_name,
                    reason,
                    action,
                    int(created_at.timestamp()),
                    1 if is_banned else 0,
                ),
            )
            await self._db.commit()
            return int(cursor.lastrowid)

    async def recent_ban_events(
        self,
        chat_ids: set[int],
        *,
        limit: int = 50,
        offset: int = 0,
        chat_id: Optional[int] = None,
        user_id: Optional[int] = None,
        only_banned: bool = False,
        since: Optional[int] = None,
    ) -> list[dict]:
        await self._ensure_connected()
        scoped_ids = set(chat_ids)
        if chat_id is not None:
            if chat_id not in scoped_ids:
                return []
            scoped_ids = {chat_id}
        if not scoped_ids:
            return []

        placeholders = ",".join("?" for _ in scoped_ids)
        params: list[object] = list(scoped_ids)
        where_extra = []
        if user_id is not None:
            where_extra.append("user_id = ?")
            params.append(user_id)
        if since is not None:
            where_extra.append("created_at >= ?")
            params.append(int(since))
        user_filter = ("AND " + " AND ".join(where_extra)) if where_extra else ""
        latest_filter = ""
        if only_banned:
            latest_filter = """
                AND NOT EXISTS (
                    SELECT 1
                    FROM ban_events newer
                    WHERE newer.chat_id = ban_events.chat_id
                      AND newer.user_id = ban_events.user_id
                      AND (
                        newer.created_at > ban_events.created_at OR
                        (newer.created_at = ban_events.created_at AND newer.id > ban_events.id)
                      )
                )
                AND action = 'ban'
                AND currently_banned = 1
            """
        query = f"""
            SELECT *,
                (
                    SELECT CASE WHEN latest.action = 'ban' AND latest.currently_banned = 1
                                THEN 1 ELSE 0 END
                    FROM ban_events latest
                    WHERE latest.chat_id = ban_events.chat_id
                      AND latest.user_id = ban_events.user_id
                    ORDER BY latest.created_at DESC, latest.id DESC
                    LIMIT 1
                ) AS is_active
            FROM ban_events
            WHERE chat_id IN ({placeholders})
            {user_filter}
            {latest_filter}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
        """
        params.extend([max(1, min(limit, 200)), max(offset, 0)])

        async with self._lock:
            cursor = await self._db.execute(query, tuple(params))
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._enrich_ban_event(self._row_to_dict(row)) for row in rows]

    async def mark_unbanned(
        self,
        chat_id: int,
        user_id: int,
        *,
        at: Optional[datetime] = None,
        operator_id: Optional[int],
        operator_name: Optional[str],
        display_name: Optional[str] = None,
        reason: str = "web_unban",
    ) -> int:
        return await self.record_ban_event(
            chat_id=chat_id,
            user_id=user_id,
            display_name=display_name,
            operator_id=operator_id,
            operator_name=operator_name,
            reason=reason,
            action="unban",
            created_at=at,
            currently_banned=False,
        )

    async def record_verification_event(
        self,
        *,
        chat_id: int,
        user_id: int,
        username: Optional[str],
        event: str,
        created_at: Optional[datetime] = None,
    ) -> int:
        await self._ensure_connected()
        created_at = created_at or datetime.now(tz=UTC)
        async with self._lock:
            cursor = await self._db.execute(
                """
                INSERT INTO verification_events (
                    chat_id, user_id, username, event, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    user_id,
                    username,
                    event,
                    int(created_at.timestamp()),
                ),
            )
            await self._db.commit()
            return int(cursor.lastrowid)

    async def recent_verification_events(
        self,
        chat_ids: set[int],
        *,
        limit: int = 50,
        offset: int = 0,
        chat_id: Optional[int] = None,
        user_id: Optional[int] = None,
        event: Optional[str] = None,
        since: Optional[int] = None,
    ) -> list[dict]:
        await self._ensure_connected()
        if since is None:
            # 默认只扫描最近 30 天的事件,避免表涨大后每次后台刷新都全表扫描;
            # 需要更早的记录时由调用方显式传 since
            since = int((datetime.now(tz=UTC) - timedelta(days=30)).timestamp())
        scoped_ids = set(chat_ids)
        if chat_id is not None:
            if chat_id not in scoped_ids:
                return []
            scoped_ids = {chat_id}
        if not scoped_ids:
            return []

        placeholders = ",".join("?" for _ in scoped_ids)
        where = [f"chat_id IN ({placeholders})"]
        params: list[object] = list(scoped_ids)
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if since is not None:
            where.append("created_at >= ?")
            params.append(int(since))
        query = f"""
            SELECT *
            FROM verification_events
            WHERE {" AND ".join(where)}
            ORDER BY created_at ASC, id ASC
        """

        async with self._lock:
            cursor = await self._db.execute(query, tuple(params))
            event_rows = [self._row_to_dict(row) for row in await cursor.fetchall()]
            await cursor.close()

        sessions: list[dict] = []
        latest_by_user: dict[tuple[int, int], dict] = {}
        terminal_events = {"verified", "expired", "failed", "admin_skip", "admin_ban", "admin_tempban"}
        for row in event_rows:
            key = (int(row["chat_id"]), int(row["user_id"]))
            if row["event"] == "joined":
                session = {
                    "id": row["id"],
                    "chat_id": row["chat_id"],
                    "user_id": row["user_id"],
                    "username": row["username"],
                    "joined_at": row["created_at"],
                    "resolved_at": None,
                    "event": "pending",
                }
                sessions.append(session)
                latest_by_user[key] = session
            elif row["event"] in terminal_events and key in latest_by_user:
                session = latest_by_user[key]
                session["event"] = row["event"]
                session["event_id"] = row["id"]
                session["resolved_at"] = row["created_at"]
                session["username"] = row["username"] or session.get("username")

        if event:
            sessions = [session for session in sessions if session["event"] == event]
        sessions.sort(key=lambda session: (int(session["joined_at"]), int(session["id"])), reverse=True)
        sliced = sessions[max(offset, 0): max(offset, 0) + max(1, min(limit, 200))]
        return [self._enrich_verification_event(session) for session in sliced]

    async def summarize_metrics(
        self,
        chat_ids: set[int],
        *,
        chat_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> dict:
        """聚合 KPI、7 天趋势、按群拆分、Top 拦截原因，一次查询给前端。"""
        await self._ensure_connected()
        scoped = set(chat_ids)
        if chat_id is not None:
            if chat_id not in scoped:
                return {"empty": True}
            scoped = {chat_id}
        if not scoped:
            return {"empty": True}

        now = now or datetime.now(tz=UTC)
        local = now.astimezone(CHINA_TZ)
        today_start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start = today_start_local.astimezone(UTC)
        week_start = today_start - timedelta(days=6)
        now_ts = int(now.timestamp())

        placeholders = ",".join("?" for _ in scoped)
        chat_params = tuple(scoped)

        async with self._lock:
            # 今日 KPI
            kpi = {
                "joined": 0,
                "verified": 0,
                "expired": 0,
                "failed": 0,
                "admin_skip": 0,
                "admin_ban": 0,
                "admin_tempban": 0,
                "ban_actions": 0,
                "ad_flagged": 0,
                "ad_total": 0,
            }

            cursor = await self._db.execute(
                f"""
                SELECT event, COUNT(*) AS n
                FROM verification_events
                WHERE chat_id IN ({placeholders})
                  AND created_at >= ? AND created_at <= ?
                GROUP BY event
                """,
                chat_params + (int(today_start.timestamp()), now_ts),
            )
            for row in await cursor.fetchall():
                # 仅统计已知事件;未知事件直接忽略,避免 KeyError 打挂仪表盘接口
                if row["event"] in kpi:
                    kpi[row["event"]] = int(row["n"])
            await cursor.close()

            cursor = await self._db.execute(
                f"""
                SELECT
                  SUM(CASE WHEN action IN ('ban','kick') THEN 1 ELSE 0 END) AS bans,
                  COUNT(*) AS total
                FROM ban_events
                WHERE chat_id IN ({placeholders})
                  AND created_at >= ? AND created_at <= ?
                """,
                chat_params + (int(today_start.timestamp()), now_ts),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row:
                kpi["ban_actions"] = int(row["bans"] or 0)

            cursor = await self._db.execute(
                f"""
                SELECT
                  SUM(CASE WHEN flagged = 1 THEN 1 ELSE 0 END) AS flagged,
                  COUNT(*) AS total
                FROM ad_decisions
                WHERE chat_id IN ({placeholders})
                  AND created_at >= ? AND created_at <= ?
                """,
                chat_params + (int(today_start.timestamp()), now_ts),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row:
                kpi["ad_flagged"] = int(row["flagged"] or 0)
                kpi["ad_total"] = int(row["total"] or 0)

            # 7 天趋势：按日聚合 join/verified/ban
            cursor = await self._db.execute(
                f"""
                SELECT event, created_at
                FROM verification_events
                WHERE chat_id IN ({placeholders})
                  AND created_at >= ?
                """,
                chat_params + (int(week_start.timestamp()),),
            )
            event_rows = await cursor.fetchall()
            await cursor.close()

            cursor = await self._db.execute(
                f"""
                SELECT action, created_at
                FROM ban_events
                WHERE chat_id IN ({placeholders})
                  AND created_at >= ?
                """,
                chat_params + (int(week_start.timestamp()),),
            )
            ban_rows = await cursor.fetchall()
            await cursor.close()

            # 按群拆分（仅今日）
            cursor = await self._db.execute(
                f"""
                SELECT chat_id, event, COUNT(*) AS n
                FROM verification_events
                WHERE chat_id IN ({placeholders})
                  AND created_at >= ? AND created_at <= ?
                GROUP BY chat_id, event
                """,
                chat_params + (int(today_start.timestamp()), now_ts),
            )
            per_chat_rows = await cursor.fetchall()
            await cursor.close()

            # 24h 小时分布（用最近 24h 而不是今日，热力更稳）；
            # 只统计 joined 事件,否则每次进群伴随的 verified/expired 等
            # 终态事件会让分布虚高至约 2 倍
            day_ago = now - timedelta(hours=24)
            cursor = await self._db.execute(
                f"""
                SELECT created_at, event
                FROM verification_events
                WHERE chat_id IN ({placeholders})
                  AND created_at >= ?
                  AND event = 'joined'
                """,
                chat_params + (int(day_ago.timestamp()),),
            )
            hour_rows = await cursor.fetchall()
            await cursor.close()

            # Top 广告判定来源（最近 7 天，仅 flagged）
            cursor = await self._db.execute(
                f"""
                SELECT source, COUNT(*) AS n
                FROM ad_decisions
                WHERE chat_id IN ({placeholders})
                  AND flagged = 1
                  AND created_at >= ?
                GROUP BY source
                ORDER BY n DESC
                LIMIT 5
                """,
                chat_params + (int(week_start.timestamp()),),
            )
            top_sources = [
                {"source": row["source"], "count": int(row["n"])}
                for row in await cursor.fetchall()
            ]
            await cursor.close()

        # 整理 7 天趋势
        days: list[dict] = []
        for i in range(7):
            d_local = today_start_local - timedelta(days=6 - i)
            d_start = d_local.astimezone(UTC)
            d_end = (d_local + timedelta(days=1)).astimezone(UTC)
            days.append({
                "date": d_local.strftime("%m-%d"),
                "start": int(d_start.timestamp()),
                "end": int(d_end.timestamp()),
                "joined": 0,
                "verified": 0,
                "bans": 0,
            })
        for row in event_rows:
            ts = int(row["created_at"])
            for d in days:
                if d["start"] <= ts < d["end"]:
                    if row["event"] == "joined":
                        d["joined"] += 1
                    elif row["event"] == "verified":
                        d["verified"] += 1
                    break
        for row in ban_rows:
            if row["action"] not in ("ban", "kick"):
                continue
            ts = int(row["created_at"])
            for d in days:
                if d["start"] <= ts < d["end"]:
                    d["bans"] += 1
                    break

        per_chat: dict[int, dict] = {}
        for row in per_chat_rows:
            cid = int(row["chat_id"])
            slot = per_chat.setdefault(cid, {"chat_id": cid, "joined": 0, "verified": 0})
            if row["event"] in slot:
                slot[row["event"]] = int(row["n"])
            elif row["event"] == "joined":
                slot["joined"] = int(row["n"])
            elif row["event"] == "verified":
                slot["verified"] = int(row["n"])
        per_chat_list = []
        for cid, slot in per_chat.items():
            joined = slot.get("joined", 0)
            verified = slot.get("verified", 0)
            rate = (verified / joined) if joined else None
            per_chat_list.append({
                "chat_id": cid,
                "joined": joined,
                "verified": verified,
                "pass_rate": rate,
            })
        per_chat_list.sort(key=lambda x: x["joined"], reverse=True)

        hour_buckets = [0] * 24
        for row in hour_rows:
            hour = datetime.fromtimestamp(int(row["created_at"]), tz=UTC) \
                .astimezone(CHINA_TZ).hour
            hour_buckets[hour] += 1

        joined = kpi["joined"]
        verified = kpi["verified"]
        pass_rate = (verified / joined) if joined else None
        return {
            "today": {
                "joined": joined,
                "verified": verified,
                "expired": kpi["expired"],
                "failed": kpi["failed"],
                "admin_skip": kpi["admin_skip"],
                "admin_ban": kpi["admin_ban"],
                "admin_tempban": kpi["admin_tempban"],
                "pass_rate": pass_rate,
                "bans": kpi["ban_actions"],
                "ad_flagged": kpi["ad_flagged"],
                "ad_total": kpi["ad_total"],
            },
            "trend": days,
            "per_chat": per_chat_list,
            "hourly": hour_buckets,
            "top_sources": top_sources,
            "generated_at": now_ts,
        }

    async def _ensure_connected(self) -> None:
        if self._db is None:
            await self.connect()

    async def _count_warnings_locked(self, chat_id: int, user_id: int, reference: datetime) -> int:
        period_start, period_end = self._month_range(reference)
        cursor = await self._db.execute(
            """
            SELECT COUNT(*) AS total
            FROM warnings
            WHERE chat_id = ? AND user_id = ?
              AND created_at >= ? AND created_at < ?
            """,
            (
                chat_id,
                user_id,
                int(period_start.timestamp()),
                int(period_end.timestamp()),
            ),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return 0
        total = row["total"] if isinstance(row, aiosqlite.Row) else row[0]
        return int(total or 0)

    @staticmethod
    def _month_range(reference: datetime) -> Tuple[datetime, datetime]:
        local = reference.astimezone(CHINA_TZ)
        start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start_local.month == 12:
            next_month_local = start_local.replace(year=start_local.year + 1, month=1)
        else:
            next_month_local = start_local.replace(month=start_local.month + 1)
        return start_local.astimezone(UTC), next_month_local.astimezone(UTC)

    @staticmethod
    def _row_to_record(row: aiosqlite.Row) -> VerificationRecord:
        created_at = datetime.fromtimestamp(row["created_at"], tz=UTC)
        expire_at = datetime.fromtimestamp(row["expire_at"], tz=UTC)
        verified_at = (
            datetime.fromtimestamp(row["verified_at"], tz=UTC)
            if row["verified_at"] is not None
            else None
        )
        return VerificationRecord(
            token=row["token"],
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            username=row["username"],
            status=row["status"],
            created_at=created_at,
            expire_at=expire_at,
            verified_at=verified_at,
            prompt_message_id=row["prompt_message_id"],
        )

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict:
        return {key: row[key] for key in row.keys()}

    @staticmethod
    def _enrich_ad_decision(row: dict) -> dict:
        row["source_label"] = AD_SOURCE_LABELS.get(row.get("source"), row.get("source"))
        row["final_action_label"] = AD_FINAL_ACTION_LABELS.get(
            row.get("final_action"), row.get("final_action")
        )
        return row

    @staticmethod
    def _enrich_ban_event(row: dict) -> dict:
        row["reason_label"] = BAN_REASON_LABELS.get(row.get("reason"), row.get("reason"))
        row["action_label"] = BAN_ACTION_LABELS.get(row.get("action"), row.get("action"))
        row["is_active"] = bool(row.get("is_active"))
        return row

    @staticmethod
    def _enrich_verification_event(row: dict) -> dict:
        row["event_label"] = VERIFICATION_EVENT_LABELS.get(row.get("event"), row.get("event"))
        return row


