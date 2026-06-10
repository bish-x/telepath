import asyncio
import logging
from types import SimpleNamespace

import pytest
from telethon import errors, types, utils

from telepath.config import Settings
from telepath.features.channel_reactions import (
    ChannelMessageEvent,
    ChannelReactionSettings,
    DEFAULT_REACTION_EMOJIS,
    ReactionCandidate,
    ReactionSendResult,
)
from telepath.storage import SQLiteAssistantRepository
from telepath.user_client import (
    ChannelReactionHistoryBackfill,
    CachedTelethonChannelReactionSender,
    TelethonChannelReactionSender,
    PrivateChatHistoryGate,
    build_channel_message_event,
    build_voice_message_event,
    classify_dialog_kind,
    dispatch_voice_message,
    get_message_duration_seconds,
    is_private_bot_dialog,
    is_reactable_channel_message,
    is_transcribable_message,
    remember_chat_from_event,
    remember_group_from_event,
    record_account_premium_status,
    refresh_account_premium_status_loop,
    run_user_client,
    should_enqueue_channel_reaction,
    smart_set_telethon_reactions,
    sync_reaction_folders,
    sync_chat_catalog,
    sync_group_catalog,
    telethon_reaction_diversity_families,
    telethon_reaction_identifier,
    transcription_quote_entities,
    voice_message_fingerprint,
)


class FakeMessage:
    id = 55
    out = False
    voice = True
    video_note = None
    file = None
    media = None


class FakeEvent:
    chat_id = 100
    sender_id = 7
    is_private = True
    is_group = False
    is_channel = False
    message = FakeMessage()
    chat = None

    async def get_chat(self):
        return self.chat or type("Chat", (), {"title": "Known Group"})()


class FakeGroupRepository:
    def __init__(self):
        self.groups = []
        self.chats = []
        self.account_premium = None

    def upsert_known_group(self, chat_id, title, last_seen_at=None):
        self.groups.append((chat_id, title, last_seen_at))

    def upsert_known_chat(self, chat_id, title, kind, last_seen_at=None):
        self.chats.append((chat_id, title, kind, last_seen_at))

    def set_account_premium(self, is_premium):
        self.account_premium = is_premium


class FakePrivateGateRepository:
    def __init__(self, cached=None):
        self.cached = cached or {}
        self.saved = []

    def get_private_chat_message_gate(self, chat_id):
        return self.cached.get(chat_id)

    def save_private_chat_message_gate(self, chat_id, message_count, is_allowed):
        self.saved.append((chat_id, message_count, is_allowed))
        self.cached[chat_id] = {
            "chat_id": chat_id,
            "message_count": message_count,
            "is_allowed": is_allowed,
        }


def test_build_voice_message_event_carries_private_chat_flag():
    event = build_voice_message_event(FakeEvent())

    assert event.chat_id == 100
    assert event.message_id == 55
    assert event.sender_id == 7
    assert event.is_private is True
    assert event.is_group is False
    assert event.duration_seconds is None
    assert event.audio_fingerprint is None


def test_build_channel_message_event_carries_channel_post_metadata():
    fake_event = FakeEvent()
    fake_event.chat_id = -100123
    fake_event.is_private = False
    fake_event.is_group = False
    fake_event.is_channel = True
    fake_event.message = FakeMessage()
    fake_event.message.grouped_id = 999

    event = build_channel_message_event(fake_event)

    assert event.chat_id == -100123
    assert event.message_id == 55
    assert event.is_channel is True
    assert event.is_group is False
    assert event.grouped_id == 999


def test_build_voice_message_event_carries_group_chat_flag():
    fake_event = FakeEvent()
    fake_event.chat_id = -100123
    fake_event.is_private = False
    fake_event.is_group = True

    event = build_voice_message_event(fake_event)

    assert event.chat_id == -100123
    assert event.is_private is False
    assert event.is_group is True


def test_build_voice_message_event_carries_audio_duration_from_message_file():
    fake_event = FakeEvent()
    fake_event.message = FakeMessage()
    fake_event.message.file = type("File", (), {"duration": 301})()

    event = build_voice_message_event(fake_event)

    assert event.duration_seconds == 301


def test_build_voice_message_event_carries_video_note_duration_from_message_file():
    fake_event = FakeEvent()
    fake_event.message = FakeMessage()
    fake_event.message.voice = None
    fake_event.message.video_note = object()
    fake_event.message.file = type("File", (), {"duration": 301})()

    event = build_voice_message_event(fake_event)

    assert event.duration_seconds == 301


def test_get_message_duration_seconds_reads_document_attribute_duration():
    document = type("Document", (), {"attributes": [types.DocumentAttributeAudio(duration=302, voice=True)]})()
    message = FakeMessage()
    message.media = type("Media", (), {"document": document})()

    assert get_message_duration_seconds(message) == 302


def test_voice_message_fingerprint_uses_telegram_document_id_for_voice():
    document = type("Document", (), {"id": 123456789, "size": 2048})()
    message = FakeMessage()
    message.media = type("Media", (), {"document": document})()

    assert voice_message_fingerprint(message) == "telegram-document:voice:123456789"


def test_voice_message_fingerprint_uses_telegram_document_id_for_video_note():
    document = type("Document", (), {"id": 987654321, "size": 4096})()
    message = FakeMessage()
    message.voice = None
    message.video_note = object()
    message.media = type("Media", (), {"document": document})()

    assert voice_message_fingerprint(message) == "telegram-document:video_note:987654321"


def test_voice_message_fingerprint_skips_messages_without_document_id():
    message = FakeMessage()
    message.media = object()

    assert voice_message_fingerprint(message) is None


def test_is_transcribable_message_accepts_voice_and_video_notes():
    voice = FakeMessage()
    video_note = FakeMessage()
    video_note.voice = None
    video_note.video_note = object()
    plain = FakeMessage()
    plain.voice = None
    plain.video_note = None

    assert is_transcribable_message(voice)
    assert is_transcribable_message(video_note)
    assert not is_transcribable_message(plain)


def test_is_transcribable_message_rejects_regular_audio_files():
    audio_file = FakeMessage()
    audio_file.voice = None
    audio_file.video_note = None
    audio_file.audio = object()
    audio_file.document = object()

    assert not is_transcribable_message(audio_file)


def test_is_reactable_channel_message_accepts_channels_but_rejects_groups():
    channel = FakeEvent()
    channel.is_channel = True
    channel.is_group = False
    channel.message = FakeMessage()
    group = FakeEvent()
    group.is_channel = True
    group.is_group = True
    group.message = FakeMessage()
    plain = FakeEvent()
    plain.is_channel = False
    plain.is_group = False
    plain.message = FakeMessage()

    assert is_reactable_channel_message(channel)
    assert not is_reactable_channel_message(group)
    assert not is_reactable_channel_message(plain)


def test_is_reactable_channel_message_rejects_channel_service_actions():
    channel = FakeEvent()
    channel.is_channel = True
    channel.is_group = False
    channel.message = type("Message", (), {"action": object()})()

    assert not is_reactable_channel_message(channel)


def test_should_enqueue_channel_reaction_requires_enabled_channel_settings():
    class FakeReactionSettings:
        def __init__(self, *, global_enabled=True, channel_settings=None, effective_settings=None):
            self.global_enabled = global_enabled
            self.channel_settings = channel_settings or {}
            self.effective_settings = effective_settings if effective_settings is not None else self.channel_settings

        def is_reaction_autolike_enabled(self):
            return self.global_enabled

        def get_reaction_channel_settings(self, chat_id):
            return self.channel_settings.get(chat_id)

        def get_effective_reaction_channel_settings(self, chat_id):
            return self.effective_settings.get(chat_id)

    channel = FakeEvent()
    channel.chat_id = -100123
    channel.is_channel = True
    channel.is_group = False
    channel.message = FakeMessage()

    assert should_enqueue_channel_reaction(
        channel,
        FakeReactionSettings(channel_settings={-100123: ChannelReactionSettings(enabled=True)}),
    )
    assert not should_enqueue_channel_reaction(
        channel,
        FakeReactionSettings(channel_settings={-100123: ChannelReactionSettings(enabled=False)}),
    )
    assert not should_enqueue_channel_reaction(channel, FakeReactionSettings(channel_settings={}))
    assert not should_enqueue_channel_reaction(
        channel,
        FakeReactionSettings(
            global_enabled=False,
            channel_settings={-100123: ChannelReactionSettings(enabled=True)},
        ),
    )

    assert should_enqueue_channel_reaction(
        channel,
        FakeReactionSettings(
            channel_settings={},
            effective_settings={-100123: ChannelReactionSettings(enabled=True)},
        ),
    )


