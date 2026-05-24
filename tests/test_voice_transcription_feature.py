import asyncio
from dataclasses import dataclass

import pytest

from telepath.features.voice_transcription import (
    TranscriptionCoordinator,
    VoiceTranscriptionUnavailableError,
    VoiceMessageEvent,
    VoiceTooLongError,
    VoiceTranscriptionFeature,
)


class FakeBlacklist:
    def __init__(self, blocked):
        self.blocked = blocked

    def is_blocked(self, chat_id):
        return chat_id in self.blocked


class FakeGroupWhitelist:
    def __init__(self, allowed):
        self.allowed = allowed

    def is_group_allowed(self, chat_id):
        return chat_id in self.allowed


class FakeTranscriber:
    def __init__(self, error=None, transcript="привет мир", delay_seconds=0):
        self.calls = []
        self.error = error
        self.transcript = transcript
        self.delay_seconds = delay_seconds

    async def transcribe(self, chat_id, message_id):
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        self.calls.append((chat_id, message_id))
        if self.error:
            raise self.error
        return self.transcript


class FakePolisher:
    def __init__(self, polished_text="Привет, мир.", error=None):
        self.calls = []
        self.polished_text = polished_text
        self.error = error

    def polish(self, text, prompt=None):
        self.calls.append((text, prompt))
        if self.error is not None:
            raise self.error
        return self.polished_text


class FakeReplies:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    async def reply(self, chat_id, message_id, text, *, decorate=False):
        if self.error:
            raise self.error
        self.sent.append((chat_id, message_id, text, decorate))


class FakeSettings:
    def __init__(self, enabled=True, prompt="runtime prompt", decoration_enabled=False):
        self.enabled = enabled
        self.prompt = prompt
        self.decoration_enabled = decoration_enabled

    def is_feature_enabled(self, name):
        return self.enabled

    def get_text_polish_prompt(self):
        return self.prompt

    def is_transcription_decoration_enabled(self):
        return self.decoration_enabled


class FakeProcessed:
    def __init__(self, already_processed=False):
        self.already_processed = already_processed
        self.is_processed_calls = []
        self.mark_processed_calls = []

    def is_processed(self, chat_id, message_id, feature):
        self.is_processed_calls.append((chat_id, message_id, feature))
        return self.already_processed

    def mark_processed(self, chat_id, message_id, feature):
        self.mark_processed_calls.append((chat_id, message_id, feature))
        return True


class FakePrivateChatGate:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.calls = []

    async def has_enough_messages(self, chat_id, minimum_messages):
        self.calls.append((chat_id, minimum_messages))
        return self.allowed


@dataclass
class Context:
    blacklist: FakeBlacklist
    group_whitelist: FakeGroupWhitelist
    transcriber: FakeTranscriber
    polisher: FakePolisher
    replies: FakeReplies
    settings: FakeSettings
    processed: FakeProcessed
    private_chat_gate: FakePrivateChatGate


def make_context(
    *,
    blocked=None,
    groups=None,
    enabled=True,
    already_processed=False,
    reply_error=None,
    transcriber_error=None,
    transcriber_delay_seconds=0,
    polished_text="Привет, мир.",
    polisher_error=None,
    decoration_enabled=False,
    private_chat_allowed=True,
):
    return Context(
        FakeBlacklist(blocked or set()),
        FakeGroupWhitelist(groups or set()),
        FakeTranscriber(error=transcriber_error, delay_seconds=transcriber_delay_seconds),
        FakePolisher(polished_text=polished_text, error=polisher_error),
        FakeReplies(error=reply_error),
        FakeSettings(enabled=enabled, decoration_enabled=decoration_enabled),
        FakeProcessed(already_processed=already_processed),
        FakePrivateChatGate(private_chat_allowed),
    )


def voice_event(
    *,
    chat_id=100,
    message_id=50,
    is_private=True,
    is_private_bot=False,
    is_group=False,
    is_outgoing=False,
    duration_seconds=60,
    audio_fingerprint=None,
):
    return VoiceMessageEvent(
        chat_id=chat_id,
        message_id=message_id,
        sender_id=7,
        is_outgoing=is_outgoing,
        is_private=is_private,
        is_private_bot=is_private_bot,
        is_group=is_group,
        duration_seconds=duration_seconds,
        audio_fingerprint=audio_fingerprint,
    )


async def test_voice_feature_transcribes_polishes_and_replies_for_unblocked_private_chat():
    context = make_context()
    feature = VoiceTranscriptionFeature()
    event = voice_event()

    result = await feature.handle(event, context)

    assert result == "voice_transcribed"
    assert context.transcriber.calls == [(100, 50)]
    assert context.polisher.calls == [("привет мир", "runtime prompt")]
    assert context.replies.sent == [(100, 50, "Привет, мир", False)]
    assert context.processed.mark_processed_calls == [(100, 50, "voice_transcription")]


