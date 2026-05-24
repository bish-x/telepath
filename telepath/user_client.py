from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from telepath.config import Settings, load_settings
from telepath.features.base import FeatureRegistry
from telepath.features.voice_transcription import (
    VoiceMessageEvent,
    VoiceTranscriptionUnavailableError,
    VoiceTooLongError,
    VoiceTranscriptionFeature,
)
from telepath.llm import build_polisher
from telepath.profanity import find_profanity_spans
from telepath.runtime import AssistantContext
from telepath.session_paths import ensure_session_parent
from telepath.storage import SQLiteAssistantRepository


logger = logging.getLogger(__name__)


class TelethonTranscriber:
    def __init__(self, client: Any, *, update_timeout_seconds: float = 60.0):
        self.client = client
        self.update_timeout_seconds = update_timeout_seconds

    async def transcribe(self, chat_id: int, message_id: int) -> str:
        from telethon import errors, events, functions, types

        loop = asyncio.get_running_loop()
        final_text: asyncio.Future[str] = loop.create_future()
        target_transcription_id: int | None = None
        latest_text = ""
        buffered_updates: list[Any] = []

        def matches(update: Any) -> bool:
            return (
                isinstance(update, types.UpdateTranscribedAudio)
                and update.msg_id == message_id
                and target_transcription_id is not None
                and update.transcription_id == target_transcription_id
            )

        def apply_update(update: Any) -> None:
            nonlocal latest_text
            if not matches(update):
                return
            if update.text:
                latest_text = update.text
            if not getattr(update, "pending", False) and not final_text.done():
                final_text.set_result(update.text or latest_text)

        async def handle_raw_update(update: Any) -> None:
            if target_transcription_id is None:
                buffered_updates.append(update)
                return
            apply_update(update)

        self.client.add_event_handler(handle_raw_update, events.Raw)
        peer = await self.client.get_input_entity(chat_id)
        try:
            try:
                result = await self.client(functions.messages.TranscribeAudioRequest(peer=peer, msg_id=message_id))
            except errors.BadRequestError as error:
                message = getattr(error, "message", "")
                if message == "MSG_VOICE_TOO_LONG":
                    raise VoiceTooLongError from error
                if message == "TRANSCRIPTION_FAILED":
                    raise VoiceTranscriptionUnavailableError from error
                raise
            latest_text = getattr(result, "text", "") or ""
            target_transcription_id = getattr(result, "transcription_id", None)
            if not getattr(result, "pending", False):
                return latest_text

            for update in buffered_updates:
                apply_update(update)
            if final_text.done():
                return final_text.result()
            try:
                return await asyncio.wait_for(final_text, timeout=self.update_timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning(
                    "voice_transcribe_update_timeout chat_id=%s message_id=%s "
                    "timeout_seconds=%s latest_text_chars=%d transcription_id=%s",
                    chat_id,
                    message_id,
                    self.update_timeout_seconds,
                    len(latest_text),
                    target_transcription_id,
                )
                return latest_text
        finally:
            self.client.remove_event_handler(handle_raw_update, events.Raw)


class PrivateChatHistoryGate:
    def __init__(
        self,
        client: Any,
        repository: Any,
        *,
        denied_recheck_seconds: int = 3600,
        history_throttle_seconds: float = 5.0,
        monotonic: Any = time.monotonic,
        sleep: Any = asyncio.sleep,
        now: Any = time.time,
    ):
        self.client = client
        self.repository = repository
        self.denied_recheck_seconds = denied_recheck_seconds
        self.history_throttle_seconds = history_throttle_seconds
        self.monotonic = monotonic
        self.sleep = sleep
        self.now = now
        self._history_fetch_lock = asyncio.Lock()
        self._last_history_fetch_at: float | None = None

    async def has_enough_messages(self, chat_id: int, minimum_messages: int) -> bool:
        cached = self.repository.get_private_chat_message_gate(chat_id)
        if cached is not None:
            if cached["is_allowed"]:
                return True
            checked_at = int(cached.get("checked_at", 0))
            if checked_at + self.denied_recheck_seconds > int(self.now()):
                return False

        count = await self._throttled_count_visible_messages(chat_id, minimum_messages)
        is_allowed = count >= minimum_messages
        self.repository.save_private_chat_message_gate(chat_id, count, is_allowed)
        return is_allowed

    async def _throttled_count_visible_messages(self, chat_id: int, minimum_messages: int) -> int:
        async with self._history_fetch_lock:
            await self._wait_for_history_fetch_slot(chat_id)
            return await self._count_visible_messages(chat_id, minimum_messages)

    async def _wait_for_history_fetch_slot(self, chat_id: int) -> None:
        if self.history_throttle_seconds <= 0:
            self._last_history_fetch_at = self.monotonic()
            return

        current = self.monotonic()
        if self._last_history_fetch_at is not None:
            delay = self.history_throttle_seconds - (current - self._last_history_fetch_at)
            if delay > 0:
                logger.info(
                    "private_chat_history_check_throttled chat_id=%s delay_seconds=%.3f",
                    chat_id,
                    delay,
                )
                await self.sleep(delay)
        self._last_history_fetch_at = self.monotonic()

    # Multiplier for how many raw messages to pull when counting *visible*
    # (non-service) ones. Telegram returns service messages — joined, pinned,
    # photo changed, etc. — inside the same iter_messages stream, and Telethon
    # counts them against the `limit`. If a chat has, say, 97 plain + 3 service
    # messages and we ask for limit=100, we'd cap at 97 plain and falsely
    # reject the chat. The multiplier gives enough headroom to find 100 plain
    # messages even when service messages are interleaved.
    _visible_fetch_multiplier = 3

    async def _count_visible_messages(self, chat_id: int, minimum_messages: int) -> int:
        count = 0
        fetch_limit = max(minimum_messages, minimum_messages * self._visible_fetch_multiplier)
        async for message in self.client.iter_messages(chat_id, limit=fetch_limit):
            if getattr(message, "action", None) is not None:
                continue
            count += 1
            if count >= minimum_messages:
                break
        return count


class TelethonReplies:
    fallback_emoji = "⭐"

    def __init__(self, client: Any, *, custom_emoji_id: str | None = None):
        self.client = client
        self.custom_emoji_id = custom_emoji_id

    async def reply(self, chat_id: int, message_id: int, text: str, *, decorate: bool = False) -> None:
        from telethon.tl.types import MessageEntityBlockquote, MessageEntityItalic

        if not decorate or not self.custom_emoji_id:
            entities = [
                *transcription_quote_entities(text, MessageEntityBlockquote),
                *profanity_italic_entities(text, MessageEntityItalic),
            ]
            if entities:
                await self.client.send_message(
                    chat_id,
                    text,
                    reply_to=message_id,
                    formatting_entities=entities,
                    parse_mode=None,
                )
                return
            await self.client.send_message(chat_id, text, reply_to=message_id)
            return

        from telethon.tl.types import MessageEntityCustomEmoji

        decorated_text = f"{self.fallback_emoji}\n{text}\n{self.fallback_emoji}"
        suffix_offset = utf16_len(f"{self.fallback_emoji}\n{text}\n")
        text_prefix = f"{self.fallback_emoji}\n"
        entities = [
            MessageEntityCustomEmoji(offset=0, length=utf16_len(self.fallback_emoji), document_id=int(self.custom_emoji_id)),
            MessageEntityCustomEmoji(
                offset=suffix_offset,
                length=utf16_len(self.fallback_emoji),
                document_id=int(self.custom_emoji_id),
            ),
            *transcription_quote_entities(text, MessageEntityBlockquote, utf16_offset=utf16_len(text_prefix)),
            *profanity_italic_entities(text, MessageEntityItalic, utf16_offset=utf16_len(text_prefix)),
        ]
        await self.client.send_message(
            chat_id,
            decorated_text,
            reply_to=message_id,
            formatting_entities=entities,
            parse_mode=None,
        )


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def transcription_quote_entities(text: str, entity_type: Any, utf16_offset: int = 0) -> list[Any]:
    if not text:
        return []
    return [entity_type(offset=utf16_offset, length=utf16_len(text), collapsed=True)]


def profanity_italic_entities(text: str, entity_type: Any, utf16_offset: int = 0) -> list[Any]:
    entities = []
    for span in find_profanity_spans(text):
        offset = utf16_offset + utf16_len(text[: span.start])
        length = utf16_len(text[span.start : span.end])
        entities.append(entity_type(offset=offset, length=length))
    return entities


def _entity_is_bot(entity: Any) -> bool:
    return bool(getattr(entity, "bot", False))


async def is_private_bot_dialog(event: Any) -> bool:
    if not getattr(event, "is_private", False):
        return False

    for attribute_name in ("chat", "sender"):
        if _entity_is_bot(getattr(event, attribute_name, None)):
            return True

    for getter_name in ("get_chat", "get_sender"):
        getter = getattr(event, getter_name, None)
        if not callable(getter):
            continue
        try:
            entity = await getter()
        except Exception:
            continue
        if _entity_is_bot(entity):
            return True
    return False


def build_voice_message_event(event: Any, *, is_private_bot: bool = False) -> VoiceMessageEvent:
    message = event.message
    return VoiceMessageEvent(
        chat_id=int(event.chat_id),
        message_id=int(message.id),
        sender_id=int(event.sender_id) if event.sender_id is not None else None,
        is_outgoing=bool(getattr(message, "out", False)),
        is_private=bool(getattr(event, "is_private", False)),
        is_group=bool(getattr(event, "is_group", False)),
        duration_seconds=get_message_duration_seconds(message),
        is_private_bot=is_private_bot,
        audio_fingerprint=voice_message_fingerprint(message),
    )


async def remember_group_from_event(event: Any, repository: Any) -> None:
    if not getattr(event, "is_group", False):
        return
    title = None
    try:
        chat = await event.get_chat()
    except Exception:
        chat = None
    if chat is not None:
        title = getattr(chat, "title", None)
    repository.upsert_known_group(int(event.chat_id), title, last_seen_at=int(time.time()))


async def sync_group_catalog(client: Any, repository: Any) -> None:
    base_seen_at = int(time.time())
    index = 0
    async for dialog in client.iter_dialogs():
        if not getattr(dialog, "is_group", False):
            continue
        repository.upsert_known_group(
            int(dialog.id),
            getattr(dialog, "title", None),
            last_seen_at=base_seen_at - index,
        )
        index += 1


def get_message_duration_seconds(message: Any) -> int | None:
    file = getattr(message, "file", None)
    duration = getattr(file, "duration", None)
    if duration is not None:
        return int(duration)

    direct_duration = getattr(message, "duration", None)
    if direct_duration is not None:
        return int(direct_duration)

    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    for attribute in getattr(document, "attributes", []) or []:
        duration = getattr(attribute, "duration", None)
        if duration is not None:
            return int(duration)
    return None


def is_transcribable_message(message: Any) -> bool:
    return bool(getattr(message, "voice", False) or getattr(message, "video_note", False))


def voice_message_fingerprint(message: Any) -> str | None:
    if not is_transcribable_message(message):
        return None
    media = getattr(message, "media", None)
    document = getattr(media, "document", None) or getattr(message, "document", None)
    document_id = getattr(document, "id", None)
    if document_id is None:
        return None
    kind = "video_note" if getattr(message, "video_note", False) else "voice"
    return f"telegram-document:{kind}:{int(document_id)}"


async def dispatch_voice_message(event: Any, registry: FeatureRegistry, context: AssistantContext) -> str | None:
    message = event.message
    if not is_transcribable_message(message):
        return None

    voice_event = build_voice_message_event(event, is_private_bot=await is_private_bot_dialog(event))
    logger.info(
        "voice_received chat_id=%s message_id=%s is_private=%s is_private_bot=%s is_group=%s duration_seconds=%s",
        voice_event.chat_id,
        voice_event.message_id,
        voice_event.is_private,
        voice_event.is_private_bot,
        voice_event.is_group,
        voice_event.duration_seconds,
    )
    try:
        result = await registry.dispatch(voice_event, context)
    except Exception:
        logger.exception(
            "voice_dispatch_failed chat_id=%s message_id=%s",
            voice_event.chat_id,
            voice_event.message_id,
        )
        return "error"
    logger.info(
        "voice_dispatch_result chat_id=%s message_id=%s result=%s",
        voice_event.chat_id,
        voice_event.message_id,
        result,
    )
    return str(result)


DEFAULT_VOICE_QUEUE_MAXSIZE = 64


class VoiceQueueWorker:
    """Bounded FIFO queue + single consumer so voice messages are processed one
    at a time. Prevents parallel hits on Telegram's transcription endpoint and
    on the configured LLM provider, both of which rate-limit aggressively."""

    def __init__(
        self,
        handler: Callable[[Any], Awaitable[Any]],
        *,
        maxsize: int = DEFAULT_VOICE_QUEUE_MAXSIZE,
        logger_: logging.Logger | None = None,
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be a positive integer")
        self._handler = handler
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._logger = logger_ or logger
        self.dropped_count = 0
        self.processed_count = 0

    @property
    def maxsize(self) -> int:
        return self._queue.maxsize

    def qsize(self) -> int:
        return self._queue.qsize()

    def submit(self, event: Any) -> bool:
        """Enqueue an event for the consumer. Returns False (and drops the event)
        when the queue is full so the Telethon handler never blocks the event
        loop with back-pressure."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_count += 1
            self._logger.warning(
                "voice_dropped_queue_full chat_id=%s message_id=%s queue_size=%s",
                getattr(event, "chat_id", "?"),
                getattr(getattr(event, "message", None), "id", "?"),
                self._queue.maxsize,
            )
            return False
        return True

    async def run(self) -> None:
        """Consume events one at a time. A failure in a single event is logged
        and the loop continues. `asyncio.CancelledError` (a BaseException
        subclass in 3.8+) bypasses the broad except and propagates cleanly,
        with `finally` still calling task_done()."""
        while True:
            event = await self._queue.get()
            try:
                await self._handler(event)
                self.processed_count += 1
            except Exception:
                self._logger.exception(
                    "voice_consumer_handler_failed chat_id=%s",
                    getattr(event, "chat_id", "?"),
                )
            finally:
                self._queue.task_done()


async def run_user_client(settings: Settings) -> None:  # pragma: no cover - integration only
    from telethon import TelegramClient, events

    ensure_session_parent(settings.telegram_session)
    client = TelegramClient(settings.telegram_session, settings.telegram_api_id, settings.telegram_api_hash)
    state = SQLiteAssistantRepository(settings.database_path)
    context = AssistantContext(
        blacklist=state,
        group_whitelist=state,
        transcriber=TelethonTranscriber(client),
        polisher=build_polisher(settings),
        replies=TelethonReplies(client, custom_emoji_id=settings.transcription_decoration_custom_emoji_id),
        settings=state,
        processed=state,
        private_chat_gate=PrivateChatHistoryGate(
            client,
            state,
            history_throttle_seconds=settings.private_history_throttle_seconds,
        ),
    )
    registry = FeatureRegistry([VoiceTranscriptionFeature()])

    async def handle_voice(event: Any) -> None:
        await dispatch_voice_message(event, registry, context)

    worker = VoiceQueueWorker(handler=handle_voice)
    consumer_task = asyncio.create_task(worker.run(), name="voice-queue-consumer")

    @client.on(events.NewMessage())
    async def on_message(event: Any) -> None:
        await remember_group_from_event(event, state)
        # Only voice/video-note events should occupy the queue — text messages
        # would block voice processing for no reason.
        if is_transcribable_message(event.message):
            worker.submit(event)

    try:
        await client.start()
        await sync_group_catalog(client, state)
        await client.run_until_disconnected()
    finally:
        consumer_task.cancel()
        try:
            await consumer_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - graceful shutdown
            pass


def main() -> None:  # pragma: no cover - integration only
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_user_client(load_settings()))


if __name__ == "__main__":
    main()