async def test_sync_reaction_folders_stores_folder_channels(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    class FakeClient:
        def __init__(self):
            self.requests = []
            self.folder_calls = []

        async def __call__(self, request):
            self.requests.append(request.__class__.__name__)
            title = type("Title", (), {"text": "AI feeds"})()
            return [type("Folder", (), {"id": 2, "title": title})()]

        async def iter_dialogs(self, *, folder=None):
            self.folder_calls.append(folder)
            dialogs = {
                2: [
                    type(
                        "Dialog",
                        (),
                        {
                            "id": -100123,
                            "title": "News",
                            "is_user": False,
                            "is_group": False,
                            "is_channel": True,
                        },
                    )(),
                    type(
                        "Dialog",
                        (),
                        {
                            "id": 100,
                            "title": "Alice",
                            "is_user": True,
                            "is_group": False,
                            "is_channel": False,
                        },
                    )(),
                ]
            }
            for dialog in dialogs.get(folder, []):
                yield dialog

    total = await sync_reaction_folders(FakeClient(), repo)

    assert total == 1
    assert repo.list_reaction_folders() == [
        {
            "folder_id": 2,
            "title": "AI feeds",
            "enabled": False,
            "mode": "positive",
            "max_reactions": 3,
            "selection_strategy": "random",
            "reaction_source": "mixed",
            "channel_count": 1,
        }
    ]
    assert repo.list_reaction_folder_channels(2) == [
        {"chat_id": -100123, "title": "News", "kind": "channel"}
    ]


async def test_sync_reaction_folders_reads_chatlist_include_peers(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    channel = types.Channel(
        id=123,
        title="News",
        photo=None,
        date=None,
        broadcast=True,
        megagroup=False,
        access_hash=456,
    )
    megagroup = types.Channel(
        id=124,
        title="Group",
        photo=None,
        date=None,
        broadcast=False,
        megagroup=True,
        access_hash=457,
    )
    user = types.User(id=125, first_name="Alice", access_hash=458)

    class FakeClient:
        def __init__(self):
            self.folder_calls = []
            self.entity_calls = []

        async def __call__(self, request):
            return [
                types.DialogFilterChatlist(
                    id=8,
                    title=types.TextWithEntities("Shared", []),
                    pinned_peers=[],
                    include_peers=[
                        types.InputPeerChannel(channel_id=123, access_hash=456),
                        types.InputPeerChannel(channel_id=124, access_hash=457),
                        types.InputPeerUser(user_id=125, access_hash=458),
                    ],
                )
            ]

        async def iter_dialogs(self, *, folder=None):
            self.folder_calls.append(folder)
            raise AssertionError("chatlists must be resolved from include_peers")
            yield

        async def get_entity(self, peers):
            self.entity_calls.append(peers)
            return [channel, megagroup, user]

    client = FakeClient()
    total = await sync_reaction_folders(client, repo)

    assert total == 1
    assert client.folder_calls == []
    assert len(client.entity_calls) == 1
    assert repo.list_reaction_folders()[0]["title"] == "Shared"
    assert repo.list_reaction_folder_channels(8) == [
        {"chat_id": utils.get_peer_id(channel), "title": "News", "kind": "channel"}
    ]


async def test_sync_reaction_folders_reads_regular_filter_include_peers_without_folder_iterator(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    channel = types.Channel(
        id=123,
        title="News",
        photo=None,
        date=None,
        broadcast=True,
        megagroup=False,
        access_hash=456,
    )

    class FakeClient:
        def __init__(self):
            self.folder_calls = []

        async def __call__(self, request):
            return [
                types.DialogFilter(
                    id=2,
                    title=types.TextWithEntities("AI feeds", []),
                    pinned_peers=[],
                    include_peers=[types.InputPeerChannel(channel_id=123, access_hash=456)],
                    exclude_peers=[],
                )
            ]

        async def iter_dialogs(self, *, folder=None):
            self.folder_calls.append(folder)
            raise AssertionError("explicit folder peers must not require folder iteration")
            yield

        async def get_entity(self, peers):
            return [channel]

    client = FakeClient()
    total = await sync_reaction_folders(client, repo)

    assert total == 1
    assert client.folder_calls == []
    assert repo.list_reaction_folder_channels(2) == [
        {"chat_id": utils.get_peer_id(channel), "title": "News", "kind": "channel"}
    ]


async def test_sync_reaction_folders_falls_back_to_include_peers_for_invalid_regular_folder(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    channel = types.Channel(
        id=123,
        title="News",
        photo=None,
        date=None,
        broadcast=True,
        megagroup=False,
        access_hash=456,
    )

    class FakeClient:
        async def __call__(self, request):
            return [
                types.DialogFilter(
                    id=2,
                    title=types.TextWithEntities("AI feeds", []),
                    pinned_peers=[],
                    include_peers=[types.InputPeerChannel(channel_id=123, access_hash=456)],
                    exclude_peers=[],
                )
            ]

        async def iter_dialogs(self, *, folder=None):
            raise errors.FolderIdInvalidError(request=None)
            yield

        async def get_entity(self, peers):
            return [channel]

    total = await sync_reaction_folders(FakeClient(), repo)

    assert total == 1
    assert repo.list_reaction_folder_channels(2) == [
        {"chat_id": utils.get_peer_id(channel), "title": "News", "kind": "channel"}
    ]


async def test_sync_reaction_folders_skips_inaccessible_chatlist_peers(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    channel = types.Channel(
        id=123,
        title="News",
        photo=None,
        date=None,
        broadcast=True,
        megagroup=False,
        access_hash=456,
    )
    good_peer = types.InputPeerChannel(channel_id=123, access_hash=456)
    private_peer = types.InputPeerChannel(channel_id=999, access_hash=999)

    class FakeClient:
        def __init__(self):
            self.entity_calls = []

        async def __call__(self, request):
            return [
                types.DialogFilterChatlist(
                    id=8,
                    title=types.TextWithEntities("Shared", []),
                    pinned_peers=[],
                    include_peers=[good_peer, private_peer],
                )
            ]

        async def get_entity(self, peers):
            self.entity_calls.append(peers)
            if isinstance(peers, list):
                raise errors.ChannelPrivateError(request=None)
            if peers is private_peer:
                raise errors.ChannelPrivateError(request=None)
            return channel

    client = FakeClient()
    total = await sync_reaction_folders(client, repo)

    assert total == 1
    assert client.entity_calls == [[good_peer, private_peer], good_peer, private_peer]
    assert repo.list_reaction_folder_channels(8) == [
        {"chat_id": utils.get_peer_id(channel), "title": "News", "kind": "channel"}
    ]


class FailingRegistry:
    async def dispatch(self, event, context):
        raise RuntimeError("pipeline failed")


class CapturingRegistry:
    def __init__(self):
        self.events = []

    async def dispatch(self, event, context):
        self.events.append(event)
        return "ok"


async def test_dispatch_voice_message_flags_private_bot_dialogs():
    event = FakeEvent()
    event.chat = type("User", (), {"bot": True})()
    registry = CapturingRegistry()

    result = await dispatch_voice_message(event, registry, context=object())

    assert result == "ok"
    assert registry.events[0].is_private_bot is True


async def test_dispatch_voice_message_skips_plain_messages_before_registry():
    event = FakeEvent()
    event.message = FakeMessage()
    event.message.voice = None
    event.message.video_note = None
    registry = CapturingRegistry()

    result = await dispatch_voice_message(event, registry, context=object())

    assert result is None
    assert registry.events == []


async def test_private_chat_history_gate_uses_cached_allowed_result_without_api():
    class FailingClient:
        async def iter_messages(self, *args, **kwargs):
            raise AssertionError("history API should not be called")

    repo = FakePrivateGateRepository(cached={100: {"chat_id": 100, "message_count": 100, "is_allowed": True}})
    gate = PrivateChatHistoryGate(FailingClient(), repo)

    assert await gate.has_enough_messages(100, 100)


async def test_private_chat_history_gate_uses_fresh_denied_result_without_api():
    class FailingClient:
        async def iter_messages(self, *args, **kwargs):
            raise AssertionError("history API should not be called")

    repo = FakePrivateGateRepository(
        cached={100: {"chat_id": 100, "message_count": 42, "is_allowed": False, "checked_at": 1000}}
    )
    gate = PrivateChatHistoryGate(FailingClient(), repo, denied_recheck_seconds=3600, now=lambda: 1200)

    assert not await gate.has_enough_messages(100, 100)


async def test_private_chat_history_gate_rechecks_stale_denied_result():
    class FakeClient:
        async def iter_messages(self, chat_id, *, limit):
            for message_id in range(100):
                yield type("Message", (), {"id": message_id, "action": None})()

    repo = FakePrivateGateRepository(
        cached={100: {"chat_id": 100, "message_count": 42, "is_allowed": False, "checked_at": 1000}}
    )
    gate = PrivateChatHistoryGate(FakeClient(), repo, denied_recheck_seconds=3600, now=lambda: 5000)

    assert await gate.has_enough_messages(100, 100)
    assert repo.saved == [(100, 100, True)]


async def test_private_chat_history_gate_counts_until_threshold_and_persists_allowed():
    class FakeClient:
        async def iter_messages(self, chat_id, *, limit):
            assert chat_id == 100
            # Gate now over-fetches so service messages can't undercount us.
            assert limit == 300
            for message_id in range(100):
                yield type("Message", (), {"id": message_id, "action": None})()

    repo = FakePrivateGateRepository()
    gate = PrivateChatHistoryGate(FakeClient(), repo)

    assert await gate.has_enough_messages(100, 100)
    assert repo.saved == [(100, 100, True)]


async def test_private_chat_history_gate_skips_action_messages_with_headroom():
    """Regression: with `limit == minimum`, a chat where the last N messages
    contain even one service event would be rejected even if hundreds of
    plain messages exist beyond. The gate must over-fetch and still reach
    the threshold."""

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit):
            assert limit == 300  # 100 minimum * 3 multiplier
            # 3 service messages mixed with 100 plain — the old impl would
            # have stopped at 97 plain and denied. With over-fetch we should
            # cross the threshold.
            yielded_plain = 0
            yielded_total = 0
            while yielded_total < 200 and yielded_plain < 100:
                yielded_total += 1
                if yielded_total in (5, 50, 95):
                    yield type("Message", (), {"id": yielded_total, "action": object()})()
                else:
                    yielded_plain += 1
                    yield type("Message", (), {"id": yielded_total, "action": None})()

    repo = FakePrivateGateRepository()
    gate = PrivateChatHistoryGate(FakeClient(), repo)

    assert await gate.has_enough_messages(100, 100)
    assert repo.saved == [(100, 100, True)]


async def test_private_chat_history_gate_persists_denied_result():
    class FakeClient:
        async def iter_messages(self, chat_id, *, limit):
            for message_id in range(42):
                yield type("Message", (), {"id": message_id, "action": None})()

    repo = FakePrivateGateRepository()
    gate = PrivateChatHistoryGate(FakeClient(), repo)

    assert not await gate.has_enough_messages(100, 100)
    assert repo.saved == [(100, 42, False)]


async def test_private_chat_history_gate_throttles_history_fetches_between_telegram_calls():
    class FakeClient:
        async def iter_messages(self, chat_id, *, limit):
            calls.append((chat_id, clock.current))
            yield type("Message", (), {"id": 1, "action": None})()

    class FakeClock:
        current = 100.0

        def monotonic(self):
            return self.current

        async def sleep(self, delay):
            sleeps.append(delay)
            self.current += delay

    calls = []
    sleeps = []
    clock = FakeClock()
    repo = FakePrivateGateRepository()
    gate = PrivateChatHistoryGate(
        FakeClient(),
        repo,
        history_throttle_seconds=5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert await gate.has_enough_messages(100, 1)
    clock.current += 1
    assert await gate.has_enough_messages(101, 1)

    assert sleeps == [4]
    assert calls == [(100, 100.0), (101, 105.0)]


async def test_channel_reaction_history_backfill_processes_enabled_existing_posts_from_channels_and_folders(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_account_premium(True)
    repo.upsert_known_chat(-100111, "Manual", "channel", last_seen_at=30)
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, True)
    repo.set_reaction_channel_mode(-100111, "negative")
    repo.set_reaction_channel_max_reactions(-100111, 1)
    repo.set_reaction_channel_selection_strategy(-100111, "priority")
    repo.set_reaction_channel_source(-100111, "standard")
    repo.upsert_reaction_channel(-100333, "Disabled")
    repo.set_reaction_channel_enabled(-100333, False)
    repo.upsert_reaction_folder(2, "Folder", position=0)
    repo.replace_reaction_folder_members(
        2,
        [{"chat_id": -100222, "title": "Folder channel", "kind": "channel"}],
    )
    repo.set_reaction_folder_enabled(2, True)
    repo.mark_processed(-100111, 2, "channel_reactions")

    class FakeClient:
        def __init__(self):
            self.calls = []
            self.messages = {
                -100111: [
                    SimpleNamespace(id=1, action=None),
                    SimpleNamespace(
                        id=2,
                        action=None,
                        reactions=types.MessageReactions(
                            results=[
                                types.ReactionCount(types.ReactionEmoji("👎"), count=1, chosen_order=1)
                            ]
                        ),
                    ),
                    SimpleNamespace(id=3, action=object()),
                ],
                -100222: [
                    SimpleNamespace(id=4, action=None, grouped_id=777),
                    SimpleNamespace(id=5, action=None, grouped_id=777),
                ],
            }

        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            self.calls.append((chat_id, limit, wait_time))
            messages = self.messages.get(chat_id, [])
            for message in messages if limit is None else messages[:limit]:
                yield message

    class FakeReactionSender:
        def __init__(self):
            self.available_calls = []
            self.sent = []
            self.available = {
                -100111: [
                    ReactionCandidate("emoji", "👍", "👍", category="positive"),
                    ReactionCandidate("emoji", "👎", "👎", category="negative"),
                    ReactionCandidate("custom", "premium:1", 1, category="positive"),
                ],
                -100222: [ReactionCandidate("emoji", "🔥", "🔥", category="positive")],
            }

        async def available_reactions(self, chat_id):
            self.available_calls.append(chat_id)
            return self.available[chat_id]

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            self.sent.append(
                (
                    event.chat_id,
                    event.message_id,
                    [reaction.emoji for reaction in reactions],
                    max_reactions,
                    [reaction.emoji for reaction in fallback_reactions],
                )
            )

    client = FakeClient()
    sender = FakeReactionSender()
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    delays = iter([8, 15])
    backfill = ChannelReactionHistoryBackfill(
        client=client,
        state=repo,
        reaction_sender=sender,
        randint=lambda minimum, maximum: next(delays),
        sleep=fake_sleep,
    )

    result = await backfill.process_history(limit_per_channel=1000)

    assert result.channel_count == 2
    assert result.scanned_count == 5
    assert result.sent_count == 2
    assert result.skipped_count == 3
    assert result.skip_reasons == {
        "already_processed": 1,
        "service_message": 1,
        "media_group_duplicate": 1,
    }
    assert result.already_running is False
    assert set(client.calls) == {(-100111, None, 1.0), (-100222, None, 1.0)}
    assert sender.available_calls == [-100111, -100222]
    assert sender.sent == [
        (-100111, 1, ["👎"], 1, []),
        (-100222, 4, ["🔥"], 3, []),
    ]
    assert sleeps == [8, 15]
    assert repo.is_processed(-100111, 1, "channel_reactions")
    assert repo.is_processed(-100222, 777, "channel_reactions_media_group")


async def test_channel_reaction_history_backfill_enqueues_manual_disabled_channel_settings(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100111, "Manual", "channel", last_seen_at=30)
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, False)
    repo.set_reaction_channel_mode(-100111, "negative")
    repo.set_reaction_channel_max_reactions(-100111, 1)
    repo.set_reaction_channel_selection_strategy(-100111, "priority")
    repo.set_reaction_channel_source(-100111, "standard")

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            yield SimpleNamespace(id=1, action=None)

    class FakeReactionSender:
        def __init__(self):
            self.available_calls = []
            self.sent = []

        async def available_reactions(self, chat_id):
            self.available_calls.append(chat_id)
            return [
                ReactionCandidate("emoji", "👍", "👍", category="positive"),
                ReactionCandidate("emoji", "👎", "👎", category="negative"),
            ]

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            self.sent.append(
                (
                    event.chat_id,
                    event.message_id,
                    [reaction.emoji for reaction in reactions],
                    max_reactions,
                )
            )

    sender = FakeReactionSender()
    backfill = ChannelReactionHistoryBackfill(
        client=FakeClient(),
        state=repo,
        reaction_sender=sender,
        sleep=lambda seconds: asyncio.sleep(0),
    )

    queued = await backfill.enqueue_history(limit_per_channel=1000, chat_id=-100111)
    await backfill.wait_history_queue_idle()

    assert queued.request_queued is True
    assert queued.channel_count == 1
    assert sender.available_calls == [-100111]
    assert sender.sent == [(-100111, 1, ["👎"], 1)]
    assert repo.is_processed(-100111, 1, "channel_reactions")


async def test_channel_reaction_history_backfill_is_single_flight(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, True)
    started = asyncio.Event()
    release = asyncio.Event()

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            started.set()
            await release.wait()
            yield SimpleNamespace(id=1, action=None)

    class FakeReactionSender:
        async def available_reactions(self, chat_id):
            return [ReactionCandidate("emoji", "👍", "👍", category="positive")]

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            return None

    backfill = ChannelReactionHistoryBackfill(
        client=FakeClient(),
        state=repo,
        reaction_sender=FakeReactionSender(),
        sleep=lambda seconds: asyncio.sleep(0),
    )
    first = asyncio.create_task(backfill.process_history(limit_per_channel=1000))
    await started.wait()

    busy = await backfill.process_history(limit_per_channel=1000)
    assert busy.already_running is True
    assert busy.sent_count == 0

    release.set()
    done = await first
    assert done.already_running is False
    assert done.sent_count == 1


async def test_channel_reaction_history_backfill_enqueue_runs_jobs_sequentially(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_reaction_channel(-100111, "First")
    repo.set_reaction_channel_enabled(-100111, True)
    repo.upsert_reaction_channel(-100222, "Second")
    repo.set_reaction_channel_enabled(-100222, True)
    started = {chat_id: asyncio.Event() for chat_id in (-100111, -100222)}
    release = {chat_id: asyncio.Event() for chat_id in (-100111, -100222)}

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            self.calls.append((chat_id, limit, wait_time))
            started[chat_id].set()
            await release[chat_id].wait()
            yield SimpleNamespace(id=abs(chat_id), action=None)

    class FakeReactionSender:
        def __init__(self):
            self.sent = []

        async def available_reactions(self, chat_id):
            return [ReactionCandidate("emoji", "👍", "👍", category="positive")]

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            self.sent.append((event.chat_id, event.message_id))

    client = FakeClient()
    sender = FakeReactionSender()
    backfill = ChannelReactionHistoryBackfill(
        client=client,
        state=repo,
        reaction_sender=sender,
        sleep=lambda seconds: asyncio.sleep(0),
    )

    first = await backfill.enqueue_history(limit_per_channel=1000, chat_id=-100111)
    assert first.request_queued is True
    assert first.queue_position == 1
    await started[-100111].wait()

    second = await backfill.enqueue_history(limit_per_channel=1000, chat_id=-100222)
    assert second.request_queued is True
    assert second.queue_position == 2
    assert not started[-100222].is_set()

    release[-100111].set()
    await started[-100222].wait()
    release[-100222].set()
    await backfill.wait_history_queue_idle()

    assert client.calls == [(-100111, None, 1.0), (-100222, None, 1.0)]
    assert sender.sent == [(-100111, 100111), (-100222, 100222)]


async def test_channel_reaction_history_backfill_enqueue_deduplicates_same_request(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, True)
    started = asyncio.Event()
    release = asyncio.Event()

    class FakeClient:
        def __init__(self):
            self.calls = 0

        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            self.calls += 1
            started.set()
            await release.wait()
            yield SimpleNamespace(id=1, action=None)

    class FakeReactionSender:
        async def available_reactions(self, chat_id):
            return [ReactionCandidate("emoji", "👍", "👍", category="positive")]

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            return None

    client = FakeClient()
    backfill = ChannelReactionHistoryBackfill(
        client=client,
        state=repo,
        reaction_sender=FakeReactionSender(),
        sleep=lambda seconds: asyncio.sleep(0),
    )

    first = await backfill.enqueue_history(limit_per_channel=1000, chat_id=-100111)
    assert first.request_queued is True
    await started.wait()

    duplicate = await backfill.enqueue_history(limit_per_channel=1000, chat_id=-100111)
    assert duplicate.request_queued is True
    assert duplicate.duplicate_queued is True
    assert duplicate.queue_position == 1

    release.set()
    await backfill.wait_history_queue_idle()

    assert client.calls == 1


async def test_channel_reaction_history_backfill_notifies_when_queued_job_completes(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, True)
    repo.set_account_premium(True)
    notifications = []

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            yield SimpleNamespace(id=1, action=None)

    class FakeReactionSender:
        async def available_reactions(self, chat_id):
            return [
                ReactionCandidate("emoji", "👍", "👍", category="positive"),
                ReactionCandidate("emoji", "🔥", "🔥", category="positive"),
                ReactionCandidate("emoji", "❤", "❤", category="positive"),
            ]

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            return len(reactions)

    async def notify(result):
        notifications.append(result)

    backfill = ChannelReactionHistoryBackfill(
        client=FakeClient(),
        state=repo,
        reaction_sender=FakeReactionSender(),
        sleep=lambda seconds: asyncio.sleep(0),
        completion_notifier=notify,
    )

    queued = await backfill.enqueue_history(limit_per_channel=1000, chat_id=-100111)
    await backfill.wait_history_queue_idle()

    assert queued.request_queued is True
    assert len(notifications) == 1
    result = notifications[0]
    assert result.target_chat_id == -100111
    assert result.limit_per_channel == 1000
    assert result.scanned_count == 1
    assert result.sent_count == 1
    assert result.reaction_count == 3
    assert result.skipped_count == 0
    assert result.failed_count == 0


async def test_channel_reaction_history_backfill_notifies_when_queued_job_fails(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, True)
    notifications = []

    class ExplodingBackfill(ChannelReactionHistoryBackfill):
        async def _process_history_locked(self, *, limit_per_channel, chat_id):
            raise RuntimeError("boom")

    async def notify(result):
        notifications.append(result)

    backfill = ExplodingBackfill(
        client=object(),
        state=repo,
        reaction_sender=object(),
        completion_notifier=notify,
    )

    queued = await backfill.enqueue_history(limit_per_channel=1000, chat_id=-100111)
    await backfill.wait_history_queue_idle()

    assert queued.request_queued is True
    assert len(notifications) == 1
    result = notifications[0]
    assert result.target_chat_id == -100111
    assert result.limit_per_channel == 1000
    assert result.failed_count == 1
    assert result.scanned_count == 0
    assert result.sent_count == 0


async def test_channel_reaction_history_backfill_skips_telegram_history_when_global_autolike_is_off(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, True)
    repo.set_reaction_autolike_enabled(False)

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            raise AssertionError("history should not be fetched when autolike is globally off")
            yield

    class FakeReactionSender:
        async def available_reactions(self, chat_id):
            raise AssertionError("available reactions should not be fetched when autolike is globally off")

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            raise AssertionError("reaction should not be sent when autolike is globally off")

    backfill = ChannelReactionHistoryBackfill(client=FakeClient(), state=repo, reaction_sender=FakeReactionSender())

    result = await backfill.process_history(limit_per_channel=1000)

    assert result.channel_count == 0
    assert result.scanned_count == 0
    assert result.sent_count == 0


async def test_channel_reaction_history_backfill_limit_counts_new_reactable_posts_not_raw_entries(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, True)
    repo.mark_processed(-100111, 2, "channel_reactions")

    class FakeClient:
        def __init__(self):
            self.calls = []
            self.seen = []

        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            self.calls.append((chat_id, limit, wait_time))
            messages = [
                SimpleNamespace(id=1, action=object()),
                SimpleNamespace(
                    id=2,
                    action=None,
                    reactions=types.MessageReactions(
                        results=[
                            types.ReactionCount(types.ReactionEmoji("👍"), count=1, chosen_order=1)
                        ]
                    ),
                ),
                *[SimpleNamespace(id=message_id, action=None) for message_id in range(3, 1004)],
            ]
            for message in messages if limit is None else messages[:limit]:
                self.seen.append(message.id)
                yield message

    class FakeReactionSender:
        def __init__(self):
            self.sent = []

        async def available_reactions(self, chat_id):
            return [ReactionCandidate("emoji", "👍", "👍", category="positive")]

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            self.sent.append(event.message_id)

    client = FakeClient()
    sender = FakeReactionSender()
    backfill = ChannelReactionHistoryBackfill(
        client=client,
        state=repo,
        reaction_sender=sender,
        sleep=lambda seconds: asyncio.sleep(0),
    )

    result = await backfill.process_history(limit_per_channel=1000, chat_id=-100111)

    assert client.calls == [(-100111, None, 1.0)]
    assert client.seen == list(range(1, 1003))
    assert sender.sent[0] == 3
    assert sender.sent[-1] == 1002
    assert len(sender.sent) == 1000
    assert result.sent_count == 1000
    assert result.skipped_count == 2
    assert result.skip_reasons == {"service_message": 1, "already_processed": 1}
    assert not repo.is_processed(-100111, 1003, "channel_reactions")


async def test_channel_reaction_history_backfill_reprocesses_processed_message_when_reaction_removed(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, True)
    repo.mark_processed(-100111, 1, "channel_reactions")

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            yield SimpleNamespace(
                id=1,
                action=None,
                reactions=types.MessageReactions(
                    results=[types.ReactionCount(types.ReactionEmoji("👍"), count=5)]
                ),
            )

    class FakeReactionSender:
        def __init__(self):
            self.sent = []

        async def available_reactions(self, chat_id):
            return [ReactionCandidate("emoji", "👍", "👍", category="positive")]

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            self.sent.append((event.chat_id, event.message_id, [reaction.emoji for reaction in reactions]))
            return len(reactions)

    sender = FakeReactionSender()
    backfill = ChannelReactionHistoryBackfill(
        client=FakeClient(),
        state=repo,
        reaction_sender=sender,
        sleep=lambda seconds: asyncio.sleep(0),
    )

    result = await backfill.process_history(limit_per_channel=1000, chat_id=-100111)

    assert sender.sent == [(-100111, 1, ["👍"])]
    assert result.sent_count == 1
    assert result.reaction_count == 1
    assert result.skipped_count == 0
    assert result.skip_reasons == {}


async def test_channel_reaction_history_backfill_reprocesses_processed_message_when_reactions_underfilled(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_account_premium(True)
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, True)
    repo.set_reaction_channel_max_reactions(-100111, 3)
    repo.mark_processed(-100111, 1, "channel_reactions")

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            yield SimpleNamespace(
                id=1,
                action=None,
                reactions=types.MessageReactions(
                    results=[
                        types.ReactionCount(types.ReactionEmoji("👍"), count=4, chosen_order=1),
                        types.ReactionCount(types.ReactionEmoji("🔥"), count=7),
                    ]
                ),
            )

    class FakeReactionSender:
        def __init__(self):
            self.sent = []

        async def available_reactions(self, chat_id):
            return [
                ReactionCandidate("emoji", "👍", "👍", category="positive"),
                ReactionCandidate("emoji", "🔥", "🔥", category="positive"),
                ReactionCandidate("emoji", "😘", "😘", category="positive"),
            ]

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            self.sent.append(
                (
                    event.chat_id,
                    event.message_id,
                    max_reactions,
                    [reaction.emoji for reaction in reactions],
                )
            )
            return 2

    sender = FakeReactionSender()
    backfill = ChannelReactionHistoryBackfill(
        client=FakeClient(),
        state=repo,
        reaction_sender=sender,
        sleep=lambda seconds: asyncio.sleep(0),
    )

    result = await backfill.process_history(limit_per_channel=1000, chat_id=-100111)

    assert [(chat_id, message_id, max_reactions) for chat_id, message_id, max_reactions, _ in sender.sent] == [
        (-100111, 1, 3)
    ]
    assert {emoji for *_, emojis in sender.sent for emoji in emojis} == {"👍", "🔥", "😘"}
    assert result.sent_count == 1
    assert result.reaction_count == 2
    assert result.skipped_count == 0
    assert result.skip_reasons == {}


async def test_channel_reaction_history_backfill_does_not_mark_no_reactions_as_processed(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, True)

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            yield SimpleNamespace(id=1, action=None)

    class FakeReactionSender:
        async def available_reactions(self, chat_id):
            return []

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            raise AssertionError("reaction should not be sent without candidates")

    backfill = ChannelReactionHistoryBackfill(
        client=FakeClient(),
        state=repo,
        reaction_sender=FakeReactionSender(),
        sleep=lambda seconds: asyncio.sleep(0),
    )

    result = await backfill.process_history(limit_per_channel=1000, chat_id=-100111)

    assert result.sent_count == 0
    assert result.skipped_count == 1
    assert result.skip_reasons == {"no_reactions_available": 1}
    assert not repo.is_processed(-100111, 1, "channel_reactions")


async def test_channel_reaction_history_backfill_does_not_mark_zero_sent_reactions_as_processed(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, True)

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            yield SimpleNamespace(id=1, action=None)

    class FakeReactionSender:
        def __init__(self):
            self.sent = []

        async def available_reactions(self, chat_id):
            return [ReactionCandidate("emoji", "👍", "👍", category="positive")]

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            self.sent.append((event.chat_id, event.message_id, [reaction.emoji for reaction in reactions]))
            return 0

    sender = FakeReactionSender()
    backfill = ChannelReactionHistoryBackfill(
        client=FakeClient(),
        state=repo,
        reaction_sender=sender,
        sleep=lambda seconds: asyncio.sleep(0),
    )

    result = await backfill.process_history(limit_per_channel=1000, chat_id=-100111)

    assert sender.sent == [(-100111, 1, ["👍"])]
    assert result.sent_count == 0
    assert result.reaction_count == 0
    assert result.skipped_count == 1
    assert result.skip_reasons == {"no_reactions_sent": 1}
    assert not repo.is_processed(-100111, 1, "channel_reactions")


async def test_channel_reaction_history_backfill_uses_none_limit_for_all_posts(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, True)

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            self.calls.append((chat_id, limit, wait_time))
            yield SimpleNamespace(id=1, action=None)

    class FakeReactionSender:
        async def available_reactions(self, chat_id):
            return [ReactionCandidate("emoji", "👍", "👍", category="positive")]

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            return None

    client = FakeClient()
    backfill = ChannelReactionHistoryBackfill(
        client=client,
        state=repo,
        reaction_sender=FakeReactionSender(),
        sleep=lambda seconds: asyncio.sleep(0),
    )

    result = await backfill.process_history(limit_per_channel=None, chat_id=-100111)

    assert client.calls == [(-100111, None, 1.0)]
    assert result.limit_per_channel is None
    assert result.sent_count == 1


async def test_channel_reaction_history_backfill_uses_cached_available_reactions(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, True)
    repo.replace_reaction_channel_available_reactions(
        -100111,
        [ReactionCandidate("emoji", "🔥", "🔥", category="positive")],
    )

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            yield SimpleNamespace(id=1, action=None)

    class FakeReactionSender:
        def __init__(self):
            self.sent = []

        async def available_reactions(self, chat_id):
            raise AssertionError("cached available reactions should avoid Telegram refresh")

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            self.sent.append([reaction.emoji for reaction in reactions])

    sender = FakeReactionSender()
    backfill = ChannelReactionHistoryBackfill(
        client=FakeClient(),
        state=repo,
        reaction_sender=sender,
        sleep=lambda seconds: asyncio.sleep(0),
    )

    result = await backfill.process_history(limit_per_channel=1000, chat_id=-100111)

    assert result.sent_count == 1
    assert sender.sent == [["🔥"]]


async def test_channel_reaction_history_backfill_avoids_repeating_same_random_set_for_channel(tmp_path, monkeypatch):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_account_premium(True)
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, True)
    repo.set_reaction_channel_mode(-100111, "all")
    repo.set_reaction_channel_selection_strategy(-100111, "random")
    repo.set_reaction_channel_max_reactions(-100111, 3)
    monkeypatch.setattr(
        "telepath.features.channel_reactions.random.sample",
        lambda population, k: list(population),
    )

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            yield SimpleNamespace(id=1, action=None)
            yield SimpleNamespace(id=2, action=None)

    class FakeReactionSender:
        def __init__(self):
            self.sent = []

        async def available_reactions(self, chat_id):
            return [
                ReactionCandidate("emoji", "👍", "👍", category="positive"),
                ReactionCandidate("emoji", "🔥", "🔥", category="positive"),
                ReactionCandidate("emoji", "🎉", "🎉", category="positive"),
                ReactionCandidate("emoji", "🤔", "🤔", category="neutral"),
            ]

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            self.sent.append(
                (
                    event.message_id,
                    [reaction.emoji for reaction in reactions],
                    [reaction.emoji for reaction in fallback_reactions],
                )
            )
            return len(reactions)

    sender = FakeReactionSender()
    backfill = ChannelReactionHistoryBackfill(
        client=FakeClient(),
        state=repo,
        reaction_sender=sender,
        sleep=lambda seconds: asyncio.sleep(0),
    )

    result = await backfill.process_history(limit_per_channel=1000, chat_id=-100111)

    assert result.sent_count == 2
    assert result.reaction_count == 6
    assert sender.sent == [
        (1, ["👍", "🔥", "🎉"], ["🤔"]),
        (2, ["👍", "🔥", "🤔"], ["🎉"]),
    ]


async def test_channel_reaction_history_backfill_tracks_actual_random_reactions_after_sender_fallback(
    tmp_path,
    monkeypatch,
):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_account_premium(True)
    repo.upsert_reaction_channel(-100111, "Manual")
    repo.set_reaction_channel_enabled(-100111, True)
    repo.set_reaction_channel_mode(-100111, "all")
    repo.set_reaction_channel_selection_strategy(-100111, "random")
    repo.set_reaction_channel_max_reactions(-100111, 3)
    monkeypatch.setattr(
        "telepath.features.channel_reactions.random.sample",
        lambda population, k: list(population),
    )

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit, wait_time=None):
            yield SimpleNamespace(id=1, action=None)
            yield SimpleNamespace(id=2, action=None)

    class FakeReactionSender:
        def __init__(self):
            self.sent = []
            self.actual_results = [
                ReactionSendResult(
                    count=3,
                    reaction_keys=(("emoji", "👍"), ("emoji", "🔥"), ("emoji", "🤔")),
                )
            ]

        async def available_reactions(self, chat_id):
            return [
                ReactionCandidate("emoji", "👍", "👍", category="positive"),
                ReactionCandidate("emoji", "🔥", "🔥", category="positive"),
                ReactionCandidate("emoji", "🎉", "🎉", category="positive"),
                ReactionCandidate("emoji", "🤔", "🤔", category="neutral"),
            ]

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            self.sent.append(
                (
                    event.message_id,
                    [reaction.emoji for reaction in reactions],
                    [reaction.emoji for reaction in fallback_reactions],
                )
            )
            if self.actual_results:
                return self.actual_results.pop(0)
            return len(reactions)

    sender = FakeReactionSender()
    backfill = ChannelReactionHistoryBackfill(
        client=FakeClient(),
        state=repo,
        reaction_sender=sender,
        sleep=lambda seconds: asyncio.sleep(0),
    )

    result = await backfill.process_history(limit_per_channel=1000, chat_id=-100111)

    assert result.sent_count == 2
    assert result.reaction_count == 6
    assert sender.sent == [
        (1, ["👍", "🔥", "🎉"], ["🤔"]),
        (2, ["👍", "🔥", "🎉"], ["🤔"]),
    ]


async def test_dispatch_voice_message_logs_pipeline_errors(caplog):
    with caplog.at_level(logging.ERROR):
        result = await dispatch_voice_message(FakeEvent(), FailingRegistry(), context=object())

    assert result == "error"
    assert "voice_dispatch_failed" in caplog.text
    assert "chat_id=100" in caplog.text
    assert "message_id=55" in caplog.text


async def test_remember_group_from_event_stores_group_chat_title():
    event = FakeEvent()
    event.chat_id = -100123
    event.is_private = False
    event.is_group = True
    repo = FakeGroupRepository()

    await remember_group_from_event(event, repo)

    assert len(repo.groups) == 1
    chat_id, title, last_seen_at = repo.groups[0]
    assert (chat_id, title) == (-100123, "Known Group")
    assert isinstance(last_seen_at, int)


async def test_sync_group_catalog_stores_user_dialog_groups_only():
    dialogs = [
        type("Dialog", (), {"id": -1001, "title": "Team", "is_group": True})(),
        type("Dialog", (), {"id": 1002, "title": "Alice", "is_group": False})(),
    ]

    class FakeClient:
        async def iter_dialogs(self):
            for dialog in dialogs:
                yield dialog

    repo = FakeGroupRepository()

    await sync_group_catalog(FakeClient(), repo)

    assert len(repo.groups) == 1
    chat_id, title, last_seen_at = repo.groups[0]
    assert (chat_id, title) == (-1001, "Team")
    assert isinstance(last_seen_at, int)


async def test_sync_group_catalog_preserves_dialog_recency_order_for_picker():
    dialogs = [
        type("Dialog", (), {"id": -1001, "title": "Recent", "is_group": True})(),
        type("Dialog", (), {"id": -1002, "title": "Older", "is_group": True})(),
    ]

    class FakeClient:
        async def iter_dialogs(self):
            for dialog in dialogs:
                yield dialog

    repo = FakeGroupRepository()

    await sync_group_catalog(FakeClient(), repo)

    assert repo.groups[0][2] > repo.groups[1][2]


def test_classify_dialog_kind_distinguishes_private_groups_and_channels():
    private = type("Dialog", (), {"is_user": True, "is_group": False, "is_channel": False})()
    group = type("Dialog", (), {"is_user": False, "is_group": True, "is_channel": True})()
    channel = type("Dialog", (), {"is_user": False, "is_group": False, "is_channel": True})()
    unknown = type("Dialog", (), {"is_user": False, "is_group": False, "is_channel": False})()

    assert classify_dialog_kind(private) == "private"
    assert classify_dialog_kind(group) == "group"
    assert classify_dialog_kind(channel) == "channel"
    assert classify_dialog_kind(unknown) == "chat"


async def test_remember_chat_from_event_stores_private_group_and_channel_chats():
    repo = FakeGroupRepository()
    private = FakeEvent()
    private.chat = type("User", (), {"first_name": "Alice"})()
    group = FakeEvent()
    group.chat_id = -200
    group.is_private = False
    group.is_group = True
    group.chat = type("Group", (), {"title": "Team"})()
    channel = FakeEvent()
    channel.chat_id = -100123
    channel.is_private = False
    channel.is_group = False
    channel.is_channel = True
    channel.chat = type("Channel", (), {"title": "News"})()

    await remember_chat_from_event(private, repo)
    await remember_chat_from_event(group, repo)
    await remember_chat_from_event(channel, repo)

    assert [chat[:3] for chat in repo.chats] == [
        (100, "Alice", "private"),
        (-200, "Team", "group"),
        (-100123, "News", "channel"),
    ]


async def test_remember_chat_from_event_uses_loaded_chat_without_api_call():
    class EventWithLoadedChat(FakeEvent):
        chat = type("User", (), {"first_name": "Alice"})()

        async def get_chat(self):
            raise AssertionError("loaded event.chat should be enough")

    repo = FakeGroupRepository()

    await remember_chat_from_event(EventWithLoadedChat(), repo)

    assert [chat[:3] for chat in repo.chats] == [(100, "Alice", "private")]


async def test_sync_chat_catalog_stores_all_dialog_kinds_in_recent_order():
    dialogs = [
        type("Dialog", (), {"id": -100123, "title": "News", "is_user": False, "is_group": False, "is_channel": True})(),
        type("Dialog", (), {"id": 100, "title": "Alice", "is_user": True, "is_group": False, "is_channel": False})(),
        type("Dialog", (), {"id": -200, "title": "Team", "is_user": False, "is_group": True, "is_channel": True})(),
    ]

    class FakeClient:
        async def iter_dialogs(self):
            for dialog in dialogs:
                yield dialog

    repo = FakeGroupRepository()

    await sync_chat_catalog(FakeClient(), repo)

    assert [chat[:3] for chat in repo.chats] == [
        (-100123, "News", "channel"),
        (100, "Alice", "private"),
        (-200, "Team", "group"),
    ]
    assert repo.chats[0][3] > repo.chats[1][3] > repo.chats[2][3]


async def test_record_account_premium_status_reads_telegram_user_flag():
    class FakeClient:
        async def get_me(self):
            return type("Me", (), {"premium": True})()

    repo = FakeGroupRepository()

    result = await record_account_premium_status(FakeClient(), repo)

    assert result is True
    assert repo.account_premium is True


async def test_record_account_premium_status_does_not_abort_when_check_fails(caplog):
    class FailingClient:
        async def get_me(self):
            raise RuntimeError("temporary telegram failure")

    repo = FakeGroupRepository()

    with caplog.at_level(logging.WARNING):
        result = await record_account_premium_status(FailingClient(), repo)

    assert result is False
    assert repo.account_premium is None
    assert "account_premium_check_failed" in caplog.text


async def test_refresh_account_premium_status_loop_updates_status_after_interval():
    class FakeClient:
        async def get_me(self):
            return type("Me", (), {"premium": True})()

    async def sleep(delay):
        sleep_calls.append(delay)
        if len(sleep_calls) > 1:
            raise asyncio.CancelledError

    sleep_calls = []
    repo = FakeGroupRepository()

    with pytest.raises(asyncio.CancelledError):
        await refresh_account_premium_status_loop(FakeClient(), repo, interval_seconds=30, sleep=sleep)

    assert sleep_calls == [30, 30]
    assert repo.account_premium is True


async def test_run_user_client_does_not_auto_sync_dialog_or_folder_catalogs(tmp_path, monkeypatch):
    settings = Settings(
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_session=str(tmp_path / "session"),
        manager_bot_token="token",
        owner_id=10,
        database_path=tmp_path / "assistant.sqlite3",
    )
    repo = SQLiteAssistantRepository(settings.database_path)
    calls = []

    class FakeClient:
        connected = True

        def on(self, event):
            def decorator(handler):
                return handler

            return decorator

        async def start(self):
            calls.append("start")

        async def get_me(self):
            calls.append("get_me")
            return type("Me", (), {"premium": True})()

        async def run_until_disconnected(self):
            calls.append("run_until_disconnected")
            await asyncio.sleep(0)
            raise RuntimeError("stop")

        def is_connected(self):
            return self.connected

        async def disconnect(self):
            self.connected = False
            calls.append("disconnect")

    async def forbidden_sync(*args, **kwargs):
        raise AssertionError("catalogs must refresh only on explicit actions")

    async def forbidden_folder_loop(*args, **kwargs):
        calls.append("folder_loop_started")
        await asyncio.Future()

    monkeypatch.setattr("telepath.user_client.sync_chat_catalog", forbidden_sync)
    monkeypatch.setattr("telepath.user_client.sync_group_catalog", forbidden_sync)
    monkeypatch.setattr("telepath.user_client.sync_reaction_folders", forbidden_sync)
    monkeypatch.setattr("telepath.user_client.refresh_reaction_folders_loop", forbidden_folder_loop)

    with pytest.raises(RuntimeError, match="stop"):
        await run_user_client(settings, client=FakeClient(), state=repo)

    assert "run_until_disconnected" in calls
    assert "folder_loop_started" not in calls


async def test_run_user_client_does_not_add_sender_delay_on_top_of_reaction_queue_delay(tmp_path, monkeypatch):
    settings = Settings(
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_session=str(tmp_path / "session"),
        manager_bot_token="token",
        owner_id=10,
        database_path=tmp_path / "assistant.sqlite3",
    )
    repo = SQLiteAssistantRepository(settings.database_path)
    sender_delays = []

    class FakeClient:
        connected = True

        def on(self, event):
            def decorator(handler):
                return handler

            return decorator

        async def start(self):
            return None

        async def get_me(self):
            return type("Me", (), {"premium": True})()

        async def run_until_disconnected(self):
            await asyncio.sleep(0)
            raise RuntimeError("stop")

        def is_connected(self):
            return self.connected

        async def disconnect(self):
            self.connected = False

    class FakeTelethonChannelReactionSender:
        def __init__(self, client, *, send_delay_seconds=None, **kwargs):
            sender_delays.append(send_delay_seconds)

        async def available_reactions(self, chat_id):
            return []

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            return 0

    async def forbidden_sync(*args, **kwargs):
        raise AssertionError("catalogs must refresh only on explicit actions")

    async def forbidden_folder_loop(*args, **kwargs):
        await asyncio.Future()

    monkeypatch.setattr("telepath.user_client.TelethonChannelReactionSender", FakeTelethonChannelReactionSender)
    monkeypatch.setattr("telepath.user_client.sync_chat_catalog", forbidden_sync)
    monkeypatch.setattr("telepath.user_client.sync_group_catalog", forbidden_sync)
    monkeypatch.setattr("telepath.user_client.sync_reaction_folders", forbidden_sync)
    monkeypatch.setattr("telepath.user_client.refresh_reaction_folders_loop", forbidden_folder_loop)

    with pytest.raises(RuntimeError, match="stop"):
        await run_user_client(settings, client=FakeClient(), state=repo)

    assert sender_delays == [0]


async def test_smart_set_reactions_sends_new_reaction_once_without_installed_reactions():
    sent = []

    class CapturingClient:
        async def __call__(self, request):
            sent.append([reaction.emoticon for reaction in request.reaction])

    async def noop_sleep(delay):
        return None

    message = type("Message", (), {"id": 55, "reactions": None})()

    sent_count = await smart_set_telethon_reactions(
        CapturingClient(),
        peer=object(),
        message=message,
        selected_reactions=[types.ReactionEmoji("👍")],
        max_reactions=3,
        sleep=noop_sleep,
        send_delay_seconds=0,
    )

    assert sent_count == 1
    assert sent == [["👍"]]


async def test_smart_set_reactions_preserves_installed_reaction_order_by_chosen_order():
    sent = []

    class CapturingClient:
        async def __call__(self, request):
            sent.append([reaction.emoticon for reaction in request.reaction])

    async def noop_sleep(delay):
        return None

    message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[
                    types.ReactionCount(types.ReactionEmoji("🔥"), count=1, chosen_order=2),
                    types.ReactionCount(types.ReactionEmoji("👍"), count=1, chosen_order=1),
                ]
            ),
        },
    )()

    await smart_set_telethon_reactions(
        CapturingClient(),
        peer=object(),
        message=message,
        selected_reactions=[types.ReactionEmoji("👎")],
        max_reactions=3,
        sleep=noop_sleep,
        send_delay_seconds=0,
    )

    assert sent[-1] == ["👍", "🔥", "👎"]


