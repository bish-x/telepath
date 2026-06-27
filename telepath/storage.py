from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from telepath.features.channel_reactions import (
    ChannelReactionSettings,
    ReactionCandidate,
    VALID_REACTION_CATEGORIES,
    VALID_REACTION_MODES,
    VALID_REACTION_SELECTION_STRATEGIES,
    VALID_REACTION_SOURCES,
)
from telepath.features.post_mirroring import PostMirrorQueuedDelivery, PostMirrorSourceSettings
from telepath.prompts import DEFAULT_TEXT_POLISH_PROMPT


class SQLiteAssistantRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS allowed_chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS known_group_chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT,
                    last_seen_at INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS known_chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT,
                    kind TEXT NOT NULL,
                    last_seen_at INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(known_group_chats)").fetchall()
            }
            if "last_seen_at" not in columns:
                conn.execute(
                    "ALTER TABLE known_group_chats ADD COLUMN last_seen_at INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blocked_chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_messages (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    feature TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, message_id, feature)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processing_messages (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    feature TEXT NOT NULL,
                    claimed_at INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, message_id, feature)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS private_chat_message_gate (
                    chat_id INTEGER PRIMARY KEY,
                    message_count INTEGER NOT NULL,
                    is_allowed INTEGER NOT NULL,
                    checked_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS private_transcription_chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT,
                    enabled INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_reaction_settings (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    enabled_seq INTEGER NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'positive',
                    selected_emojis TEXT NOT NULL DEFAULT '[]',
                    disabled_emojis TEXT NOT NULL DEFAULT '[]',
                    max_reactions INTEGER NOT NULL DEFAULT 3,
                    selection_strategy TEXT NOT NULL DEFAULT 'random',
                    reaction_source TEXT NOT NULL DEFAULT 'mixed',
                    emoji_categories TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS post_mirror_sources (
                    source_chat_id INTEGER PRIMARY KEY,
                    title TEXT,
                    kind TEXT NOT NULL DEFAULT 'channel',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    target_thread_id INTEGER,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS post_mirror_folders (
                    folder_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS post_mirror_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_chat_id INTEGER NOT NULL,
                    message_ids TEXT NOT NULL,
                    is_channel INTEGER NOT NULL,
                    is_group INTEGER NOT NULL,
                    grouped_id INTEGER,
                    target_chat_id INTEGER NOT NULL,
                    target_thread_id INTEGER,
                    origin TEXT NOT NULL DEFAULT 'realtime',
                    status TEXT NOT NULL DEFAULT 'pending',
                    ready_at INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    sent_at INTEGER,
                    UNIQUE(source_chat_id, message_ids)
                )
                """
            )
            reaction_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(channel_reaction_settings)").fetchall()
            }
            if "selection_strategy" not in reaction_columns:
                conn.execute(
                    "ALTER TABLE channel_reaction_settings "
                    "ADD COLUMN selection_strategy TEXT NOT NULL DEFAULT 'random'"
                )
            if "emoji_categories" not in reaction_columns:
                conn.execute(
                    "ALTER TABLE channel_reaction_settings "
                    "ADD COLUMN emoji_categories TEXT NOT NULL DEFAULT '{}'"
                )
            if "reaction_source" not in reaction_columns:
                conn.execute(
                    "ALTER TABLE channel_reaction_settings "
                    "ADD COLUMN reaction_source TEXT NOT NULL DEFAULT 'mixed'"
                )
            if "disabled_emojis" not in reaction_columns:
                conn.execute(
                    "ALTER TABLE channel_reaction_settings "
                    "ADD COLUMN disabled_emojis TEXT NOT NULL DEFAULT '[]'"
                )
            if "enabled_seq" not in reaction_columns:
                conn.execute(
                    "ALTER TABLE channel_reaction_settings "
                    "ADD COLUMN enabled_seq INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reaction_folders (
                    folder_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    enabled_seq INTEGER NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'positive',
                    max_reactions INTEGER NOT NULL DEFAULT 3,
                    selection_strategy TEXT NOT NULL DEFAULT 'random',
                    reaction_source TEXT NOT NULL DEFAULT 'mixed',
                    position INTEGER NOT NULL DEFAULT 0,
                    is_visible INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            folder_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(reaction_folders)").fetchall()
            }
            if "reaction_source" not in folder_columns:
                conn.execute(
                    "ALTER TABLE reaction_folders "
                    "ADD COLUMN reaction_source TEXT NOT NULL DEFAULT 'mixed'"
                )
            if "position" not in folder_columns:
                conn.execute(
                    "ALTER TABLE reaction_folders "
                    "ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
                )
            if "is_visible" not in folder_columns:
                conn.execute(
                    "ALTER TABLE reaction_folders "
                    "ADD COLUMN is_visible INTEGER NOT NULL DEFAULT 1"
                )
            if "enabled_seq" not in folder_columns:
                conn.execute(
                    "ALTER TABLE reaction_folders "
                    "ADD COLUMN enabled_seq INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reaction_folder_members (
                    folder_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    title TEXT,
                    kind TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (folder_id, chat_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_available_reactions (
                    chat_id INTEGER NOT NULL,
                    emoji TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    category TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, emoji)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_available_reaction_checks (
                    chat_id INTEGER PRIMARY KEY,
                    checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO channel_available_reaction_checks (chat_id, checked_at)
                SELECT chat_id, MAX(updated_at)
                FROM channel_available_reactions
                GROUP BY chat_id
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            global_mode = self._reaction_global_mode_from_connection(conn)
            conn.execute(
                """
                UPDATE channel_reaction_settings
                SET mode = ?, selected_emojis = '[]', updated_at = CURRENT_TIMESTAMP
                WHERE mode = 'custom'
                """,
                (global_mode,),
            )

    def block_chat(self, chat_id: int, title: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO blocked_chats (chat_id, title)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title
                """,
                (chat_id, title),
            )

    def unblock_chat(self, chat_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM blocked_chats WHERE chat_id = ?", (chat_id,))

    def is_blocked(self, chat_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM blocked_chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return row is not None

    def list_blocked_chats(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chat_id, title FROM blocked_chats ORDER BY chat_id ASC"
            ).fetchall()
        return [{"chat_id": row["chat_id"], "title": row["title"]} for row in rows]

    def allow_group(self, chat_id: int, title: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO allowed_chats (chat_id, title)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title
                """,
                (chat_id, title),
            )

    def disallow_group(self, chat_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM allowed_chats WHERE chat_id = ?", (chat_id,))

    def is_group_allowed(self, chat_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM allowed_chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return row is not None

    def list_allowed_groups(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chat_id, title FROM allowed_chats ORDER BY chat_id ASC"
            ).fetchall()
        return [{"chat_id": row["chat_id"], "title": row["title"]} for row in rows]

    def upsert_known_group(self, chat_id: int, title: str | None = None, last_seen_at: int | None = None) -> None:
        seen_at = int(last_seen_at if last_seen_at is not None else time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO known_group_chats (chat_id, title, last_seen_at, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, title, seen_at),
            )
        self.upsert_known_chat(chat_id, title, "group", last_seen_at=seen_at)

    def list_known_groups(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chat_id, title
                FROM known_group_chats
                ORDER BY last_seen_at DESC, updated_at DESC, chat_id ASC
                """
            ).fetchall()
        return [{"chat_id": row["chat_id"], "title": row["title"]} for row in rows]

    def upsert_known_chat(
        self,
        chat_id: int,
        title: str | None = None,
        kind: str = "chat",
        last_seen_at: int | None = None,
    ) -> None:
        normalized_kind = kind if kind in {"private", "group", "channel", "chat"} else "chat"
        seen_at = int(last_seen_at if last_seen_at is not None else time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO known_chats (chat_id, title, kind, last_seen_at, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = COALESCE(excluded.title, known_chats.title),
                    kind = excluded.kind,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, title, normalized_kind, seen_at),
            )

    def list_known_chats(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is not None and kind not in {"private", "group", "channel", "chat"}:
            return []
        with self._connect() as conn:
            if kind is None:
                rows = conn.execute(
                    """
                    SELECT chat_id, title, kind
                    FROM known_chats
                    ORDER BY last_seen_at DESC, updated_at DESC, chat_id ASC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT chat_id, title, kind
                    FROM known_chats
                    WHERE kind = ?
                    ORDER BY last_seen_at DESC, updated_at DESC, chat_id ASC
                    """,
                    (kind,),
                ).fetchall()
        return [{"chat_id": row["chat_id"], "title": row["title"], "kind": row["kind"]} for row in rows]

    def add_chat(self, chat_id: int, title: str | None = None) -> None:
        self.block_chat(chat_id, title)

    def remove_chat(self, chat_id: int) -> None:
        self.unblock_chat(chat_id)

    def is_allowed(self, chat_id: int) -> bool:
        return not self.is_blocked(chat_id)

    def list_chats(self) -> list[dict[str, Any]]:
        return self.list_blocked_chats()

    def is_processed(self, chat_id: int, message_id: int, feature: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM processed_messages
                WHERE chat_id = ? AND message_id = ? AND feature = ?
                """,
                (chat_id, message_id, feature),
            ).fetchone()
        return row is not None

    def mark_processed(self, chat_id: int, message_id: int, feature: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO processed_messages (chat_id, message_id, feature)
                VALUES (?, ?, ?)
                """,
                (chat_id, message_id, feature),
            )
        return cursor.rowcount == 1

    def try_claim_processing(
        self,
        chat_id: int,
        message_ids: tuple[int, ...],
        feature: str,
        *,
        claim_ttl_seconds: int = 3600,
    ) -> bool:
        normalized_ids = tuple(dict.fromkeys(int(message_id) for message_id in message_ids))
        if not normalized_ids:
            return False
        now = int(time.time())
        cutoff = now - max(1, int(claim_ttl_seconds))
        placeholders = ",".join("?" for _ in normalized_ids)
        inserted: list[int] = []
        with self._connect() as conn:
            conn.execute("DELETE FROM processing_messages WHERE claimed_at < ?", (cutoff,))
            processed = conn.execute(
                f"""
                SELECT 1 FROM processed_messages
                WHERE chat_id = ? AND feature = ? AND message_id IN ({placeholders})
                LIMIT 1
                """,
                (int(chat_id), feature, *normalized_ids),
            ).fetchone()
            if processed is not None:
                return False
            for message_id in normalized_ids:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO processing_messages (chat_id, message_id, feature, claimed_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (int(chat_id), message_id, feature, now),
                )
                if cursor.rowcount != 1:
                    if inserted:
                        self._delete_processing_claims(conn, int(chat_id), tuple(inserted), feature)
                    return False
                inserted.append(message_id)
        return True

    def mark_many_processed(self, chat_id: int, message_ids: tuple[int, ...], feature: str) -> None:
        normalized_ids = tuple(dict.fromkeys(int(message_id) for message_id in message_ids))
        if not normalized_ids:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO processed_messages (chat_id, message_id, feature)
                VALUES (?, ?, ?)
                """,
                [(int(chat_id), message_id, feature) for message_id in normalized_ids],
            )
            self._delete_processing_claims(conn, int(chat_id), normalized_ids, feature)

    def release_processing_claims(self, chat_id: int, message_ids: tuple[int, ...], feature: str) -> None:
        normalized_ids = tuple(dict.fromkeys(int(message_id) for message_id in message_ids))
        if not normalized_ids:
            return
        with self._connect() as conn:
            self._delete_processing_claims(conn, int(chat_id), normalized_ids, feature)

    def enqueue_post_mirror_delivery(
        self,
        *,
        source_chat_id: int,
        message_ids: tuple[int, ...],
        is_channel: bool,
        is_group: bool,
        grouped_id: int | None,
        target_chat_id: int,
        target_thread_id: int | None,
        origin: str = "realtime",
        ready_at: int = 0,
    ) -> bool:
        normalized_ids = tuple(dict.fromkeys(int(message_id) for message_id in message_ids))
        if not normalized_ids:
            return False
        payload = self._post_mirror_message_ids_payload(normalized_ids)
        normalized_origin = origin if origin in {"realtime", "history"} else "realtime"
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO post_mirror_outbox (
                    source_chat_id,
                    message_ids,
                    is_channel,
                    is_group,
                    grouped_id,
                    target_chat_id,
                    target_thread_id,
                    origin,
                    ready_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(source_chat_id),
                    payload,
                    1 if is_channel else 0,
                    1 if is_group else 0,
                    int(grouped_id) if grouped_id is not None else None,
                    int(target_chat_id),
                    int(target_thread_id) if target_thread_id is not None else None,
                    normalized_origin,
                    int(ready_at),
                ),
            )
        return cursor.rowcount == 1

    def list_ready_post_mirror_deliveries(self, *, now: int, limit: int = 25) -> list[PostMirrorQueuedDelivery]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    id,
                    source_chat_id,
                    message_ids,
                    is_channel,
                    is_group,
                    grouped_id,
                    target_chat_id,
                    target_thread_id,
                    origin,
                    ready_at,
                    attempts,
                    last_error
                FROM post_mirror_outbox
                WHERE status = 'pending' AND ready_at <= ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (int(now), max(1, int(limit))),
            ).fetchall()
        return [self._post_mirror_delivery_from_row(row) for row in rows]

    def defer_post_mirror_delivery(
        self,
        job_id: int,
        *,
        delay_seconds: int,
        error: str | None = None,
        now: int | None = None,
    ) -> None:
        current_time = int(time.time()) if now is None else int(now)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE post_mirror_outbox
                SET
                    ready_at = ?,
                    attempts = attempts + 1,
                    last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'pending'
                """,
                (current_time + max(1, int(delay_seconds)), error, int(job_id)),
            )

    def mark_post_mirror_delivery_sent(self, job_id: int, *, now: int | None = None) -> None:
        sent_at = int(time.time()) if now is None else int(now)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE post_mirror_outbox
                SET status = 'sent', sent_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (sent_at, int(job_id)),
            )

    def cancel_post_mirror_delivery(self, job_id: int, *, error: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE post_mirror_outbox
                SET status = 'cancelled', last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'pending'
                """,
                (error, int(job_id)),
            )

    @staticmethod
    def _post_mirror_message_ids_payload(message_ids: tuple[int, ...]) -> str:
        return json.dumps(list(message_ids), separators=(",", ":"))

    def _post_mirror_delivery_from_row(self, row: sqlite3.Row) -> PostMirrorQueuedDelivery:
        try:
            message_ids = tuple(int(message_id) for message_id in json.loads(row["message_ids"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            message_ids = ()
        return PostMirrorQueuedDelivery(
            id=int(row["id"]),
            source_chat_id=int(row["source_chat_id"]),
            message_ids=message_ids,
            is_channel=bool(row["is_channel"]),
            is_group=bool(row["is_group"]),
            grouped_id=int(row["grouped_id"]) if row["grouped_id"] is not None else None,
            target_chat_id=int(row["target_chat_id"]),
            target_thread_id=int(row["target_thread_id"]) if row["target_thread_id"] is not None else None,
            origin=row["origin"],
            ready_at=int(row["ready_at"]),
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
        )

    def _delete_processing_claims(
        self,
        conn: sqlite3.Connection,
        chat_id: int,
        message_ids: tuple[int, ...],
        feature: str,
    ) -> None:
        placeholders = ",".join("?" for _ in message_ids)
        conn.execute(
            f"""
            DELETE FROM processing_messages
            WHERE chat_id = ? AND feature = ? AND message_id IN ({placeholders})
            """,
            (chat_id, feature, *message_ids),
        )

    def get_private_chat_message_gate(self, chat_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT chat_id, message_count, is_allowed, checked_at
                FROM private_chat_message_gate
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "chat_id": row["chat_id"],
            "message_count": row["message_count"],
            "is_allowed": bool(row["is_allowed"]),
            "checked_at": row["checked_at"],
        }

    def save_private_chat_message_gate(self, chat_id: int, message_count: int, is_allowed: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO private_chat_message_gate (chat_id, message_count, is_allowed, checked_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    message_count = excluded.message_count,
                    is_allowed = excluded.is_allowed,
                    checked_at = excluded.checked_at
                """,
                (chat_id, message_count, 1 if is_allowed else 0, int(time.time())),
            )

    def is_private_chat_transcription_enabled(self, chat_id: int) -> bool:
        return self.get_private_chat_transcription_override(chat_id) is True

    def get_private_chat_transcription_override(self, chat_id: int) -> bool | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT enabled
                FROM private_transcription_chats
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
        return bool(row["enabled"]) if row is not None else None

    def set_private_chat_transcription(self, chat_id: int, title: str | None, enabled: bool) -> None:
        self.upsert_known_chat(chat_id, title, "private")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO private_transcription_chats (chat_id, title, enabled, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    enabled = excluded.enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, title, 1 if enabled else 0),
            )

    def list_private_chat_transcription_overrides(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chat_id, title, enabled
                FROM private_transcription_chats
                ORDER BY updated_at DESC, chat_id ASC
                """
            ).fetchall()
        return [
            {"chat_id": row["chat_id"], "title": row["title"], "enabled": bool(row["enabled"])}
            for row in rows
        ]

    def is_feature_enabled(self, feature: str) -> bool:
        value = self._get_setting(f"feature.{feature}.enabled")
        if value is None:
            return True
        return value == "1"

    def set_feature_enabled(self, feature: str, enabled: bool) -> None:
        self._set_setting(f"feature.{feature}.enabled", "1" if enabled else "0")

    def is_transcription_decoration_enabled(self) -> bool:
        return self._get_setting("transcription.decoration.enabled") == "1"

    def set_transcription_decoration_enabled(self, enabled: bool) -> None:
        self._set_setting("transcription.decoration.enabled", "1" if enabled else "0")

    def get_private_chat_min_messages(self) -> int:
        return self._get_int_setting("transcription.private.min_messages", default=100)

    def set_private_chat_min_messages(self, minimum_messages: int) -> None:
        if minimum_messages < 1:
            raise ValueError("minimum messages must be positive")
        self._set_setting("transcription.private.min_messages", str(int(minimum_messages)))

    def get_voice_min_duration_seconds(self) -> int:
        return self._get_int_setting("transcription.voice.min_duration_seconds", default=0)

    def set_voice_min_duration_seconds(self, seconds: int) -> None:
        if seconds < 0:
            raise ValueError("duration must be non-negative")
        self._set_setting("transcription.voice.min_duration_seconds", str(int(seconds)))

    def get_voice_max_duration_seconds(self) -> int:
        return self._get_int_setting("transcription.voice.max_duration_seconds", default=300)

    def set_voice_max_duration_seconds(self, seconds: int) -> None:
        if seconds < 1:
            raise ValueError("maximum duration must be positive")
        self._set_setting("transcription.voice.max_duration_seconds", str(int(seconds)))

    def is_account_premium(self) -> bool:
        return self._get_setting("account.telegram_premium") == "1"

    def set_account_premium(self, is_premium: bool) -> None:
        self._set_setting("account.telegram_premium", "1" if is_premium else "0")

    def is_reaction_autolike_enabled(self) -> bool:
        value = self._get_setting("reactions.enabled")
        return value != "0"

    def set_reaction_autolike_enabled(self, enabled: bool) -> None:
        self._set_setting("reactions.enabled", "1" if enabled else "0")

    def is_post_mirroring_enabled(self) -> bool:
        return self._get_setting("post_mirror.enabled") == "1"

    def set_post_mirroring_enabled(self, enabled: bool) -> None:
        self._set_setting("post_mirror.enabled", "1" if enabled else "0")

    def get_post_mirror_target_chat_id(self) -> int | None:
        value = self._get_setting("post_mirror.target_chat_id")
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def set_post_mirror_target_chat_id(self, chat_id: int | None) -> None:
        new_chat_id = int(chat_id) if chat_id is not None else None
        with self._connect() as conn:
            previous_value = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                ("post_mirror.target_chat_id",),
            ).fetchone()
            previous_chat_id: int | None = None
            if previous_value is not None:
                try:
                    previous_chat_id = int(previous_value["value"])
                except (TypeError, ValueError):
                    previous_chat_id = None

            if new_chat_id is None:
                conn.execute("DELETE FROM settings WHERE key = ?", ("post_mirror.target_chat_id",))
            else:
                conn.execute(
                    """
                    INSERT INTO settings (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    ("post_mirror.target_chat_id", str(new_chat_id)),
                )

            if previous_chat_id != new_chat_id:
                conn.execute(
                    """
                    UPDATE post_mirror_sources
                    SET enabled = 0, target_thread_id = NULL, updated_at = CURRENT_TIMESTAMP
                    """
                )
                conn.execute(
                    """
                    UPDATE post_mirror_folders
                    SET enabled = 0, updated_at = CURRENT_TIMESTAMP
                    """
                )
                conn.execute(
                    """
                    UPDATE post_mirror_outbox
                    SET
                        status = 'cancelled',
                        last_error = 'target group changed',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'pending'
                    """
                )
                return

    def upsert_post_mirror_source(self, source_chat_id: int, title: str | None = None, kind: str = "channel") -> None:
        normalized_kind = kind if kind in {"channel", "group", "chat"} else "chat"
        self.upsert_known_chat(source_chat_id, title, normalized_kind)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO post_mirror_sources (source_chat_id, title, kind, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source_chat_id) DO UPDATE SET
                    title = COALESCE(excluded.title, post_mirror_sources.title),
                    kind = excluded.kind,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (int(source_chat_id), title, normalized_kind),
            )

    def get_post_mirror_source_settings(self, source_chat_id: int) -> PostMirrorSourceSettings | None:
        source_chat_id = int(source_chat_id)
        with self._connect() as conn:
            source_row = conn.execute(
                """
                SELECT title, kind, enabled, target_thread_id
                FROM post_mirror_sources
                WHERE source_chat_id = ?
                """,
                (source_chat_id,),
            ).fetchone()
            folder_row = conn.execute(
                """
                SELECT
                    COALESCE(k.title, m.title) AS title,
                    m.kind,
                    COALESCE(p.enabled, 0) AS enabled
                FROM reaction_folder_members m
                JOIN reaction_folders f ON f.folder_id = m.folder_id
                LEFT JOIN post_mirror_folders p ON p.folder_id = m.folder_id
                LEFT JOIN known_chats k ON k.chat_id = m.chat_id
                WHERE
                    m.chat_id = ?
                    AND m.kind IN ('channel', 'group')
                    AND f.is_visible = 1
                ORDER BY COALESCE(p.enabled, 0) DESC, f.position ASC, f.folder_id ASC
                LIMIT 1
                """,
                (source_chat_id,),
            ).fetchone()
        folder_enabled = bool(folder_row and folder_row["enabled"])
        if source_row is None and not folder_enabled:
            return None
        if source_row is None:
            return PostMirrorSourceSettings(
                enabled=True,
                target_thread_id=None,
                title=folder_row["title"] if folder_row is not None else None,
                kind=folder_row["kind"] if folder_row is not None else "channel",
            )
        title = source_row["title"]
        kind = source_row["kind"]
        if folder_row is not None:
            title = title or folder_row["title"]
            kind = kind or folder_row["kind"]
        return PostMirrorSourceSettings(
            enabled=bool(source_row["enabled"]) or folder_enabled,
            target_thread_id=source_row["target_thread_id"],
            title=title,
            kind=kind,
        )

    def list_post_mirror_sources(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_chat_id, title, kind, enabled, target_thread_id
                FROM post_mirror_sources
                ORDER BY enabled DESC, updated_at DESC, source_chat_id ASC
                """
            ).fetchall()
        return [
            {
                "source_chat_id": row["source_chat_id"],
                "title": row["title"],
                "kind": row["kind"],
                "enabled": bool(row["enabled"]),
                "target_thread_id": row["target_thread_id"],
            }
            for row in rows
        ]

    def set_post_mirror_source_enabled(self, source_chat_id: int, enabled: bool) -> None:
        self._ensure_post_mirror_source(source_chat_id)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE post_mirror_sources
                SET enabled = ?, updated_at = CURRENT_TIMESTAMP
                WHERE source_chat_id = ?
                """,
                (1 if enabled else 0, int(source_chat_id)),
            )

    def set_post_mirror_source_topic(self, source_chat_id: int, target_thread_id: int | None) -> None:
        if target_thread_id is not None and int(target_thread_id) <= 0:
            raise ValueError("topic id must be positive")
        self._ensure_post_mirror_source(source_chat_id)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE post_mirror_sources
                SET target_thread_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE source_chat_id = ?
                """,
                (target_thread_id, int(source_chat_id)),
            )

    def set_post_mirror_folder_enabled(self, folder_id: int, enabled: bool) -> None:
        self._ensure_reaction_folder(folder_id)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO post_mirror_folders (folder_id, enabled, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(folder_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (int(folder_id), 1 if enabled else 0),
            )

    def list_post_mirror_folder_sources(self, folder_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT
                    m.chat_id,
                    COALESCE(k.title, m.title) AS title,
                    m.kind
                FROM reaction_folder_members m
                LEFT JOIN known_chats k ON k.chat_id = m.chat_id
                WHERE m.folder_id = ? AND m.kind IN ('channel', 'group')
                ORDER BY title COLLATE NOCASE ASC, m.chat_id ASC
                """,
                (int(folder_id),),
            ).fetchall()
        return [
            {"chat_id": row["chat_id"], "title": row["title"], "kind": row["kind"]}
            for row in rows
        ]

    def list_enabled_post_mirror_folder_sources(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT
                    m.chat_id,
                    COALESCE(k.title, m.title) AS title,
                    m.kind
                FROM reaction_folder_members m
                JOIN reaction_folders f ON f.folder_id = m.folder_id
                JOIN post_mirror_folders p ON p.folder_id = m.folder_id
                LEFT JOIN known_chats k ON k.chat_id = m.chat_id
                WHERE
                    f.is_visible = 1
                    AND p.enabled = 1
                    AND m.kind IN ('channel', 'group')
                ORDER BY title COLLATE NOCASE ASC, m.chat_id ASC
                """
            ).fetchall()
        return [
            {"chat_id": row["chat_id"], "title": row["title"], "kind": row["kind"]}
            for row in rows
        ]

    def list_post_mirror_folders(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    f.folder_id,
                    f.title,
                    COALESCE(p.enabled, 0) AS enabled,
                    COUNT(DISTINCT CASE WHEN m.kind IN ('channel', 'group') THEN m.chat_id END) AS source_count,
                    COUNT(
                        DISTINCT CASE
                            WHEN m.kind IN ('channel', 'group') AND s.target_thread_id IS NOT NULL THEN m.chat_id
                        END
                    ) AS configured_count,
                    COUNT(
                        DISTINCT CASE
                            WHEN
                                m.kind IN ('channel', 'group')
                                AND pm.chat_id IS NOT NULL
                                THEN m.chat_id
                        END
                    ) AS history_processed_count
                FROM reaction_folders f
                LEFT JOIN post_mirror_folders p ON p.folder_id = f.folder_id
                LEFT JOIN reaction_folder_members m ON m.folder_id = f.folder_id
                LEFT JOIN post_mirror_sources s ON s.source_chat_id = m.chat_id
                LEFT JOIN (
                    SELECT DISTINCT chat_id
                    FROM processed_messages
                    WHERE feature = 'post_mirroring'
                ) pm ON pm.chat_id = m.chat_id
                WHERE f.is_visible = 1
                GROUP BY f.folder_id, f.title, p.enabled, f.position
                ORDER BY f.position ASC, f.folder_id ASC
                """
            ).fetchall()
        return [
            {
                "folder_id": row["folder_id"],
                "title": row["title"],
                "enabled": bool(row["enabled"]),
                "source_count": int(row["source_count"]),
                "configured_count": int(row["configured_count"]),
                "history_processed_count": int(row["history_processed_count"]),
            }
            for row in rows
        ]

    def get_reaction_delay_range_seconds(self) -> tuple[int, int]:
        minimum = self._get_int_setting("reactions.delay.min_seconds", default=240)
        maximum = self._get_int_setting("reactions.delay.max_seconds", default=900)
        if minimum < 0 or maximum < minimum:
            return (240, 900)
        return (minimum, maximum)

    def set_reaction_delay_range_seconds(self, minimum_seconds: int, maximum_seconds: int) -> None:
        if minimum_seconds < 0 or maximum_seconds < minimum_seconds:
            raise ValueError("delay range must be non-negative and ordered")
        self._set_setting("reactions.delay.min_seconds", str(int(minimum_seconds)))
        self._set_setting("reactions.delay.max_seconds", str(int(maximum_seconds)))

    def get_reaction_global_mode(self) -> str:
        mode = self._get_setting("reactions.global.mode") or "positive"
        return mode if mode in VALID_REACTION_MODES - {"custom"} else "positive"

    def set_reaction_global_mode(self, mode: str) -> None:
        if mode not in VALID_REACTION_MODES - {"custom"}:
            raise ValueError("mode must be one of all, positive, negative")
        self._set_setting("reactions.global.mode", mode)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE channel_reaction_settings
                SET mode = ?, updated_at = CURRENT_TIMESTAMP
                WHERE mode IN ('all', 'positive', 'negative')
                """,
                (mode,),
            )

    def upsert_reaction_channel(self, chat_id: int, title: str | None = None) -> None:
        self.upsert_known_chat(chat_id, title, "channel")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO channel_reaction_settings (chat_id, title, mode, max_reactions, selection_strategy)
                VALUES (?, ?, 'positive', 3, 'random')
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = COALESCE(excluded.title, channel_reaction_settings.title),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, title),
            )

    def get_reaction_channel_settings(self, chat_id: int) -> ChannelReactionSettings | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT title, enabled, mode, selected_emojis, disabled_emojis, max_reactions, selection_strategy, reaction_source, emoji_categories
                FROM channel_reaction_settings
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        return self._reaction_channel_settings_from_row(row)

    def _reaction_channel_settings_from_row(self, row: sqlite3.Row) -> ChannelReactionSettings:
        return ChannelReactionSettings(
            enabled=bool(row["enabled"]),
            mode=str(row["mode"]),
            selected_emojis=tuple(self._decode_json_list(row["selected_emojis"])),
            disabled_emojis=tuple(self._decode_json_list(row["disabled_emojis"])),
            max_reactions=int(row["max_reactions"]),
            selection_strategy=str(row["selection_strategy"]),
            reaction_source=str(row["reaction_source"]),
            emoji_categories=self._decode_json_dict(row["emoji_categories"]),
            title=row["title"],
        )

    def upsert_reaction_folder(self, folder_id: int, title: str, *, position: int = 0) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO reaction_folders (folder_id, title, position, is_visible, updated_at)
                VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(folder_id) DO UPDATE SET
                    title = excluded.title,
                    position = excluded.position,
                    is_visible = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (int(folder_id), title, int(position)),
            )

    def replace_reaction_folder_members(self, folder_id: int, members: list[dict[str, Any]]) -> None:
        normalized_members = []
        for member in members:
            kind = str(member.get("kind") or "chat")
            if kind not in {"private", "group", "channel", "chat"}:
                kind = "chat"
            chat_id = int(member["chat_id"])
            title = member.get("title")
            normalized_members.append((int(folder_id), chat_id, title, kind))
        with self._connect() as conn:
            conn.execute("DELETE FROM reaction_folder_members WHERE folder_id = ?", (int(folder_id),))
            conn.executemany(
                """
                INSERT INTO reaction_folder_members (folder_id, chat_id, title, kind, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                normalized_members,
            )
        for _, chat_id, title, kind in normalized_members:
            self.upsert_known_chat(chat_id, title, kind)

    def replace_reaction_folder_catalog(self, folders: list[dict[str, Any]]) -> None:
        visible_ids = [int(folder["folder_id"]) for folder in folders]
        with self._connect() as conn:
            if visible_ids:
                placeholders = ",".join("?" for _ in visible_ids)
                conn.execute(
                    f"""
                    UPDATE reaction_folders
                    SET is_visible = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE folder_id NOT IN ({placeholders})
                    """,
                    visible_ids,
                )
                conn.execute(
                    f"DELETE FROM reaction_folder_members WHERE folder_id NOT IN ({placeholders})",
                    visible_ids,
                )
            else:
                conn.execute("UPDATE reaction_folders SET is_visible = 0, updated_at = CURRENT_TIMESTAMP")
                conn.execute("DELETE FROM reaction_folder_members")
        for position, folder in enumerate(folders):
            folder_id = int(folder["folder_id"])
            self.upsert_reaction_folder(
                folder_id,
                str(folder.get("title") or folder_id),
                position=int(folder.get("position", position)),
            )
            self.replace_reaction_folder_members(folder_id, list(folder.get("members") or []))

    def get_reaction_folder_settings(self, folder_id: int) -> ChannelReactionSettings | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT title, enabled, mode, max_reactions, selection_strategy, reaction_source
                FROM reaction_folders
                WHERE folder_id = ? AND is_visible = 1
                """,
                (int(folder_id),),
            ).fetchone()
        if row is None:
            return None
        return self._reaction_folder_settings_from_row(row)

    def list_reaction_folders(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    f.folder_id,
                    f.title,
                    f.enabled,
                    f.mode,
                    f.max_reactions,
                    f.selection_strategy,
                    f.reaction_source,
                    COUNT(CASE WHEN m.kind = 'channel' THEN 1 END) AS channel_count
                FROM reaction_folders f
                LEFT JOIN reaction_folder_members m ON m.folder_id = f.folder_id
                WHERE f.is_visible = 1
                GROUP BY
                    f.folder_id, f.title, f.enabled, f.mode, f.max_reactions,
                    f.selection_strategy, f.reaction_source, f.position
                ORDER BY f.position ASC, f.folder_id ASC
                """
            ).fetchall()
        return [
            {
                "folder_id": row["folder_id"],
                "title": row["title"],
                "enabled": bool(row["enabled"]),
                "mode": row["mode"],
                "max_reactions": row["max_reactions"],
                "selection_strategy": row["selection_strategy"],
                "reaction_source": row["reaction_source"],
                "channel_count": int(row["channel_count"]),
            }
            for row in rows
        ]

    def list_reaction_folder_channels(self, folder_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chat_id, title, kind
                FROM reaction_folder_members
                WHERE folder_id = ? AND kind = 'channel'
                ORDER BY title COLLATE NOCASE ASC, chat_id ASC
                """,
                (int(folder_id),),
            ).fetchall()
        return [
            {"chat_id": row["chat_id"], "title": row["title"], "kind": row["kind"]}
            for row in rows
        ]

    def set_reaction_folder_enabled(self, folder_id: int, enabled: bool) -> None:
        self._ensure_reaction_folder(folder_id)
        with self._connect() as conn:
            enabled_seq = self._next_reaction_precedence_seq(conn) if enabled else None
            conn.execute(
                """
                UPDATE reaction_folders
                SET
                    enabled = ?,
                    enabled_seq = CASE WHEN ? = 1 THEN ? ELSE enabled_seq END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE folder_id = ?
                """,
                (1 if enabled else 0, 1 if enabled else 0, enabled_seq, int(folder_id)),
            )

    def set_reaction_folder_mode(self, folder_id: int, mode: str) -> None:
        if mode not in VALID_REACTION_MODES - {"custom"}:
            raise ValueError("mode must be one of all, positive, negative")
        self._ensure_reaction_folder(folder_id)
        self._set_reaction_folder_field(folder_id, "mode", mode)

    def set_reaction_folder_max_reactions(self, folder_id: int, max_reactions: int) -> None:
        if max_reactions not in {1, 3}:
            raise ValueError("max reactions must be 1 or 3")
        self._ensure_reaction_folder(folder_id)
        self._set_reaction_folder_field(folder_id, "max_reactions", int(max_reactions))

    def set_reaction_folder_selection_strategy(self, folder_id: int, strategy: str) -> None:
        if strategy not in VALID_REACTION_SELECTION_STRATEGIES:
            raise ValueError("selection strategy must be priority or random")
        self._ensure_reaction_folder(folder_id)
        self._set_reaction_folder_field(folder_id, "selection_strategy", strategy)

    def set_reaction_folder_source(self, folder_id: int, source: str) -> None:
        if source not in VALID_REACTION_SOURCES:
            raise ValueError("reaction source must be mixed, standard, or premium")
        self._ensure_reaction_folder(folder_id)
        self._set_reaction_folder_field(folder_id, "reaction_source", source)

    def get_effective_reaction_channel_settings(self, chat_id: int) -> ChannelReactionSettings | None:
        resolved = self._resolve_effective_reaction_channel_settings(chat_id)
        return resolved[0] if resolved is not None else None

    def get_effective_reaction_channel_settings_source(self, chat_id: int) -> str | None:
        resolved = self._resolve_effective_reaction_channel_settings(chat_id)
        return resolved[1] if resolved is not None else None

    def _resolve_effective_reaction_channel_settings(
        self,
        chat_id: int,
    ) -> tuple[ChannelReactionSettings, str] | None:
        with self._connect() as conn:
            explicit_row = conn.execute(
                """
                SELECT
                    title,
                    enabled,
                    enabled_seq,
                    mode,
                    selected_emojis,
                    disabled_emojis,
                    max_reactions,
                    selection_strategy,
                    reaction_source,
                    emoji_categories
                FROM channel_reaction_settings
                WHERE chat_id = ?
                """,
                (int(chat_id),),
            ).fetchone()
            folder_row = conn.execute(
                """
                SELECT f.title, f.enabled, f.enabled_seq, f.mode, f.max_reactions, f.selection_strategy, f.reaction_source
                FROM reaction_folders f
                JOIN reaction_folder_members m ON m.folder_id = f.folder_id
                WHERE
                    m.chat_id = ?
                    AND m.kind = 'channel'
                    AND f.enabled = 1
                    AND f.is_visible = 1
                ORDER BY f.enabled_seq DESC, f.position DESC, f.folder_id DESC
                LIMIT 1
                """,
                (int(chat_id),),
            ).fetchone()
        if explicit_row is not None:
            explicit = self._reaction_channel_settings_from_row(explicit_row)
            if not explicit.enabled:
                return explicit, "channel"
            explicit_seq = int(explicit_row["enabled_seq"] or 0)
            folder_seq = int(folder_row["enabled_seq"] or 0) if folder_row is not None else -1
            if folder_row is None or explicit_seq >= folder_seq:
                return explicit, "channel"
        if folder_row is None:
            return None
        return self._reaction_folder_settings_from_row(folder_row), "folder"

    def list_reaction_channels(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chat_id, title, enabled, mode, max_reactions, selection_strategy, reaction_source
                FROM channel_reaction_settings
                ORDER BY updated_at DESC, chat_id ASC
                """
            ).fetchall()
        return [
            {
                "chat_id": row["chat_id"],
                "title": row["title"],
                "enabled": bool(row["enabled"]),
                "mode": row["mode"],
                "max_reactions": row["max_reactions"],
                "selection_strategy": row["selection_strategy"],
                "reaction_source": row["reaction_source"],
            }
            for row in rows
        ]

    def set_reaction_channel_enabled(self, chat_id: int, enabled: bool) -> None:
        self._ensure_reaction_channel(chat_id)
        with self._connect() as conn:
            enabled_seq = self._next_reaction_precedence_seq(conn) if enabled else None
            conn.execute(
                """
                UPDATE channel_reaction_settings
                SET
                    enabled = ?,
                    enabled_seq = CASE WHEN ? = 1 THEN ? ELSE enabled_seq END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
                """,
                (1 if enabled else 0, 1 if enabled else 0, enabled_seq, chat_id),
            )

    def set_reaction_channel_mode(self, chat_id: int, mode: str) -> None:
        if mode not in VALID_REACTION_MODES:
            raise ValueError("mode must be one of all, positive, negative, custom")
        self._ensure_reaction_channel(chat_id)
        self._set_reaction_channel_field(chat_id, "mode", mode)

    def set_reaction_channel_max_reactions(self, chat_id: int, max_reactions: int) -> None:
        if max_reactions not in {1, 3}:
            raise ValueError("max reactions must be 1 or 3")
        self._ensure_reaction_channel(chat_id)
        self._set_reaction_channel_field(chat_id, "max_reactions", int(max_reactions))

    def set_reaction_channel_selection_strategy(self, chat_id: int, strategy: str) -> None:
        if strategy not in VALID_REACTION_SELECTION_STRATEGIES:
            raise ValueError("selection strategy must be priority or random")
        self._ensure_reaction_channel(chat_id)
        self._set_reaction_channel_field(chat_id, "selection_strategy", strategy)

    def set_reaction_channel_source(self, chat_id: int, source: str) -> None:
        if source not in VALID_REACTION_SOURCES:
            raise ValueError("reaction source must be mixed, standard, or premium")
        self._ensure_reaction_channel(chat_id)
        self._set_reaction_channel_field(chat_id, "reaction_source", source)

    def set_reaction_channel_emoji_category(self, chat_id: int, emoji: str, category: str) -> None:
        if category not in VALID_REACTION_CATEGORIES:
            raise ValueError("category must be positive, negative, or neutral")
        self._ensure_reaction_channel(chat_id)
        current = self.get_reaction_channel_settings(chat_id)
        categories = dict(current.emoji_categories if current else {})
        categories[emoji] = category
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE channel_reaction_settings
                SET emoji_categories = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
                """,
                (json.dumps(categories, ensure_ascii=False, sort_keys=True), chat_id),
            )

    def cycle_reaction_channel_emoji_category(self, chat_id: int, emoji: str) -> str:
        self._ensure_reaction_channel(chat_id)
        current = self.get_reaction_channel_settings(chat_id)
        categories = dict(current.emoji_categories if current else {})
        current_category = categories.get(emoji)
        if current_category not in VALID_REACTION_CATEGORIES:
            current_category = self._observed_reaction_category(chat_id, emoji) or "neutral"
        next_category = {
            "neutral": "positive",
            "positive": "negative",
            "negative": "neutral",
        }[current_category]
        self.set_reaction_channel_emoji_category(chat_id, emoji, next_category)
        return next_category

    def toggle_reaction_channel_emoji(self, chat_id: int, emoji: str) -> None:
        self._ensure_reaction_channel(chat_id)
        current = self.get_reaction_channel_settings(chat_id)
        disabled = list(current.disabled_emojis if current else ())
        if emoji in disabled:
            disabled.remove(emoji)
        else:
            disabled.append(emoji)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE channel_reaction_settings
                SET disabled_emojis = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
                """,
                (json.dumps(disabled, ensure_ascii=False), chat_id),
            )

    def replace_reaction_channel_available_reactions(
        self,
        chat_id: int,
        reactions: list[ReactionCandidate],
    ) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM channel_available_reactions WHERE chat_id = ?", (chat_id,))
            conn.executemany(
                """
                INSERT INTO channel_available_reactions (chat_id, emoji, kind, category, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                [
                    (chat_id, reaction.emoji, reaction.kind, reaction.category)
                    for reaction in reactions
                ],
            )
            conn.execute(
                """
                INSERT INTO channel_available_reaction_checks (chat_id, checked_at)
                VALUES (?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET checked_at = CURRENT_TIMESTAMP
                """,
                (chat_id,),
            )

    def has_reaction_channel_available_reactions_checked(self, chat_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM channel_available_reaction_checks
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
        return row is not None

    def list_reaction_channel_available_reactions(self, chat_id: int) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT emoji, kind, category
                FROM channel_available_reactions
                WHERE chat_id = ?
                ORDER BY updated_at ASC, rowid ASC
                """,
                (chat_id,),
            ).fetchall()
        settings = self.get_reaction_channel_settings(chat_id)
        overrides = settings.emoji_categories if settings else {}
        return [
            {
                "emoji": row["emoji"],
                "kind": row["kind"],
                "category": overrides.get(row["emoji"], row["category"]),
            }
            for row in rows
        ]

    def get_text_polish_prompt(self) -> str:
        return self._get_setting("text_polish.prompt") or DEFAULT_TEXT_POLISH_PROMPT

    def set_text_polish_prompt(self, prompt: str) -> None:
        normalized = prompt.strip()
        if not normalized:
            raise ValueError("Prompt cannot be empty")
        self._set_setting("text_polish.prompt", normalized)

    def reset_text_polish_prompt(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", ("text_polish.prompt",))

    def _get_setting(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    @staticmethod
    def _reaction_global_mode_from_connection(conn: sqlite3.Connection) -> str:
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                ("reactions.global.mode",),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        mode = str(row["value"]) if row else "positive"
        return mode if mode in VALID_REACTION_MODES - {"custom"} else "positive"

    def _get_int_setting(self, key: str, *, default: int) -> int:
        value = self._get_setting(key)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            return default

    def _set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )

    def _next_reaction_precedence_seq(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("reactions.precedence.seq",),
        ).fetchone()
        try:
            current = int(row["value"]) if row else 0
        except (TypeError, ValueError):
            current = 0
        next_seq = current + 1
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            ("reactions.precedence.seq", str(next_seq)),
        )
        return next_seq

    def _ensure_reaction_channel(self, chat_id: int) -> None:
        if self.get_reaction_channel_settings(chat_id) is None:
            self.upsert_reaction_channel(chat_id, None)

    def _ensure_post_mirror_source(self, source_chat_id: int) -> None:
        if not self._post_mirror_source_exists(source_chat_id):
            self.upsert_post_mirror_source(source_chat_id, None, "chat")

    def _post_mirror_source_exists(self, source_chat_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM post_mirror_sources WHERE source_chat_id = ?",
                (int(source_chat_id),),
            ).fetchone()
        return row is not None

    def _ensure_reaction_folder(self, folder_id: int) -> None:
        if self.get_reaction_folder_settings(folder_id) is None:
            self.upsert_reaction_folder(folder_id, str(folder_id))

    def _set_reaction_channel_field(self, chat_id: int, field: str, value: object) -> None:
        if field not in {"enabled", "mode", "max_reactions", "selection_strategy", "reaction_source"}:
            raise ValueError("unsupported reaction channel field")
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE channel_reaction_settings
                SET {field} = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
                """,
                (value, chat_id),
            )

    def _set_reaction_folder_field(self, folder_id: int, field: str, value: object) -> None:
        if field not in {"enabled", "mode", "max_reactions", "selection_strategy", "reaction_source"}:
            raise ValueError("unsupported reaction folder field")
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE reaction_folders
                SET {field} = ?, updated_at = CURRENT_TIMESTAMP
                WHERE folder_id = ?
                """,
                (value, int(folder_id)),
            )

    @staticmethod
    def _reaction_folder_settings_from_row(row: sqlite3.Row) -> ChannelReactionSettings:
        mode = str(row["mode"])
        if mode not in VALID_REACTION_MODES - {"custom"}:
            mode = "positive"
        strategy = str(row["selection_strategy"])
        if strategy not in VALID_REACTION_SELECTION_STRATEGIES:
            strategy = "random"
        source = str(row["reaction_source"])
        if source not in VALID_REACTION_SOURCES:
            source = "mixed"
        max_reactions = int(row["max_reactions"])
        if max_reactions not in {1, 3}:
            max_reactions = 3
        return ChannelReactionSettings(
            enabled=bool(row["enabled"]),
            mode=mode,
            max_reactions=max_reactions,
            selection_strategy=strategy,
            reaction_source=source,
            title=row["title"],
        )

    @staticmethod
    def _decode_json_list(value: str | None) -> list[str]:
        if not value:
            return []
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(decoded, list):
            return []
        return [str(item) for item in decoded]

    @staticmethod
    def _decode_json_dict(value: str | None) -> dict[str, str]:
        if not value:
            return {}
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if not isinstance(decoded, dict):
            return {}
        return {
            str(key): str(category)
            for key, category in decoded.items()
            if str(category) in VALID_REACTION_CATEGORIES
        }

    def _observed_reaction_category(self, chat_id: int, emoji: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT category
                FROM channel_available_reactions
                WHERE chat_id = ? AND emoji = ?
                """,
                (chat_id, emoji),
            ).fetchone()
        if row is None:
            return None
        category = str(row["category"])
        return category if category in VALID_REACTION_CATEGORIES else None


SQLiteWhitelistRepository = SQLiteAssistantRepository
