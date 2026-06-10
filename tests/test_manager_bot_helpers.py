from pathlib import Path

from telepath.config import Settings
from telepath.features.channel_reactions import ReactionCandidate
from telepath.manager_bot import (
    _active_llm_model,
    _bind_reaction_history_completion_notifier,
    _is_callback_query_expired_error,
    _is_message_not_modified_error,
    _parse_reaction_available_refresh_action,
    _parse_reaction_history_backfill_action,
    _panel_callback_ack_text,
    _panel_markup,
    _reaction_history_backfill_completion_message,
    _reaction_history_backfill_feedback,
    _render_reaction_folder_refresh_action,
    _render_reaction_channel_detail_action,
    _render_reaction_history_backfill_action,
    _render_reaction_available_refresh_action,
    _should_retry_panel_without_custom_icons,
)
from telepath.panel import ControlPanelService, PanelButton, PanelView
from telepath.storage import SQLiteAssistantRepository
from telepath.user_client import ChannelReactionHistoryBackfillResult


def _settings(**overrides) -> Settings:
    base = dict(
        telegram_api_id=1,
        telegram_api_hash="h",
        telegram_session="s",
        manager_bot_token="t",
        owner_id=1,
        database_path=Path("data/x.sqlite3"),
    )
    base.update(overrides)
    return Settings(**base)


def test_active_llm_model_for_each_provider():
    assert _active_llm_model(_settings(llm_provider="openai", openai_model="gpt-x")) == "gpt-x"
    assert (
        _active_llm_model(_settings(llm_provider="anthropic", anthropic_model="claude-x"))
        == "claude-x"
    )
    assert _active_llm_model(_settings(llm_provider="copilot", copilot_model="cop-x")) == "cop-x"


def test_active_llm_model_returns_none_for_unknown_provider():
    assert _active_llm_model(_settings(llm_provider="unknown")) is None


def test_panel_markup_returns_none_when_keyboard_empty():
    view = PanelView(text="hi", keyboard=[])

    assert _panel_markup(view) is None


def test_panel_markup_builds_inline_keyboard_with_panel_prefix():
    view = PanelView(
        text="hi",
        keyboard=[
            [PanelButton("A", "main"), PanelButton("B", "status")],
            [PanelButton("Back", "back")],
        ],
    )

    markup = _panel_markup(view)

    assert markup is not None
    rows = markup.inline_keyboard
    assert [(btn.text, btn.callback_data) for btn in rows[0]] == [
        ("A", "panel:main"),
        ("B", "panel:status"),
    ]
    assert [(btn.text, btn.callback_data) for btn in rows[1]] == [("Back", "panel:back")]


def test_panel_markup_preserves_button_style_and_custom_emoji_icon():
    view = PanelView(
        text="hi",
        keyboard=[
            [PanelButton("Premium", "premium", style="primary", icon_custom_emoji_id="1234567890123456789")],
            [PanelButton("Bad", "bad", style="danger")],
        ],
    )

    markup = _panel_markup(view)

    assert markup is not None
    premium = markup.inline_keyboard[0][0].model_dump(exclude_none=True)
    bad = markup.inline_keyboard[1][0].model_dump(exclude_none=True)
    assert premium["style"] == "primary"
    assert premium["icon_custom_emoji_id"] == "1234567890123456789"
    assert bad["style"] == "danger"


def test_panel_markup_can_strip_custom_emoji_icons_for_fallback():
    view = PanelView(
        text="hi",
        keyboard=[
            [PanelButton("Premium", "premium", style="primary", icon_custom_emoji_id="1234567890123456789")],
        ],
    )

    markup = _panel_markup(view, include_custom_icons=False)

    assert markup is not None
    button = markup.inline_keyboard[0][0].model_dump(exclude_none=True)
    assert button["style"] == "primary"
    assert "icon_custom_emoji_id" not in button


def test_custom_emoji_icon_retry_gate_is_limited_to_custom_icon_errors():
    view_with_icon = PanelView(
        text="hi",
        keyboard=[[PanelButton("Premium", "premium", icon_custom_emoji_id="1234567890123456789")]],
    )
    plain_view = PanelView(text="hi", keyboard=[[PanelButton("A", "main")]])

    assert _should_retry_panel_without_custom_icons(
        Exception("Bad Request: custom emoji icon is not allowed"),
        view_with_icon,
    )
    assert not _should_retry_panel_without_custom_icons(Exception("Bad Request: message is not modified"), view_with_icon)
    assert not _should_retry_panel_without_custom_icons(
        Exception("Bad Request: custom emoji icon is not allowed"),
        plain_view,
    )


