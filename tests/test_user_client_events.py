import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
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
    POST_MIRROR_HISTORY_POST_DELAY_RANGE_SECONDS,
    POST_MIRROR_HISTORY_TOPIC_CREATE_DELAY_RANGE_SECONDS,
    POST_MIRROR_OUTBOX_ONLINE_DELIVERY_WINDOW_SECONDS,
    POST_MIRROR_OUTBOX_PREFERRED_DELIVERY_WINDOW_SECONDS,
    OnlineGatedForumTopicManager,
    PostMirrorOperationGate,
    PostMirrorHistoryBackfill,
    PostMirrorOutboxDeliveryWorker,
    PostMirrorOutboxEnqueuer,
    PostMirrorQueueWorker,
    TelegramAuthorizationOnlineGate,
    TelethonChannelReactionSender,
    TelethonForumTopicManager,
    TelethonPostMirrorSender,
    PrivateChatHistoryGate,
    build_channel_message_event,
    build_post_mirror_event,
    build_voice_message_event,
    classify_dialog_kind,
    dispatch_post_mirror,
    dispatch_voice_message,
    get_message_duration_seconds,
    is_private_bot_dialog,
    is_reactable_channel_message,
    is_mirrorable_source_message,
    is_transcribable_message,
    remember_chat_from_event,
    remember_group_from_event,
    record_account_premium_status,
    refresh_account_premium_status_loop,
    run_user_client,
    should_enqueue_channel_reaction,
    should_enqueue_post_mirror,
    smart_set_telethon_reactions,
    sync_reaction_folders,
    sync_post_mirror_topic_title,
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


def test_post_mirror_history_defaults_use_conservative_flood_pacing():
    assert POST_MIRROR_HISTORY_POST_DELAY_RANGE_SECONDS == (60, 120)
    assert POST_MIRROR_HISTORY_TOPIC_CREATE_DELAY_RANGE_SECONDS == (180, 360)


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


class FakeHistoryMessage:
    def __init__(self, message_id, *, grouped_id=None, action=None):
        self.id = message_id
        self.grouped_id = grouped_id
        self.action = action
        self.message = f"message {message_id}"
        self.media = None


class FakePostMirrorSender:
    def __init__(self):
        self.calls = []

    async def copy_post(self, event, *, target_chat_id, target_thread_id):
        from telepath.features.post_mirroring import PostMirrorSendResult

        self.calls.append((event.message_ids, target_chat_id, target_thread_id))
        return PostMirrorSendResult(message_count=len(event.message_ids))


class FakeLogger:
    def __init__(self):
        self.info_calls = []
        self.warning_calls = []
        self.exception_calls = []

    def info(self, message, *args):
        self.info_calls.append((message, args))

    def warning(self, message, *args):
        self.warning_calls.append((message, args))

    def exception(self, message, *args):
        self.exception_calls.append((message, args))


def post_mirror_outbox_status(repo: SQLiteAssistantRepository, job_id: int) -> str:
    conn = sqlite3.connect(repo.path)
    try:
        row = conn.execute("SELECT status FROM post_mirror_outbox WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


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


def test_build_post_mirror_event_carries_album_messages():
    fake_event = FakeEvent()
    fake_event.chat_id = -100123
    fake_event.is_private = False
    fake_event.is_group = False
    fake_event.is_channel = True
    first = FakeMessage()
    first.id = 55
    second = FakeMessage()
    second.id = 56
    for message in (first, second):
        message.grouped_id = 999
    fake_event.messages = [first, second]
    fake_event.message = first

    event = build_post_mirror_event(fake_event)

    assert event.chat_id == -100123
    assert event.message_id == 55
    assert event.message_ids == (55, 56)
    assert event.grouped_id == 999
    assert event.messages == (first, second)


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


def test_is_mirrorable_source_message_accepts_channels_and_groups_but_rejects_private_and_services():
    channel = FakeEvent()
    channel.is_channel = True
    channel.is_group = False
    channel.is_private = False
    channel.message = FakeMessage()
    group = FakeEvent()
    group.is_channel = False
    group.is_group = True
    group.is_private = False
    group.message = FakeMessage()
    private = FakeEvent()
    private.is_channel = False
    private.is_group = False
    private.is_private = True
    private.message = FakeMessage()
    service = FakeEvent()
    service.is_channel = True
    service.is_group = False
    service.is_private = False
    service.message = type("Message", (), {"action": object()})()
    grouped_new_message = FakeEvent()
    grouped_new_message.is_channel = True
    grouped_new_message.is_group = False
    grouped_new_message.is_private = False
    grouped_new_message.message = type("Message", (), {"action": None, "grouped_id": 999})()

    assert is_mirrorable_source_message(channel)
    assert is_mirrorable_source_message(group)
    assert not is_mirrorable_source_message(private)
    assert not is_mirrorable_source_message(service)
    assert not is_mirrorable_source_message(grouped_new_message)


def test_should_enqueue_post_mirror_requires_enabled_source_and_target_but_not_topic():
    class FakeMirrorSettings:
        def __init__(self, *, global_enabled=True, target_chat_id=-100900, source_settings=None):
            self.global_enabled = global_enabled
            self.target_chat_id = target_chat_id
            self.source_settings = source_settings

        def is_post_mirroring_enabled(self):
            return self.global_enabled

        def get_post_mirror_target_chat_id(self):
            return self.target_chat_id

        def get_post_mirror_source_settings(self, chat_id):
            return self.source_settings

    from telepath.features.post_mirroring import PostMirrorSourceSettings

    channel = FakeEvent()
    channel.chat_id = -100123
    channel.is_channel = True
    channel.is_group = False
    channel.is_private = False
    channel.message = FakeMessage()

    assert should_enqueue_post_mirror(
        channel,
        FakeMirrorSettings(source_settings=PostMirrorSourceSettings(enabled=True, target_thread_id=42)),
    )
    assert should_enqueue_post_mirror(
        channel,
        FakeMirrorSettings(source_settings=PostMirrorSourceSettings(enabled=True, target_thread_id=None)),
    )
    assert not should_enqueue_post_mirror(
        channel,
        FakeMirrorSettings(global_enabled=False, source_settings=PostMirrorSourceSettings(enabled=True, target_thread_id=42)),
    )
    assert not should_enqueue_post_mirror(
        channel,
        FakeMirrorSettings(target_chat_id=None, source_settings=PostMirrorSourceSettings(enabled=True, target_thread_id=42)),
    )
    assert not should_enqueue_post_mirror(
        channel,
        FakeMirrorSettings(source_settings=PostMirrorSourceSettings(enabled=False, target_thread_id=42)),
    )


def test_should_enqueue_post_mirror_rejects_target_group_to_prevent_loops():
    class FakeMirrorSettings:
        def is_post_mirroring_enabled(self):
            return True

        def get_post_mirror_target_chat_id(self):
            return -100123

        def get_post_mirror_source_settings(self, chat_id):
            return PostMirrorSourceSettings(enabled=True, target_thread_id=42)

    from telepath.features.post_mirroring import PostMirrorSourceSettings

    event = FakeEvent()
    event.chat_id = -100123
    event.is_channel = False
    event.is_group = True
    event.is_private = False
    event.message = FakeMessage()

    assert not should_enqueue_post_mirror(event, FakeMirrorSettings())


async def test_forum_topic_manager_creates_topic_and_returns_root_message_id():
    from telethon.tl.functions.messages import CreateForumTopicRequest
    from telethon.tl.types import MessageActionTopicCreate

    class FakeClient:
        def __init__(self):
            self.requests = []

        async def get_input_entity(self, chat_id):
            assert chat_id == -100900
            return "target-peer"

        async def __call__(self, request):
            self.requests.append(request)
            return SimpleNamespace(
                updates=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            id=77,
                            action=MessageActionTopicCreate(title="Source Channel", icon_color=0x6FB9F0),
                        )
                    )
                ]
            )

    client = FakeClient()

    topic_id = await TelethonForumTopicManager(client).create_topic(-100900, "Source Channel")

    assert topic_id == 77
    topic_requests = [request for request in client.requests if isinstance(request, CreateForumTopicRequest)]
    assert len(topic_requests) == 1
    assert topic_requests[0].peer == "target-peer"
    assert topic_requests[0].title == "Source Channel"
    offline_requests = [
        request for request in client.requests if request.__class__.__name__ == "UpdateStatusRequest"
    ]
    assert len(offline_requests) == 2
    assert all(request.offline is True for request in offline_requests)


async def test_forum_topic_manager_renames_topic_to_source_title():
    from telethon.tl.functions.messages import EditForumTopicRequest

    class FakeClient:
        def __init__(self):
            self.requests = []

        async def get_input_entity(self, chat_id):
            assert chat_id == -100900
            return "target-peer"

        async def __call__(self, request):
            self.requests.append(request)
            return object()

    client = FakeClient()

    await TelethonForumTopicManager(client).rename_topic(-100900, 77, "New Channel")

    edit_requests = [request for request in client.requests if isinstance(request, EditForumTopicRequest)]
    assert len(edit_requests) == 1
    assert edit_requests[0].peer == "target-peer"
    assert edit_requests[0].topic_id == 77
    assert edit_requests[0].title == "New Channel"
    offline_requests = [
        request for request in client.requests if request.__class__.__name__ == "UpdateStatusRequest"
    ]
    assert len(offline_requests) == 2
    assert all(request.offline is True for request in offline_requests)


async def test_post_mirror_sender_marks_current_session_offline_after_text_send():
    class TextMessage:
        id = 1
        message = "hello"
        media = None
        entities = None

    class FakeClient:
        def __init__(self):
            self.sent_messages = []
            self.requests = []

        async def send_message(self, *args, **kwargs):
            self.sent_messages.append((args, kwargs))

        async def __call__(self, request):
            self.requests.append(request)

    event = build_post_mirror_event(
        type(
            "Event",
            (),
            {
                "chat_id": -100111,
                "is_channel": True,
                "is_group": False,
                "messages": [TextMessage()],
                "message": TextMessage(),
            },
        )()
    )
    client = FakeClient()

    await TelethonPostMirrorSender(client).copy_post(event, target_chat_id=-100900, target_thread_id=77)

    assert len(client.sent_messages) == 1
    assert [request.__class__.__name__ for request in client.requests] == ["UpdateStatusRequest"]
    assert client.requests[0].offline is True


async def test_sync_post_mirror_topic_title_renames_only_when_source_title_changed(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100123, "Old Channel", "channel")
    repo.set_post_mirror_source_topic(-100123, 77)
    calls = []

    class FakeTopicManager:
        async def rename_topic(self, target_chat_id, topic_id, title):
            calls.append((target_chat_id, topic_id, title))

    changed = await sync_post_mirror_topic_title(
        state=repo,
        topic_manager=FakeTopicManager(),
        source_chat_id=-100123,
        title="New Channel",
        kind="channel",
    )
    unchanged = await sync_post_mirror_topic_title(
        state=repo,
        topic_manager=FakeTopicManager(),
        source_chat_id=-100123,
        title="New Channel",
        kind="channel",
    )

    assert changed == "renamed"
    assert unchanged == "skipped_current"
    assert calls == [(-100900, 77, "New Channel")]
    assert repo.get_post_mirror_source_settings(-100123).title == "New Channel"