async def test_smart_set_reactions_fills_open_slots_from_fallback_after_installed_duplicates():
    sent = []

    class CapturingClient:
        async def __call__(self, request):
            sent.append([telethon_reaction_identifier(reaction) for reaction in request.reaction])

    async def noop_sleep(delay):
        return None

    installed = types.ReactionCustomEmoji(document_id=111)
    message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[types.ReactionCount(installed, count=1, chosen_order=1)]
            ),
        },
    )()

    await smart_set_telethon_reactions(
        CapturingClient(),
        peer=object(),
        message=message,
        selected_reactions=[
            types.ReactionCustomEmoji(document_id=222),
            types.ReactionCustomEmoji(document_id=111),
        ],
        fallback_reactions=[types.ReactionEmoji("👍")],
        max_reactions=3,
        sleep=noop_sleep,
        send_delay_seconds=0,
    )

    assert sent == [[111, 222, "👍"]]


async def test_smart_set_reactions_preserves_caller_priority_over_premium_type():
    sent = []

    class CapturingClient:
        async def __call__(self, request):
            sent.append([telethon_reaction_identifier(reaction) for reaction in request.reaction])

    async def noop_sleep(delay):
        return None

    installed = types.ReactionCustomEmoji(document_id=111)
    message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[types.ReactionCount(installed, count=1, chosen_order=1)]
            ),
        },
    )()

    await smart_set_telethon_reactions(
        CapturingClient(),
        peer=object(),
        message=message,
        selected_reactions=[
            types.ReactionEmoji("👍"),
            types.ReactionCustomEmoji(document_id=222),
        ],
        max_reactions=3,
        sleep=noop_sleep,
        send_delay_seconds=0,
    )

    assert sent == [[111, "👍", 222]]


