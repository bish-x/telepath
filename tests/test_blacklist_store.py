from telepath.storage import SQLiteAssistantRepository


def test_blacklist_repository_blocks_lists_and_unblocks_chats(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    assert not repo.is_blocked(123)

    repo.block_chat(chat_id=123, title="Alice")
    repo.block_chat(chat_id=456, title=None)

    assert repo.is_blocked(123)
    assert repo.is_blocked(456)
    assert repo.list_blocked_chats() == [
        {"chat_id": 123, "title": "Alice"},
        {"chat_id": 456, "title": None},
    ]

    repo.unblock_chat(123)

    assert not repo.is_blocked(123)
    assert repo.is_blocked(456)


def test_repository_allows_lists_and_removes_whitelisted_groups(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    assert not repo.is_group_allowed(-100123)

    repo.allow_group(chat_id=-100123, title="Team")
    repo.allow_group(chat_id=-100456, title=None)

    assert repo.is_group_allowed(-100123)
    assert repo.is_group_allowed(-100456)
    assert repo.list_allowed_groups() == [
        {"chat_id": -100456, "title": None},
        {"chat_id": -100123, "title": "Team"},
    ]

    repo.disallow_group(-100123)

    assert not repo.is_group_allowed(-100123)
    assert repo.is_group_allowed(-100456)


def test_repository_stores_known_groups_for_panel_picker(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    repo.upsert_known_group(chat_id=-100456, title="Beta", last_seen_at=10)
    repo.upsert_known_group(chat_id=-100123, title="Alpha", last_seen_at=20)
    repo.upsert_known_group(chat_id=-100456, title="Beta renamed", last_seen_at=30)

    assert repo.list_known_groups() == [
        {"chat_id": -100456, "title": "Beta renamed"},
        {"chat_id": -100123, "title": "Alpha"},
    ]
