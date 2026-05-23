from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

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
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
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


SQLiteWhitelistRepository = SQLiteAssistantRepository