async def test_smart_set_reactions_prefers_unique_standard_over_duplicate_premium_fill():
    sent = []

    class CapturingClient:
        async def __call__(self, request):
            sent.append([telethon_reaction_identifier(reaction) for reaction in request.reaction])

    async def noop_sleep(delay):
        return None

    installed = types.ReactionCustomEmoji(document_id=111)
    message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[types.ReactionCount(installed, count=1, chosen_order=1)]
            ),
        },
    )()

    await smart_set_telethon_reactions(
        CapturingClient(),
        peer=object(),
        message=message,
        selected_reactions=[
            types.ReactionCustomEmoji(document_id=111),
            types.ReactionCustomEmoji(document_id=222),
            types.ReactionCustomEmoji(document_id=111),
            types.ReactionEmoji("👍"),
        ],
        max_reactions=3,
        sleep=noop_sleep,
        send_delay_seconds=0,
    )

    assert sent == [[111, 222, "👍"]]


async def test_smart_set_reactions_uses_standard_when_duplicate_premium_would_leave_slot_open():
    sent = []

    class CapturingClient:
        async def __call__(self, request):
            sent.append([telethon_reaction_identifier(reaction) for reaction in request.reaction])

    async def noop_sleep(delay):
        return None

    installed = types.ReactionCustomEmoji(document_id=111)
    message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[types.ReactionCount(installed, count=1, chosen_order=1)]
            ),
        },
    )()

    await smart_set_telethon_reactions(
        CapturingClient(),
        peer=object(),
        message=message,
        selected_reactions=[
            types.ReactionCustomEmoji(document_id=222),
            types.ReactionCustomEmoji(document_id=111),
            types.ReactionEmoji("👍"),
        ],
        max_reactions=3,
        sleep=noop_sleep,
        send_delay_seconds=0,
    )

    assert sent == [[111, 222, "👍"]]


