from telepath.storage import SQLiteWhitelistRepository


def test_settings_repository_persists_transcription_enabled_flag(tmp_path):
    repo = SQLiteWhitelistRepository(tmp_path / "assistant.sqlite3")

    assert repo.is_feature_enabled("voice_transcription")

    repo.set_feature_enabled("voice_transcription", False)
    assert not repo.is_feature_enabled("voice_transcription")

    reopened = SQLiteWhitelistRepository(tmp_path / "assistant.sqlite3")
    assert not reopened.is_feature_enabled("voice_transcription")


def test_settings_repository_persists_transcription_decoration_flag(tmp_path):
    repo = SQLiteWhitelistRepository(tmp_path / "assistant.sqlite3")

    assert not repo.is_transcription_decoration_enabled()

    repo.set_transcription_decoration_enabled(True)
    assert repo.is_transcription_decoration_enabled()

    reopened = SQLiteWhitelistRepository(tmp_path / "assistant.sqlite3")
    assert reopened.is_transcription_decoration_enabled()

    reopened.set_transcription_decoration_enabled(False)
    assert not repo.is_transcription_decoration_enabled()


def test_settings_repository_persists_prompt_override_and_reset(tmp_path):
    repo = SQLiteWhitelistRepository(tmp_path / "assistant.sqlite3")

    assert "Не делай summary" in repo.get_text_polish_prompt()

    repo.set_text_polish_prompt("Исправь пунктуацию и сохрани смысл.")
    assert repo.get_text_polish_prompt() == "Исправь пунктуацию и сохрани смысл."

    repo.reset_text_polish_prompt()
    assert "Не делай summary" in repo.get_text_polish_prompt()


def test_processed_messages_are_idempotent(tmp_path):
    repo = SQLiteWhitelistRepository(tmp_path / "assistant.sqlite3")

    assert not repo.is_processed(1, 2, "voice_transcription")
    assert repo.mark_processed(1, 2, "voice_transcription")
    assert repo.is_processed(1, 2, "voice_transcription")
    assert not repo.mark_processed(1, 2, "voice_transcription")
    assert repo.mark_processed(1, 2, "other_feature")


def test_private_chat_message_gate_persists_allowed_chats(tmp_path):
    repo = SQLiteWhitelistRepository(tmp_path / "assistant.sqlite3")

    assert repo.get_private_chat_message_gate(100) is None

    repo.save_private_chat_message_gate(chat_id=100, message_count=100, is_allowed=True)

    reopened = SQLiteWhitelistRepository(tmp_path / "assistant.sqlite3")
    gate = reopened.get_private_chat_message_gate(100)
    assert gate is not None
    assert gate["chat_id"] == 100
    assert gate["message_count"] == 100
    assert gate["is_allowed"] is True
    assert isinstance(gate["checked_at"], int)


def test_private_chat_message_gate_persists_denied_chats(tmp_path):
    repo = SQLiteWhitelistRepository(tmp_path / "assistant.sqlite3")

    repo.save_private_chat_message_gate(chat_id=100, message_count=42, is_allowed=False)

    gate = repo.get_private_chat_message_gate(100)
    assert gate is not None
    assert gate["chat_id"] == 100
    assert gate["message_count"] == 42
    assert gate["is_allowed"] is False
    assert isinstance(gate["checked_at"], int)


def test_set_text_polish_prompt_rejects_empty_string(tmp_path):
    import pytest

    repo = SQLiteWhitelistRepository(tmp_path / "assistant.sqlite3")
    with pytest.raises(ValueError, match="Prompt cannot be empty"):
        repo.set_text_polish_prompt("   ")


def test_legacy_aliases_block_and_unblock_chats(tmp_path):
    repo = SQLiteWhitelistRepository(tmp_path / "assistant.sqlite3")
    repo.add_chat(123, "Alice")  # alias for block_chat
    assert repo.is_blocked(123)
    assert not repo.is_allowed(123)
    assert {"chat_id": 123, "title": "Alice"} in repo.list_chats()  # alias for list_blocked_chats
    repo.remove_chat(123)  # alias for unblock_chat
    assert not repo.is_blocked(123)
    assert repo.is_allowed(123)


def test_storage_schema_migrates_legacy_known_groups_without_last_seen_at(tmp_path):
    """Older DBs created known_group_chats without last_seen_at; new code adds the column."""
    import sqlite3

    db_path = tmp_path / "legacy.sqlite3"
    # Build a legacy schema lacking last_seen_at.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE known_group_chats (chat_id INTEGER PRIMARY KEY, title TEXT, updated_at INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("INSERT INTO known_group_chats(chat_id, title, updated_at) VALUES (-100, 'Legacy', 0)")
        conn.commit()

    repo = SQLiteWhitelistRepository(db_path)  # triggers migration

    # Migration should have added last_seen_at and preserved the row.
    groups = repo.list_known_groups()
    assert any(g["chat_id"] == -100 for g in groups)
