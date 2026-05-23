import logging

import pytest
from telethon import types

from telepath.user_client import (
    PrivateChatHistoryGate,
    build_voice_message_event,
    dispatch_voice_message,
    get_message_duration_seconds,
    is_private_bot_dialog,
    is_transcribable_message,
    remember_group_from_event,
    sync_group_catalog,
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
    message = FakeMessage()
    chat = None

    async def get_chat(self):
        return self.chat or type("Chat", (), {"title": "Known Group"})()


class FakeGroupRepository:
    def __init__(self):
        self.groups = []

    def upsert_known_group(self, chat_id, title, last_seen_at=None):
        self.groups.append((chat_id, title, last_seen_at))


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