async def test_smart_set_reactions_keeps_full_unique_installed_reaction_set():
    sent = []

    class CapturingClient:
        async def __call__(self, request):
            sent.append([reaction.emoticon for reaction in request.reaction])

    async def noop_sleep(delay):
        return None

    message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[
                    types.ReactionCount(types.ReactionEmoji("👍"), count=1, chosen_order=1),
                    types.ReactionCount(types.ReactionEmoji("🔥"), count=1, chosen_order=2),
                    types.ReactionCount(types.ReactionEmoji("❤"), count=1, chosen_order=3),
                ]
            ),
        },
    )()

    await smart_set_telethon_reactions(
        CapturingClient(),
        peer=object(),
        message=message,
        selected_reactions=[types.ReactionEmoji("👎")],
        max_reactions=3,
        sleep=noop_sleep,
        send_delay_seconds=0,
    )

    assert sent == []


async def test_smart_set_reactions_uses_fallback_when_selected_reaction_is_invalid():
    sent = []

    class FlakyClient:
        async def __call__(self, request):
            attempt = [reaction.emoticon for reaction in request.reaction]
            sent.append(attempt)
            if "🔥" in attempt:
                raise RuntimeError("REACTION_INVALID")

    async def noop_sleep(delay):
        return None

    message = type("Message", (), {"id": 55, "reactions": None})()

    await smart_set_telethon_reactions(
        FlakyClient(),
        peer=object(),
        message=message,
        selected_reactions=[types.ReactionEmoji("🔥")],
        fallback_reactions=[types.ReactionEmoji("👍")],
        max_reactions=1,
        sleep=noop_sleep,
        send_delay_seconds=0,
    )

    assert sent == [["🔥"], ["👍"]]