async def test_post_mirror_sender_aborts_album_when_any_media_download_fails(tmp_path):
    class MediaMessage:
        def __init__(self, message_id, text):
            self.id = message_id
            self.message = text
            self.media = object()
            self.file = object()
            self.entities = None

    class FakeClient:
        def __init__(self):
            self.sent_files = []
            self.sent_messages = []

        async def download_media(self, message, file):
            if message.id == 2:
                return None
            path = tmp_path / f"{message.id}.jpg"
            path.write_bytes(b"media")
            return str(path)

        async def send_file(self, *args, **kwargs):
            self.sent_files.append((args, kwargs))

        async def send_message(self, *args, **kwargs):
            self.sent_messages.append((args, kwargs))

    event = build_post_mirror_event(
        type(
            "Event",
            (),
            {
                "chat_id": -100111,
                "is_channel": True,
                "is_group": False,
                "messages": [MediaMessage(1, "first"), MediaMessage(2, "second")],
                "message": MediaMessage(1, "first"),
            },
        )()
    )
    client = FakeClient()

    with pytest.raises(RuntimeError, match="download"):
        await TelethonPostMirrorSender(client).copy_post(event, target_chat_id=-100900, target_thread_id=77)

    assert client.sent_files == []
    assert client.sent_messages == []


async def test_post_mirror_sender_copies_link_preview_posts_as_text_without_media_download(tmp_path):
    entity = types.MessageEntityUrl(offset=0, length=len("https://example.com"))

    class MessageMediaWebPage:
        webpage = object()

    class LinkPreviewMessage:
        id = 1
        message = "https://example.com"
        media = MessageMediaWebPage()
        file = None
        photo = None
        document = None
        entities = [entity]

    class FakeClient:
        def __init__(self):
            self.download_calls = 0
            self.sent_messages = []

        async def download_media(self, message, file):
            self.download_calls += 1
            return None

        async def send_message(self, *args, **kwargs):
            self.sent_messages.append((args, kwargs))

    event = build_post_mirror_event(
        type(
            "Event",
            (),
            {
                "chat_id": -100111,
                "is_channel": True,
                "is_group": False,
                "messages": [LinkPreviewMessage()],
                "message": LinkPreviewMessage(),
            },
        )()
    )
    client = FakeClient()

    result = await TelethonPostMirrorSender(client).copy_post(event, target_chat_id=-100900, target_thread_id=77)

    assert result.message_count == 1
    assert client.download_calls == 0
    assert client.sent_messages[0][0] == (-100900, "https://example.com")
    assert client.sent_messages[0][1]["link_preview"] is True
    assert client.sent_messages[0][1]["formatting_entities"] == [entity]


async def test_post_mirror_sender_copies_non_downloadable_media_message_to_topic(tmp_path):
    class MessageMediaPoll:
        pass

    class PollMessage:
        id = 1
        message = "Choose one"
        media = MessageMediaPoll()
        file = None
        photo = None
        document = None
        entities = ("poll-entity",)

    class FakeClient:
        def __init__(self):
            self.download_calls = 0
            self.sent_messages = []
            self.sent_files = []

        async def download_media(self, message, file):
            self.download_calls += 1
            return None

        async def send_message(self, *args, **kwargs):
            self.sent_messages.append((args, kwargs))

        async def send_file(self, *args, **kwargs):
            self.sent_files.append((args, kwargs))

    message = PollMessage()
    event = build_post_mirror_event(
        type(
            "Event",
            (),
            {
                "chat_id": -100111,
                "is_channel": True,
                "is_group": False,
                "messages": [message],
                "message": message,
            },
        )()
    )
    client = FakeClient()

    result = await TelethonPostMirrorSender(client).copy_post(event, target_chat_id=-100900, target_thread_id=77)

    assert result.message_count == 1
    assert result.media_count == 0
    assert client.download_calls == 0
    assert client.sent_files == []
    assert client.sent_messages == [((-100900, message), {"reply_to": 77})]


async def test_post_mirror_sender_downloads_nested_document_media(tmp_path):
    document = SimpleNamespace(mime_type="application/pdf", attributes=("file-name",))

    class DocumentMessage:
        def __init__(self):
            self.id = 1
            self.message = "report"
            self.media = SimpleNamespace(document=document)
            self.file = None
            self.photo = None
            self.document = None
            self.entities = None

    class FakeClient:
        def __init__(self):
            self.download_calls = []
            self.sent_files = []
            self.sent_messages = []

        async def download_media(self, message, file):
            self.download_calls.append(message.id)
            path = tmp_path / "report.pdf"
            path.write_bytes(b"pdf")
            return str(path)

        async def send_file(self, *args, **kwargs):
            self.sent_files.append((args, kwargs))

        async def send_message(self, *args, **kwargs):
            self.sent_messages.append((args, kwargs))

    event = build_post_mirror_event(
        type(
            "Event",
            (),
            {
                "chat_id": -100111,
                "is_channel": True,
                "is_group": False,
                "messages": [DocumentMessage()],
                "message": DocumentMessage(),
            },
        )()
    )
    client = FakeClient()

    result = await TelethonPostMirrorSender(client).copy_post(event, target_chat_id=-100900, target_thread_id=77)

    assert result.media_count == 1
    assert client.download_calls == [1]
    assert client.sent_files[0][0][0] == -100900
    assert client.sent_files[0][1]["caption"] == "report"
    assert client.sent_files[0][1]["mime_type"] == "application/pdf"
    assert client.sent_files[0][1]["attributes"] == ("file-name",)


async def test_post_mirror_sender_preserves_voice_note_flag(tmp_path):
    class VoiceMessage:
        id = 1
        message = ""
        media = SimpleNamespace(document=SimpleNamespace(mime_type="audio/ogg", attributes=()))
        file = object()
        photo = None
        document = None
        voice = True
        video_note = False
        entities = None

    class FakeClient:
        def __init__(self):
            self.sent_files = []

        async def download_media(self, message, file):
            path = tmp_path / "voice.ogg"
            path.write_bytes(b"ogg")
            return str(path)

        async def send_file(self, *args, **kwargs):
            self.sent_files.append((args, kwargs))

    event = build_post_mirror_event(
        type(
            "Event",
            (),
            {
                "chat_id": -100111,
                "is_channel": True,
                "is_group": False,
                "messages": [VoiceMessage()],
                "message": VoiceMessage(),
            },
        )()
    )
    client = FakeClient()

    await TelethonPostMirrorSender(client).copy_post(event, target_chat_id=-100900, target_thread_id=77)

    assert client.sent_files[0][1]["voice_note"] is True


async def test_post_mirror_sender_preserves_video_flags(tmp_path):
    class VideoMessage:
        id = 1
        message = ""
        media = SimpleNamespace(document=SimpleNamespace(mime_type="video/mp4", attributes=()))
        file = object()
        photo = None
        document = None
        voice = False
        video = True
        video_note = False
        entities = None

    class VideoNoteMessage:
        id = 2
        message = ""
        media = SimpleNamespace(document=SimpleNamespace(mime_type="video/mp4", attributes=()))
        file = object()
        photo = None
        document = None
        voice = False
        video = False
        video_note = True
        entities = None

    class FakeClient:
        def __init__(self):
            self.sent_files = []

        async def download_media(self, message, file):
            path = tmp_path / f"{message.id}.mp4"
            path.write_bytes(b"mp4")
            return str(path)

        async def send_file(self, *args, **kwargs):
            self.sent_files.append((args, kwargs))

    client = FakeClient()
    for message_cls in (VideoMessage, VideoNoteMessage):
        event = build_post_mirror_event(
            type(
                "Event",
                (),
                {
                    "chat_id": -100111,
                    "is_channel": True,
                    "is_group": False,
                    "messages": [message_cls()],
                    "message": message_cls(),
                },
            )()
        )
        await TelethonPostMirrorSender(client).copy_post(event, target_chat_id=-100900, target_thread_id=77)

    assert client.sent_files[0][1]["supports_streaming"] is True
    assert "video_note" not in client.sent_files[0][1]
    assert client.sent_files[1][1]["video_note"] is True
    assert "supports_streaming" not in client.sent_files[1][1]


async def test_post_mirror_sender_repairs_missing_video_dimensions_from_file_probe(tmp_path):
    class VideoMessage:
        id = 1
        message = ""
        media = SimpleNamespace(document=SimpleNamespace(mime_type="video/mp4", attributes=()))
        file = object()
        photo = None
        document = None
        voice = False
        video = True
        video_note = False
        entities = None

    class FakeClient:
        def __init__(self):
            self.sent_files = []

        async def download_media(self, message, file):
            path = tmp_path / "video.mp4"
            path.write_bytes(b"mp4")
            return str(path)

        async def send_file(self, *args, **kwargs):
            self.sent_files.append((args, kwargs))

    async def probe_video_metadata(path):
        return (720, 1280, 3)

    event = build_post_mirror_event(
        type(
            "Event",
            (),
            {
                "chat_id": -100111,
                "is_channel": True,
                "is_group": False,
                "messages": [VideoMessage()],
                "message": VideoMessage(),
            },
        )()
    )
    client = FakeClient()

    await TelethonPostMirrorSender(client, video_metadata_probe=probe_video_metadata).copy_post(
        event,
        target_chat_id=-100900,
        target_thread_id=77,
    )

    video_attr = next(
        attr for attr in client.sent_files[0][1]["attributes"] if isinstance(attr, types.DocumentAttributeVideo)
    )
    assert video_attr.w == 720
    assert video_attr.h == 1280
    assert video_attr.duration == 3
    assert video_attr.supports_streaming is True


