from telepath.features.channel_reactions import ChannelReactionSettings, ReactionCandidate
from telepath.storage import SQLiteAssistantRepository


def test_repository_persists_account_premium_status(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    assert not repo.is_account_premium()

    repo.set_account_premium(True)

    reopened = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    assert reopened.is_account_premium()


def test_repository_persists_reaction_autolike_global_enabled_flag(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    assert repo.is_reaction_autolike_enabled()

    repo.set_reaction_autolike_enabled(False)

    reopened = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    assert not reopened.is_reaction_autolike_enabled()


def test_repository_persists_known_chats_with_kind_and_recent_order(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    repo.upsert_known_chat(100, "Alice", "private", last_seen_at=10)
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    repo.upsert_known_chat(-200, "Team", "group", last_seen_at=20)

    assert repo.list_known_chats() == [
        {"chat_id": -100123, "title": "News", "kind": "channel"},
        {"chat_id": -200, "title": "Team", "kind": "group"},
        {"chat_id": 100, "title": "Alice", "kind": "private"},
    ]
    assert repo.list_known_chats(kind="private") == [
        {"chat_id": 100, "title": "Alice", "kind": "private"}
    ]


def test_repository_does_not_clear_known_chat_title_on_empty_reaction_upsert(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)

    repo.set_reaction_channel_enabled(-100123, True)

    assert repo.list_known_chats(kind="channel") == [
        {"chat_id": -100123, "title": "News", "kind": "channel"}
    ]


def test_repository_persists_private_chat_transcription_override(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    assert not repo.is_private_chat_transcription_enabled(100)

    repo.set_private_chat_transcription(100, "Alice", True)

    reopened = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    assert reopened.is_private_chat_transcription_enabled(100)
    assert reopened.get_private_chat_transcription_override(100) is True
    assert reopened.list_private_chat_transcription_overrides() == [
        {"chat_id": 100, "title": "Alice", "enabled": True}
    ]

    reopened.set_private_chat_transcription(100, "Alice", False)
    assert not reopened.is_private_chat_transcription_enabled(100)
    assert reopened.get_private_chat_transcription_override(100) is False


def test_repository_persists_transcription_threshold_and_min_voice_duration(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    assert repo.get_private_chat_min_messages() == 100
    assert repo.get_voice_min_duration_seconds() == 0

    repo.set_private_chat_min_messages(250)
    repo.set_voice_min_duration_seconds(12)

    reopened = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    assert reopened.get_private_chat_min_messages() == 250
    assert reopened.get_voice_min_duration_seconds() == 12


def test_repository_rejects_invalid_transcription_numeric_settings(tmp_path):
    import pytest

    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    with pytest.raises(ValueError, match="minimum messages"):
        repo.set_private_chat_min_messages(0)
    with pytest.raises(ValueError, match="duration"):
        repo.set_voice_min_duration_seconds(-1)


def test_repository_persists_channel_reaction_settings(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    assert repo.get_reaction_channel_settings(-100123) is None

    repo.upsert_reaction_channel(-100123, "News")
    repo.set_reaction_channel_enabled(-100123, True)
    repo.set_reaction_channel_mode(-100123, "negative")
    repo.set_reaction_channel_max_reactions(-100123, 3)
    repo.set_reaction_channel_selection_strategy(-100123, "random")
    repo.set_reaction_channel_source(-100123, "premium")
    repo.toggle_reaction_channel_emoji(-100123, "👎")
    repo.toggle_reaction_channel_emoji(-100123, "💩")
    repo.set_reaction_channel_emoji_category(-100123, "1234567890123", "negative")

    reopened = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    settings = reopened.get_reaction_channel_settings(-100123)

    assert settings == ChannelReactionSettings(
        enabled=True,
        mode="negative",
        selected_emojis=(),
        disabled_emojis=("👎", "💩"),
        max_reactions=3,
        selection_strategy="random",
        reaction_source="premium",
        emoji_categories={"1234567890123": "negative"},
        title="News",
    )
    assert reopened.list_reaction_channels() == [
        {
            "chat_id": -100123,
            "title": "News",
            "enabled": True,
            "mode": "negative",
            "max_reactions": 3,
            "selection_strategy": "random",
            "reaction_source": "premium",
        }
    ]


def test_repository_creates_reaction_channels_with_autolike_defaults(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    repo.upsert_reaction_channel(-100123, "News")

    settings = repo.get_reaction_channel_settings(-100123)
    assert settings.mode == "positive"
    assert settings.max_reactions == 3
    assert settings.selection_strategy == "random"


def test_repository_uses_enabled_reaction_folder_as_channel_fallback(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    repo.upsert_reaction_folder(2, "AI feeds", position=0)
    repo.replace_reaction_folder_members(
        2,
        [{"chat_id": -100123, "title": "News", "kind": "channel"}],
    )
    repo.set_reaction_folder_enabled(2, True)
    repo.set_reaction_folder_mode(2, "all")
    repo.set_reaction_folder_max_reactions(2, 3)
    repo.set_reaction_folder_selection_strategy(2, "random")
    repo.set_reaction_folder_source(2, "premium")

    reopened = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    assert reopened.list_reaction_folders() == [
        {
            "folder_id": 2,
            "title": "AI feeds",
            "enabled": True,
            "mode": "all",
            "max_reactions": 3,
            "selection_strategy": "random",
            "reaction_source": "premium",
            "channel_count": 1,
        }
    ]
    assert reopened.get_effective_reaction_channel_settings(-100123) == ChannelReactionSettings(
        enabled=True,
        mode="all",
        max_reactions=3,
        selection_strategy="random",
        reaction_source="premium",
        title="AI feeds",
    )


def test_repository_uses_last_enabled_reaction_folder_for_duplicate_channel(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    repo.upsert_reaction_folder(1, "First", position=0)
    repo.replace_reaction_folder_members(
        1,
        [{"chat_id": -100123, "title": "News", "kind": "channel"}],
    )
    repo.set_reaction_folder_mode(1, "positive")
    repo.set_reaction_folder_enabled(1, True)

    repo.upsert_reaction_folder(2, "Second", position=1)
    repo.replace_reaction_folder_members(
        2,
        [{"chat_id": -100123, "title": "News", "kind": "channel"}],
    )
    repo.set_reaction_folder_mode(2, "negative")
    repo.set_reaction_folder_source(2, "premium")
    repo.set_reaction_folder_enabled(2, True)

    settings = repo.get_effective_reaction_channel_settings(-100123)

    assert settings == ChannelReactionSettings(
        enabled=True,
        mode="negative",
        max_reactions=3,
        selection_strategy="random",
        reaction_source="premium",
        title="Second",
    )

    repo.set_reaction_folder_enabled(1, True)

    settings = repo.get_effective_reaction_channel_settings(-100123)

    assert settings == ChannelReactionSettings(
        enabled=True,
        mode="positive",
        max_reactions=3,
        selection_strategy="random",
        reaction_source="mixed",
        title="First",
    )


def test_repository_uses_last_enabled_channel_or_folder_source(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    repo.upsert_reaction_folder(2, "Folder", position=0)
    repo.replace_reaction_folder_members(
        2,
        [{"chat_id": -100123, "title": "News", "kind": "channel"}],
    )
    repo.set_reaction_folder_mode(2, "all")
    repo.set_reaction_folder_source(2, "premium")
    repo.set_reaction_folder_enabled(2, True)

    repo.upsert_reaction_channel(-100123, "News")
    repo.set_reaction_channel_mode(-100123, "negative")
    repo.set_reaction_channel_source(-100123, "standard")
    repo.set_reaction_channel_enabled(-100123, True)

    settings = repo.get_effective_reaction_channel_settings(-100123)

    assert settings == ChannelReactionSettings(
        enabled=True,
        mode="negative",
        max_reactions=3,
        selection_strategy="random",
        reaction_source="standard",
        title="News",
    )

    repo.set_reaction_folder_enabled(2, True)

    settings = repo.get_effective_reaction_channel_settings(-100123)

    assert settings == ChannelReactionSettings(
        enabled=True,
        mode="all",
        max_reactions=3,
        selection_strategy="random",
        reaction_source="premium",
        title="Folder",
    )


def test_repository_channel_reaction_settings_override_folder_fallback(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    repo.upsert_reaction_folder(2, "AI feeds", position=0)
    repo.replace_reaction_folder_members(
        2,
        [{"chat_id": -100123, "title": "News", "kind": "channel"}],
    )
    repo.set_reaction_folder_enabled(2, True)
    repo.set_reaction_folder_mode(2, "all")
    repo.upsert_reaction_channel(-100123, "News")
    repo.set_reaction_channel_enabled(-100123, False)
    repo.set_reaction_channel_mode(-100123, "negative")

    settings = repo.get_effective_reaction_channel_settings(-100123)

    assert settings == ChannelReactionSettings(
        enabled=False,
        mode="negative",
        max_reactions=3,
        selection_strategy="random",
        title="News",
    )


def test_repository_channel_defaults_ignore_current_global_reaction_mode(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_reaction_global_mode("all")

    repo.upsert_reaction_channel(-100123, "News")

    settings = repo.get_reaction_channel_settings(-100123)
    assert settings.mode == "positive"
    assert settings.max_reactions == 3
    assert settings.selection_strategy == "random"


def test_repository_global_reaction_mode_updates_channels_and_preserves_disabled_reactions(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    assert repo.get_reaction_global_mode() == "positive"
    repo.set_reaction_global_mode("negative")
    repo.upsert_reaction_channel(-1001, "News")
    repo.upsert_reaction_channel(-1002, "Muted")
    repo.toggle_reaction_channel_emoji(-1002, "🔥")
    repo.set_reaction_global_mode("all")

    assert repo.get_reaction_global_mode() == "all"
    assert repo.get_reaction_channel_settings(-1001).mode == "all"
    assert repo.get_reaction_channel_settings(-1002).mode == "all"
    assert repo.get_reaction_channel_settings(-1002).disabled_emojis == ("🔥",)


def test_repository_migrates_legacy_custom_whitelists_to_default_enabled_mode(tmp_path):
    import sqlite3

    path = tmp_path / "assistant.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO settings (key, value)
            VALUES ('reactions.global.mode', 'all')
            """
        )
        conn.execute(
            """
            CREATE TABLE channel_reaction_settings (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                enabled INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT 'positive',
                selected_emojis TEXT NOT NULL DEFAULT '[]',
                max_reactions INTEGER NOT NULL DEFAULT 1,
                selection_strategy TEXT NOT NULL DEFAULT 'priority',
                reaction_source TEXT NOT NULL DEFAULT 'mixed',
                emoji_categories TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO channel_reaction_settings (
                chat_id, title, enabled, mode, selected_emojis, max_reactions,
                selection_strategy, reaction_source, emoji_categories
            )
            VALUES (-100123, 'Legacy', 1, 'custom', '["🔥"]', 1, 'priority', 'mixed', '{}')
            """
        )

    reopened = SQLiteAssistantRepository(path)
    settings = reopened.get_reaction_channel_settings(-100123)

    assert settings.mode == "all"
    assert settings.selected_emojis == ()
    assert settings.disabled_emojis == ()


def test_repository_persists_reaction_delay_range(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    assert repo.get_reaction_delay_range_seconds() == (240, 900)

    repo.set_reaction_delay_range_seconds(45, 120)

    reopened = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    assert reopened.get_reaction_delay_range_seconds() == (45, 120)


def test_repository_persists_observed_channel_available_reactions(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    assert not repo.has_reaction_channel_available_reactions_checked(-100123)

    repo.replace_reaction_channel_available_reactions(
        -100123,
        [
            ReactionCandidate(kind="emoji", emoji="👍", value="ignored", category="positive"),
            ReactionCandidate(kind="emoji", emoji="👎", value="ignored", category="negative"),
            ReactionCandidate(kind="custom", emoji="1234567890123", value="ignored", category="positive"),
        ],
    )

    reopened = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    assert reopened.has_reaction_channel_available_reactions_checked(-100123)
    assert reopened.list_reaction_channel_available_reactions(-100123) == [
        {"emoji": "👍", "kind": "emoji", "category": "positive"},
        {"emoji": "👎", "kind": "emoji", "category": "negative"},
        {"emoji": "1234567890123", "kind": "custom", "category": "positive"},
    ]

    reopened.replace_reaction_channel_available_reactions(
        -100123,
        [ReactionCandidate(kind="emoji", emoji="🔥", value="ignored", category="positive")],
    )
    assert reopened.list_reaction_channel_available_reactions(-100123) == [
        {"emoji": "🔥", "kind": "emoji", "category": "positive"}
    ]


def test_repository_marks_available_reactions_checked_even_when_telegram_returns_none(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    repo.replace_reaction_channel_available_reactions(-100123, [])

    reopened = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    assert reopened.has_reaction_channel_available_reactions_checked(-100123)
    assert reopened.list_reaction_channel_available_reactions(-100123) == []


def test_repository_marks_legacy_observed_available_reactions_as_checked(tmp_path):
    import sqlite3

    path = tmp_path / "assistant.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE channel_available_reactions (
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
            INSERT INTO channel_available_reactions (chat_id, emoji, kind, category)
            VALUES (-100123, '👍', 'emoji', 'positive')
            """
        )

    repo = SQLiteAssistantRepository(path)

    assert repo.has_reaction_channel_available_reactions_checked(-100123)
    assert repo.list_reaction_channel_available_reactions(-100123) == [
        {"emoji": "👍", "kind": "emoji", "category": "positive"}
    ]


def test_repository_applies_reaction_category_overrides_to_picker_items(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_reaction_channel(-100123, "News")
    repo.replace_reaction_channel_available_reactions(
        -100123,
        [
            ReactionCandidate(kind="emoji", emoji="🔥", value="ignored", category="positive"),
            ReactionCandidate(kind="custom", emoji="1234567890123", value="ignored", category="neutral"),
        ],
    )

    repo.set_reaction_channel_emoji_category(-100123, "🔥", "negative")
    assert repo.cycle_reaction_channel_emoji_category(-100123, "1234567890123") == "positive"

    reopened = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    assert reopened.list_reaction_channel_available_reactions(-100123) == [
        {"emoji": "🔥", "kind": "emoji", "category": "negative"},
        {"emoji": "1234567890123", "kind": "custom", "category": "positive"},
    ]


def test_repository_rejects_invalid_reaction_settings(tmp_path):
    import pytest

    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_reaction_channel(-100123, "News")

    with pytest.raises(ValueError, match="mode"):
        repo.set_reaction_channel_mode(-100123, "weird")
    with pytest.raises(ValueError, match="mode"):
        repo.set_reaction_global_mode("custom")
    with pytest.raises(ValueError, match="max reactions"):
        repo.set_reaction_channel_max_reactions(-100123, 2)
    with pytest.raises(ValueError, match="selection strategy"):
        repo.set_reaction_channel_selection_strategy(-100123, "shuffle")
    with pytest.raises(ValueError, match="reaction source"):
        repo.set_reaction_channel_source(-100123, "paid")
    with pytest.raises(ValueError, match="mode"):
        repo.set_reaction_folder_mode(2, "weird")
    with pytest.raises(ValueError, match="max reactions"):
        repo.set_reaction_folder_max_reactions(2, 2)
    with pytest.raises(ValueError, match="selection strategy"):
        repo.set_reaction_folder_selection_strategy(2, "shuffle")
    with pytest.raises(ValueError, match="reaction source"):
        repo.set_reaction_folder_source(2, "paid")
    with pytest.raises(ValueError, match="category"):
        repo.set_reaction_channel_emoji_category(-100123, "🔥", "weird")
    with pytest.raises(ValueError, match="delay"):
        repo.set_reaction_delay_range_seconds(-1, 10)
    with pytest.raises(ValueError, match="delay"):
        repo.set_reaction_delay_range_seconds(20, 10)