async def test_smart_set_reactions_uses_visible_reactions_when_cached_candidates_are_invalid():
    sent = []
    invalid = {"🆒", "💯", "😎"}

    class FlakyClient:
        async def __call__(self, request):
            attempt = [telethon_reaction_identifier(reaction) for reaction in request.reaction]
            sent.append(attempt)
            if any(reaction in invalid for reaction in attempt):
                raise RuntimeError("REACTION_INVALID")

    async def noop_sleep(delay):
        return None

    message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[
                    types.ReactionCount(types.ReactionEmoji("👍"), count=5),
                    types.ReactionCount(types.ReactionEmoji("❤"), count=3),
                    types.ReactionCount(types.ReactionEmoji("🔥"), count=1),
                ]
            ),
        },
    )()

    sent_count = await smart_set_telethon_reactions(
        FlakyClient(),
        peer=object(),
        message=message,
        selected_reactions=[
            types.ReactionEmoji("🆒"),
            types.ReactionEmoji("💯"),
            types.ReactionEmoji("😎"),
        ],
        fallback_reactions=[],
        max_reactions=3,
        sleep=noop_sleep,
        send_delay_seconds=0,
    )

    assert sent_count == 3
    assert sent[-1] == ["👍", "❤", "🔥"]


async def test_smart_set_reactions_prefers_unseen_fallback_when_selected_reactions_are_visible():
    sent = []

    class CapturingClient:
        async def __call__(self, request):
            sent.append([reaction.emoticon for reaction in request.reaction])

    async def noop_sleep(delay):
        return None

    message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[
                    types.ReactionCount(types.ReactionEmoji("😍"), count=4),
                    types.ReactionCount(types.ReactionEmoji("❤"), count=3),
                    types.ReactionCount(types.ReactionEmoji("🔥"), count=3),
                ]
            ),
        },
    )()

    sent_count = await smart_set_telethon_reactions(
        CapturingClient(),
        peer=object(),
        message=message,
        selected_reactions=[
            types.ReactionEmoji("😍"),
            types.ReactionEmoji("❤"),
            types.ReactionEmoji("🔥"),
        ],
        fallback_reactions=[
            types.ReactionEmoji("🤔"),
            types.ReactionEmoji("😐"),
            types.ReactionEmoji("🤯"),
        ],
        max_reactions=3,
        sleep=noop_sleep,
        send_delay_seconds=0,
    )

    assert sent_count == 3
    assert sent == [["🤔", "😐", "🤯"]]