async def test_post_mirror_video_metadata_probe_applies_rotation(monkeypatch, tmp_path):
    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return (
                b'{"streams":[{"width":1920,"height":1080,"duration":"3.2",'
                b'"tags":{"rotate":"90"},"side_data_list":[{"rotation":90}]}]}',
                b"",
            )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr("telepath.user_client.shutil.which", lambda name: "/usr/bin/ffprobe")
    monkeypatch.setattr("telepath.user_client.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    metadata = await TelethonPostMirrorSender(SimpleNamespace())._probe_video_metadata(tmp_path / "video.mp4")

    assert metadata is not None
    assert metadata.width == 1080
    assert metadata.height == 1920
    assert metadata.duration == 3


async def test_post_mirror_sender_normalizes_album_caption_entities(tmp_path):
    entity = types.MessageEntityBold(offset=0, length=4)

    class MediaMessage:
        file = object()
        photo = None
        document = None
        voice = False
        video = False
        video_note = False

        def __init__(self, message_id, caption, entities):
            self.id = message_id
            self.message = caption
            self.entities = entities
            self.media = SimpleNamespace(document=SimpleNamespace(mime_type="image/jpeg", attributes=()))

    class FakeClient:
        def __init__(self):
            self.sent_files = []

        async def download_media(self, message, file):
            path = tmp_path / f"{message.id}.jpg"
            path.write_bytes(b"jpg")
            return str(path)

        async def send_file(self, *args, **kwargs):
            self.sent_files.append((args, kwargs))

    messages = [MediaMessage(1, "", None), MediaMessage(2, "bold", [entity])]
    event = build_post_mirror_event(
        type(
            "Event",
            (),
            {
                "chat_id": -100111,
                "is_channel": True,
                "is_group": False,
                "messages": messages,
                "message": messages[0],
            },
        )()
    )
    client = FakeClient()

    await TelethonPostMirrorSender(client).copy_post(event, target_chat_id=-100900, target_thread_id=77)

    assert client.sent_files[0][1]["formatting_entities"] == [[], [entity]]


async def test_post_mirror_sender_preserves_album_video_attributes(tmp_path):
    video_attributes = (types.DocumentAttributeVideo(duration=3, w=720, h=1280, supports_streaming=True),)

    class VideoMessage:
        id = 1
        message = "video"
        entities = None
        media = SimpleNamespace(document=SimpleNamespace(mime_type="video/mp4", attributes=video_attributes))
        file = object()
        photo = None
        document = None
        voice = False
        video = True
        video_note = False

    class PhotoMessage:
        id = 2
        message = ""
        entities = None
        media = SimpleNamespace(photo=object())
        file = object()
        photo = object()
        document = None
        voice = False
        video = False
        video_note = False

    class FakeClient:
        def __init__(self):
            self.file_to_media_calls = []
            self.requests = []

        async def download_media(self, message, file):
            path = tmp_path / f"{message.id}.bin"
            path.write_bytes(b"media")
            return str(path)

        async def get_input_entity(self, entity):
            return entity

        async def _file_to_media(self, file, **kwargs):
            self.file_to_media_calls.append((file, kwargs))
            return None, SimpleNamespace(media_file=file), False

        async def __call__(self, request):
            self.requests.append(request)
            return SimpleNamespace()

        def _get_response_message(self, random_ids, result, entity):
            return []

    event = build_post_mirror_event(
        type(
            "Event",
            (),
            {
                "chat_id": -100111,
                "is_channel": True,
                "is_group": False,
                "messages": [VideoMessage(), PhotoMessage()],
                "message": VideoMessage(),
            },
        )()
    )
    client = FakeClient()

    await TelethonPostMirrorSender(client).copy_post(event, target_chat_id=-100900, target_thread_id=77)

    assert client.file_to_media_calls[0][1]["attributes"] == video_attributes
    assert client.file_to_media_calls[0][1]["mime_type"] == "video/mp4"
    assert client.file_to_media_calls[0][1]["supports_streaming"] is True
    assert "attributes" not in client.file_to_media_calls[1][1]
    send_requests = [request for request in client.requests if request.__class__.__name__ != "UpdateStatusRequest"]
    assert send_requests[-1].reply_to.reply_to_msg_id == 77
    assert len(send_requests[-1].multi_media) == 2


async def test_post_mirror_sender_repairs_album_video_dimensions_from_file_probe(tmp_path):
    class VideoMessage:
        id = 1
        message = "video"
        entities = None
        media = SimpleNamespace(document=SimpleNamespace(mime_type="video/mp4", attributes=()))
        file = object()
        photo = None
        document = None
        voice = False
        video = True
        video_note = False

    class PhotoMessage:
        id = 2
        message = ""
        entities = None
        media = SimpleNamespace(photo=object())
        file = object()
        photo = object()
        document = None
        voice = False
        video = False
        video_note = False

    class FakeClient:
        def __init__(self):
            self.file_to_media_calls = []
            self.requests = []

        async def download_media(self, message, file):
            path = tmp_path / f"{message.id}.bin"
            path.write_bytes(b"media")
            return str(path)

        async def get_input_entity(self, entity):
            return entity

        async def _file_to_media(self, file, **kwargs):
            self.file_to_media_calls.append((file, kwargs))
            return None, SimpleNamespace(media_file=file), False

        async def __call__(self, request):
            self.requests.append(request)
            return SimpleNamespace()

        def _get_response_message(self, random_ids, result, entity):
            return []

    async def probe_video_metadata(path):
        return (720, 1280, 3)

    event = build_post_mirror_event(
        type(
            "Event",
            (),
            {
                "chat_id": -100111,
                "is_channel": True,
                "is_group": False,
                "messages": [VideoMessage(), PhotoMessage()],
                "message": VideoMessage(),
            },
        )()
    )
    client = FakeClient()

    await TelethonPostMirrorSender(client, video_metadata_probe=probe_video_metadata).copy_post(
        event,
        target_chat_id=-100900,
        target_thread_id=77,
    )

    video_attr = next(
        attr for attr in client.file_to_media_calls[0][1]["attributes"] if isinstance(attr, types.DocumentAttributeVideo)
    )
    assert video_attr.w == 720
    assert video_attr.h == 1280
    assert video_attr.duration == 3
    assert video_attr.supports_streaming is True
    assert "attributes" not in client.file_to_media_calls[1][1]


async def test_post_mirror_sender_falls_back_to_individual_files_when_album_is_invalid(tmp_path):
    class MediaMessage:
        file = object()
        photo = object()
        document = None
        voice = False
        video = False
        video_note = False

        def __init__(self, message_id, caption):
            self.id = message_id
            self.message = caption
            self.entities = None
            self.media = SimpleNamespace(photo=object())

    class FakeClient:
        def __init__(self):
            self.sent_files = []

        async def download_media(self, message, file):
            path = tmp_path / f"{message.id}.jpg"
            path.write_bytes(b"jpg")
            return str(path)

        async def send_file(self, *args, **kwargs):
            self.sent_files.append((args, kwargs))
            if isinstance(args[1], list):
                raise errors.DocumentInvalidError(request=None)

    messages = [MediaMessage(1, "one"), MediaMessage(2, "two")]
    event = build_post_mirror_event(
        type(
            "Event",
            (),
            {
                "chat_id": -100111,
                "is_channel": True,
                "is_group": False,
                "messages": messages,
                "message": messages[0],
            },
        )()
    )
    client = FakeClient()

    result = await TelethonPostMirrorSender(client).copy_post(event, target_chat_id=-100900, target_thread_id=77)

    assert result.media_count == 2
    assert len(client.sent_files) == 3
    assert isinstance(client.sent_files[0][0][1], list)
    assert client.sent_files[1][0][0] == -100900
    assert client.sent_files[1][1]["caption"] == "one"
    assert client.sent_files[1][1]["reply_to"] == 77
    assert client.sent_files[2][1]["caption"] == "two"


async def test_post_mirror_sender_retries_individual_invalid_media_as_document(tmp_path):
    class MediaMessage:
        id = 1
        message = "file"
        entities = None
        media = SimpleNamespace(photo=object())
        file = object()
        photo = object()
        document = None
        voice = False
        video = False
        video_note = False

    class FakeClient:
        def __init__(self):
            self.sent_files = []

        async def download_media(self, message, file):
            path = tmp_path / "file.bin"
            path.write_bytes(b"file")
            return str(path)

        async def send_file(self, *args, **kwargs):
            self.sent_files.append((args, kwargs))
            if not kwargs.get("force_document"):
                raise errors.DocumentInvalidError(request=None)

    event = build_post_mirror_event(
        type(
            "Event",
            (),
            {
                "chat_id": -100111,
                "is_channel": True,
                "is_group": False,
                "messages": [MediaMessage()],
                "message": MediaMessage(),
            },
        )()
    )
    client = FakeClient()

    result = await TelethonPostMirrorSender(client).copy_post(event, target_chat_id=-100900, target_thread_id=77)

    assert result.media_count == 1
    assert len(client.sent_files) == 2
    assert client.sent_files[1][1]["force_document"] is True
    assert client.sent_files[1][1]["caption"] == "file"


async def test_post_mirror_sender_retries_send_file_after_flood_wait(tmp_path):
    class MediaMessage:
        id = 1
        message = "file"
        entities = None
        media = SimpleNamespace(photo=object())
        file = object()
        photo = object()
        document = None
        voice = False
        video = False
        video_note = False

    class FakeClient:
        def __init__(self):
            self.send_file_calls = 0

        async def download_media(self, message, file):
            path = tmp_path / "file.jpg"
            path.write_bytes(b"file")
            return str(path)

        async def send_file(self, *args, **kwargs):
            self.send_file_calls += 1
            if self.send_file_calls == 1:
                raise errors.FloodWaitError(request=None, capture=2)

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    event = build_post_mirror_event(
        type(
            "Event",
            (),
            {
                "chat_id": -100111,
                "is_channel": True,
                "is_group": False,
                "messages": [MediaMessage()],
                "message": MediaMessage(),
            },
        )()
    )
    client = FakeClient()

    result = await TelethonPostMirrorSender(client, sleep=fake_sleep).copy_post(
        event,
        target_chat_id=-100900,
        target_thread_id=77,
    )

    assert result.media_count == 1
    assert client.send_file_calls == 2
    assert sleeps == [7]


async def test_post_mirror_history_backfill_copies_latest_posts_chronologically(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.set_post_mirror_source_enabled(-100111, True)
    sender = FakePostMirrorSender()

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            assert chat_id == -100111
            assert limit is None
            assert reverse is False
            assert wait_time == 1.0
            for message in [
                FakeHistoryMessage(4),
                FakeHistoryMessage(3, action=object()),
                FakeHistoryMessage(2),
                FakeHistoryMessage(1),
            ]:
                yield message

    backfill = PostMirrorHistoryBackfill(
        client=FakeClient(),
        state=repo,
        post_mirror_sender=sender,
        history_post_delay_range_seconds=(0, 0),
    )

    result = await backfill.process_history(limit_per_source=2, chat_id=-100111)

    assert result.source_count == 1
    assert result.scanned_count == 3
    assert result.mirrored_count == 2
    assert sender.calls == [((2,), -100900, 77), ((4,), -100900, 77)]


async def test_post_mirror_history_backfill_logs_job_source_and_batch_progress(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.set_post_mirror_source_enabled(-100111, True)
    sender = FakePostMirrorSender()
    logger_ = FakeLogger()

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            for message in [FakeHistoryMessage(2), FakeHistoryMessage(1)]:
                yield message

    backfill = PostMirrorHistoryBackfill(
        client=FakeClient(),
        state=repo,
        post_mirror_sender=sender,
        history_post_delay_range_seconds=(0, 0),
        logger_=logger_,
    )

    result = await backfill.process_history(limit_per_source=2, chat_id=-100111)

    assert result.mirrored_count == 2
    messages = [message for message, _ in logger_.info_calls]
    assert "post_mirror_history_started source_count=%s limit_per_source=%s chat_id=%s folder_id=%s target_chat_id=%s" in messages
    assert "post_mirror_history_source_started chat_id=%s has_topic=%s target_thread_id=%s limit_per_source=%s" in messages
    assert (
        "post_mirror_history_batch_result chat_id=%s message_id=%s message_count=%s grouped_id=%s result=%s"
        in messages
    )
    assert (
        "post_mirror_history_source_finished chat_id=%s scanned_count=%s mirrored_count=%s skipped_count=%s "
        "target_thread_id=%s"
    ) in messages
    assert (
        "post_mirror_history_finished source_count=%s scanned_count=%s mirrored_count=%s skipped_count=%s "
        "failed_count=%s limit_per_source=%s target_chat_id=%s"
    ) in messages


async def test_post_mirror_history_backfill_groups_albums_as_one_post(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.set_post_mirror_source_enabled(-100111, True)
    sender = FakePostMirrorSender()

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            for message in [
                FakeHistoryMessage(5),
                FakeHistoryMessage(4, grouped_id=900),
                FakeHistoryMessage(3, grouped_id=900),
                FakeHistoryMessage(2),
            ]:
                yield message

    backfill = PostMirrorHistoryBackfill(
        client=FakeClient(),
        state=repo,
        post_mirror_sender=sender,
        history_post_delay_range_seconds=(0, 0),
    )

    result = await backfill.process_history(limit_per_source=2, chat_id=-100111)

    assert result.mirrored_count == 2
    assert sender.calls == [((3, 4), -100900, 77), ((5,), -100900, 77)]


async def test_post_mirror_history_backfill_paces_history_post_operations(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.set_post_mirror_source_enabled(-100111, True)
    sender = FakePostMirrorSender()
    sleeps = []

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            for message in [FakeHistoryMessage(2), FakeHistoryMessage(1)]:
                yield message

    async def fake_sleep(delay):
        sleeps.append(delay)

    backfill = PostMirrorHistoryBackfill(
        client=FakeClient(),
        state=repo,
        post_mirror_sender=sender,
        history_post_delay_range_seconds=(4, 9),
        randint=lambda minimum, maximum: 6,
        sleep=fake_sleep,
    )

    result = await backfill.process_history(limit_per_source=2, chat_id=-100111)

    assert result.mirrored_count == 2
    assert sender.calls == [((1,), -100900, 77), ((2,), -100900, 77)]
    assert sleeps == [6]


async def test_post_mirror_history_backfill_streams_full_history_chronologically(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.set_post_mirror_source_enabled(-100111, True)
    events = []

    class Sender(FakePostMirrorSender):
        async def copy_post(self, event, *, target_chat_id, target_thread_id):
            events.append(f"send:{event.message_id}")
            return await super().copy_post(event, target_chat_id=target_chat_id, target_thread_id=target_thread_id)

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            assert chat_id == -100111
            assert limit is None
            assert reverse is True
            assert wait_time == 1.0
            events.append("yield:1")
            yield FakeHistoryMessage(1)
            events.append("after:1")
            events.append("yield:2")
            yield FakeHistoryMessage(2)
            events.append("after:2")

    async def fake_sleep(delay):
        events.append(f"sleep:{delay}")

    sender = Sender()
    backfill = PostMirrorHistoryBackfill(
        client=FakeClient(),
        state=repo,
        post_mirror_sender=sender,
        history_post_delay_range_seconds=(4, 9),
        randint=lambda minimum, maximum: 6,
        sleep=fake_sleep,
    )

    result = await backfill.process_history(limit_per_source=None, chat_id=-100111)

    assert result.scanned_count == 2
    assert result.mirrored_count == 2
    assert sender.calls == [((1,), -100900, 77), ((2,), -100900, 77)]
    assert events == ["yield:1", "send:1", "after:1", "yield:2", "sleep:6", "send:2", "after:2"]


async def test_post_mirror_history_backfill_yields_to_realtime_before_next_history_post(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.set_post_mirror_source_enabled(-100111, True)
    gate = PostMirrorOperationGate()
    events = []
    realtime_done = asyncio.Event()

    class Sender(FakePostMirrorSender):
        async def copy_post(self, event, *, target_chat_id, target_thread_id):
            events.append(f"history:{event.message_id}")
            return await super().copy_post(event, target_chat_id=target_chat_id, target_thread_id=target_thread_id)

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            for message in [FakeHistoryMessage(2), FakeHistoryMessage(1)]:
                yield message

    async def fake_sleep(delay):
        events.append(f"sleep:{delay}")
        gate.notify_realtime_queued()

        async def run_realtime():
            async with gate.realtime_operation():
                events.append("realtime")
            realtime_done.set()

        asyncio.create_task(run_realtime())
        await asyncio.sleep(0)

    backfill = PostMirrorHistoryBackfill(
        client=FakeClient(),
        state=repo,
        post_mirror_sender=Sender(),
        operation_gate=gate,
        history_post_delay_range_seconds=(4, 9),
        randint=lambda minimum, maximum: 6,
        sleep=fake_sleep,
    )

    result = await backfill.process_history(limit_per_source=2, chat_id=-100111)
    await asyncio.wait_for(realtime_done.wait(), timeout=1)

    assert result.mirrored_count == 2
    assert events == ["history:1", "sleep:6", "realtime", "history:2"]


async def test_post_mirror_history_backfill_yields_pending_realtime_before_history_sleep(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.set_post_mirror_source_enabled(-100111, True)
    gate = PostMirrorOperationGate()
    events = []
    realtime_done = asyncio.Event()

    class Sender(FakePostMirrorSender):
        async def copy_post(self, event, *, target_chat_id, target_thread_id):
            events.append(f"history:{event.message_id}")
            if event.message_id == 1:
                gate.notify_realtime_queued()

                async def run_realtime():
                    async with gate.realtime_operation():
                        events.append("realtime")
                    realtime_done.set()

                asyncio.create_task(run_realtime())
            return await super().copy_post(event, target_chat_id=target_chat_id, target_thread_id=target_thread_id)

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            for message in [FakeHistoryMessage(2), FakeHistoryMessage(1)]:
                yield message

    async def fake_sleep(delay):
        events.append(f"sleep:{delay}")
        await asyncio.sleep(0)

    backfill = PostMirrorHistoryBackfill(
        client=FakeClient(),
        state=repo,
        post_mirror_sender=Sender(),
        operation_gate=gate,
        history_post_delay_range_seconds=(4, 9),
        randint=lambda minimum, maximum: 6,
        sleep=fake_sleep,
    )

    result = await backfill.process_history(limit_per_source=2, chat_id=-100111)
    await asyncio.wait_for(realtime_done.wait(), timeout=1)

    assert result.mirrored_count == 2
    assert events == ["history:1", "realtime", "sleep:6", "history:2"]


async def test_post_mirror_history_fetch_wait_yields_to_realtime_before_copy(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.set_post_mirror_source_enabled(-100111, True)
    gate = PostMirrorOperationGate()
    events = []
    realtime_done = asyncio.Event()

    class Sender(FakePostMirrorSender):
        async def copy_post(self, event, *, target_chat_id, target_thread_id):
            events.append("history-copy")
            return await super().copy_post(event, target_chat_id=target_chat_id, target_thread_id=target_thread_id)

    class FakeClient:
        def __init__(self):
            self.queued_realtime = False

        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            events.append("history-fetch-start")
            if self.queued_realtime:
                events.append("history-fetch-done")
                yield FakeHistoryMessage(1)
                return
            self.queued_realtime = True
            gate.notify_realtime_queued()

            async def run_realtime():
                async with gate.realtime_operation():
                    events.append("realtime")
                realtime_done.set()

            asyncio.create_task(run_realtime())
            await asyncio.sleep(0)
            events.append("history-fetch-done")
            yield FakeHistoryMessage(1)

    backfill = PostMirrorHistoryBackfill(
        client=FakeClient(),
        state=repo,
        post_mirror_sender=Sender(),
        operation_gate=gate,
    )

    result = await backfill.process_history(limit_per_source=None, chat_id=-100111)
    await asyncio.wait_for(realtime_done.wait(), timeout=1)

    assert result.mirrored_count == 1
    assert events == ["history-fetch-start", "realtime", "history-fetch-start", "history-fetch-done", "history-copy"]


async def test_post_mirror_history_backfill_retries_after_flood_wait(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_enabled(-100111, True)
    sleeps = []

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            yield FakeHistoryMessage(1)

    class TopicManager:
        def __init__(self):
            self.calls = 0

        async def create_topic(self, target_chat_id, title):
            self.calls += 1
            if self.calls == 1:
                raise errors.FloodWaitError(request=None, capture=2)
            return 77

    async def fake_sleep(delay):
        sleeps.append(delay)

    sender = FakePostMirrorSender()
    topic_manager = TopicManager()
    backfill = PostMirrorHistoryBackfill(
        client=FakeClient(),
        state=repo,
        post_mirror_sender=sender,
        post_mirror_topic_manager=topic_manager,
        history_topic_create_delay_range_seconds=(0, 0),
        sleep=fake_sleep,
    )

    result = await backfill.process_history(limit_per_source=None, chat_id=-100111)

    assert result.failed_count == 0
    assert result.mirrored_count == 1
    assert topic_manager.calls == 2
    assert sleeps == [7]
    assert repo.get_post_mirror_source_settings(-100111).target_thread_id == 77


async def test_post_mirror_history_folder_flood_wait_does_not_skip_remaining_sources(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_reaction_folder(2, "Mirror folder", position=0)
    repo.replace_reaction_folder_members(
        2,
        [
            {"chat_id": -100111, "title": "Source A", "kind": "channel"},
            {"chat_id": -100222, "title": "Source B", "kind": "channel"},
            {"chat_id": -100333, "title": "Source C", "kind": "channel"},
        ],
    )
    repo.set_post_mirror_folder_enabled(2, True)
    sleeps = []
    sender = FakePostMirrorSender()

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            yield FakeHistoryMessage(1)

    class TopicManager:
        def __init__(self):
            self.calls = []

        async def create_topic(self, target_chat_id, title):
            self.calls.append((target_chat_id, title))
            if len(self.calls) == 1:
                raise errors.FloodWaitError(request=None, capture=2)
            return 70 + len(self.calls)

    async def fake_sleep(delay):
        sleeps.append(delay)

    topic_manager = TopicManager()
    backfill = PostMirrorHistoryBackfill(
        client=FakeClient(),
        state=repo,
        post_mirror_sender=sender,
        post_mirror_topic_manager=topic_manager,
        history_post_delay_range_seconds=(0, 0),
        history_topic_create_delay_range_seconds=(0, 0),
        sleep=fake_sleep,
    )

    result = await backfill.process_history(limit_per_source=None, folder_id=2)

    assert result.failed_count == 0
    assert result.mirrored_count == 3
    assert sleeps == [7]
    assert topic_manager.calls == [
        (-100900, "Source C"),
        (-100900, "Source C"),
        (-100900, "Source B"),
        (-100900, "Source A"),
    ]
    assert sender.calls == [
        ((1,), -100900, 72),
        ((1,), -100900, 73),
        ((1,), -100900, 74),
    ]
    assert repo.get_post_mirror_source_settings(-100111).target_thread_id == 74
    assert repo.get_post_mirror_source_settings(-100222).target_thread_id == 73
    assert repo.get_post_mirror_source_settings(-100333).target_thread_id == 72


async def test_post_mirror_history_backfill_retries_history_fetch_after_flood_wait(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.set_post_mirror_source_enabled(-100111, True)
    sleeps = []
    sender = FakePostMirrorSender()

    class HistoryIterator:
        def __init__(self):
            self.calls = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.calls += 1
            if self.calls == 1:
                raise errors.FloodWaitError(request=None, capture=2)
            if self.calls == 2:
                return FakeHistoryMessage(1)
            raise StopAsyncIteration

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            raise AssertionError("iter_messages must return an iterator directly")

    client = FakeClient()
    iterator = HistoryIterator()
    client.iter_messages = lambda *args, **kwargs: iterator

    async def fake_sleep(delay):
        sleeps.append(delay)

    backfill = PostMirrorHistoryBackfill(
        client=client,
        state=repo,
        post_mirror_sender=sender,
        history_post_delay_range_seconds=(0, 0),
        sleep=fake_sleep,
    )

    result = await backfill.process_history(limit_per_source=None, chat_id=-100111)

    assert result.failed_count == 0
    assert result.mirrored_count == 1
    assert sleeps == [7]
    assert sender.calls == [((1,), -100900, 77)]


async def test_post_mirror_history_backfill_waits_after_creating_topic_before_copy(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_enabled(-100111, True)
    events = []

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            yield FakeHistoryMessage(1)

    class Sender(FakePostMirrorSender):
        async def copy_post(self, event, *, target_chat_id, target_thread_id):
            events.append("copy")
            return await super().copy_post(event, target_chat_id=target_chat_id, target_thread_id=target_thread_id)

    class TopicManager:
        async def create_topic(self, target_chat_id, title):
            events.append("topic")
            return 77

    async def fake_sleep(delay):
        events.append(f"sleep:{delay}")

    backfill = PostMirrorHistoryBackfill(
        client=FakeClient(),
        state=repo,
        post_mirror_sender=Sender(),
        post_mirror_topic_manager=TopicManager(),
        history_topic_create_delay_range_seconds=(180, 360),
        randint=lambda minimum, maximum: 240,
        sleep=fake_sleep,
    )

    result = await backfill.process_history(limit_per_source=None, chat_id=-100111)

    assert result.mirrored_count == 1
    assert events == ["topic", "sleep:240", "copy"]


async def test_post_mirror_history_topic_cooldown_yields_to_realtime(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_enabled(-100111, True)
    gate = PostMirrorOperationGate()
    events = []
    realtime_done = asyncio.Event()

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            yield FakeHistoryMessage(1)

    class Sender(FakePostMirrorSender):
        async def copy_post(self, event, *, target_chat_id, target_thread_id):
            events.append("copy")
            return await super().copy_post(event, target_chat_id=target_chat_id, target_thread_id=target_thread_id)

    class TopicManager:
        async def create_topic(self, target_chat_id, title):
            events.append("topic")
            return 77

    async def fake_sleep(delay):
        events.append(f"sleep:{delay}")
        gate.notify_realtime_queued()

        async def run_realtime():
            async with gate.realtime_operation():
                events.append("realtime")
            realtime_done.set()

        asyncio.create_task(run_realtime())
        await asyncio.sleep(0)
        events.append("sleep-done")

    backfill = PostMirrorHistoryBackfill(
        client=FakeClient(),
        state=repo,
        post_mirror_sender=Sender(),
        post_mirror_topic_manager=TopicManager(),
        operation_gate=gate,
        history_topic_create_delay_range_seconds=(180, 360),
        randint=lambda minimum, maximum: 240,
        sleep=fake_sleep,
    )

    result = await backfill.process_history(limit_per_source=None, chat_id=-100111)
    await asyncio.wait_for(realtime_done.wait(), timeout=1)

    assert result.mirrored_count == 1
    assert events == ["topic", "sleep:240", "realtime", "sleep-done", "copy"]


async def test_post_mirror_history_sender_flood_wait_yields_to_realtime(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.set_post_mirror_source_enabled(-100111, True)
    gate = PostMirrorOperationGate()
    events = []
    realtime_done = asyncio.Event()

    class MediaMessage:
        id = 1
        message = "video"
        entities = None
        media = SimpleNamespace(photo=object())
        file = object()
        photo = object()
        document = None
        voice = False
        video = False
        video_note = False

    class FakeClient:
        def __init__(self):
            self.send_file_calls = 0

        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            yield MediaMessage()

        async def download_media(self, message, file):
            path = tmp_path / "video.mp4"
            path.write_bytes(b"video")
            return str(path)

        async def send_file(self, *args, **kwargs):
            self.send_file_calls += 1
            events.append(f"send:{self.send_file_calls}")
            if self.send_file_calls == 1:
                raise errors.FloodWaitError(request=None, capture=2)

    async def fake_sleep(delay):
        events.append(f"sleep:{delay}")
        gate.notify_realtime_queued()

        async def run_realtime():
            async with gate.realtime_operation():
                events.append("realtime")
            realtime_done.set()

        asyncio.create_task(run_realtime())
        await asyncio.sleep(0)
        events.append("sleep-done")

    client = FakeClient()
    backfill = PostMirrorHistoryBackfill(
        client=client,
        state=repo,
        post_mirror_sender=TelethonPostMirrorSender(client, sleep=fake_sleep),
        operation_gate=gate,
        sleep=fake_sleep,
    )

    result = await backfill.process_history(limit_per_source=None, chat_id=-100111)
    await asyncio.wait_for(realtime_done.wait(), timeout=1)

    assert result.mirrored_count == 1
    assert events == ["send:1", "sleep:7", "realtime", "sleep-done", "send:2"]


async def test_post_mirror_history_media_copy_yields_to_realtime_before_download(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.set_post_mirror_source_enabled(-100111, True)
    gate = PostMirrorOperationGate()
    events = []
    realtime_done = asyncio.Event()

    class MediaMessage:
        id = 1
        message = "photo"
        entities = None
        file = object()
        photo = object()
        document = None
        voice = False
        video = False
        video_note = False

        def __init__(self):
            self._queued_realtime = False

        @property
        def media(self):
            if not self._queued_realtime:
                self._queued_realtime = True
                events.append("realtime-queued")
                gate.notify_realtime_queued()

                async def run_realtime():
                    async with gate.realtime_operation():
                        events.append("realtime")
                    realtime_done.set()

                asyncio.create_task(run_realtime())
            return SimpleNamespace(photo=object())

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            yield MediaMessage()

        async def download_media(self, message, file):
            events.append("history-download")
            path = tmp_path / "photo.jpg"
            path.write_bytes(b"photo")
            return str(path)

        async def send_file(self, *args, **kwargs):
            events.append("history-send")

    client = FakeClient()
    backfill = PostMirrorHistoryBackfill(
        client=client,
        state=repo,
        post_mirror_sender=TelethonPostMirrorSender(client),
        operation_gate=gate,
    )

    result = await backfill.process_history(limit_per_source=None, chat_id=-100111)
    await asyncio.wait_for(realtime_done.wait(), timeout=1)

    assert result.mirrored_count == 1
    assert events == ["realtime-queued", "realtime", "history-download", "history-send"]


async def test_post_mirror_history_download_is_cancelled_when_realtime_arrives(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.set_post_mirror_source_enabled(-100111, True)
    gate = PostMirrorOperationGate()
    events = []
    realtime_done = asyncio.Event()

    class MediaMessage:
        id = 1
        message = "photo"
        entities = None
        media = SimpleNamespace(photo=object())
        file = object()
        photo = object()
        document = None
        voice = False
        video = False
        video_note = False

    class FakeClient:
        def __init__(self):
            self.download_calls = 0

        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            yield MediaMessage()

        async def download_media(self, message, file):
            self.download_calls += 1
            events.append(f"history-download:{self.download_calls}")
            if self.download_calls == 1:
                gate.notify_realtime_queued()

                async def run_realtime():
                    async with gate.realtime_operation():
                        events.append("realtime")
                    realtime_done.set()

                asyncio.create_task(run_realtime())
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    events.append("history-download-cancelled")
                    raise
                events.append("history-download-finished-without-cancel")
            path = tmp_path / "photo.jpg"
            path.write_bytes(b"photo")
            return str(path)

        async def send_file(self, *args, **kwargs):
            events.append("history-send")

    client = FakeClient()
    backfill = PostMirrorHistoryBackfill(
        client=client,
        state=repo,
        post_mirror_sender=TelethonPostMirrorSender(client),
        operation_gate=gate,
    )

    result = await backfill.process_history(limit_per_source=None, chat_id=-100111)
    await asyncio.wait_for(realtime_done.wait(), timeout=1)

    assert result.mirrored_count == 1
    assert events == [
        "history-download:1",
        "history-download-cancelled",
        "realtime",
        "history-download:2",
        "history-send",
    ]


async def test_post_mirror_history_single_media_upload_is_cancelled_when_realtime_arrives(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.set_post_mirror_source_enabled(-100111, True)
    gate = PostMirrorOperationGate()
    events = []
    realtime_done = asyncio.Event()

    video_attributes = (types.DocumentAttributeVideo(duration=3, w=720, h=1280, supports_streaming=True),)

    class VideoMessage:
        id = 1
        message = "video"
        entities = None
        media = SimpleNamespace(document=SimpleNamespace(mime_type="video/mp4", attributes=video_attributes))
        file = object()
        photo = None
        document = None
        voice = False
        video = True
        video_note = False

    class FakeClient:
        def __init__(self):
            self.file_to_media_calls = 0

        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            yield VideoMessage()

        async def download_media(self, message, file):
            path = tmp_path / "video.mp4"
            path.write_bytes(b"video")
            return str(path)

        async def get_input_entity(self, entity):
            return entity

        async def _file_to_media(self, file, **kwargs):
            self.file_to_media_calls += 1
            events.append(f"history-file-to-media:{self.file_to_media_calls}")
            if self.file_to_media_calls == 1:
                gate.notify_realtime_queued()

                async def run_realtime():
                    async with gate.realtime_operation():
                        events.append("realtime")
                    realtime_done.set()

                asyncio.create_task(run_realtime())
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    events.append("history-file-to-media-cancelled")
                    raise
                events.append("history-file-to-media-finished-without-cancel")
            return None, SimpleNamespace(media_file=file), False

        async def __call__(self, request):
            if request.__class__.__name__ == "UpdateStatusRequest":
                return SimpleNamespace()
            events.append("history-send-media")
            return SimpleNamespace()

        async def send_file(self, *args, **kwargs):
            events.append("legacy-send-file")

        def _get_response_message(self, request, result, entity):
            return SimpleNamespace(id=1)

    client = FakeClient()
    backfill = PostMirrorHistoryBackfill(
        client=client,
        state=repo,
        post_mirror_sender=TelethonPostMirrorSender(client),
        operation_gate=gate,
    )

    result = await backfill.process_history(limit_per_source=None, chat_id=-100111)
    await asyncio.wait_for(realtime_done.wait(), timeout=1)

    assert result.mirrored_count == 1
    assert events == [
        "history-file-to-media:1",
        "history-file-to-media-cancelled",
        "realtime",
        "history-file-to-media:2",
        "history-send-media",
    ]


async def test_post_mirror_history_album_copy_yields_to_realtime_before_file_to_media(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.set_post_mirror_source_enabled(-100111, True)
    gate = PostMirrorOperationGate()
    events = []
    realtime_done = asyncio.Event()

    video_attributes = (types.DocumentAttributeVideo(duration=3, w=720, h=1280, supports_streaming=True),)

    class VideoMessage:
        id = 1
        grouped_id = 900
        message = "video"
        entities = None
        media = SimpleNamespace(document=SimpleNamespace(mime_type="video/mp4", attributes=video_attributes))
        file = object()
        photo = None
        document = None
        voice = False
        video = True
        video_note = False

    class PhotoMessage:
        id = 2
        grouped_id = 900
        message = ""
        entities = None
        media = SimpleNamespace(photo=object())
        file = object()
        photo = object()
        document = None
        voice = False
        video = False
        video_note = False

    class FakeClient:
        def __init__(self):
            self.queued_realtime = False

        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            yield VideoMessage()
            yield PhotoMessage()

        async def download_media(self, message, file):
            path = tmp_path / f"{message.id}.bin"
            path.write_bytes(b"media")
            return str(path)

        async def get_input_entity(self, entity):
            events.append("history-entity")
            if self.queued_realtime:
                return entity
            self.queued_realtime = True
            gate.notify_realtime_queued()

            async def run_realtime():
                async with gate.realtime_operation():
                    events.append("realtime")
                realtime_done.set()

            asyncio.create_task(run_realtime())
            await asyncio.sleep(0)
            return entity

        async def _file_to_media(self, file, **kwargs):
            events.append("history-file-to-media")
            return None, SimpleNamespace(media_file=file), False

        async def __call__(self, request):
            if request.__class__.__name__ == "UpdateStatusRequest":
                return SimpleNamespace()
            events.append("history-send")
            return SimpleNamespace()

        def _get_response_message(self, random_ids, result, entity):
            return []

    client = FakeClient()
    backfill = PostMirrorHistoryBackfill(
        client=client,
        state=repo,
        post_mirror_sender=TelethonPostMirrorSender(client),
        operation_gate=gate,
    )

    result = await backfill.process_history(limit_per_source=None, chat_id=-100111)
    await asyncio.wait_for(realtime_done.wait(), timeout=1)

    assert result.mirrored_count == 1
    assert events == [
        "history-entity",
        "realtime",
        "history-entity",
        "history-file-to-media",
        "history-file-to-media",
        "history-send",
    ]


async def test_post_mirror_history_backfill_uses_enabled_folder_sources_and_creates_topic_lazily(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_reaction_folder(2, "Mirror folder", position=0)
    repo.replace_reaction_folder_members(
        2,
        [{"chat_id": -100111, "title": "Source Channel", "kind": "channel"}],
    )
    repo.set_post_mirror_folder_enabled(2, True)
    sender = FakePostMirrorSender()

    class FakeClient:
        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            assert chat_id == -100111
            yield FakeHistoryMessage(1)

    class FakeTopicManager:
        def __init__(self):
            self.created = []

        async def create_topic(self, target_chat_id, title):
            self.created.append((target_chat_id, title))
            return 77

    topic_manager = FakeTopicManager()
    backfill = PostMirrorHistoryBackfill(
        client=FakeClient(),
        state=repo,
        post_mirror_sender=sender,
        post_mirror_topic_manager=topic_manager,
        history_topic_create_delay_range_seconds=(0, 0),
    )

    result = await backfill.process_history(limit_per_source=1)

    assert result.source_count == 1
    assert result.mirrored_count == 1
    assert topic_manager.created == [(-100900, "Source Channel")]
    assert sender.calls == [((1,), -100900, 77)]
    assert repo.get_post_mirror_source_settings(-100111).target_thread_id == 77


async def test_post_mirror_history_backfill_can_scope_to_one_enabled_folder(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_reaction_folder(2, "Folder A", position=0)
    repo.replace_reaction_folder_members(
        2,
        [{"chat_id": -100111, "title": "Source A", "kind": "channel"}],
    )
    repo.set_post_mirror_folder_enabled(2, True)
    repo.upsert_reaction_folder(3, "Folder B", position=1)
    repo.replace_reaction_folder_members(
        3,
        [{"chat_id": -100222, "title": "Source B", "kind": "channel"}],
    )
    repo.set_post_mirror_folder_enabled(3, True)
    sender = FakePostMirrorSender()

    class FakeClient:
        def __init__(self):
            self.chat_ids = []

        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            self.chat_ids.append(chat_id)
            yield FakeHistoryMessage(1)

    class FakeTopicManager:
        async def create_topic(self, target_chat_id, title):
            return 77

    client = FakeClient()
    backfill = PostMirrorHistoryBackfill(
        client=client,
        state=repo,
        post_mirror_sender=sender,
        post_mirror_topic_manager=FakeTopicManager(),
        history_topic_create_delay_range_seconds=(0, 0),
    )

    result = await backfill.process_history(limit_per_source=1, folder_id=2)

    assert result.source_count == 1
    assert result.mirrored_count == 1
    assert client.chat_ids == [-100111]
    assert sender.calls == [((1,), -100900, 77)]
    assert repo.get_post_mirror_source_settings(-100222).target_thread_id is None


async def test_post_mirror_history_backfill_enqueue_deduplicates_same_request(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.set_post_mirror_source_enabled(-100111, True)

    class SlowClient:
        async def iter_messages(self, chat_id, *, limit=None, reverse=False, wait_time=None):
            await asyncio.sleep(0.01)
            yield FakeHistoryMessage(1)

    backfill = PostMirrorHistoryBackfill(
        client=SlowClient(),
        state=repo,
        post_mirror_sender=FakePostMirrorSender(),
    )

    first = await backfill.enqueue_history(limit_per_source=None, chat_id=-100111)
    duplicate = await backfill.enqueue_history(limit_per_source=None, chat_id=-100111)
    await backfill.wait_history_queue_idle()

    assert first.request_queued is True
    assert duplicate.request_queued is True
    assert duplicate.duplicate_queued is True
    assert duplicate.queue_position == 1


def test_post_mirror_queue_worker_defaults_to_no_realtime_drops():
    async def handler(event):
        return None

    worker = PostMirrorQueueWorker(handler=handler)
    events = [
        type(
            "Event",
            (),
            {"chat_id": -100123, "message": type("Message", (), {"id": message_id})()},
        )()
        for message_id in range(300)
    ]

    assert all(worker.submit(event) for event in events)
    assert worker.dropped_count == 0


async def test_post_mirror_queue_worker_marks_realtime_pending_before_processing():
    gate = PostMirrorOperationGate()
    events = []

    async def handler(event):
        events.append("realtime")

    worker = PostMirrorQueueWorker(handler=handler, operation_gate=gate)
    event = type("Event", (), {"chat_id": -100123, "message": type("Message", (), {"id": 1})()})()

    assert worker.submit(event) is True

    async def run_history():
        async with gate.history_operation():
            events.append("history")

    history_task = asyncio.create_task(run_history())
    await asyncio.sleep(0)
    assert events == []

    worker_task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(history_task, timeout=1)
    finally:
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)

    assert events == ["realtime", "history"]


async def test_post_mirror_queue_worker_retries_after_flood_wait():
    attempts = 0
    sleeps = []
    done = asyncio.Event()

    async def handler(event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise errors.FloodWaitError(request=None, capture=3)
        done.set()

    async def fake_sleep(delay):
        sleeps.append(delay)

    worker = PostMirrorQueueWorker(handler=handler, sleep=fake_sleep)
    event = type("Event", (), {"chat_id": -100123, "message": type("Message", (), {"id": 1})()})()

    assert worker.submit(event) is True
    worker_task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(done.wait(), timeout=1)
        await asyncio.wait_for(worker._queue.join(), timeout=1)
    finally:
        worker_task.cancel()

    assert attempts == 2
    assert sleeps == [8]
    assert worker.processed_count == 1


async def test_dispatch_post_mirror_propagates_flood_wait_for_queue_retry():
    class Registry:
        async def dispatch(self, event, context):
            raise errors.FloodWaitError(request=None, capture=4)

    message = type("Message", (), {"id": 1, "media": None, "message": "x", "action": None})()
    event = type(
        "Event",
        (),
        {
            "chat_id": -100111,
            "is_channel": True,
            "is_group": False,
            "messages": [message],
            "message": message,
        },
    )()

    with pytest.raises(errors.FloodWaitError):
        await dispatch_post_mirror(event, Registry(), SimpleNamespace())


async def test_post_mirror_outbox_enqueuer_persists_job_without_sending(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    enqueuer = PostMirrorOutboxEnqueuer(repo, origin="realtime")
    message = FakeHistoryMessage(10)
    event = build_post_mirror_event(
        type(
            "Event",
            (),
            {
                "chat_id": -100111,
                "is_channel": True,
                "is_group": False,
                "messages": [message],
                "message": message,
            },
        )()
    )

    result = await enqueuer.copy_post(event, target_chat_id=-100900, target_thread_id=77)

    assert result.message_count == 1
    assert result.media_count == 0
    jobs = repo.list_ready_post_mirror_deliveries(now=0, limit=10)
    assert len(jobs) == 1
    assert jobs[0].source_chat_id == -100111
    assert jobs[0].message_ids == (10,)
    assert jobs[0].target_chat_id == -100900
    assert jobs[0].target_thread_id == 77
    assert jobs[0].origin == "realtime"


async def test_telegram_authorization_online_gate_ignores_current_session():
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)

    class FakeClient:
        def __init__(self):
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            return SimpleNamespace(
                authorizations=[
                    SimpleNamespace(current=True, date_active=now),
                    SimpleNamespace(current=False, date_active=now - timedelta(seconds=10)),
                ]
            )

    client = FakeClient()
    gate = TelegramAuthorizationOnlineGate(client, freshness_seconds=60, now=lambda: now)

    assert await gate.is_online() is True
    assert [request.__class__.__name__ for request in client.requests] == ["GetAuthorizationsRequest"]

    class CurrentOnlyClient:
        def __init__(self):
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            return SimpleNamespace(authorizations=[SimpleNamespace(current=True, date_active=now)])

    current_only_client = CurrentOnlyClient()
    current_only_gate = TelegramAuthorizationOnlineGate(current_only_client, freshness_seconds=60, now=lambda: now)

    assert await current_only_gate.is_online() is False
    assert [request.__class__.__name__ for request in current_only_client.requests] == [
        "GetAuthorizationsRequest",
        "UpdateStatusRequest",
    ]


async def test_post_mirror_outbox_delivery_worker_skips_online_gate_when_no_ready_jobs(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    class ExplodingGate:
        async def is_online(self):
            raise AssertionError("empty outbox must not poll Telegram authorizations")

    class FakeClient:
        async def __call__(self, request):
            raise AssertionError("empty outbox must not update Telegram status")

    class FailingSender:
        async def copy_post(self, event, *, target_chat_id, target_thread_id):
            raise AssertionError("empty outbox must not send")

    worker = PostMirrorOutboxDeliveryWorker(
        state=repo,
        client=FakeClient(),
        post_mirror_sender=FailingSender(),
        online_gate=ExplodingGate(),
        now=lambda: 1000,
    )

    assert await worker.drain_once(limit=10) == 0


async def test_post_mirror_outbox_delivery_worker_skips_sends_while_offline(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.enqueue_post_mirror_delivery(
        source_chat_id=-100111,
        message_ids=(10,),
        is_channel=True,
        is_group=False,
        grouped_id=None,
        target_chat_id=-100900,
        target_thread_id=77,
        origin="realtime",
        ready_at=1000,
    )

    class OfflineGate:
        async def is_online(self):
            return False

    class FailingSender:
        async def copy_post(self, event, *, target_chat_id, target_thread_id):
            raise AssertionError("offline delivery worker must not send")

    worker = PostMirrorOutboxDeliveryWorker(
        state=repo,
        client=SimpleNamespace(),
        post_mirror_sender=FailingSender(),
        online_gate=OfflineGate(),
        now=lambda: 1000,
    )

    assert await worker.drain_once(limit=10) == 0
    assert len(repo.list_ready_post_mirror_deliveries(now=1000, limit=10)) == 1


async def test_post_mirror_outbox_delivery_worker_sends_ready_jobs_when_online(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_enabled(-100111, True)
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.enqueue_post_mirror_delivery(
        source_chat_id=-100111,
        message_ids=(10, 11),
        is_channel=True,
        is_group=False,
        grouped_id=999,
        target_chat_id=-100900,
        target_thread_id=77,
        origin="history",
        ready_at=1000,
    )
    messages = [FakeHistoryMessage(10, grouped_id=999), FakeHistoryMessage(11, grouped_id=999)]

    class OnlineGate:
        async def is_online(self):
            return True

    class FakeClient:
        async def get_messages(self, chat_id, *, ids):
            assert chat_id == -100111
            assert ids == [10, 11]
            return list(reversed(messages))

    class Sender:
        def __init__(self):
            self.calls = []

        async def copy_post(self, event, *, target_chat_id, target_thread_id):
            self.calls.append((event, target_chat_id, target_thread_id))
            from telepath.features.post_mirroring import PostMirrorSendResult

            return PostMirrorSendResult(message_count=len(event.message_ids))

    sender = Sender()
    worker = PostMirrorOutboxDeliveryWorker(
        state=repo,
        client=FakeClient(),
        post_mirror_sender=sender,
        online_gate=OnlineGate(),
        now=lambda: 1000,
    )

    assert await worker.drain_once(limit=10) == 1
    assert repo.list_ready_post_mirror_deliveries(now=2000, limit=10) == []
    assert len(sender.calls) == 1
    event, target_chat_id, target_thread_id = sender.calls[0]
    assert event.message_ids == (10, 11)
    assert [message.id for message in event.messages] == [10, 11]
    assert target_chat_id == -100900
    assert target_thread_id == 77


async def test_post_mirror_outbox_delivery_worker_creates_missing_topic_only_when_online(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_enabled(-100111, True)
    repo.enqueue_post_mirror_delivery(
        source_chat_id=-100111,
        message_ids=(10,),
        is_channel=True,
        is_group=False,
        grouped_id=None,
        target_chat_id=-100900,
        target_thread_id=None,
        origin="history",
        ready_at=1000,
    )

    class OnlineGate:
        async def is_online(self):
            return True

    class FakeClient:
        async def get_messages(self, chat_id, *, ids):
            return [FakeHistoryMessage(ids[0])]

    class TopicManager:
        def __init__(self):
            self.calls = []

        async def create_topic(self, target_chat_id, title):
            self.calls.append((target_chat_id, title))
            return 77

    sender = FakePostMirrorSender()
    topic_manager = TopicManager()
    worker = PostMirrorOutboxDeliveryWorker(
        state=repo,
        client=FakeClient(),
        post_mirror_sender=sender,
        online_gate=OnlineGate(),
        post_mirror_topic_manager=topic_manager,
        now=lambda: 1000,
    )

    assert await worker.drain_once(limit=10) == 1
    assert topic_manager.calls == [(-100900, "Source Channel")]
    assert sender.calls == [((10,), -100900, 77)]
    assert repo.get_post_mirror_source_settings(-100111).target_thread_id == 77


async def test_online_gated_topic_manager_skips_rename_while_offline():
    class OfflineGate:
        async def is_online(self):
            return False

    class TopicManager:
        def __init__(self):
            self.renames = []

        async def rename_topic(self, target_chat_id, topic_id, title):
            self.renames.append((target_chat_id, topic_id, title))

    topic_manager = TopicManager()
    gated = OnlineGatedForumTopicManager(topic_manager, OfflineGate())

    await gated.rename_topic(-100900, 77, "New Title")

    assert topic_manager.renames == []


async def test_online_gated_topic_manager_does_not_force_offline_after_online_gate():
    from telepath.presence import mark_current_session_offline

    requests = []

    class FakeClient:
        async def __call__(self, request):
            requests.append(request)

    class OnlineGate:
        async def is_online(self):
            return True

    class TopicManager:
        def __init__(self, client):
            self.client = client

        async def create_topic(self, target_chat_id, title):
            await mark_current_session_offline(self.client)
            return 77

        async def rename_topic(self, target_chat_id, topic_id, title):
            await mark_current_session_offline(self.client)

    client = FakeClient()
    topic_manager = TopicManager(client)
    gated = OnlineGatedForumTopicManager(topic_manager, OnlineGate())

    assert await gated.create_topic(-100900, "Source") == 77
    await gated.rename_topic(-100900, 77, "Renamed")

    assert requests == []


async def test_post_mirror_outbox_delivery_worker_cancels_disabled_source_without_sending(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_enabled(-100111, False)
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.enqueue_post_mirror_delivery(
        source_chat_id=-100111,
        message_ids=(10,),
        is_channel=True,
        is_group=False,
        grouped_id=None,
        target_chat_id=-100900,
        target_thread_id=77,
        origin="history",
        ready_at=1000,
    )

    class OnlineGate:
        async def is_online(self):
            return True

    class FailingSender:
        async def copy_post(self, event, *, target_chat_id, target_thread_id):
            raise AssertionError("disabled source must not send")

    worker = PostMirrorOutboxDeliveryWorker(
        state=repo,
        client=SimpleNamespace(),
        post_mirror_sender=FailingSender(),
        online_gate=OnlineGate(),
        now=lambda: 1000,
    )

    assert await worker.drain_once(limit=10) == 0
    assert repo.list_ready_post_mirror_deliveries(now=1000, limit=10) == []


async def test_post_mirror_outbox_delivery_worker_cancels_repeatedly_unavailable_source_messages(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_enabled(-100111, True)
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.enqueue_post_mirror_delivery(
        source_chat_id=-100111,
        message_ids=(10,),
        is_channel=True,
        is_group=False,
        grouped_id=None,
        target_chat_id=-100900,
        target_thread_id=77,
        origin="realtime",
        ready_at=1000,
    )
    job = repo.list_ready_post_mirror_deliveries(now=1000, limit=1)[0]
    for attempt in range(5):
        repo.defer_post_mirror_delivery(
            job.id,
            delay_seconds=1,
            error="source messages unavailable",
            now=1000 + attempt,
        )
    assert repo.list_ready_post_mirror_deliveries(now=2000, limit=1)[0].attempts == 5

    class OnlineGate:
        async def is_online(self):
            return True

    class FakeClient:
        async def get_messages(self, chat_id, *, ids):
            return []

    class FailingSender:
        async def copy_post(self, event, *, target_chat_id, target_thread_id):
            raise AssertionError("unavailable source messages must not be sent")

    worker = PostMirrorOutboxDeliveryWorker(
        state=repo,
        client=FakeClient(),
        post_mirror_sender=FailingSender(),
        online_gate=OnlineGate(),
        now=lambda: 2000,
    )

    assert await worker.drain_once(limit=10) == 0
    assert post_mirror_outbox_status(repo, job.id) == "cancelled"


async def test_post_mirror_outbox_delivery_worker_cancels_unsupported_paid_media(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_enabled(-100111, True)
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.enqueue_post_mirror_delivery(
        source_chat_id=-100111,
        message_ids=(10,),
        is_channel=True,
        is_group=False,
        grouped_id=None,
        target_chat_id=-100900,
        target_thread_id=77,
        origin="realtime",
        ready_at=1000,
    )
    job = repo.list_ready_post_mirror_deliveries(now=1000, limit=10)[0]

    class OnlineGate:
        async def is_online(self):
            return True

    class FakeClient:
        async def get_messages(self, chat_id, *, ids):
            return [FakeHistoryMessage(ids[0])]

    class PaidMediaSender:
        async def copy_post(self, event, *, target_chat_id, target_thread_id):
            raise TypeError("Cannot use <telethon.tl.types.MessageMediaPaidMedia object at 0x1> as file")

    worker = PostMirrorOutboxDeliveryWorker(
        state=repo,
        client=FakeClient(),
        post_mirror_sender=PaidMediaSender(),
        online_gate=OnlineGate(),
        now=lambda: 1000,
    )

    assert await worker.drain_once(limit=10) == 0
    assert post_mirror_outbox_status(repo, job.id) == "cancelled"


async def test_post_mirror_outbox_delivery_worker_paces_actual_sends(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_enabled(-100111, True)
    repo.set_post_mirror_source_topic(-100111, 77)
    for message_id in (10, 11):
        repo.enqueue_post_mirror_delivery(
            source_chat_id=-100111,
            message_ids=(message_id,),
            is_channel=True,
            is_group=False,
            grouped_id=None,
            target_chat_id=-100900,
            target_thread_id=77,
            origin="history",
            ready_at=1000,
        )
    sleeps = []

    class OnlineGate:
        async def is_online(self):
            return True

    class FakeClient:
        async def get_messages(self, chat_id, *, ids):
            return [FakeHistoryMessage(ids[0])]

    class Sender(FakePostMirrorSender):
        pass

    async def fake_sleep(delay):
        sleeps.append(delay)

    worker = PostMirrorOutboxDeliveryWorker(
        state=repo,
        client=FakeClient(),
        post_mirror_sender=Sender(),
        online_gate=OnlineGate(),
        delivery_delay_range_seconds=(4, 9),
        randint=lambda minimum, maximum: 6,
        sleep=fake_sleep,
        now=lambda: 1000,
    )

    assert await worker.drain_once(limit=10) == 2
    assert sleeps == [6]


async def test_post_mirror_outbox_delivery_worker_stops_when_owner_goes_offline_mid_drain(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_enabled(-100111, True)
    repo.set_post_mirror_source_topic(-100111, 77)
    for message_id in (10, 11):
        repo.enqueue_post_mirror_delivery(
            source_chat_id=-100111,
            message_ids=(message_id,),
            is_channel=True,
            is_group=False,
            grouped_id=None,
            target_chat_id=-100900,
            target_thread_id=77,
            origin="realtime",
            ready_at=1000,
        )
    sleeps = []

    class OnlineThenOfflineGate:
        def __init__(self):
            self.calls = 0

        async def is_online(self):
            self.calls += 1
            return self.calls == 1

    class FakeClient:
        async def get_messages(self, chat_id, *, ids):
            return [FakeHistoryMessage(ids[0])]

    async def fake_sleep(delay):
        sleeps.append(delay)

    gate = OnlineThenOfflineGate()
    sender = FakePostMirrorSender()
    worker = PostMirrorOutboxDeliveryWorker(
        state=repo,
        client=FakeClient(),
        post_mirror_sender=sender,
        online_gate=gate,
        delivery_delay_range_seconds=(1, 1),
        sleep=fake_sleep,
        now=lambda: 1000,
    )

    assert await worker.drain_once(limit=10) == 1
    assert gate.calls == 2
    assert sleeps == [1]
    assert sender.calls == [((10,), -100900, 77)]
    assert [job.message_ids for job in repo.list_ready_post_mirror_deliveries(now=1000, limit=10)] == [(11,)]


async def test_post_mirror_outbox_delivery_worker_drains_ready_backlog_until_empty(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_enabled(-100111, True)
    repo.set_post_mirror_source_topic(-100111, 77)
    for message_id in range(1, 13):
        repo.enqueue_post_mirror_delivery(
            source_chat_id=-100111,
            message_ids=(message_id,),
            is_channel=True,
            is_group=False,
            grouped_id=None,
            target_chat_id=-100900,
            target_thread_id=77,
            origin="realtime",
            ready_at=1000,
        )

    class OnlineGate:
        async def is_online(self):
            return True

    class FakeClient:
        async def get_messages(self, chat_id, *, ids):
            return [FakeHistoryMessage(ids[0])]

    sender = FakePostMirrorSender()
    worker = PostMirrorOutboxDeliveryWorker(
        state=repo,
        client=FakeClient(),
        post_mirror_sender=sender,
        online_gate=OnlineGate(),
        delivery_delay_range_seconds=(0, 0),
        now=lambda: 1000,
    )

    assert await worker.drain_once() == 12
    assert repo.list_ready_post_mirror_deliveries(now=1000, limit=20) == []
    assert len(sender.calls) == 12


async def test_post_mirror_outbox_delivery_worker_caps_small_backlog_to_preferred_window(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_enabled(-100111, True)
    repo.set_post_mirror_source_topic(-100111, 77)
    for message_id in (10, 11, 12):
        repo.enqueue_post_mirror_delivery(
            source_chat_id=-100111,
            message_ids=(message_id,),
            is_channel=True,
            is_group=False,
            grouped_id=None,
            target_chat_id=-100900,
            target_thread_id=77,
            origin="history",
            ready_at=1000,
        )
    elapsed = 0.0
    sleeps = []

    class OnlineGate:
        async def is_online(self):
            return True

    class FakeClient:
        async def get_messages(self, chat_id, *, ids):
            return [FakeHistoryMessage(ids[0])]

    async def fake_sleep(delay):
        nonlocal elapsed
        sleeps.append(delay)
        elapsed += delay

    worker = PostMirrorOutboxDeliveryWorker(
        state=repo,
        client=FakeClient(),
        post_mirror_sender=FakePostMirrorSender(),
        online_gate=OnlineGate(),
        delivery_delay_range_seconds=(200, 200),
        randint=lambda minimum, maximum: 200,
        sleep=fake_sleep,
        monotonic=lambda: elapsed,
        online_delivery_window_seconds=300,
        now=lambda: 1000,
    )

    assert await worker.drain_once() == 3
    assert sleeps == [60.0, 60.0]
    assert repo.list_ready_post_mirror_deliveries(now=1000, limit=10) == []


async def test_post_mirror_outbox_delivery_worker_prefers_two_minute_small_backlog_window(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_enabled(-100111, True)
    repo.set_post_mirror_source_topic(-100111, 77)
    for message_id in (10, 11, 12, 13):
        repo.enqueue_post_mirror_delivery(
            source_chat_id=-100111,
            message_ids=(message_id,),
            is_channel=True,
            is_group=False,
            grouped_id=None,
            target_chat_id=-100900,
            target_thread_id=77,
            origin="realtime",
            ready_at=1000,
        )
    elapsed = 0.0
    sleeps = []

    class OnlineGate:
        async def is_online(self):
            return True

    class FakeClient:
        async def get_messages(self, chat_id, *, ids):
            return [FakeHistoryMessage(ids[0])]

    async def fake_sleep(delay):
        nonlocal elapsed
        sleeps.append(delay)
        elapsed += delay

    worker = PostMirrorOutboxDeliveryWorker(
        state=repo,
        client=FakeClient(),
        post_mirror_sender=FakePostMirrorSender(),
        online_gate=OnlineGate(),
        delivery_delay_range_seconds=(60, 120),
        randint=lambda minimum, maximum: 120,
        sleep=fake_sleep,
        monotonic=lambda: elapsed,
        now=lambda: 1000,
    )

    assert await worker.drain_once() == 4
    assert sleeps == [40.0, 40.0, 40.0]
    assert sum(sleeps) == 120


async def test_post_mirror_outbox_delivery_worker_keeps_minimum_spacing_for_large_backlog(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_enabled(-100111, True)
    repo.set_post_mirror_source_topic(-100111, 77)
    for message_id in range(1, 81):
        repo.enqueue_post_mirror_delivery(
            source_chat_id=-100111,
            message_ids=(message_id,),
            is_channel=True,
            is_group=False,
            grouped_id=None,
            target_chat_id=-100900,
            target_thread_id=77,
            origin="history",
            ready_at=1000,
        )
    elapsed = 0.0
    sleeps = []

    class OnlineGate:
        async def is_online(self):
            return True

    class FakeClient:
        async def get_messages(self, chat_id, *, ids):
            return [FakeHistoryMessage(ids[0])]

    async def fake_sleep(delay):
        nonlocal elapsed
        sleeps.append(delay)
        elapsed += delay

    worker = PostMirrorOutboxDeliveryWorker(
        state=repo,
        client=FakeClient(),
        post_mirror_sender=FakePostMirrorSender(),
        online_gate=OnlineGate(),
        delivery_delay_range_seconds=(60, 120),
        randint=lambda minimum, maximum: 120,
        sleep=fake_sleep,
        monotonic=lambda: elapsed,
        now=lambda: 1000,
    )

    assert await worker.drain_once() == 80
    assert sleeps
    assert min(sleeps) >= 1
    assert max(sleeps) <= 120
    assert round(sum(sleeps), 6) == 300


async def test_post_mirror_outbox_delivery_worker_does_not_force_offline_after_online_gated_drain(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100111, "Source Channel", "channel")
    repo.set_post_mirror_source_enabled(-100111, True)
    repo.set_post_mirror_source_topic(-100111, 77)
    repo.enqueue_post_mirror_delivery(
        source_chat_id=-100111,
        message_ids=(10,),
        is_channel=True,
        is_group=False,
        grouped_id=None,
        target_chat_id=-100900,
        target_thread_id=77,
        origin="realtime",
        ready_at=1000,
    )
    calls = []
    requests = []

    class OnlineGate:
        async def is_online(self):
            return True

    class FakeClient:
        async def __call__(self, request):
            requests.append(request)

        async def get_messages(self, chat_id, *, ids):
            calls.append("load")
            return [FakeHistoryMessage(ids[0])]

    class Sender(FakePostMirrorSender):
        async def copy_post(self, event, *, target_chat_id, target_thread_id):
            calls.append("send")
            return await super().copy_post(event, target_chat_id=target_chat_id, target_thread_id=target_thread_id)

    worker = PostMirrorOutboxDeliveryWorker(
        state=repo,
        client=FakeClient(),
        post_mirror_sender=Sender(),
        online_gate=OnlineGate(),
        now=lambda: 1000,
    )

    assert await worker.drain_once() == 1
    assert calls == ["load", "send"]
    assert [request.__class__.__name__ for request in requests] == []


def test_post_mirror_outbox_online_delivery_window_defaults_to_five_minutes():
    assert POST_MIRROR_OUTBOX_ONLINE_DELIVERY_WINDOW_SECONDS == 300


def test_post_mirror_outbox_preferred_delivery_window_defaults_to_two_minutes():
    assert POST_MIRROR_OUTBOX_PREFERRED_DELIVERY_WINDOW_SECONDS == 120


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

    client = FakeClient()
    total = await sync_reaction_folders(client, repo)

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
    assert client.requests == [
        "GetDialogFiltersRequest",
        "UpdateStatusRequest",
        "UpdateStatusRequest",
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
    assert repo.list_post_mirror_folder_sources(8) == [
        {"chat_id": utils.get_peer_id(megagroup), "title": "Group", "kind": "group"},
        {"chat_id": utils.get_peer_id(channel), "title": "News", "kind": "channel"},
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


async def test_run_user_client_syncs_post_mirror_topic_title_on_new_source_post(tmp_path, monkeypatch):
    settings = Settings(
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_session=str(tmp_path / "session"),
        manager_bot_token="token",
        owner_id=10,
        database_path=tmp_path / "assistant.sqlite3",
    )
    repo = SQLiteAssistantRepository(settings.database_path)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100123, "Old Channel", "channel")
    repo.set_post_mirror_source_topic(-100123, 77)
    rename_calls = []

    class FakeClient:
        connected = True

        def __init__(self):
            self.handlers = []

        def on(self, event):
            def decorator(handler):
                self.handlers.append(handler)
                return handler

            return decorator

        async def start(self):
            return None

        async def get_me(self):
            return type("Me", (), {"premium": True})()

        async def __call__(self, request):
            return SimpleNamespace(
                authorizations=[
                    SimpleNamespace(current=True, date_active=datetime.now(timezone.utc)),
                    SimpleNamespace(current=False, date_active=datetime.now(timezone.utc)),
                ]
            )

        async def run_until_disconnected(self):
            event = type(
                "Event",
                (),
                {
                    "chat_id": -100123,
                    "sender_id": None,
                    "is_private": False,
                    "is_group": False,
                    "is_channel": True,
                    "chat": type("Chat", (), {"title": "New Channel"})(),
                    "message": type(
                        "Message",
                        (),
                        {"id": 55, "out": False, "voice": False, "video_note": False, "media": None},
                    )(),
                },
            )()
            await self.handlers[0](event)
            raise RuntimeError("stop")

        def is_connected(self):
            return self.connected

        async def disconnect(self):
            self.connected = False

    class FakeForumTopicManager:
        def __init__(self, client):
            self.client = client

        async def rename_topic(self, target_chat_id, topic_id, title):
            rename_calls.append((target_chat_id, topic_id, title))

    async def forbidden_sync(*args, **kwargs):
        raise AssertionError("catalogs must refresh only on explicit actions")

    monkeypatch.setattr("telepath.user_client.TelethonForumTopicManager", FakeForumTopicManager)
    monkeypatch.setattr("telepath.user_client.sync_chat_catalog", forbidden_sync)
    monkeypatch.setattr("telepath.user_client.sync_group_catalog", forbidden_sync)
    monkeypatch.setattr("telepath.user_client.sync_reaction_folders", forbidden_sync)

    with pytest.raises(RuntimeError, match="stop"):
        await run_user_client(settings, client=FakeClient(), state=repo)

    assert rename_calls == [(-100900, 77, "New Channel")]
    assert repo.get_post_mirror_source_settings(-100123).title == "New Channel"


async def test_run_user_client_enqueues_post_mirror_before_topic_title_rename(tmp_path, monkeypatch):
    settings = Settings(
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_session=str(tmp_path / "session"),
        manager_bot_token="token",
        owner_id=10,
        database_path=tmp_path / "assistant.sqlite3",
    )
    repo = SQLiteAssistantRepository(settings.database_path)
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100123, "Old Channel", "channel")
    repo.set_post_mirror_source_topic(-100123, 77)
    repo.set_post_mirror_source_enabled(-100123, True)
    order = []

    class FakeClient:
        connected = True

        def __init__(self):
            self.handlers = []

        def on(self, event):
            def decorator(handler):
                self.handlers.append(handler)
                return handler

            return decorator

        async def start(self):
            return None

        async def get_me(self):
            return type("Me", (), {"premium": True})()

        async def __call__(self, request):
            return SimpleNamespace(
                authorizations=[
                    SimpleNamespace(current=True, date_active=datetime.now(timezone.utc)),
                    SimpleNamespace(current=False, date_active=datetime.now(timezone.utc)),
                ]
            )

        async def run_until_disconnected(self):
            event = type(
                "Event",
                (),
                {
                    "chat_id": -100123,
                    "sender_id": None,
                    "is_private": False,
                    "is_group": False,
                    "is_channel": True,
                    "chat": type("Chat", (), {"title": "New Channel"})(),
                    "message": type(
                        "Message",
                        (),
                        {
                            "id": 55,
                            "out": False,
                            "voice": False,
                            "video_note": False,
                            "media": None,
                            "action": None,
                            "grouped_id": None,
                        },
                    )(),
                },
            )()
            await self.handlers[0](event)
            raise RuntimeError("stop")

        def is_connected(self):
            return self.connected

        async def disconnect(self):
            self.connected = False

    class FakeForumTopicManager:
        def __init__(self, client):
            self.client = client

        async def rename_topic(self, target_chat_id, topic_id, title):
            order.append("rename")

    class FakePostMirrorQueueWorker:
        def __init__(self, handler, **kwargs):
            self.handler = handler

        def submit(self, event):
            order.append("submit")
            return True

        async def run(self):
            await asyncio.Future()

    async def forbidden_sync(*args, **kwargs):
        raise AssertionError("catalogs must refresh only on explicit actions")

    monkeypatch.setattr("telepath.user_client.TelethonForumTopicManager", FakeForumTopicManager)
    monkeypatch.setattr("telepath.user_client.PostMirrorQueueWorker", FakePostMirrorQueueWorker)
    monkeypatch.setattr("telepath.user_client.sync_chat_catalog", forbidden_sync)
    monkeypatch.setattr("telepath.user_client.sync_group_catalog", forbidden_sync)
    monkeypatch.setattr("telepath.user_client.sync_reaction_folders", forbidden_sync)

    with pytest.raises(RuntimeError, match="stop"):
        await run_user_client(settings, client=FakeClient(), state=repo)

    assert order == ["submit", "rename"]


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
    requests = []

    class CapturingClient:
        async def __call__(self, request):
            requests.append(request)
            if hasattr(request, "reaction"):
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
    assert [request.__class__.__name__ for request in requests] == [
        "SendReactionRequest",
        "UpdateStatusRequest",
    ]
    assert requests[1].offline is True


async def test_channel_reaction_sender_excludes_paid_available_reactions():
    class FakeClient:
        def __init__(self):
            self.requests = []

        async def get_input_entity(self, chat_id):
            return object()

        async def __call__(self, request):
            self.requests.append(request)
            if request.__class__.__name__ == "UpdateStatusRequest":
                return None
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

    client = FakeClient()
    sender = TelethonChannelReactionSender(client, send_delay_seconds=0)

    reactions = await sender.available_reactions(-100123)

    assert [(reaction.kind, reaction.emoji, reaction.category) for reaction in reactions] == [
        ("emoji", "👍", "positive"),
        ("custom", "1234567890123456789", "neutral"),
    ]
    assert [request.__class__.__name__ for request in client.requests] == [
        "UpdateStatusRequest",
        "GetFullChannelRequest",
        "UpdateStatusRequest",
    ]
    assert client.requests[0].offline is True
    assert client.requests[2].offline is True


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