async def test_voice_feature_skips_blocked_and_non_private_messages():
    context = make_context(blocked={100})
    feature = VoiceTranscriptionFeature()

    assert await feature.handle(voice_event(chat_id=100), context) == "chat_blocked"
    assert await feature.handle(voice_event(chat_id=-100, message_id=51, is_private=False, is_group=False), context) == "unsupported_chat_type_skipped"
    assert context.transcriber.calls == []
    assert context.replies.sent == []


async def test_voice_feature_skips_private_bot_chats():
    context = make_context()
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(is_private_bot=True), context)

    assert result == "private_bot_chat_skipped"
    assert context.transcriber.calls == []
    assert context.polisher.calls == []
    assert context.replies.sent == []


async def test_voice_feature_skips_group_messages_unless_group_is_whitelisted():
    context = make_context(groups={-100200})
    feature = VoiceTranscriptionFeature()

    assert await feature.handle(voice_event(chat_id=-100100, is_private=False, is_group=True), context) == "group_not_allowed"

    result = await feature.handle(voice_event(chat_id=-100200, message_id=51, is_private=False, is_group=True), context)

    assert result == "voice_transcribed"
    assert context.transcriber.calls == [(-100200, 51)]
    assert context.replies.sent == [(-100200, 51, "Привет, мир", False)]


async def test_voice_feature_processes_outgoing_private_messages():
    context = make_context()
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(message_id=52, is_outgoing=True), context)

    assert result == "voice_transcribed"
    assert context.transcriber.calls == [(100, 52)]
    assert context.replies.sent == [(100, 52, "Привет, мир", False)]


async def test_voice_feature_skips_private_chats_with_too_few_messages():
    context = make_context(private_chat_allowed=False)
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(message_id=52), context)

    assert result == "private_chat_too_new"
    assert context.private_chat_gate.calls == [(100, 100)]
    assert context.transcriber.calls == []
    assert context.polisher.calls == []
    assert context.replies.sent == []


async def test_voice_feature_does_not_check_private_history_for_groups():
    context = make_context(groups={-100200}, private_chat_allowed=False)
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(chat_id=-100200, message_id=53, is_private=False, is_group=True), context)

    assert result == "voice_transcribed"
    assert context.private_chat_gate.calls == []
    assert context.transcriber.calls == [(-100200, 53)]


async def test_voice_feature_replies_from_cached_transcription_for_duplicate_voice():
    context = make_context()
    feature = VoiceTranscriptionFeature()

    first = await feature.handle(voice_event(message_id=51, audio_fingerprint="telegram-document:voice:123"), context)

    result = await feature.handle(voice_event(message_id=52, audio_fingerprint="telegram-document:voice:123"), context)

    assert first == "voice_transcribed"
    assert result == "voice_transcribed_from_cache"
    assert context.transcriber.calls == [(100, 51)]
    assert context.polisher.calls == [("привет мир", "runtime prompt")]
    assert context.replies.sent == [
        (100, 51, "Привет, мир", False),
        (100, 52, "Привет, мир", False),
    ]


async def test_voice_feature_expires_cached_transcriptions_after_ttl():
    now = 1000.0

    def clock():
        return now

    context = make_context()
    feature = VoiceTranscriptionFeature(
        coordinator=TranscriptionCoordinator(ttl_seconds=3600, now=clock),
    )

    first = await feature.handle(voice_event(message_id=51, audio_fingerprint="telegram-document:voice:123"), context)
    now += 3601
    second = await feature.handle(voice_event(message_id=52, audio_fingerprint="telegram-document:voice:123"), context)

    assert first == "voice_transcribed"
    assert second == "voice_transcribed"
    assert context.transcriber.calls == [(100, 51), (100, 52)]
    assert context.polisher.calls == [
        ("привет мир", "runtime prompt"),
        ("привет мир", "runtime prompt"),
    ]


async def test_voice_feature_queues_duplicate_fingerprints_while_first_is_processing():
    context = make_context(transcriber_delay_seconds=0.01)
    feature = VoiceTranscriptionFeature()

    results = await asyncio.gather(
        feature.handle(voice_event(message_id=52, audio_fingerprint="telegram-document:voice:123"), context),
        feature.handle(voice_event(message_id=53, audio_fingerprint="telegram-document:voice:123"), context),
    )

    assert results == ["voice_transcribed", "voice_transcribed"]
    assert context.transcriber.calls == [(100, 52)]
    assert context.polisher.calls == [("привет мир", "runtime prompt")]
    assert context.replies.sent == [
        (100, 52, "Привет, мир", False),
        (100, 53, "Привет, мир", False),
    ]
    assert context.processed.mark_processed_calls == [
        (100, 52, "voice_transcription"),
        (100, 53, "voice_transcription"),
    ]