def test_message_not_modified_detection_is_limited_to_identical_edit_errors():
    assert _is_message_not_modified_error(
        Exception("Bad Request: message is not modified: specified new message content and reply markup are exactly the same")
    )
    assert not _is_message_not_modified_error(Exception("Bad Request: custom emoji icon is not allowed"))


def test_callback_query_expired_detection_is_limited_to_stale_callback_answers():
    assert _is_callback_query_expired_error(
        Exception("Telegram server says - Bad Request: query is too old and response timeout expired or query ID is invalid")
    )
    assert not _is_callback_query_expired_error(Exception("Bad Request: message is not modified"))


def test_panel_callback_ack_text_is_only_for_state_changing_channel_actions():
    assert _panel_callback_ack_text("reactions.channel.toggle:-100123:0", PanelView(text="", keyboard=[])) == "Обновлено."
    assert _panel_callback_ack_text("reactions.channel.max3:-100123:0", PanelView(text="", keyboard=[])) == "Обновлено."
    assert _panel_callback_ack_text("reactions.channel.strategy:-100123:random:0") == "Обновлено."
    assert _panel_callback_ack_text("reactions.channel.source:-100123:premium:0") == "Обновлено."
    assert _panel_callback_ack_text("reactions.channel.mode:-100123:negative:0") == "Обновлено."
    assert _panel_callback_ack_text("reactions.channel.emoji:-100123:🔥:0") == "Обновлено."
    assert _panel_callback_ack_text("rce:-100123:🔥:0") == "Обновлено."
    assert _panel_callback_ack_text("rcc:-100123:🔥:0") == "Обновлено."
    assert _panel_callback_ack_text("reactions.folder.toggle:2") == "Обновлено."
    assert _panel_callback_ack_text("reactions.folder.mode:2:all") == "Обновлено."
    assert _panel_callback_ack_text("reactions.folder.source:2:premium") == "Обновлено."

    assert _panel_callback_ack_text("reactions.channel:-100123:0") is None
    assert _panel_callback_ack_text("reactions.folder:2") is None
    assert _panel_callback_ack_text("reactions.channel.emojis:-100123:0") is None
    assert _panel_callback_ack_text("reactions.page:0") is None
    assert _panel_callback_ack_text("status.refresh") is None


def test_parse_reaction_available_refresh_action():
    assert _parse_reaction_available_refresh_action("rcr:-100123:2") == (-100123, 2)
    assert _parse_reaction_available_refresh_action("rcr:bad:2") is None
    assert _parse_reaction_available_refresh_action("reactions.channel:-100123:2") is None


def test_parse_reaction_history_backfill_action():
    assert _parse_reaction_history_backfill_action("rhb:all:1000") == (None, 1000, 0)
    assert _parse_reaction_history_backfill_action("rhb:all:all") == (None, None, 0)
    assert _parse_reaction_history_backfill_action("rhb:ch:-100123:5000:2") == (-100123, 5000, 2)
    assert _parse_reaction_history_backfill_action("rhb:ch:-100123:all:2") == (-100123, None, 2)
    assert _parse_reaction_history_backfill_action("rhb:all:10") is None
    assert _parse_reaction_history_backfill_action("rhb:all:bad") is None
    assert _parse_reaction_history_backfill_action("rhb:ch:-100123:0:2") is None
    assert _parse_reaction_history_backfill_action("reactions.history") is None


