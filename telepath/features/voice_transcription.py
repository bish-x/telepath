from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceMessageEvent:
    chat_id: int
    message_id: int
    sender_id: int | None
    is_outgoing: bool
    is_private: bool
    is_group: bool
    duration_seconds: int | None
    is_private_bot: bool = False
    audio_fingerprint: str | None = None


class BlacklistPort(Protocol):
    def is_blocked(self, chat_id: int) -> bool: ...


class GroupWhitelistPort(Protocol):
    def is_group_allowed(self, chat_id: int) -> bool: ...


class TranscriberPort(Protocol):
    async def transcribe(self, chat_id: int, message_id: int) -> str: ...


class VoiceTooLongError(Exception):
    pass


class VoiceTranscriptionUnavailableError(Exception):
    pass


class TextPolisherPort(Protocol):
    def polish(self, text: str, prompt: str | None = None) -> str: ...


class ReplyPort(Protocol):
    async def reply(self, chat_id: int, message_id: int, text: str, *, decorate: bool = False) -> None: ...


class SettingsPort(Protocol):
    def is_feature_enabled(self, feature: str) -> bool: ...
    def get_text_polish_prompt(self) -> str: ...
    def is_transcription_decoration_enabled(self) -> bool: ...


class ProcessedMessagesPort(Protocol):
    def is_processed(self, chat_id: int, message_id: int, feature: str) -> bool: ...
    def mark_processed(self, chat_id: int, message_id: int, feature: str) -> bool: ...


class PrivateChatGatePort(Protocol):
    async def has_enough_messages(self, chat_id: int, minimum_messages: int) -> bool: ...


class VoiceFeatureContext(Protocol):
    blacklist: BlacklistPort
    group_whitelist: GroupWhitelistPort
    transcriber: TranscriberPort
    polisher: TextPolisherPort
    replies: ReplyPort
    settings: SettingsPort
    processed: ProcessedMessagesPort
    private_chat_gate: PrivateChatGatePort


@dataclass(frozen=True)
class PreparedTranscription:
    status: str
    text: str | None = None
    mark_processed: bool = False


@dataclass(frozen=True)
class CachedTranscription:
    text: str
    expires_at: float


class TranscriptionCoordinator:
    def __init__(self, *, ttl_seconds: float = 3600.0, now: Callable[[], float] | None = None) -> None:
        self._lock = asyncio.Lock()
        self._in_flight: dict[str, asyncio.Task[PreparedTranscription]] = {}
        self._cache: dict[str, CachedTranscription] = {}
        self._ttl_seconds = ttl_seconds
        self._now = now or time.monotonic

    async def prepare(
        self,
        audio_fingerprint: str | None,
        factory: Callable[[], Awaitable[PreparedTranscription]],
    ) -> PreparedTranscription:
        if audio_fingerprint is None:
            return await factory()

        async with self._lock:
            cached = self._get_cached_locked(audio_fingerprint)
            if cached is not None:
                return cached
            task = self._in_flight.get(audio_fingerprint)
            if task is None:
                task = asyncio.create_task(factory())
                self._in_flight[audio_fingerprint] = task

        try:
            result = await task
            if result.text is not None:
                async with self._lock:
                    self._cache[audio_fingerprint] = CachedTranscription(
                        text=result.text,
                        expires_at=self._now() + self._ttl_seconds,
                    )
            return result
        finally:
            if task.done():
                async with self._lock:
                    if self._in_flight.get(audio_fingerprint) is task:
                        self._in_flight.pop(audio_fingerprint, None)

    def _get_cached_locked(self, audio_fingerprint: str) -> PreparedTranscription | None:
        cached = self._cache.get(audio_fingerprint)
        if cached is None:
            return None
        if cached.expires_at <= self._now():
            self._cache.pop(audio_fingerprint, None)
            return None
        return PreparedTranscription("voice_transcribed_from_cache", text=cached.text)


def remove_terminal_paragraph_periods(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        stripped = line.rstrip()
        trailing = line[len(stripped) :]
        if stripped.endswith(".") and not stripped.endswith("..."):
            stripped = stripped[:-1]
        lines.append(f"{stripped}{trailing}")
    return "\n".join(lines)


class VoiceTranscriptionFeature:
    name = "voice_transcription"
    max_duration_seconds = 300
    min_private_chat_messages = 100

    def __init__(self, coordinator: TranscriptionCoordinator | None = None) -> None:
        self.coordinator = coordinator or TranscriptionCoordinator()

    def can_handle(self, event: object) -> bool:
        return isinstance(event, VoiceMessageEvent)

    async def handle(self, event: VoiceMessageEvent, context: VoiceFeatureContext) -> str:
        if not context.settings.is_feature_enabled(self.name):
            return "feature_disabled"
        if event.is_private:
            if event.is_private_bot:
                return "private_bot_chat_skipped"
            if context.blacklist.is_blocked(event.chat_id):
                return "chat_blocked"
            if not await context.private_chat_gate.has_enough_messages(event.chat_id, self.min_private_chat_messages):
                return "private_chat_too_new"
        elif event.is_group:
            if not context.group_whitelist.is_group_allowed(event.chat_id):
                return "group_not_allowed"
        else:
            return "unsupported_chat_type_skipped"
        if context.processed.is_processed(event.chat_id, event.message_id, self.name):
            return "already_processed"
        if event.duration_seconds is not None and event.duration_seconds > self.max_duration_seconds:
            context.processed.mark_processed(event.chat_id, event.message_id, self.name)
            return "voice_too_long"

        prompt = context.settings.get_text_polish_prompt()
        prepared = await self.coordinator.prepare(
            event.audio_fingerprint,
            lambda: self._prepare_transcription(event, context, prompt),
        )
        if prepared.text is None:
            if prepared.mark_processed:
                context.processed.mark_processed(event.chat_id, event.message_id, self.name)
            return prepared.status

        await context.replies.reply(
            event.chat_id,
            event.message_id,
            prepared.text,
            decorate=context.settings.is_transcription_decoration_enabled(),
        )
        context.processed.mark_processed(event.chat_id, event.message_id, self.name)
        return prepared.status

    async def _prepare_transcription(
        self,
        event: VoiceMessageEvent,
        context: VoiceFeatureContext,
        prompt: str,
    ) -> PreparedTranscription:
        try:
            raw_text = await context.transcriber.transcribe(event.chat_id, event.message_id)
        except VoiceTooLongError:
            return PreparedTranscription("voice_too_long", mark_processed=True)
        except VoiceTranscriptionUnavailableError:
            return PreparedTranscription("transcription_unavailable", mark_processed=True)
        if not raw_text.strip():
            return PreparedTranscription("empty_transcription", mark_processed=True)
        try:
            polished_text = remove_terminal_paragraph_periods(context.polisher.polish(raw_text, prompt=prompt))
        except Exception as exc:
            logger.warning(
                "voice_polish_failed chat_id=%s message_id=%s error=%s falling back to raw transcription",
                event.chat_id,
                event.message_id,
                exc,
            )
            fallback_text = remove_terminal_paragraph_periods(raw_text.strip())
            if not fallback_text:
                return PreparedTranscription("empty_transcription", mark_processed=True)
            return PreparedTranscription("voice_transcribed_unpolished", text=fallback_text)
        if not polished_text:
            return PreparedTranscription("empty_transcription", mark_processed=True)
        return PreparedTranscription("voice_transcribed", text=polished_text)