async def test_voice_feature_passes_realtime_decoration_flag_to_reply():
    context = make_context(decoration_enabled=True)
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(message_id=54), context)

    assert result == "voice_transcribed"
    assert context.replies.sent == [(100, 54, "Привет, мир", True)]


async def test_voice_feature_removes_terminal_periods_at_paragraph_ends_only():
    context = make_context(
        polished_text=(
            "Первый абзац.\n\n"
            "Второй абзац.\n"
            "Вопрос остается?\n"
            "Восклицание остается!\n"
            "Многоточие остается..."
        )
    )
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(message_id=53), context)

    assert result == "voice_transcribed"
    assert context.replies.sent == [
        (
            100,
            53,
            "Первый абзац\n\n"
            "Второй абзац\n"
            "Вопрос остается?\n"
            "Восклицание остается!\n"
            "Многоточие остается...",
            False,
        )
    ]


async def test_voice_feature_skips_when_transcription_is_disabled():
    context = make_context(enabled=False)
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(), context)

    assert result == "feature_disabled"
    assert context.transcriber.calls == []


async def test_voice_feature_skips_already_processed_messages():
    context = make_context(already_processed=True)
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(), context)

    assert result == "already_processed"
    assert context.transcriber.calls == []


async def test_voice_feature_skips_and_marks_processed_when_message_is_too_long():
    context = make_context()
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(duration_seconds=301), context)

    assert result == "voice_too_long"
    assert context.transcriber.calls == []
    assert context.polisher.calls == []
    assert context.replies.sent == []
    assert context.processed.mark_processed_calls == [(100, 50, "voice_transcription")]


async def test_voice_feature_allows_unknown_duration_to_fall_through_to_telegram():
    context = make_context()
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(duration_seconds=None), context)

    assert result == "voice_transcribed"
    assert context.transcriber.calls == [(100, 50)]


async def test_voice_feature_handles_server_too_long_error_like_duration_preflight():
    context = make_context(transcriber_error=VoiceTooLongError())
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(duration_seconds=None), context)

    assert result == "voice_too_long"
    assert context.transcriber.calls == [(100, 50)]
    assert context.polisher.calls == []
    assert context.replies.sent == []
    assert context.processed.mark_processed_calls == [(100, 50, "voice_transcription")]


async def test_voice_feature_silently_marks_processed_when_telegram_cannot_transcribe():
    context = make_context(transcriber_error=VoiceTranscriptionUnavailableError())
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(duration_seconds=None), context)

    assert result == "transcription_unavailable"
    assert context.transcriber.calls == [(100, 50)]
    assert context.polisher.calls == []
    assert context.replies.sent == []
    assert context.processed.mark_processed_calls == [(100, 50, "voice_transcription")]


async def test_voice_feature_silently_marks_processed_when_transcription_text_is_empty():
    context = make_context()
    context.transcriber.transcript = "   "
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(), context)

    assert result == "empty_transcription"
    assert context.polisher.calls == []
    assert context.replies.sent == []
    assert context.processed.mark_processed_calls == [(100, 50, "voice_transcription")]


async def test_voice_feature_does_not_mark_processed_when_reply_fails():
    context = make_context(reply_error=RuntimeError("send failed"))
    feature = VoiceTranscriptionFeature()

    with pytest.raises(RuntimeError, match="send failed"):
        await feature.handle(voice_event(), context)

    assert context.replies.sent == []
    assert context.processed.mark_processed_calls == []


def test_voice_feature_can_handle_only_voice_events():
    feature = VoiceTranscriptionFeature()
    assert feature.can_handle(voice_event()) is True
    assert feature.can_handle("not an event") is False
    assert feature.can_handle(object()) is False


async def test_voice_feature_marks_processed_when_polished_text_is_empty():
    context = make_context(polished_text="")
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(), context)

    assert result == "empty_transcription"
    assert context.replies.sent == []
    assert context.processed.mark_processed_calls == [(100, 50, "voice_transcription")]


async def test_voice_feature_falls_back_to_raw_text_when_polisher_fails():
    context = make_context(polisher_error=RuntimeError("copilot timed out"))
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(), context)

    assert result == "voice_transcribed_unpolished"
    assert context.transcriber.calls == [(100, 50)]
    assert context.polisher.calls == [("привет мир", "runtime prompt")]
    assert context.replies.sent == [(100, 50, "привет мир", False)]
    assert context.processed.mark_processed_calls == [(100, 50, "voice_transcription")]


async def test_voice_feature_marks_processed_when_polisher_fails_and_raw_is_blank():
    context = make_context(polisher_error=RuntimeError("copilot crashed"))
    context.transcriber.transcript = "   "
    feature = VoiceTranscriptionFeature()

    result = await feature.handle(voice_event(), context)

    # transcriber returns whitespace-only — short-circuited before polish even runs
    assert result == "empty_transcription"
    assert context.polisher.calls == []
    assert context.replies.sent == []
    assert context.processed.mark_processed_calls == [(100, 50, "voice_transcription")]