async def test_smart_set_reactions_uses_neutral_fallback_when_selected_positive_reactions_are_visible():
    sent = []

    class CapturingClient:
        async def __call__(self, request):
            sent.append([reaction.emoticon for reaction in request.reaction])

    async def noop_sleep(delay):
        return None

    message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[
                    types.ReactionCount(types.ReactionEmoji("⚡"), count=3),
                    types.ReactionCount(types.ReactionEmoji("👍"), count=2),
                    types.ReactionCount(types.ReactionEmoji("🎉"), count=2),
                ]
            ),
        },
    )()

    sent_count = await smart_set_telethon_reactions(
        CapturingClient(),
        peer=object(),
        message=message,
        selected_reactions=[
            types.ReactionEmoji("⚡"),
            types.ReactionEmoji("👍"),
            types.ReactionEmoji("🎉"),
        ],
        fallback_reactions=[
            types.ReactionEmoji("🤔"),
            types.ReactionEmoji("👀"),
            types.ReactionEmoji("🤓"),
        ],
        max_reactions=3,
        sleep=noop_sleep,
        send_delay_seconds=0,
    )

    assert sent_count == 3
    assert sent == [["🤔", "👀", "🤓"]]


async def test_smart_set_reactions_uses_premium_then_standard_unique_fallback_before_visible_repeats():
    sent = []

    class CapturingClient:
        async def __call__(self, request):
            sent.append([telethon_reaction_identifier(reaction) for reaction in request.reaction])

    async def noop_sleep(delay):
        return None

    message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[
                    types.ReactionCount(types.ReactionEmoji("⚡"), count=3),
                    types.ReactionCount(types.ReactionEmoji("👍"), count=2),
                    types.ReactionCount(types.ReactionEmoji("🎉"), count=2),
                ]
            ),
        },
    )()

    sent_count = await smart_set_telethon_reactions(
        CapturingClient(),
        peer=object(),
        message=message,
        selected_reactions=[
            types.ReactionEmoji("⚡"),
            types.ReactionEmoji("👍"),
            types.ReactionEmoji("🎉"),
        ],
        fallback_reactions=[
            types.ReactionCustomEmoji(document_id=111),
            types.ReactionEmoji("🤔"),
        ],
        max_reactions=3,
        sleep=noop_sleep,
        send_delay_seconds=0,
    )

    assert sent_count == 3
    assert sent == [[111, "🤔", "⚡"]]


async def test_smart_set_reactions_prefers_distinct_unseen_fallback_over_visible_heart_family():
    sent = []

    class CapturingClient:
        async def __call__(self, request):
            sent.append([telethon_reaction_identifier(reaction) for reaction in request.reaction])

    async def noop_sleep(delay):
        return None

    message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[
                    types.ReactionCount(types.ReactionEmoji("❤"), count=4),
                    types.ReactionCount(types.ReactionEmoji("🔥"), count=2),
                ]
            ),
        },
    )()

    sent_count = await smart_set_telethon_reactions(
        CapturingClient(),
        peer=object(),
        message=message,
        selected_reactions=[
            types.ReactionEmoji("❤"),
            types.ReactionEmoji("🔥"),
            types.ReactionEmoji("💘"),
        ],
        fallback_reactions=[
            types.ReactionEmoji("💋"),
            types.ReactionEmoji("⚡"),
            types.ReactionEmoji("😎"),
        ],
        max_reactions=3,
        sleep=noop_sleep,
        send_delay_seconds=0,
    )

    assert sent_count == 3
    assert sent == [["💋", "⚡", "😎"]]


@pytest.mark.parametrize(
    ("visible", "selected", "fallback", "expected"),
    [
        (
            ["👍", "👏"],
            ["👍", "👏", "👌"],
            ["💯", "⚡", "💋"],
            ["💯", "⚡", "💋"],
        ),
        (
            ["😁", "🤣"],
            ["😁", "🤣", "😇"],
            ["🎉", "💋", "⚡"],
            ["🎉", "💋", "⚡"],
        ),
        (
            ["🎉", "🏆"],
            ["🎉", "🏆", "🍾"],
            ["💋", "⚡", "😎"],
            ["💋", "⚡", "😎"],
        ),
        (
            ["🤯", "👀"],
            ["🤯", "👀", "😱"],
            ["💋", "⚡", "😎"],
            ["💋", "⚡", "😎"],
        ),
        (
            ["😭", "😢"],
            ["😭", "😢", "💔"],
            ["🤬", "👎", "🤮"],
            ["🤬", "👎", "🤮"],
        ),
    ],
)
async def test_smart_set_reactions_prefers_distinct_unseen_fallback_over_visible_semantic_family(
    visible,
    selected,
    fallback,
    expected,
):
    sent = []

    class CapturingClient:
        async def __call__(self, request):
            sent.append([telethon_reaction_identifier(reaction) for reaction in request.reaction])

    message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[
                    types.ReactionCount(types.ReactionEmoji(emoji), count=index + 1)
                    for index, emoji in enumerate(visible)
                ]
            ),
        },
    )()

    sent_count = await smart_set_telethon_reactions(
        CapturingClient(),
        peer=object(),
        message=message,
        selected_reactions=[types.ReactionEmoji(emoji) for emoji in selected],
        fallback_reactions=[types.ReactionEmoji(emoji) for emoji in fallback],
        max_reactions=3,
        sleep=lambda delay: None,
        send_delay_seconds=0,
    )

    assert sent_count == 3
    assert sent == [expected]


def test_all_standard_reactions_have_diversity_family():
    missing = [
        emoji
        for emoji in DEFAULT_REACTION_EMOJIS
        if not telethon_reaction_diversity_families(types.ReactionEmoji(emoji))
    ]

    assert missing == []


async def test_smart_set_reactions_diversifies_selected_reaction_families_even_without_visible_reactions():
    sent = []

    class CapturingClient:
        async def __call__(self, request):
            sent.append([telethon_reaction_identifier(reaction) for reaction in request.reaction])

    message = type("Message", (), {"id": 55, "reactions": None})()

    sent_count = await smart_set_telethon_reactions(
        CapturingClient(),
        peer=object(),
        message=message,
        selected_reactions=[
            types.ReactionEmoji("❤"),
            types.ReactionEmoji("💘"),
            types.ReactionEmoji("❤‍🔥"),
        ],
        fallback_reactions=[
            types.ReactionEmoji("💋"),
            types.ReactionEmoji("⚡"),
            types.ReactionEmoji("😎"),
        ],
        max_reactions=3,
        sleep=lambda delay: None,
        send_delay_seconds=0,
    )

    assert sent_count == 3
    assert sent == [["❤", "💋", "⚡"]]


async def test_smart_set_reactions_refreshes_message_after_send_delay_before_visibility_choice():
    calls = []
    sent = []

    stale_message = type("Message", (), {"id": 55, "reactions": None})()
    fresh_message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[
                    types.ReactionCount(types.ReactionEmoji("😍"), count=4),
                    types.ReactionCount(types.ReactionEmoji("❤"), count=3),
                    types.ReactionCount(types.ReactionEmoji("🔥"), count=3),
                ]
            ),
        },
    )()

    class RefreshingClient:
        async def get_messages(self, peer, *, ids):
            calls.append(("get_messages", ids))
            return fresh_message

        async def __call__(self, request):
            calls.append(("send", request.msg_id))
            sent.append([reaction.emoticon for reaction in request.reaction])

    async def delayed_sleep(delay):
        calls.append(("sleep", delay))

    sent_count = await smart_set_telethon_reactions(
        RefreshingClient(),
        peer=object(),
        message=stale_message,
        selected_reactions=[
            types.ReactionEmoji("😍"),
            types.ReactionEmoji("❤"),
            types.ReactionEmoji("🔥"),
        ],
        fallback_reactions=[
            types.ReactionEmoji("🤔"),
            types.ReactionEmoji("😐"),
            types.ReactionEmoji("🤯"),
        ],
        max_reactions=3,
        sleep=delayed_sleep,
        send_delay_seconds=240,
    )

    assert sent_count == 3
    assert calls == [("sleep", 240), ("get_messages", 55), ("send", 55)]
    assert sent == [["🤔", "😐", "🤯"]]


async def test_channel_reaction_sender_refreshes_message_without_send_delay_for_history_visibility():
    calls = []
    sent = []
    stale_message = type("Message", (), {"id": 55, "reactions": None})()
    fresh_message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[
                    types.ReactionCount(types.ReactionEmoji("👍"), count=8),
                    types.ReactionCount(types.ReactionEmoji("🔥"), count=5),
                    types.ReactionCount(types.ReactionEmoji("❤"), count=3),
                ]
            ),
        },
    )()

    class RefreshingClient:
        async def get_input_entity(self, chat_id):
            calls.append(("get_input_entity", chat_id))
            return "peer"

        async def get_messages(self, peer, *, ids):
            calls.append(("get_messages", peer, ids))
            return fresh_message

        async def __call__(self, request):
            calls.append(("send", request.msg_id))
            sent.append([reaction.emoticon for reaction in request.reaction])

    sender = TelethonChannelReactionSender(RefreshingClient(), send_delay_seconds=0)

    result = await sender.send_reactions(
        ChannelMessageEvent(
            chat_id=-100123,
            message_id=55,
            is_channel=True,
            is_group=False,
            message=stale_message,
        ),
        [
            ReactionCandidate("emoji", "👍", types.ReactionEmoji("👍"), category="positive"),
            ReactionCandidate("emoji", "🔥", types.ReactionEmoji("🔥"), category="positive"),
            ReactionCandidate("emoji", "❤", types.ReactionEmoji("❤"), category="positive"),
        ],
        max_reactions=3,
        fallback_reactions=[
            ReactionCandidate("emoji", "🤔", types.ReactionEmoji("🤔"), category="neutral"),
            ReactionCandidate("emoji", "😐", types.ReactionEmoji("😐"), category="neutral"),
            ReactionCandidate("emoji", "👀", types.ReactionEmoji("👀"), category="neutral"),
        ],
    )

    assert result.count == 3
    assert calls == [
        ("get_input_entity", -100123),
        ("get_messages", "peer", 55),
        ("send", 55),
    ]
    assert sent == [["🤔", "😐", "👀"]]