async def test_render_reaction_history_backfill_action_enqueues_channel_history(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    repo.upsert_reaction_channel(-100123, "News")
    repo.set_reaction_channel_enabled(-100123, True)
    panel = ControlPanelService(owner_id=10, state=repo)

    class FakeBackfill:
        def __init__(self):
            self.calls = []

        async def enqueue_history(self, *, limit_per_channel, chat_id=None):
            self.calls.append((limit_per_channel, chat_id))
            return ChannelReactionHistoryBackfillResult(
                channel_count=1,
                scanned_count=0,
                sent_count=0,
                skipped_count=0,
                limit_per_channel=limit_per_channel,
                target_chat_id=chat_id,
                request_queued=True,
                queue_position=1,
            )

    backfill = FakeBackfill()

    view = await _render_reaction_history_backfill_action(
        user_id=10,
        action="rhb:ch:-100123:all:0",
        panel=panel,
        reaction_history_backfill=backfill,
    )

    assert backfill.calls == [(None, -100123)]
    assert "История добавлена в очередь" in view.text
    assert "Позиция: 1" in view.text
    assert "8-15 сек" in view.text
    assert "Реакций поставлено" not in view.text
    assert view.action == "reactions.channel.history:-100123:0"


async def test_render_reaction_history_backfill_action_reports_split_runtime(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = await _render_reaction_history_backfill_action(
        user_id=10,
        action="rhb:all:1000",
        panel=panel,
        reaction_history_backfill=None,
    )

    assert "Telegram user client недоступен" in view.text


def test_reaction_history_backfill_feedback_reports_missing_targets():
    feedback = _reaction_history_backfill_feedback(
        ChannelReactionHistoryBackfillResult(
            channel_count=0,
            scanned_count=0,
            sent_count=0,
            skipped_count=0,
            limit_per_channel=1000,
        )
    )

    assert "Нет включенных каналов для массовой обработки истории" in feedback
    assert "карточки" in feedback
    assert "Новых постов" not in feedback


def test_reaction_history_backfill_feedback_reports_missing_channel_settings():
    feedback = _reaction_history_backfill_feedback(
        ChannelReactionHistoryBackfillResult(
            channel_count=0,
            scanned_count=0,
            sent_count=0,
            skipped_count=0,
            limit_per_channel=1000,
            target_chat_id=-100123,
        )
    )

    assert "нет сохраненных настроек" in feedback
    assert "массовой" not in feedback


def test_reaction_history_backfill_completion_message_includes_channel_and_metrics():
    message = _reaction_history_backfill_completion_message(
        ChannelReactionHistoryBackfillResult(
            channel_count=1,
            scanned_count=7,
            sent_count=3,
            skipped_count=4,
            failed_count=1,
            limit_per_channel=1000,
            target_chat_id=-100123,
            reaction_count=5,
            skip_reasons={"already_processed": 2, "service_message": 1, "media_group_duplicate": 1},
        ),
        channel_title="News",
    )

    assert "История автолайка завершена с ошибками" in message
    assert "Канал: News (-100123)" in message
    assert "Лимит: 1000 новых пригодных постов" in message
    assert "Просканировано: 7" in message
    assert "Реакций поставлено: 5" in message
    assert "Постов с реакциями: 3" in message
    assert "Пропущено: 4" in message
    assert "Причины: уже обработаны 2, сервисные 1, альбомы 1" in message
    assert "Ошибок: 1" in message


async def test_bind_reaction_history_completion_notifier_sends_owner_message(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)

    class FakeBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, chat_id, text):
            self.messages.append((chat_id, text))

    class FakeBackfill:
        def __init__(self):
            self.notifier = None

        def set_completion_notifier(self, notifier):
            self.notifier = notifier

    bot = FakeBot()
    backfill = FakeBackfill()

    _bind_reaction_history_completion_notifier(
        bot=bot,
        owner_id=10,
        state=repo,
        reaction_history_backfill=backfill,
    )

    assert backfill.notifier is not None
    await backfill.notifier(
        ChannelReactionHistoryBackfillResult(
            channel_count=1,
            scanned_count=2,
            sent_count=1,
            skipped_count=1,
            limit_per_channel=None,
            target_chat_id=-100123,
            reaction_count=2,
        )
    )

    assert len(bot.messages) == 1
    assert bot.messages[0][0] == 10
    assert "Канал: News (-100123)" in bot.messages[0][1]
    assert "Лимит: все посты" in bot.messages[0][1]
    assert "Реакций поставлено: 2" in bot.messages[0][1]


async def test_render_reaction_available_refresh_action_updates_available_reactions(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    panel = ControlPanelService(owner_id=10, state=repo)

    class FakeExporter:
        client = object()

    class FakeSender:
        def __init__(self, client):
            self.client = client

        async def available_reactions(self, chat_id):
            assert chat_id == -100123
            return [ReactionCandidate(kind="emoji", emoji="👍", value="ignored", category="positive")]

    view = await _render_reaction_available_refresh_action(
        user_id=10,
        action="rcr:-100123:0",
        panel=panel,
        state=repo,
        chat_exporter=FakeExporter(),
        sender_factory=FakeSender,
    )

    assert "Доступных реакций: 1" in view.text
    assert "Доступные реакции обновлены: 1." in view.text
    assert repo.list_reaction_channel_available_reactions(-100123) == [
        {"emoji": "👍", "kind": "emoji", "category": "positive"}
    ]


async def test_render_reaction_available_refresh_action_reports_split_manager_without_user_client(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    panel = ControlPanelService(owner_id=10, state=repo)

    view = await _render_reaction_available_refresh_action(
        user_id=10,
        action="rcr:-100123:0",
        panel=panel,
        state=repo,
        chat_exporter=None,
        sender_factory=None,
    )

    assert "Не могу обновить из manager-only режима" in view.text


async def test_render_reaction_folder_refresh_action_updates_cached_folders(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    class FakeClient:
        async def __call__(self, request):
            title = type("Title", (), {"text": "AI feeds"})()
            return [type("Folder", (), {"id": 2, "title": title})()]

        async def iter_dialogs(self, *, folder=None):
            if folder == 2:
                yield type(
                    "Dialog",
                    (),
                    {
                        "id": -100123,
                        "title": "News",
                        "is_user": False,
                        "is_group": False,
                        "is_channel": True,
                    },
                )()

    class FakeExporter:
        client = FakeClient()

    view = await _render_reaction_folder_refresh_action(
        user_id=10,
        panel=panel,
        state=repo,
        chat_exporter=FakeExporter(),
    )

    assert "Папки обновлены: 1" in view.text
    assert "○ AI feeds · 1 канал" in [button.text for row in view.keyboard for button in row]


async def test_render_reaction_channel_detail_action_refreshes_available_reactions_once(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    panel = ControlPanelService(owner_id=10, state=repo)
    calls = []

    class FakeExporter:
        client = object()

    class FakeSender:
        def __init__(self, client):
            self.client = client

        async def available_reactions(self, chat_id):
            calls.append(chat_id)
            return [
                ReactionCandidate(kind="emoji", emoji="👍", value="ignored", category="positive"),
                ReactionCandidate(kind="custom", emoji="1234567890123", value="ignored", category="neutral"),
            ]

    first = await _render_reaction_channel_detail_action(
        user_id=10,
        action="reactions.channel:-100123:0",
        panel=panel,
        state=repo,
        chat_exporter=FakeExporter(),
        sender_factory=FakeSender,
    )
    second = await _render_reaction_channel_detail_action(
        user_id=10,
        action="reactions.channel:-100123:0",
        panel=panel,
        state=repo,
        chat_exporter=FakeExporter(),
        sender_factory=FakeSender,
    )

    assert calls == [-100123]
    assert "Доступных реакций: 2" in first.text
    assert "Доступных реакций: 2" in second.text
    assert repo.list_reaction_channel_available_reactions(-100123) == [
        {"emoji": "👍", "kind": "emoji", "category": "positive"},
        {"emoji": "1234567890123", "kind": "custom", "category": "neutral"},
    ]


async def test_render_reaction_channel_detail_action_marks_empty_refresh_as_checked(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    panel = ControlPanelService(owner_id=10, state=repo)
    calls = []

    class FakeExporter:
        client = object()

    class FakeSender:
        def __init__(self, client):
            self.client = client

        async def available_reactions(self, chat_id):
            calls.append(chat_id)
            return []

    view = await _render_reaction_channel_detail_action(
        user_id=10,
        action="reactions.channel:-100123:0",
        panel=panel,
        state=repo,
        chat_exporter=FakeExporter(),
        sender_factory=FakeSender,
    )
    await _render_reaction_channel_detail_action(
        user_id=10,
        action="reactions.channel:-100123:0",
        panel=panel,
        state=repo,
        chat_exporter=FakeExporter(),
        sender_factory=FakeSender,
    )

    assert calls == [-100123]
    assert repo.has_reaction_channel_available_reactions_checked(-100123)
    assert "Доступных реакций: 0" in view.text


async def test_render_reaction_channel_detail_action_reports_split_manager_without_user_client(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    panel = ControlPanelService(owner_id=10, state=repo)

    view = await _render_reaction_channel_detail_action(
        user_id=10,
        action="reactions.channel:-100123:0",
        panel=panel,
        state=repo,
        chat_exporter=None,
        sender_factory=None,
    )

    assert "Не могу автоматически обновить из manager-only режима" in view.text
    assert not repo.has_reaction_channel_available_reactions_checked(-100123)