async def test_channel_reaction_sender_refreshes_available_reactions_when_cached_candidates_underfill():
    calls = []
    sent = []
    invalid = {"🆒", "💯", "😎"}
    message = type("Message", (), {"id": 55, "reactions": None})()

    class RefreshingAvailableClient:
        async def get_input_entity(self, chat_id):
            calls.append(("get_input_entity", chat_id))
            return "peer"

        async def get_messages(self, peer, *, ids):
            calls.append(("get_messages", peer, ids))
            return message

        async def __call__(self, request):
            if hasattr(request, "reaction"):
                attempt = [telethon_reaction_identifier(reaction) for reaction in request.reaction]
                sent.append(attempt)
                if any(reaction in invalid for reaction in attempt):
                    raise RuntimeError("REACTION_INVALID")
                return None

            calls.append(("get_full_channel", request.__class__.__name__))
            full_chat = type(
                "FullChat",
                (),
                {
                    "available_reactions": types.ChatReactionsSome(
                        [
                            types.ReactionEmoji("⭐"),
                            types.ReactionEmoji("🔥"),
                            types.ReactionEmoji("👍"),
                        ]
                    )
                },
            )()
            return type("FullChannel", (), {"full_chat": full_chat})()

    sender = TelethonChannelReactionSender(RefreshingAvailableClient(), send_delay_seconds=0)

    result = await sender.send_reactions(
        ChannelMessageEvent(
            chat_id=-100123,
            message_id=55,
            is_channel=True,
            is_group=False,
            message=message,
        ),
        [
            ReactionCandidate("emoji", "🆒", types.ReactionEmoji("🆒"), category="positive"),
            ReactionCandidate("emoji", "💯", types.ReactionEmoji("💯"), category="positive"),
            ReactionCandidate("emoji", "😎", types.ReactionEmoji("😎"), category="positive"),
        ],
        max_reactions=3,
    )

    assert result.count == 3
    assert result.reaction_keys == (("emoji", "⭐"), ("emoji", "🔥"), ("emoji", "👍"))
    assert ("get_full_channel", "GetFullChannelRequest") in calls
    assert sent[-1] == ["⭐", "🔥", "👍"]


async def test_channel_reaction_sender_returns_actual_reaction_keys_after_visibility_fallback():
    stale_message = type("Message", (), {"id": 55, "reactions": None})()
    fresh_message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[
                    types.ReactionCount(types.ReactionEmoji("⚡"), count=3),
                    types.ReactionCount(types.ReactionEmoji("👍"), count=2),
                    types.ReactionCount(types.ReactionEmoji("🎉"), count=2),
                ]
            ),
        },
    )()

    class RefreshingClient:
        async def get_input_entity(self, chat_id):
            return "peer"

        async def get_messages(self, peer, *, ids):
            return fresh_message

        async def __call__(self, request):
            return None

    sender = TelethonChannelReactionSender(RefreshingClient(), send_delay_seconds=0)

    result = await sender.send_reactions(
        ChannelMessageEvent(
            chat_id=-100123,
            message_id=55,
            is_channel=True,
            is_group=False,
            message=stale_message,
        ),
        [
            ReactionCandidate("emoji", "⚡", types.ReactionEmoji("⚡"), category="positive"),
            ReactionCandidate("emoji", "👍", types.ReactionEmoji("👍"), category="positive"),
            ReactionCandidate("emoji", "🎉", types.ReactionEmoji("🎉"), category="positive"),
        ],
        max_reactions=3,
        fallback_reactions=[
            ReactionCandidate("custom", "111", types.ReactionCustomEmoji(document_id=111), category="neutral"),
            ReactionCandidate("emoji", "🤔", types.ReactionEmoji("🤔"), category="neutral"),
        ],
    )

    assert result.count == 3
    assert result.reaction_keys == (("custom", "111"), ("emoji", "🤔"), ("emoji", "⚡"))


async def test_smart_set_reactions_uses_visible_fallback_when_unique_reaction_limit_is_reached():
    attempts = []
    visible = {"🔥", "❤", "👍"}

    class UniqueLimitClient:
        async def __call__(self, request):
            attempt = [reaction.emoticon for reaction in request.reaction]
            attempts.append(attempt)
            if any(emoji not in visible for emoji in attempt):
                raise RuntimeError("REACTIONS_UNIQ_MAX")

    async def noop_sleep(delay):
        return None

    message = type(
        "Message",
        (),
        {
            "id": 55,
            "reactions": types.MessageReactions(
                results=[
                    types.ReactionCount(types.ReactionEmoji("🔥"), count=17),
                    types.ReactionCount(types.ReactionEmoji("❤"), count=11),
                    types.ReactionCount(types.ReactionEmoji("👍"), count=8),
                ]
            ),
        },
    )()

    sent_count = await smart_set_telethon_reactions(
        UniqueLimitClient(),
        peer=object(),
        message=message,
        selected_reactions=[
            types.ReactionEmoji("🤔"),
            types.ReactionEmoji("😐"),
            types.ReactionEmoji("🤯"),
        ],
        fallback_reactions=[
            types.ReactionEmoji("🔥"),
            types.ReactionEmoji("❤"),
            types.ReactionEmoji("👍"),
        ],
        max_reactions=3,
        sleep=noop_sleep,
        send_delay_seconds=0,
    )

    assert sent_count == 3
    assert attempts[0] == ["🤔", "😐", "🤯"]
    assert attempts[-1] == ["🔥", "❤", "👍"]


async def test_smart_set_reactions_never_sends_paid_reactions():
    sent = []

    class CapturingClient:
        async def __call__(self, request):
            sent.append([type(reaction).__name__ for reaction in request.reaction])

    async def noop_sleep(delay):
        return None

    message = type("Message", (), {"id": 55, "reactions": None})()

    await smart_set_telethon_reactions(
        CapturingClient(),
        peer=object(),
        message=message,
        selected_reactions=[types.ReactionPaid(), types.ReactionEmoji("👍")],
        fallback_reactions=[types.ReactionPaid(), types.ReactionEmoji("🔥")],
        max_reactions=3,
        sleep=noop_sleep,
        send_delay_seconds=0,
    )

    assert sent == [["ReactionEmoji", "ReactionEmoji"]]


async def test_channel_reaction_sender_excludes_paid_available_reactions():
    class FakeClient:
        async def get_input_entity(self, chat_id):
            return object()

        async def __call__(self, request):
            full_chat = type(
                "FullChat",
                (),
                {
                    "available_reactions": types.ChatReactionsSome(
                        [
                            types.ReactionPaid(),
                            types.ReactionEmoji("👍"),
                            types.ReactionCustomEmoji(1234567890123456789),
                        ]
                    )
                },
            )()
            return type("FullChannel", (), {"full_chat": full_chat})()

    sender = TelethonChannelReactionSender(FakeClient(), send_delay_seconds=0)

    reactions = await sender.available_reactions(-100123)

    assert [(reaction.kind, reaction.emoji, reaction.category) for reaction in reactions] == [
        ("emoji", "👍", "positive"),
        ("custom", "1234567890123456789", "neutral"),
    ]


async def test_cached_channel_reaction_sender_uses_cached_available_reactions_without_telegram_fetch(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.replace_reaction_channel_available_reactions(
        -100123,
        [
            ReactionCandidate("emoji", "👍", "ignored", category="positive"),
            ReactionCandidate("custom", "1234567890123456789", "ignored", category="neutral"),
        ],
    )

    class ExplodingSender:
        async def available_reactions(self, chat_id):
            raise AssertionError("available reactions should be read from cache")

        async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
            return len(reactions)

    sender = CachedTelethonChannelReactionSender(ExplodingSender(), repo)

    available = await sender.available_reactions(-100123)

    assert [(reaction.kind, reaction.emoji, telethon_reaction_identifier(reaction.value)) for reaction in available] == [
        ("emoji", "👍", "👍"),
        ("custom", "1234567890123456789", 1234567890123456789),
    ]


# --- coverage: small helper branches ---------------------------------------


def test_transcription_quote_entities_returns_empty_for_empty_text():
    assert transcription_quote_entities("", object()) == []


def test_get_message_duration_seconds_falls_back_to_direct_duration():
    msg = type("Msg", (), {"file": None, "duration": 3.5, "media": None})()
    assert get_message_duration_seconds(msg) == 3


def test_get_message_duration_seconds_returns_none_when_no_source():
    msg = type("Msg", (), {"file": None, "duration": None, "media": None})()
    assert get_message_duration_seconds(msg) is None


def test_voice_message_fingerprint_returns_none_for_non_transcribable():
    msg = type("Msg", (), {"voice": False, "video_note": False, "media": None, "document": None})()
    assert voice_message_fingerprint(msg) is None


async def test_is_private_bot_dialog_returns_false_for_non_private_event():
    event = type("E", (), {"is_private": False, "chat": None, "sender": None})()
    assert await is_private_bot_dialog(event) is False


async def test_is_private_bot_dialog_uses_chat_attribute_when_set_to_bot():
    bot_chat = type("Chat", (), {"bot": True})()
    event = type("E", (), {"is_private": True, "chat": bot_chat, "sender": None})()

    assert await is_private_bot_dialog(event) is True


async def test_is_private_bot_dialog_skips_failed_getters_and_finds_bot_via_sender_getter():
    class FailingChatGetter:
        is_private = True
        chat = None
        sender = None

        async def get_chat(self):
            raise RuntimeError("API error")

        async def get_sender(self):
            return type("User", (), {"bot": True})()

    assert await is_private_bot_dialog(FailingChatGetter()) is True


async def test_remember_group_from_event_skips_non_group_events():
    event = type("E", (), {"is_group": False})()
    repo = FakeGroupRepository()

    await remember_group_from_event(event, repo)

    assert repo.groups == []


async def test_remember_group_from_event_falls_back_when_get_chat_raises():
    class BrokenEvent:
        chat_id = -100
        is_group = True

        async def get_chat(self):
            raise RuntimeError("network down")

    repo = FakeGroupRepository()

    await remember_group_from_event(BrokenEvent(), repo)

    assert len(repo.groups) == 1
    chat_id, title, _ = repo.groups[0]
    assert (chat_id, title) == (-100, None)


async def test_private_chat_history_gate_with_zero_throttle_skips_wait():
    """history_throttle_seconds <= 0 → early return in _wait_for_history_fetch_slot."""
    monotonic_calls = []

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit):
            for i in range(limit):
                yield type("Message", (), {"id": i, "action": None})()

    repo = FakePrivateGateRepository()
    gate = PrivateChatHistoryGate(
        FakeClient(),
        repo,
        history_throttle_seconds=0,
        monotonic=lambda: monotonic_calls.append(1) or 100.0,
        sleep=None,  # would crash if reached
    )

    assert await gate.has_enough_messages(100, 100)


async def test_private_chat_history_gate_skips_service_messages():
    """Messages with .action set (joined, pinned, etc.) must not count toward the threshold."""

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit):
            # 5 plain messages then 95 service messages — should NOT meet a threshold of 6.
            for i in range(5):
                yield type("Message", (), {"id": i, "action": None})()
            for i in range(95):
                yield type("Message", (), {"id": 100 + i, "action": object()})()

    repo = FakePrivateGateRepository()
    gate = PrivateChatHistoryGate(FakeClient(), repo)

    assert not await gate.has_enough_messages(100, 6)
    assert repo.saved == [(100, 5, False)]
