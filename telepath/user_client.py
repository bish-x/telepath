from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import random
import shutil
import tempfile
import time
from collections import Counter, OrderedDict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from telethon import errors as telethon_errors

from telepath.config import Settings, load_settings
from telepath.features.base import FeatureRegistry
from telepath.features.channel_reactions import (
    DEFAULT_REACTION_EMOJIS,
    ChannelMessageEvent,
    ChannelReactionFeature,
    ReactionCandidate,
    ReactionSendResult,
    effective_max_reactions,
    fallback_reaction_candidates,
    order_reaction_candidates,
    reaction_candidate_key,
    reaction_category,
    select_from_ordered_reaction_candidates,
    sent_reaction_count as resolve_sent_reaction_count,
    sent_reaction_keys as resolve_sent_reaction_keys,
)
from telepath.features.post_mirroring import (
    PostMirrorEvent,
    PostMirrorFeature,
    PostMirrorQueuedDelivery,
    PostMirrorSendResult,
)
from telepath.features.voice_transcription import (
    VoiceTranscriptionPendingTimeoutError,
    VoiceMessageEvent,
    VoiceTranscriptionUnavailableError,
    VoiceTooLongError,
    VoiceTranscriptionFeature,
)
from telepath.llm import build_polisher
from telepath.presence import mark_current_session_offline, suppress_current_session_offline_updates
from telepath.profanity import find_profanity_spans
from telepath.runtime import AssistantContext
from telepath.session_paths import ensure_session_parent
from telepath.storage import SQLiteAssistantRepository


logger = logging.getLogger(__name__)

POST_MIRROR_FLOOD_WAIT_EXTRA_SECONDS = 5
_POST_MIRROR_FLOOD_WAIT_SLEEP: contextvars.ContextVar[
    Callable[[float], Awaitable[Any]] | None
] = contextvars.ContextVar("post_mirror_flood_wait_sleep", default=None)
_POST_MIRROR_REALTIME_YIELD: contextvars.ContextVar[
    Callable[[], Awaitable[Any]] | None
] = contextvars.ContextVar("post_mirror_realtime_yield", default=None)
_POST_MIRROR_INTERRUPTIBLE_HISTORY_AWAIT: contextvars.ContextVar[
    Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]] | None
] = contextvars.ContextVar("post_mirror_interruptible_history_await", default=None)


def post_mirror_flood_wait_delay_seconds(error: telethon_errors.FloodWaitError) -> int:
    return max(1, int(getattr(error, "seconds", 0) or 0)) + POST_MIRROR_FLOOD_WAIT_EXTRA_SECONDS


async def _yield_post_mirror_history_to_realtime_if_pending() -> None:
    callback = _POST_MIRROR_REALTIME_YIELD.get()
    if callback is not None:
        await callback()


class PostMirrorHistoryPreempted(Exception):
    pass


async def _await_post_mirror_history_interruptibly(factory: Callable[[], Awaitable[Any]]) -> Any:
    callback = _POST_MIRROR_INTERRUPTIBLE_HISTORY_AWAIT.get()
    if callback is None:
        return await factory()
    return await callback(factory)


async def _retry_post_mirror_history_preemptions(factory: Callable[[], Awaitable[Any]]) -> Any:
    while True:
        try:
            return await _await_post_mirror_history_interruptibly(factory)
        except PostMirrorHistoryPreempted:
            continue


class TelethonTranscriber:
    def __init__(
        self,
        client: Any,
        *,
        update_timeout_seconds: float = 60.0,
        empty_timeout_retries: int = 2,
        empty_timeout_retry_delay_seconds: float = 0.0,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        if empty_timeout_retries < 0:
            raise ValueError("empty_timeout_retries must be non-negative")
        self.client = client
        self.update_timeout_seconds = update_timeout_seconds
        self.empty_timeout_retries = empty_timeout_retries
        self.empty_timeout_retry_delay_seconds = empty_timeout_retry_delay_seconds
        self.sleep = sleep

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
        try:
            peer = await self.client.get_input_entity(chat_id)
        finally:
            await mark_current_session_offline(self.client)
        try:
            for attempt in range(self.empty_timeout_retries + 1):
                final_text = loop.create_future()
                try:
                    try:
                        result = await self.client(
                            functions.messages.TranscribeAudioRequest(peer=peer, msg_id=message_id)
                        )
                    finally:
                        await mark_current_session_offline(self.client)
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
                buffered_updates.clear()
                if final_text.done():
                    return final_text.result()
                try:
                    return await asyncio.wait_for(final_text, timeout=self.update_timeout_seconds)
                except asyncio.TimeoutError:
                    logger.warning(
                        "voice_transcribe_update_timeout chat_id=%s message_id=%s "
                        "timeout_seconds=%s latest_text_chars=%d transcription_id=%s "
                        "empty_timeout_retry=%s/%s",
                        chat_id,
                        message_id,
                        self.update_timeout_seconds,
                        len(latest_text),
                        target_transcription_id,
                        attempt,
                        self.empty_timeout_retries,
                    )
                    if latest_text:
                        return latest_text
                    if attempt >= self.empty_timeout_retries:
                        raise VoiceTranscriptionPendingTimeoutError from None
                    target_transcription_id = None
                    if self.empty_timeout_retry_delay_seconds > 0:
                        await self.sleep(self.empty_timeout_retry_delay_seconds)
            raise VoiceTranscriptionPendingTimeoutError
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
        try:
            async for message in self.client.iter_messages(chat_id, limit=fetch_limit):
                if getattr(message, "action", None) is not None:
                    continue
                count += 1
                if count >= minimum_messages:
                    break
        finally:
            await mark_current_session_offline(self.client)
        return count


class TelethonReplies:
    fallback_emoji = "⭐"

    def __init__(self, client: Any, *, custom_emoji_id: str | None = None):
        self.client = client
        self.custom_emoji_id = custom_emoji_id

    async def _send_message(self, *args: Any, **kwargs: Any) -> None:
        try:
            await self.client.send_message(*args, **kwargs)
        finally:
            await mark_current_session_offline(self.client)

    async def reply(self, chat_id: int, message_id: int, text: str, *, decorate: bool = False) -> None:
        from telethon.tl.types import MessageEntityBlockquote, MessageEntityItalic

        if not decorate or not self.custom_emoji_id:
            entities = [
                *transcription_quote_entities(text, MessageEntityBlockquote),
                *profanity_italic_entities(text, MessageEntityItalic),
            ]
            if entities:
                await self._send_message(
                    chat_id,
                    text,
                    reply_to=message_id,
                    formatting_entities=entities,
                    parse_mode=None,
                )
                return
            await self._send_message(chat_id, text, reply_to=message_id)
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
        await self._send_message(
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
    try:
        async for dialog in client.iter_dialogs():
            if not getattr(dialog, "is_group", False):
                continue
            repository.upsert_known_group(
                int(dialog.id),
                getattr(dialog, "title", None),
                last_seen_at=base_seen_at - index,
            )
            index += 1
    finally:
        await mark_current_session_offline(client)


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


def build_channel_message_event(event: Any) -> ChannelMessageEvent:
    message = event.message
    grouped_id = getattr(message, "grouped_id", None)
    return ChannelMessageEvent(
        chat_id=int(event.chat_id),
        message_id=int(message.id),
        is_channel=bool(getattr(event, "is_channel", False)),
        is_group=bool(getattr(event, "is_group", False)),
        grouped_id=int(grouped_id) if grouped_id is not None else None,
        message=message,
    )


def build_post_mirror_event(event: Any) -> PostMirrorEvent:
    messages = tuple(getattr(event, "messages", None) or (event.message,))
    first_message = messages[0]
    grouped_id = getattr(first_message, "grouped_id", None)
    return PostMirrorEvent(
        chat_id=int(event.chat_id),
        message_id=int(first_message.id),
        message_ids=tuple(int(message.id) for message in messages),
        is_channel=bool(getattr(event, "is_channel", False)),
        is_group=bool(getattr(event, "is_group", False)),
        grouped_id=int(grouped_id) if grouped_id is not None else None,
        messages=messages,
    )


def is_reactable_channel_message(event: Any) -> bool:
    message = getattr(event, "message", None)
    if getattr(message, "action", None) is not None:
        return False
    return bool(getattr(event, "is_channel", False) and not getattr(event, "is_group", False))


def should_enqueue_channel_reaction(event: Any, repository: Any) -> bool:
    if not is_reactable_channel_message(event):
        return False
    if not repository.is_reaction_autolike_enabled():
        return False
    settings = repository.get_effective_reaction_channel_settings(int(event.chat_id))
    return bool(settings and settings.enabled)


def is_mirrorable_source_message(event: Any) -> bool:
    message = getattr(event, "message", None)
    if getattr(message, "action", None) is not None:
        return False
    if getattr(message, "grouped_id", None) is not None and not getattr(event, "messages", None):
        return False
    return bool(getattr(event, "is_channel", False) or getattr(event, "is_group", False))


def should_enqueue_post_mirror(event: Any, repository: Any) -> bool:
    if not is_mirrorable_source_message(event):
        return False
    if not repository.is_post_mirroring_enabled():
        return False
    target_chat_id = repository.get_post_mirror_target_chat_id()
    if target_chat_id is None:
        return False
    if int(event.chat_id) == int(target_chat_id):
        return False
    settings = repository.get_post_mirror_source_settings(int(event.chat_id))
    return bool(settings and settings.enabled)


def classify_dialog_kind(dialog: Any) -> str:
    if getattr(dialog, "is_user", False):
        return "private"
    if getattr(dialog, "is_group", False):
        return "group"
    if getattr(dialog, "is_channel", False):
        return "channel"
    return "chat"


async def remember_chat_from_event(event: Any, repository: Any) -> tuple[str | None, str]:
    kind = "chat"
    if getattr(event, "is_private", False):
        kind = "private"
    elif getattr(event, "is_group", False):
        kind = "group"
    elif getattr(event, "is_channel", False):
        kind = "channel"

    title = None
    chat = getattr(event, "chat", None)
    if chat is None:
        try:
            chat = await event.get_chat()
        except Exception:
            chat = None
    if chat is not None:
        title = (
            getattr(chat, "title", None)
            or getattr(chat, "first_name", None)
            or getattr(chat, "username", None)
        )
    repository.upsert_known_chat(int(event.chat_id), title, kind, last_seen_at=int(time.time()))
    return title, kind


async def sync_chat_catalog(client: Any, repository: Any, *, post_mirror_topic_manager: Any | None = None) -> None:
    base_seen_at = int(time.time())
    index = 0
    try:
        async for dialog in client.iter_dialogs():
            chat_id = int(dialog.id)
            title = getattr(dialog, "title", None)
            kind = classify_dialog_kind(dialog)
            repository.upsert_known_chat(
                chat_id,
                title,
                kind,
                last_seen_at=base_seen_at - index,
            )
            if post_mirror_topic_manager is not None:
                await sync_post_mirror_topic_title(
                    state=repository,
                    topic_manager=post_mirror_topic_manager,
                    source_chat_id=chat_id,
                    title=title,
                    kind=kind,
                )
            index += 1
    finally:
        await mark_current_session_offline(client)


async def sync_post_mirror_topic_title(
    *,
    state: Any,
    topic_manager: Any | None,
    source_chat_id: int,
    title: str | None,
    kind: str,
) -> str:
    normalized_title = (title or "").strip()
    if not normalized_title:
        return "skipped_no_title"
    if kind not in {"channel", "group"}:
        return "skipped_unsupported_chat"

    settings = state.get_post_mirror_source_settings(int(source_chat_id))
    if settings is None:
        return "skipped_unconfigured"

    current_title = (settings.title or "").strip()
    if current_title == normalized_title:
        return "skipped_current"

    target_chat_id = state.get_post_mirror_target_chat_id()
    if (
        target_chat_id is None
        or int(source_chat_id) == int(target_chat_id)
        or settings.target_thread_id is None
    ):
        state.upsert_post_mirror_source(int(source_chat_id), normalized_title, kind)
        return "updated_source_title"

    if topic_manager is None:
        return "skipped_no_topic_manager"

    await topic_manager.rename_topic(target_chat_id, int(settings.target_thread_id), normalized_title)
    state.upsert_post_mirror_source(int(source_chat_id), normalized_title, kind)
    return "renamed"


def _dialog_filter_title(folder: Any) -> str:
    title = getattr(folder, "title", None)
    if isinstance(title, str) and title.strip():
        return title.strip()
    text = getattr(title, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    folder_id = getattr(folder, "id", None)
    return f"Папка {folder_id}" if folder_id is not None else "Папка"


def _dialog_filter_type(folder: Any) -> str:
    return folder.__class__.__name__


def _dialog_filter_explicit_peers(folder: Any) -> list[Any]:
    from telethon import utils

    peers = list(getattr(folder, "pinned_peers", None) or [])
    peers.extend(getattr(folder, "include_peers", None) or [])
    unique: list[Any] = []
    seen: set[object] = set()
    for peer in peers:
        try:
            key: object = utils.get_peer_id(peer)
        except TypeError:
            key = (peer.__class__.__name__, repr(peer))
        if key in seen:
            continue
        seen.add(key)
        unique.append(peer)
    return unique


def _classify_peer_entity_kind(entity: Any) -> str:
    if getattr(entity, "broadcast", False):
        return "channel"
    if getattr(entity, "megagroup", False):
        return "group"
    class_name = entity.__class__.__name__
    if class_name in {"Chat", "ChatForbidden"}:
        return "group"
    if class_name in {"User", "UserEmpty"}:
        return "private"
    return "chat"


def _entity_title(entity: Any) -> str | None:
    title = (
        getattr(entity, "title", None)
        or getattr(entity, "first_name", None)
        or getattr(entity, "username", None)
    )
    return str(title) if title else None


def _reaction_folder_peer_resolve_errors() -> tuple[type[Exception], ...]:
    from telethon import errors

    return (
        ValueError,
        errors.ChannelInvalidError,
        errors.ChannelPrivateError,
        errors.ChatAdminRequiredError,
    )


async def _entities_for_reaction_folder_peers(client: Any, peers: list[Any], *, folder_id: int) -> list[Any]:
    if not peers:
        return []
    resolve_errors = _reaction_folder_peer_resolve_errors()
    try:
        try:
            entities = await client.get_entity(peers)
        finally:
            await mark_current_session_offline(client)
    except resolve_errors:
        logger.warning(
            "reaction_folder_peer_batch_resolve_failed folder_id=%s peer_count=%s",
            folder_id,
            len(peers),
        )
        resolved = []
        for peer in peers:
            try:
                try:
                    resolved.append(await client.get_entity(peer))
                finally:
                    await mark_current_session_offline(client)
            except resolve_errors:
                logger.warning(
                    "reaction_folder_peer_skipped folder_id=%s peer_type=%s",
                    folder_id,
                    peer.__class__.__name__,
                )
        return resolved
    if isinstance(entities, (list, tuple)):
        return list(entities)
    return [entities]


async def _reaction_folder_members_from_peers(client: Any, peers: list[Any], *, folder_id: int) -> list[dict[str, Any]]:
    from telethon import utils

    members_by_chat_id: dict[int, dict[str, Any]] = {}
    for entity in await _entities_for_reaction_folder_peers(client, peers, folder_id=folder_id):
        kind = _classify_peer_entity_kind(entity)
        if kind not in {"channel", "group"}:
            continue
        try:
            chat_id = int(utils.get_peer_id(entity))
        except TypeError:
            logger.warning(
                "reaction_folder_peer_id_failed folder_id=%s entity_type=%s",
                folder_id,
                entity.__class__.__name__,
                exc_info=True,
            )
            continue
        members_by_chat_id[chat_id] = {
            "chat_id": chat_id,
            "title": _entity_title(entity),
            "kind": kind,
        }
    return list(members_by_chat_id.values())


async def sync_reaction_folders(client: Any, repository: Any) -> int:
    from telethon import errors
    from telethon.tl.functions.messages import GetDialogFiltersRequest

    try:
        result = await client(GetDialogFiltersRequest())
    finally:
        await mark_current_session_offline(client)
    filters = getattr(result, "filters", result) or []
    folders: list[dict[str, Any]] = []
    for position, folder in enumerate(filters):
        folder_id = getattr(folder, "id", None)
        if folder_id is None:
            continue
        folder_id = int(folder_id)
        members: list[dict[str, Any]] = []
        explicit_peers = _dialog_filter_explicit_peers(folder)
        if explicit_peers:
            members = await _reaction_folder_members_from_peers(
                client,
                explicit_peers,
                folder_id=folder_id,
            )
        else:
            try:
                try:
                    async for dialog in client.iter_dialogs(folder=folder_id):
                        kind = classify_dialog_kind(dialog)
                        if kind not in {"channel", "group"}:
                            continue
                        members.append(
                            {
                                "chat_id": int(dialog.id),
                                "title": getattr(dialog, "title", None),
                                "kind": kind,
                            }
                        )
                finally:
                    await mark_current_session_offline(client)
            except errors.FolderIdInvalidError:
                logger.warning(
                    "reaction_folder_invalid_folder_id folder_id=%s folder_type=%s",
                    folder_id,
                    _dialog_filter_type(folder),
                )
                members = await _reaction_folder_members_from_peers(
                    client,
                    explicit_peers,
                    folder_id=folder_id,
                )
        folders.append(
            {
                "folder_id": folder_id,
                "title": _dialog_filter_title(folder),
                "position": position,
                "members": members,
            }
        )
    repository.replace_reaction_folder_catalog(folders)
    return len(folders)


async def refresh_reaction_folders_loop(
    client: Any,
    repository: Any,
    *,
    interval_seconds: float = 1800.0,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    while True:
        await sleep(interval_seconds)
        try:
            count = await sync_reaction_folders(client, repository)
        except Exception:
            logger.exception("reaction_folder_sync_failed")
            continue
        logger.info("reaction_folder_sync_complete folders=%s", count)


async def record_account_premium_status(client: Any, repository: Any) -> bool:
    try:
        me = await client.get_me()
    except Exception:
        logger.warning("account_premium_check_failed", exc_info=True)
        return False
    finally:
        await mark_current_session_offline(client)
    repository.set_account_premium(bool(getattr(me, "premium", False) or getattr(me, "is_premium", False)))
    return True


async def refresh_account_premium_status_loop(
    client: Any,
    repository: Any,
    *,
    interval_seconds: float = 3600.0,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    while True:
        await sleep(interval_seconds)
        await record_account_premium_status(client, repository)


async def dispatch_channel_message(event: Any, registry: FeatureRegistry, context: AssistantContext) -> str | None:
    if not is_reactable_channel_message(event):
        return None

    channel_event = build_channel_message_event(event)
    logger.info(
        "channel_post_received chat_id=%s message_id=%s grouped_id=%s",
        channel_event.chat_id,
        channel_event.message_id,
        channel_event.grouped_id,
    )
    try:
        result = await registry.dispatch(channel_event, context)
    except Exception:
        logger.exception(
            "channel_reaction_dispatch_failed chat_id=%s message_id=%s",
            channel_event.chat_id,
            channel_event.message_id,
        )
        return "error"
    logger.info(
        "channel_reaction_dispatch_result chat_id=%s message_id=%s result=%s",
        channel_event.chat_id,
        channel_event.message_id,
        result,
    )
    return str(result)


async def dispatch_post_mirror(event: Any, registry: FeatureRegistry, context: AssistantContext) -> str | None:
    if not is_mirrorable_source_message(event):
        return None

    mirror_event = build_post_mirror_event(event)
    logger.info(
        "post_mirror_received chat_id=%s message_id=%s grouped_id=%s message_count=%s",
        mirror_event.chat_id,
        mirror_event.message_id,
        mirror_event.grouped_id,
        len(mirror_event.message_ids),
    )
    try:
        result = await registry.dispatch(mirror_event, context)
    except telethon_errors.FloodWaitError:
        logger.warning(
            "post_mirror_dispatch_flood_wait chat_id=%s message_id=%s",
            mirror_event.chat_id,
            mirror_event.message_id,
        )
        raise
    except Exception:
        logger.exception(
            "post_mirror_dispatch_failed chat_id=%s message_id=%s",
            mirror_event.chat_id,
            mirror_event.message_id,
        )
        return "error"
    logger.info(
        "post_mirror_dispatch_result chat_id=%s message_id=%s result=%s",
        mirror_event.chat_id,
        mirror_event.message_id,
        result,
    )
    return str(result)


class PostMirrorMediaDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class PostMirrorVideoMetadata:
    width: int
    height: int
    duration: int


class TelethonPostMirrorSender:
    def __init__(
        self,
        client: Any,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        video_metadata_probe: Callable[[Path], Awaitable[tuple[int, int, int] | PostMirrorVideoMetadata | None]]
        | None = None,
        logger_: logging.Logger | None = None,
    ):
        self.client = client
        self._sleep = sleep
        self._video_metadata_probe = video_metadata_probe or self._probe_video_metadata
        self._logger = logger_ or logger

    async def copy_post(
        self,
        event: PostMirrorEvent,
        *,
        target_chat_id: int,
        target_thread_id: int | None,
    ) -> PostMirrorSendResult:
        messages = event.messages
        if not messages:
            return PostMirrorSendResult()

        media_count = 0
        sent_count = 0
        with tempfile.TemporaryDirectory(prefix="telepath-post-mirror-") as temp_dir:
            temp_path = Path(temp_dir)
            text_messages: list[tuple[str, Any]] = []
            copy_messages: list[Any] = []
            media_files: list[Path] = []
            media_messages: list[Any] = []
            captions: list[str] = []
            caption_entities: list[list[Any]] = []

            for message in messages:
                if getattr(message, "media", None) is None:
                    text = getattr(message, "message", None) or ""
                    if text:
                        text_messages.append((text, self._message_entities_for_send(getattr(message, "entities", None))))
                    continue

                if not self._has_downloadable_media(message):
                    if self._is_webpage_media(message):
                        text = getattr(message, "message", None) or ""
                        if text:
                            text_messages.append(
                                (text, self._message_entities_for_send(getattr(message, "entities", None)))
                            )
                    else:
                        copy_messages.append(message)
                    continue

                await _yield_post_mirror_history_to_realtime_if_pending()
                downloaded = await self._download_media_to_path(message, temp_path)
                if downloaded is None:
                    raise PostMirrorMediaDownloadError(f"media download failed for message_id={int(message.id)}")
                media_files.append(downloaded)
                media_messages.append(message)
                media_count += 1
                captions.append(getattr(message, "message", None) or "")
                caption_entities.append(self._message_entities_for_send(getattr(message, "entities", None)))

            for text, text_entities in text_messages:
                await self._call_with_flood_wait_retry(
                    self.client.send_message,
                    target_chat_id,
                    text,
                    reply_to=target_thread_id,
                    formatting_entities=text_entities or None,
                    parse_mode=None,
                    link_preview=True,
                )
                sent_count += 1

            for message in copy_messages:
                await _yield_post_mirror_history_to_realtime_if_pending()
                await self._call_with_flood_wait_retry(
                    self.client.send_message,
                    target_chat_id,
                    message,
                    reply_to=target_thread_id,
                )
                sent_count += 1

            if media_files:
                multiple = len(media_files) > 1
                can_prepare_media = self._can_send_media_with_prepared_uploads()
                formatting_entities = None
                if any(caption_entities):
                    formatting_entities = caption_entities if multiple else caption_entities[0]
                send_file_kwargs: dict[str, Any] = {
                    "caption": captions if multiple else (captions[0] or None),
                    "reply_to": target_thread_id,
                    "formatting_entities": formatting_entities,
                    "parse_mode": None,
                }
                if not multiple and not can_prepare_media:
                    send_file_kwargs.update(await self._media_send_options(media_messages[0], media_files[0]))
                try:
                    await _yield_post_mirror_history_to_realtime_if_pending()
                    if multiple and can_prepare_media:
                        await self._send_album_with_media_options(
                            target_chat_id=target_chat_id,
                            target_thread_id=target_thread_id,
                            media_files=media_files,
                            media_messages=media_messages,
                            captions=captions,
                            formatting_entities=(
                                formatting_entities if isinstance(formatting_entities, list) else None
                            ),
                        )
                    elif not multiple and can_prepare_media:
                        await self._send_single_media_file(
                            target_chat_id=target_chat_id,
                            target_thread_id=target_thread_id,
                            file=media_files[0],
                            message=media_messages[0],
                            caption=captions[0],
                            formatting_entities=formatting_entities if isinstance(formatting_entities, list) else None,
                        )
                    else:
                        await self._call_with_flood_wait_retry(
                            self.client.send_file,
                            target_chat_id,
                            media_files if multiple else media_files[0],
                            **send_file_kwargs,
                        )
                except telethon_errors.DocumentInvalidError:
                    self._logger.warning("post_mirror_media_invalid_fallback_to_individual_files")
                    await self._send_media_files_individually(
                        target_chat_id=target_chat_id,
                        target_thread_id=target_thread_id,
                        media_files=media_files,
                        media_messages=media_messages,
                        captions=captions,
                        formatting_entities=caption_entities,
                        force_document_first=not multiple,
                    )
                sent_count += len(media_files)

        return PostMirrorSendResult(message_count=sent_count, media_count=media_count)

    @staticmethod
    def _message_entities_for_send(entities: Any) -> list[Any]:
        if not entities:
            return []
        from telethon import types

        if not isinstance(entities, (list, tuple)):
            return []
        return [entity for entity in entities if isinstance(entity, types.TypeMessageEntity)]

    @staticmethod
    def _has_downloadable_media(message: Any) -> bool:
        return (
            getattr(message, "file", None) is not None
            or TelethonPostMirrorSender._message_photo(message) is not None
            or TelethonPostMirrorSender._message_document(message) is not None
        )

    @staticmethod
    def _is_webpage_media(message: Any) -> bool:
        media = TelethonPostMirrorSender._message_media(message)
        if media is None:
            return False
        return media.__class__.__name__ == "MessageMediaWebPage" or getattr(media, "webpage", None) is not None

    @staticmethod
    def _message_media(message: Any) -> Any | None:
        return getattr(message, "media", None)

    @staticmethod
    def _message_document(message: Any) -> Any | None:
        direct = getattr(message, "document", None)
        if direct is not None:
            return direct
        media = TelethonPostMirrorSender._message_media(message)
        return getattr(media, "document", None) if media is not None else None

    @staticmethod
    def _message_photo(message: Any) -> Any | None:
        direct = getattr(message, "photo", None)
        if direct is not None:
            return direct
        media = TelethonPostMirrorSender._message_media(message)
        return getattr(media, "photo", None) if media is not None else None

    @staticmethod
    def _single_media_send_options(message: Any) -> dict[str, Any]:
        options: dict[str, Any] = {}
        document = TelethonPostMirrorSender._message_document(message)
        if document is not None:
            mime_type = getattr(document, "mime_type", None)
            attributes = getattr(document, "attributes", None)
            if mime_type:
                options["mime_type"] = mime_type
            if attributes:
                options["attributes"] = attributes

        if getattr(message, "voice", False):
            options["voice_note"] = True
        if getattr(message, "video_note", False):
            options["video_note"] = True
        elif getattr(message, "video", False):
            options["supports_streaming"] = True
        return options

    async def _media_send_options(self, message: Any, file: Path) -> dict[str, Any]:
        options = self._single_media_send_options(message)
        if not self._looks_like_video_message(message, options):
            return options

        from telethon import types

        attributes = list(options.get("attributes") or [])
        video_attr = self._video_attribute(attributes)
        if self._has_valid_video_dimensions(video_attr):
            return options

        await _yield_post_mirror_history_to_realtime_if_pending()
        metadata = await self._video_metadata_probe(file)
        if metadata is None:
            return options
        if isinstance(metadata, tuple):
            metadata = PostMirrorVideoMetadata(
                width=int(metadata[0]),
                height=int(metadata[1]),
                duration=int(metadata[2]),
            )
        if metadata.width <= 1 or metadata.height <= 1:
            return options

        if video_attr is not None:
            round_message = bool(getattr(video_attr, "round_message", False))
            supports_streaming = bool(
                getattr(video_attr, "supports_streaming", False) or options.get("supports_streaming")
            )
            duration = int(getattr(video_attr, "duration", 0) or metadata.duration or 0)
        else:
            round_message = bool(getattr(message, "video_note", False))
            supports_streaming = bool(options.get("supports_streaming"))
            duration = int(metadata.duration or 0)
        repaired = types.DocumentAttributeVideo(
            duration=duration,
            w=int(metadata.width),
            h=int(metadata.height),
            round_message=round_message,
            supports_streaming=supports_streaming,
        )
        attributes = [
            repaired if isinstance(attribute, types.DocumentAttributeVideo) else attribute
            for attribute in attributes
        ]
        if video_attr is None:
            attributes.append(repaired)
        options["attributes"] = tuple(attributes)
        return options

    @staticmethod
    def _looks_like_video_message(message: Any, options: dict[str, Any]) -> bool:
        if getattr(message, "video", False) or getattr(message, "video_note", False):
            return True
        mime_type = str(options.get("mime_type") or "")
        return mime_type.startswith("video/")

    @staticmethod
    def _video_attribute(attributes: list[Any]) -> Any | None:
        from telethon import types

        for attribute in attributes:
            if isinstance(attribute, types.DocumentAttributeVideo):
                return attribute
        return None

    @staticmethod
    def _has_valid_video_dimensions(attribute: Any | None) -> bool:
        if attribute is None:
            return False
        return int(getattr(attribute, "w", 0) or 0) > 1 and int(getattr(attribute, "h", 0) or 0) > 1

    def _can_send_media_with_prepared_uploads(self) -> bool:
        return (
            hasattr(self.client, "_file_to_media")
            and hasattr(self.client, "_get_response_message")
            and hasattr(self.client, "get_input_entity")
        )

    async def _send_media_files_individually(
        self,
        *,
        target_chat_id: int,
        target_thread_id: int,
        media_files: list[Path],
        media_messages: list[Any],
        captions: list[str],
        formatting_entities: list[list[Any]],
        force_document_first: bool = False,
    ) -> None:
        for index, (file, message) in enumerate(zip(media_files, media_messages)):
            file_entities = formatting_entities[index] if index < len(formatting_entities) else []
            try:
                await _yield_post_mirror_history_to_realtime_if_pending()
                if self._can_send_media_with_prepared_uploads():
                    await self._send_single_media_file(
                        target_chat_id=target_chat_id,
                        target_thread_id=target_thread_id,
                        file=file,
                        message=message,
                        caption=captions[index],
                        formatting_entities=file_entities,
                        force_document=force_document_first,
                    )
                else:
                    kwargs: dict[str, Any] = {
                        "caption": captions[index] or None,
                        "reply_to": target_thread_id,
                        "parse_mode": None,
                    }
                    if file_entities:
                        kwargs["formatting_entities"] = file_entities
                    kwargs.update(await self._media_send_options(message, file))
                    if force_document_first:
                        kwargs["force_document"] = True
                    await self._call_with_flood_wait_retry(self.client.send_file, target_chat_id, file, **kwargs)
            except telethon_errors.DocumentInvalidError:
                self._logger.warning("post_mirror_media_invalid_retrying_as_document")
                await _yield_post_mirror_history_to_realtime_if_pending()
                if self._can_send_media_with_prepared_uploads():
                    await self._send_single_media_file(
                        target_chat_id=target_chat_id,
                        target_thread_id=target_thread_id,
                        file=file,
                        message=message,
                        caption=captions[index],
                        formatting_entities=file_entities,
                        force_document=True,
                    )
                else:
                    kwargs = {
                        "caption": captions[index] or None,
                        "reply_to": target_thread_id,
                        "parse_mode": None,
                        "force_document": True,
                    }
                    if file_entities:
                        kwargs["formatting_entities"] = file_entities
                    kwargs.update(await self._media_send_options(message, file))
                    await self._call_with_flood_wait_retry(self.client.send_file, target_chat_id, file, **kwargs)

    async def _send_single_media_file(
        self,
        *,
        target_chat_id: int,
        target_thread_id: int,
        file: Path,
        message: Any,
        caption: str,
        formatting_entities: list[Any] | None,
        force_document: bool = False,
    ) -> None:
        from telethon import functions, types

        await _yield_post_mirror_history_to_realtime_if_pending()
        try:
            entity = await _retry_post_mirror_history_preemptions(
                lambda: self.client.get_input_entity(target_chat_id)
            )
        finally:
            await mark_current_session_offline(self.client, logger_=self._logger)
        options = await self._media_send_options(message, file)
        file_to_media_kwargs: dict[str, Any] = {
            "force_document": force_document,
        }
        for key in ("attributes", "mime_type", "voice_note", "video_note", "supports_streaming"):
            if key in options:
                file_to_media_kwargs[key] = options[key]
        while True:
            try:
                await _yield_post_mirror_history_to_realtime_if_pending()
                _, input_media, _ = await _await_post_mirror_history_interruptibly(
                    lambda: self.client._file_to_media(file, **file_to_media_kwargs)
                )
                break
            except PostMirrorHistoryPreempted:
                continue
        if input_media is None:
            raise TypeError(f"Cannot use {file!r} as file")
        request = functions.messages.SendMediaRequest(
            entity,
            input_media,
            reply_to=types.InputReplyToMessage(int(target_thread_id)),
            message=caption or "",
            entities=formatting_entities or None,
        )
        result = await self._call_with_flood_wait_retry(self.client, request)
        self.client._get_response_message(request, result, entity)

    async def _send_album_with_media_options(
        self,
        *,
        target_chat_id: int,
        target_thread_id: int,
        media_files: list[Path],
        media_messages: list[Any],
        captions: list[str],
        formatting_entities: list[list[Any]] | None,
    ) -> None:
        from telethon import functions, types, utils

        await _yield_post_mirror_history_to_realtime_if_pending()
        try:
            entity = await _retry_post_mirror_history_preemptions(
                lambda: self.client.get_input_entity(target_chat_id)
            )
        finally:
            await mark_current_session_offline(self.client, logger_=self._logger)
        reply_to = types.InputReplyToMessage(int(target_thread_id))
        for start in range(0, len(media_files), 10):
            chunk_files = media_files[start : start + 10]
            chunk_messages = media_messages[start : start + 10]
            chunk_captions = captions[start : start + 10]
            chunk_entities = formatting_entities[start : start + 10] if formatting_entities else None
            media = []
            for index, (file, message) in enumerate(zip(chunk_files, chunk_messages)):
                await _yield_post_mirror_history_to_realtime_if_pending()
                options = await self._media_send_options(message, file)
                file_to_media_kwargs: dict[str, Any] = {
                    "nosound_video": True,
                }
                for key in ("attributes", "mime_type", "voice_note", "video_note", "supports_streaming"):
                    if key in options:
                        file_to_media_kwargs[key] = options[key]
                while True:
                    try:
                        await _yield_post_mirror_history_to_realtime_if_pending()
                        _, input_media, _ = await _await_post_mirror_history_interruptibly(
                            lambda: self.client._file_to_media(file, **file_to_media_kwargs)
                        )
                        break
                    except PostMirrorHistoryPreempted:
                        continue
                if isinstance(input_media, (types.InputMediaUploadedPhoto, types.InputMediaPhotoExternal)):
                    result = await self._call_with_flood_wait_retry(
                        self.client,
                        functions.messages.UploadMediaRequest(entity, media=input_media),
                        interruptible=True,
                    )
                    input_media = utils.get_input_media(result.photo)
                elif isinstance(input_media, (types.InputMediaUploadedDocument, types.InputMediaDocumentExternal)):
                    result = await self._call_with_flood_wait_retry(
                        self.client,
                        functions.messages.UploadMediaRequest(entity, media=input_media),
                        interruptible=True,
                    )
                    input_media = utils.get_input_media(
                        result.document,
                        supports_streaming=bool(file_to_media_kwargs.get("supports_streaming")),
                    )
                media.append(
                    types.InputSingleMedia(
                        input_media,
                        message=chunk_captions[index] if index < len(chunk_captions) else "",
                        entities=chunk_entities[index] if chunk_entities and index < len(chunk_entities) else None,
                    )
                )
            request = functions.messages.SendMultiMediaRequest(
                entity,
                reply_to=reply_to,
                multi_media=media,
            )
            await _yield_post_mirror_history_to_realtime_if_pending()
            result = await self._call_with_flood_wait_retry(self.client, request)
            self.client._get_response_message([item.random_id for item in media], result, entity)

    async def _call_with_flood_wait_retry(
        self,
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        interruptible: bool = False,
        **kwargs: Any,
    ) -> Any:
        while True:
            try:
                await _yield_post_mirror_history_to_realtime_if_pending()
                try:
                    if interruptible:
                        return await _await_post_mirror_history_interruptibly(lambda: func(*args, **kwargs))
                    return await func(*args, **kwargs)
                finally:
                    await mark_current_session_offline(self.client, logger_=self._logger)
            except PostMirrorHistoryPreempted:
                if interruptible:
                    continue
                raise
            except telethon_errors.FloodWaitError as exc:
                wait_seconds = post_mirror_flood_wait_delay_seconds(exc)
                self._logger.warning("post_mirror_sender_flood_wait wait_seconds=%s", wait_seconds)
                sleep = _POST_MIRROR_FLOOD_WAIT_SLEEP.get() or self._sleep
                await sleep(wait_seconds)

    async def _probe_video_metadata(self, file: Path) -> PostMirrorVideoMetadata | None:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None
        try:
            process = await asyncio.create_subprocess_exec(
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,duration:stream_tags=rotate:stream_side_data=rotation:format=duration",
                "-of",
                "json",
                str(file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        except Exception:
            self._logger.exception("post_mirror_video_probe_failed path=%s", file)
            return None
        if process.returncode != 0:
            return None
        try:
            payload = json.loads(stdout.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return None
        streams = payload.get("streams") or []
        if not streams:
            return None
        stream = streams[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        if width > 0 and height > 0 and self._video_rotation_requires_dimension_swap(stream):
            width, height = height, width
        duration_value = stream.get("duration") or (payload.get("format") or {}).get("duration") or 0
        try:
            duration = int(float(duration_value))
        except (TypeError, ValueError):
            duration = 0
        if width <= 1 or height <= 1:
            return None
        return PostMirrorVideoMetadata(width=width, height=height, duration=duration)

    @classmethod
    def _video_rotation_requires_dimension_swap(cls, stream: dict[str, Any]) -> bool:
        rotation = cls._video_rotation_degrees(stream)
        if rotation is None:
            return False
        return abs(rotation) % 180 == 90

    @classmethod
    def _video_rotation_degrees(cls, stream: dict[str, Any]) -> int | None:
        tags = stream.get("tags") or {}
        rotation = cls._parse_video_rotation(tags.get("rotate"))
        if rotation is not None:
            return rotation
        for item in stream.get("side_data_list") or []:
            rotation = cls._parse_video_rotation(item.get("rotation"))
            if rotation is not None:
                return rotation
        return None

    @staticmethod
    def _parse_video_rotation(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return None

    async def _download_media_to_path(self, message: Any, directory: Path) -> Path | None:
        while True:
            try:
                await _yield_post_mirror_history_to_realtime_if_pending()
                try:
                    downloaded = await _await_post_mirror_history_interruptibly(
                        lambda: self.client.download_media(message, file=directory)
                    )
                finally:
                    await mark_current_session_offline(self.client, logger_=self._logger)
                break
            except PostMirrorHistoryPreempted:
                continue
        if downloaded is None:
            return None
        if isinstance(downloaded, bytes):
            path = directory / f"{int(message.id)}.bin"
            path.write_bytes(downloaded)
            return path
        path = Path(downloaded)
        return path if path.exists() else None


class PostMirrorOutboxEnqueuer:
    def __init__(
        self,
        state: Any,
        *,
        origin: str = "realtime",
        ready_at: Callable[[], int] | None = None,
    ) -> None:
        self._state = state
        self._origin = origin if origin in {"realtime", "history"} else "realtime"
        self._ready_at = ready_at or (lambda: 0)

    async def copy_post(
        self,
        event: PostMirrorEvent,
        *,
        target_chat_id: int,
        target_thread_id: int,
    ) -> PostMirrorSendResult:
        message_ids = event.message_ids or (event.message_id,)
        self._state.enqueue_post_mirror_delivery(
            source_chat_id=event.chat_id,
            message_ids=message_ids,
            is_channel=event.is_channel,
            is_group=event.is_group,
            grouped_id=event.grouped_id,
            target_chat_id=target_chat_id,
            target_thread_id=target_thread_id,
            origin=self._origin,
            ready_at=self._ready_at(),
        )
        media_count = sum(1 for message in event.messages if getattr(message, "media", None) is not None)
        return PostMirrorSendResult(message_count=len(message_ids), media_count=media_count)


class TelegramAuthorizationOnlineGate:
    def __init__(
        self,
        client: Any,
        *,
        freshness_seconds: int = 180,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._freshness_seconds = max(1, int(freshness_seconds))
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def is_online(self) -> bool:
        from telethon import functions

        try:
            result = await self._client(functions.account.GetAuthorizationsRequest())
        except Exception:
            logger.exception("post_mirror_online_gate_check_failed")
            await mark_current_session_offline(self._client)
            return False
        now = self._normalize_datetime(self._now())
        for authorization in getattr(result, "authorizations", None) or []:
            if getattr(authorization, "current", False):
                continue
            active_at = getattr(authorization, "date_active", None)
            if active_at is None:
                continue
            active_at = self._normalize_datetime(active_at)
            if (now - active_at).total_seconds() <= self._freshness_seconds:
                return True
        await mark_current_session_offline(self._client)
        return False

    @staticmethod
    def _normalize_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class OnlineGatedForumTopicManager:
    def __init__(
        self,
        topic_manager: Any,
        online_gate: Any,
        *,
        logger_: logging.Logger | None = None,
    ) -> None:
        self._topic_manager = topic_manager
        self._online_gate = online_gate
        self._logger = logger_ or logger

    async def create_topic(self, target_chat_id: int, title: str) -> int:
        if not await self._online_gate.is_online():
            raise RuntimeError("owner is offline; topic creation is deferred")
        with suppress_current_session_offline_updates():
            return await self._topic_manager.create_topic(target_chat_id, title)

    async def rename_topic(self, target_chat_id: int, topic_id: int, title: str) -> None:
        if not await self._online_gate.is_online():
            self._logger.info(
                "post_mirror_topic_rename_deferred_until_owner_online target_chat_id=%s topic_id=%s",
                target_chat_id,
                topic_id,
            )
            return
        with suppress_current_session_offline_updates():
            await self._topic_manager.rename_topic(target_chat_id, topic_id, title)


POST_MIRROR_OUTBOX_DELIVERY_POLL_SECONDS = 30.0
POST_MIRROR_OUTBOX_DELIVERY_DELAY_RANGE_SECONDS = (60, 120)
POST_MIRROR_OUTBOX_PREFERRED_DELIVERY_WINDOW_SECONDS = 120
POST_MIRROR_OUTBOX_ONLINE_DELIVERY_WINDOW_SECONDS = 300
POST_MIRROR_OUTBOX_SPAM_SAFE_SPACING_SECONDS = 5.0
POST_MIRROR_OUTBOX_DELIVERY_BATCH_SIZE = 1000
POST_MIRROR_OUTBOX_RETRY_DELAY_SECONDS = 300
POST_MIRROR_OUTBOX_PERMANENT_FAILURE_ATTEMPTS = 5


class PostMirrorOutboxDeliveryWorker:
    def __init__(
        self,
        *,
        state: Any,
        client: Any,
        post_mirror_sender: Any,
        online_gate: Any,
        post_mirror_topic_manager: Any | None = None,
        poll_seconds: float = POST_MIRROR_OUTBOX_DELIVERY_POLL_SECONDS,
        delivery_delay_range_seconds: tuple[int, int] = POST_MIRROR_OUTBOX_DELIVERY_DELAY_RANGE_SECONDS,
        online_delivery_window_seconds: int = POST_MIRROR_OUTBOX_ONLINE_DELIVERY_WINDOW_SECONDS,
        randint: Callable[[int, int], int] = random.randint,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], int] | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self._state = state
        self._client = client
        self._post_mirror_sender = post_mirror_sender
        self._online_gate = online_gate
        self._post_mirror_topic_manager = post_mirror_topic_manager
        self._poll_seconds = max(0.1, float(poll_seconds))
        self._delivery_delay_range_seconds = self._normalize_delay_range(delivery_delay_range_seconds)
        self._online_delivery_window_seconds = max(1, int(online_delivery_window_seconds))
        self._randint = randint
        self._sleep = sleep
        self._monotonic = monotonic
        self._now = now or (lambda: int(time.time()))
        self._logger = logger_ or logger

    async def run(self) -> None:
        while True:
            await self.drain_once()
            await self._sleep(self._poll_seconds)

    async def drain_once(self, *, limit: int | None = None) -> int:
        started_at = self._monotonic()
        sent_count = 0
        remaining_limit = None if limit is None else max(0, int(limit))
        while remaining_limit is None or remaining_limit > 0:
            batch_limit = POST_MIRROR_OUTBOX_DELIVERY_BATCH_SIZE
            if remaining_limit is not None:
                batch_limit = min(batch_limit, remaining_limit)
            jobs = self._state.list_ready_post_mirror_deliveries(now=self._now(), limit=batch_limit)
            if not jobs:
                break
            if not await self._online_gate.is_online():
                self._logger.info("post_mirror_outbox_waiting_for_owner_online")
                break
            for index, job in enumerate(jobs):
                if sent_count > 0:
                    await self._sleep_between_deliveries(
                        started_at=started_at,
                        remaining_sleep_count=len(jobs) - index,
                    )
                    if not await self._online_gate.is_online():
                        self._logger.info("post_mirror_outbox_owner_went_offline")
                        return sent_count
                with suppress_current_session_offline_updates():
                    delivered = await self._deliver_job(job)
                if delivered:
                    sent_count += 1
                if remaining_limit is not None:
                    remaining_limit -= 1
                    if remaining_limit <= 0:
                        break
            if len(jobs) < batch_limit:
                break
        return sent_count

    async def _deliver_job(self, job: PostMirrorQueuedDelivery) -> bool:
        try:
            if not self._delivery_still_allowed(job):
                self._state.cancel_post_mirror_delivery(job.id, error="post mirror target or source disabled")
                return False
            messages = await self._load_messages(job)
            if not messages:
                if job.attempts >= POST_MIRROR_OUTBOX_PERMANENT_FAILURE_ATTEMPTS:
                    self._state.cancel_post_mirror_delivery(
                        job.id,
                        error="source messages unavailable after repeated attempts",
                    )
                    self._logger.warning(
                        "post_mirror_outbox_cancelled_permanent job_id=%s error=%s",
                        job.id,
                        "source messages unavailable",
                    )
                else:
                    self._state.defer_post_mirror_delivery(
                        job.id,
                        delay_seconds=POST_MIRROR_OUTBOX_RETRY_DELAY_SECONDS,
                        error="source messages unavailable",
                        now=self._now(),
                    )
                return False
            target_thread_id = await self._resolve_target_thread_id(job)
            if target_thread_id is None:
                self._state.defer_post_mirror_delivery(
                    job.id,
                    delay_seconds=POST_MIRROR_OUTBOX_RETRY_DELAY_SECONDS,
                    error="target topic unavailable",
                    now=self._now(),
                )
                return False
            event = PostMirrorEvent(
                chat_id=job.source_chat_id,
                message_id=job.message_ids[0],
                message_ids=job.message_ids,
                is_channel=job.is_channel,
                is_group=job.is_group,
                grouped_id=job.grouped_id,
                messages=tuple(messages),
            )
            result = await self._post_mirror_sender.copy_post(
                event,
                target_chat_id=job.target_chat_id,
                target_thread_id=target_thread_id,
            )
            if result.message_count <= 0 and result.media_count <= 0:
                self._state.defer_post_mirror_delivery(
                    job.id,
                    delay_seconds=POST_MIRROR_OUTBOX_RETRY_DELAY_SECONDS,
                    error="empty send result",
                    now=self._now(),
                )
                return False
            self._state.mark_post_mirror_delivery_sent(job.id, now=self._now())
            self._logger.info(
                "post_mirror_outbox_delivered job_id=%s source_chat_id=%s message_ids=%s target_chat_id=%s",
                job.id,
                job.source_chat_id,
                ",".join(str(message_id) for message_id in job.message_ids),
                job.target_chat_id,
            )
            return True
        except telethon_errors.FloodWaitError as exc:
            wait_seconds = post_mirror_flood_wait_delay_seconds(exc)
            self._state.defer_post_mirror_delivery(
                job.id,
                delay_seconds=wait_seconds,
                error=f"flood wait {wait_seconds}s",
                now=self._now(),
            )
            self._logger.warning("post_mirror_outbox_flood_wait job_id=%s wait_seconds=%s", job.id, wait_seconds)
            return False
        except Exception as exc:
            error = str(exc)[:500]
            if self._is_permanent_delivery_error(exc):
                self._state.cancel_post_mirror_delivery(job.id, error=error)
                self._logger.warning(
                    "post_mirror_outbox_cancelled_permanent job_id=%s error=%s",
                    job.id,
                    error,
                )
            else:
                self._state.defer_post_mirror_delivery(
                    job.id,
                    delay_seconds=POST_MIRROR_OUTBOX_RETRY_DELAY_SECONDS,
                    error=error,
                    now=self._now(),
                )
                self._logger.exception("post_mirror_outbox_delivery_failed job_id=%s", job.id)
            return False

    @staticmethod
    def _is_permanent_delivery_error(exc: Exception) -> bool:
        return isinstance(exc, TypeError) and "MessageMediaPaidMedia" in str(exc)

    def _delivery_still_allowed(self, job: PostMirrorQueuedDelivery) -> bool:
        if not self._state.is_post_mirroring_enabled():
            return False
        target_chat_id = self._state.get_post_mirror_target_chat_id()
        if target_chat_id is None or int(target_chat_id) != int(job.target_chat_id):
            return False
        settings = self._state.get_post_mirror_source_settings(job.source_chat_id)
        return settings is not None and bool(settings.enabled)

    async def _load_messages(self, job: PostMirrorQueuedDelivery) -> list[Any]:
        try:
            result = await self._client.get_messages(job.source_chat_id, ids=list(job.message_ids))
        finally:
            await mark_current_session_offline(self._client, logger_=self._logger)
        if result is None:
            return []
        if isinstance(result, (list, tuple)):
            messages = [message for message in result if message is not None]
        else:
            messages = [result]
        by_id = {int(getattr(message, "id")): message for message in messages}
        return [by_id[message_id] for message_id in job.message_ids if message_id in by_id]

    async def _resolve_target_thread_id(self, job: PostMirrorQueuedDelivery) -> int | None:
        if job.target_thread_id is not None:
            return int(job.target_thread_id)
        if self._post_mirror_topic_manager is None:
            return None
        settings = self._state.get_post_mirror_source_settings(job.source_chat_id)
        title = (getattr(settings, "title", None) or "").strip() or str(job.source_chat_id)
        if settings is not None and getattr(settings, "kind", None) in {"channel", "group"}:
            kind = settings.kind
        else:
            kind = "group" if job.is_group else "channel"
        topic_id = await self._post_mirror_topic_manager.create_topic(job.target_chat_id, title)
        self._state.upsert_post_mirror_source(job.source_chat_id, title, kind)
        self._state.set_post_mirror_source_topic(job.source_chat_id, int(topic_id))
        return int(topic_id)

    async def _sleep_between_deliveries(
        self,
        *,
        started_at: float | None = None,
        remaining_sleep_count: int = 1,
    ) -> None:
        minimum, maximum = self._delivery_delay_range_seconds
        if maximum <= 0:
            return
        delay_seconds = self._randint(minimum, maximum)
        if started_at is not None:
            elapsed_seconds = max(0.0, self._monotonic() - started_at)
            sleep_count = max(1, int(remaining_sleep_count))
            preferred_remaining_seconds = max(
                0.0,
                POST_MIRROR_OUTBOX_PREFERRED_DELIVERY_WINDOW_SECONDS - elapsed_seconds,
            )
            online_remaining_seconds = max(0.0, self._online_delivery_window_seconds - elapsed_seconds)
            preferred_delay_seconds = preferred_remaining_seconds / sleep_count
            online_delay_seconds = online_remaining_seconds / sleep_count
            if preferred_delay_seconds >= POST_MIRROR_OUTBOX_SPAM_SAFE_SPACING_SECONDS:
                budgeted_delay_seconds = preferred_delay_seconds
            else:
                budgeted_delay_seconds = online_delay_seconds
            if delay_seconds > budgeted_delay_seconds:
                self._logger.info(
                    "post_mirror_outbox_delivery_delay_capped elapsed_seconds=%s requested_delay_seconds=%s capped_delay_seconds=%s",
                    round(elapsed_seconds, 3),
                    delay_seconds,
                    round(budgeted_delay_seconds, 3),
                )
                delay_seconds = budgeted_delay_seconds
        if delay_seconds <= 0:
            return
        await self._sleep(delay_seconds)

    @staticmethod
    def _normalize_delay_range(delay_range: tuple[int, int]) -> tuple[int, int]:
        minimum, maximum = delay_range
        minimum = int(minimum)
        maximum = int(maximum)
        if minimum < 0 or maximum < minimum:
            raise ValueError("delivery_delay_range_seconds must be non-negative and ordered")
        return minimum, maximum


class TelethonForumTopicManager:
    def __init__(
        self,
        client: Any,
        *,
        random_id_factory: Callable[[], int] | None = None,
    ) -> None:
        self.client = client
        self.random_id_factory = random_id_factory or (lambda: random.getrandbits(63))

    async def create_topic(self, target_chat_id: int, title: str) -> int:
        from telethon.tl.functions.messages import CreateForumTopicRequest
        from telethon.tl.types import MessageActionTopicCreate

        await _yield_post_mirror_history_to_realtime_if_pending()
        try:
            peer = await _retry_post_mirror_history_preemptions(lambda: self.client.get_input_entity(target_chat_id))
        finally:
            await mark_current_session_offline(self.client)
        await _yield_post_mirror_history_to_realtime_if_pending()
        try:
            result = await self.client(
                CreateForumTopicRequest(
                    peer=peer,
                    title=title.strip() or str(target_chat_id),
                    icon_color=0x6FB9F0,
                    random_id=self.random_id_factory(),
                )
            )
        finally:
            await mark_current_session_offline(self.client)
        for message in self._result_messages(result):
            if isinstance(getattr(message, "action", None), MessageActionTopicCreate):
                return int(message.id)
        raise RuntimeError("Telegram did not return the created forum topic id.")

    async def rename_topic(self, target_chat_id: int, topic_id: int, title: str) -> None:
        from telethon.tl.functions.messages import EditForumTopicRequest

        await _yield_post_mirror_history_to_realtime_if_pending()
        try:
            peer = await _retry_post_mirror_history_preemptions(lambda: self.client.get_input_entity(target_chat_id))
        finally:
            await mark_current_session_offline(self.client)
        await _yield_post_mirror_history_to_realtime_if_pending()
        try:
            await self.client(
                EditForumTopicRequest(
                    peer=peer,
                    topic_id=int(topic_id),
                    title=title.strip() or str(topic_id),
                )
            )
        finally:
            await mark_current_session_offline(self.client)

    @staticmethod
    def _result_messages(result: Any) -> list[Any]:
        messages = list(getattr(result, "messages", None) or [])
        for update in getattr(result, "updates", None) or []:
            message = getattr(update, "message", None)
            if message is not None:
                messages.append(message)
        message = getattr(result, "message", None)
        if message is not None:
            messages.append(message)
        return messages


@dataclass(frozen=True)
class PostMirrorHistoryBackfillResult:
    source_count: int
    scanned_count: int
    mirrored_count: int
    skipped_count: int
    failed_count: int
    limit_per_source: int | None
    target_chat_id: int | None = None
    request_queued: bool = False
    duplicate_queued: bool = False
    queue_position: int | None = None


POST_MIRROR_HISTORY_FETCH_WAIT_SECONDS = 1.0
POST_MIRROR_HISTORY_POST_DELAY_RANGE_SECONDS = (60, 120)
POST_MIRROR_HISTORY_TOPIC_CREATE_DELAY_RANGE_SECONDS = (180, 360)


class PostMirrorOperationGate:
    def __init__(self) -> None:
        self._active: str | None = None
        self._realtime_backlog = 0
        self._changed = asyncio.Event()
        self._interruptible_history_tasks: set[asyncio.Task[Any]] = set()
        self._preempted_history_tasks: set[asyncio.Task[Any]] = set()
        self._changed.set()

    def notify_realtime_queued(self) -> None:
        self._realtime_backlog += 1
        for task in tuple(self._interruptible_history_tasks):
            if not task.done():
                self._preempted_history_tasks.add(task)
                task.cancel()
        self._changed.set()

    async def acquire_history(self) -> None:
        while self._active is not None or self._realtime_backlog > 0:
            self._changed.clear()
            if self._active is None and self._realtime_backlog <= 0:
                break
            await self._changed.wait()
        self._active = "history"

    def release_history(self) -> None:
        if self._active == "history":
            self._active = None
            self._changed.set()

    async def yield_to_realtime_if_pending(self) -> None:
        if self._realtime_backlog <= 0:
            return
        should_reacquire_history = self._active == "history"
        if should_reacquire_history:
            self.release_history()
        try:
            while self._active is not None or self._realtime_backlog > 0:
                self._changed.clear()
                if self._active is None and self._realtime_backlog <= 0:
                    break
                await self._changed.wait()
        finally:
            if should_reacquire_history:
                await self.acquire_history()

    async def acquire_realtime(self) -> None:
        if self._realtime_backlog <= 0:
            self._realtime_backlog = 1
        while self._active is not None:
            self._changed.clear()
            if self._active is None:
                break
            await self._changed.wait()
        self._active = "realtime"

    def release_realtime(self) -> None:
        if self._active == "realtime":
            self._active = None
        if self._realtime_backlog > 0:
            self._realtime_backlog -= 1
        self._changed.set()

    @asynccontextmanager
    async def history_operation(self) -> Any:
        await self.acquire_history()
        try:
            yield
        finally:
            self.release_history()

    @asynccontextmanager
    async def realtime_operation(self) -> Any:
        await self.acquire_realtime()
        try:
            yield
        finally:
            self.release_realtime()

    async def interruptible_history_await(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        task = asyncio.create_task(factory())
        self._interruptible_history_tasks.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            if task in self._preempted_history_tasks:
                await self.yield_to_realtime_if_pending()
                raise PostMirrorHistoryPreempted from None
            raise
        finally:
            self._interruptible_history_tasks.discard(task)
            self._preempted_history_tasks.discard(task)

    async def sleep_during_history_pause(
        self,
        delay: float,
        sleep: Callable[[float], Awaitable[Any]],
    ) -> None:
        await self.yield_to_realtime_if_pending()
        if self._active != "history":
            await sleep(delay)
            return
        self.release_history()
        try:
            await sleep(delay)
        finally:
            await self.acquire_history()


class PostMirrorHistoryBackfill:
    def __init__(
        self,
        *,
        client: Any | None = None,
        state: Any | None = None,
        post_mirror_sender: Any | None = None,
        post_mirror_topic_manager: Any | None = None,
        operation_gate: PostMirrorOperationGate | None = None,
        history_fetch_wait_seconds: float = POST_MIRROR_HISTORY_FETCH_WAIT_SECONDS,
        history_post_delay_range_seconds: tuple[int, int] = POST_MIRROR_HISTORY_POST_DELAY_RANGE_SECONDS,
        history_topic_create_delay_range_seconds: tuple[int, int] = POST_MIRROR_HISTORY_TOPIC_CREATE_DELAY_RANGE_SECONDS,
        randint: Callable[[int, int], int] = random.randint,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        logger_: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._state = state
        self._post_mirror_sender = post_mirror_sender
        self._post_mirror_topic_manager = post_mirror_topic_manager
        self._operation_gate = operation_gate
        self._history_fetch_wait_seconds = float(history_fetch_wait_seconds)
        self._history_post_delay_range_seconds = self._normalize_delay_range(history_post_delay_range_seconds)
        self._history_topic_create_delay_range_seconds = self._normalize_delay_range(
            history_topic_create_delay_range_seconds
        )
        self._randint = randint
        self._sleep = sleep
        self._logger = logger_ or logger
        self._lock = asyncio.Lock()
        self._queue_lock = asyncio.Lock()
        self._queue: deque[tuple[int | None, int | None, int | None]] = deque()
        self._active_request: tuple[int | None, int | None, int | None] | None = None
        self._queue_task: asyncio.Task[None] | None = None

    def bind(
        self,
        *,
        client: Any,
        state: Any,
        post_mirror_sender: Any,
        post_mirror_topic_manager: Any | None = None,
        operation_gate: PostMirrorOperationGate | None = None,
    ) -> None:
        self._client = client
        self._state = state
        self._post_mirror_sender = post_mirror_sender
        self._post_mirror_topic_manager = post_mirror_topic_manager
        if operation_gate is not None:
            self._operation_gate = operation_gate

    async def process_history(
        self,
        *,
        limit_per_source: int | None,
        chat_id: int | None = None,
        folder_id: int | None = None,
    ) -> PostMirrorHistoryBackfillResult:
        if self._client is None or self._state is None or self._post_mirror_sender is None:
            raise RuntimeError("Telegram user client недоступен.")
        if limit_per_source is not None and limit_per_source <= 0:
            raise ValueError("limit_per_source must be positive or None")
        if chat_id is not None and folder_id is not None:
            raise ValueError("chat_id and folder_id are mutually exclusive")

        async with self._lock:
            return await self._process_history_locked(
                limit_per_source=limit_per_source,
                chat_id=chat_id,
                folder_id=folder_id,
            )

    async def enqueue_history(
        self,
        *,
        limit_per_source: int | None,
        chat_id: int | None = None,
        folder_id: int | None = None,
    ) -> PostMirrorHistoryBackfillResult:
        if self._client is None or self._state is None or self._post_mirror_sender is None:
            raise RuntimeError("Telegram user client недоступен.")
        if limit_per_source is not None and limit_per_source <= 0:
            raise ValueError("limit_per_source must be positive or None")
        if chat_id is not None and folder_id is not None:
            raise ValueError("chat_id and folder_id are mutually exclusive")
        request = (
            limit_per_source,
            int(chat_id) if chat_id is not None else None,
            int(folder_id) if folder_id is not None else None,
        )
        async with self._queue_lock:
            duplicate_position = self._request_queue_position_locked(request)
            if duplicate_position is not None:
                return PostMirrorHistoryBackfillResult(
                    source_count=len(self._target_source_ids(chat_id, folder_id)),
                    scanned_count=0,
                    mirrored_count=0,
                    skipped_count=0,
                    failed_count=0,
                    limit_per_source=limit_per_source,
                    target_chat_id=self._state.get_post_mirror_target_chat_id(),
                    request_queued=True,
                    duplicate_queued=True,
                    queue_position=duplicate_position,
                )
            self._queue.append(request)
            queue_position = len(self._queue) + (1 if self._active_request is not None else 0)
            if self._queue_task is None or self._queue_task.done():
                self._queue_task = asyncio.create_task(
                    self._run_history_queue(),
                    name="post-mirror-history-queue",
                )
        return PostMirrorHistoryBackfillResult(
            source_count=len(self._target_source_ids(chat_id, folder_id)),
            scanned_count=0,
            mirrored_count=0,
            skipped_count=0,
            failed_count=0,
            limit_per_source=limit_per_source,
            target_chat_id=self._state.get_post_mirror_target_chat_id(),
            request_queued=True,
            queue_position=queue_position,
        )

    def _request_queue_position_locked(self, request: tuple[int | None, int | None, int | None]) -> int | None:
        if self._active_request == request:
            return 1
        offset = 1 if self._active_request is not None else 0
        for index, queued_request in enumerate(self._queue, start=offset + 1):
            if queued_request == request:
                return index
        return None

    async def wait_history_queue_idle(self) -> None:
        task = self._queue_task
        if task is not None:
            await task

    async def _run_history_queue(self) -> None:
        while True:
            async with self._queue_lock:
                if not self._queue:
                    self._active_request = None
                    self._queue_task = None
                    return
                request = self._queue.popleft()
                self._active_request = request
            try:
                limit_per_source, chat_id, folder_id = request
                await self.process_history(
                    limit_per_source=limit_per_source,
                    chat_id=chat_id,
                    folder_id=folder_id,
                )
            except Exception:
                self._logger.exception("post_mirror_history_job_failed")
            finally:
                async with self._queue_lock:
                    if self._active_request == request:
                        self._active_request = None

    async def _process_history_locked(
        self,
        *,
        limit_per_source: int | None,
        chat_id: int | None,
        folder_id: int | None,
    ) -> PostMirrorHistoryBackfillResult:
        target_chat_id = self._state.get_post_mirror_target_chat_id()
        source_ids = self._target_source_ids(chat_id, folder_id)
        if not self._state.is_post_mirroring_enabled() or target_chat_id is None or not source_ids:
            return PostMirrorHistoryBackfillResult(
                source_count=len(source_ids),
                scanned_count=0,
                mirrored_count=0,
                skipped_count=0,
                failed_count=0,
                limit_per_source=limit_per_source,
                target_chat_id=target_chat_id,
            )

        feature = PostMirrorFeature()
        context = SimpleNamespace(
            post_mirror_settings=self._state,
            post_mirror_sender=self._post_mirror_sender,
            post_mirror_topic_manager=self._post_mirror_topic_manager,
            post_mirror_topic_create_cooldown=self._sleep_after_history_topic_create,
            processed=self._state,
        )
        scanned_count = 0
        mirrored_count = 0
        skipped_count = 0
        failed_count = 0
        delay_next_history_post = False
        self._logger.info(
            "post_mirror_history_started source_count=%s limit_per_source=%s chat_id=%s folder_id=%s target_chat_id=%s",
            len(source_ids),
            limit_per_source,
            chat_id,
            folder_id,
            target_chat_id,
        )

        async def process_batch(source_chat_id: int, settings: Any, batch: tuple[Any, ...]) -> None:
            nonlocal mirrored_count, skipped_count, delay_next_history_post
            if delay_next_history_post:
                await self._sleep_between_history_posts()
                delay_next_history_post = False
            result = await self._handle_history_batch(
                feature=feature,
                context=context,
                source_chat_id=source_chat_id,
                settings=settings,
                batch=batch,
            )
            self._logger.info(
                "post_mirror_history_batch_result chat_id=%s message_id=%s message_count=%s grouped_id=%s result=%s",
                source_chat_id,
                int(batch[0].id),
                len(batch),
                (
                    int(getattr(batch[0], "grouped_id"))
                    if getattr(batch[0], "grouped_id", None) is not None
                    else None
                ),
                result,
            )
            if result == "mirrored":
                mirrored_count += 1
                delay_next_history_post = True
            else:
                skipped_count += 1

        for source_chat_id in source_ids:
            settings = self._state.get_post_mirror_source_settings(source_chat_id)
            if settings is None or not settings.enabled:
                skipped_count += 1
                self._logger.info(
                    "post_mirror_history_source_skipped chat_id=%s reason=%s",
                    source_chat_id,
                    "missing_settings" if settings is None else "disabled",
                )
                continue
            source_scanned_before = scanned_count
            source_mirrored_before = mirrored_count
            source_skipped_before = skipped_count
            self._logger.info(
                "post_mirror_history_source_started chat_id=%s has_topic=%s target_thread_id=%s limit_per_source=%s",
                source_chat_id,
                settings.target_thread_id is not None,
                settings.target_thread_id,
                limit_per_source,
            )
            try:
                if limit_per_source is None:
                    pending_album_key: int | None = None
                    pending_album: list[Any] = []
                    async for message in self._iter_history_messages(
                        source_chat_id,
                        reverse=True,
                    ):
                        scanned_count += 1
                        if getattr(message, "action", None) is not None:
                            if pending_album:
                                await process_batch(source_chat_id, settings, tuple(pending_album))
                                pending_album = []
                                pending_album_key = None
                            continue

                        grouped_id = getattr(message, "grouped_id", None)
                        if grouped_id is None:
                            if pending_album:
                                await process_batch(source_chat_id, settings, tuple(pending_album))
                                pending_album = []
                                pending_album_key = None
                            await process_batch(source_chat_id, settings, (message,))
                            continue

                        album_key = int(grouped_id)
                        if pending_album and pending_album_key != album_key:
                            await process_batch(source_chat_id, settings, tuple(pending_album))
                            pending_album = []
                        pending_album_key = album_key
                        pending_album.append(message)
                    if pending_album:
                        await process_batch(source_chat_id, settings, tuple(pending_album))
                else:
                    batches, scanned = await self._latest_post_batches(source_chat_id, limit_per_source)
                    scanned_count += scanned
                    for batch in batches:
                        await process_batch(source_chat_id, settings, batch)
                final_settings = self._state.get_post_mirror_source_settings(source_chat_id)
                self._logger.info(
                    "post_mirror_history_source_finished chat_id=%s scanned_count=%s mirrored_count=%s skipped_count=%s "
                    "target_thread_id=%s",
                    source_chat_id,
                    scanned_count - source_scanned_before,
                    mirrored_count - source_mirrored_before,
                    skipped_count - source_skipped_before,
                    getattr(final_settings, "target_thread_id", None),
                )
            except Exception:
                failed_count += 1
                self._logger.exception(
                    "post_mirror_history_source_failed chat_id=%s scanned_count=%s mirrored_count=%s skipped_count=%s",
                    source_chat_id,
                    scanned_count - source_scanned_before,
                    mirrored_count - source_mirrored_before,
                    skipped_count - source_skipped_before,
                )

        self._logger.info(
            "post_mirror_history_finished source_count=%s scanned_count=%s mirrored_count=%s skipped_count=%s "
            "failed_count=%s limit_per_source=%s target_chat_id=%s",
            len(source_ids),
            scanned_count,
            mirrored_count,
            skipped_count,
            failed_count,
            limit_per_source,
            target_chat_id,
        )
        return PostMirrorHistoryBackfillResult(
            source_count=len(source_ids),
            scanned_count=scanned_count,
            mirrored_count=mirrored_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            limit_per_source=limit_per_source,
            target_chat_id=target_chat_id,
        )

    def _target_source_ids(self, chat_id: int | None, folder_id: int | None = None) -> list[int]:
        target_chat_id = self._state.get_post_mirror_target_chat_id()
        if chat_id is not None:
            source_chat_id = int(chat_id)
            if target_chat_id is not None and source_chat_id == int(target_chat_id):
                return []
            return [source_chat_id] if self._state.get_post_mirror_source_settings(source_chat_id) is not None else []
        if folder_id is not None:
            folder_sources = getattr(self._state, "list_post_mirror_folder_sources", None)
            if not callable(folder_sources):
                return []
            source_ids = []
            for source in folder_sources(int(folder_id)):
                source_chat_id = int(source["chat_id"])
                if target_chat_id is not None and source_chat_id == int(target_chat_id):
                    continue
                settings = self._state.get_post_mirror_source_settings(source_chat_id)
                if settings is not None and settings.enabled:
                    source_ids.append(source_chat_id)
            return sorted(set(source_ids))
        source_ids = [
            int(source["source_chat_id"])
            for source in self._state.list_post_mirror_sources()
            if source["enabled"] and (target_chat_id is None or int(source["source_chat_id"]) != int(target_chat_id))
        ]
        folder_sources = getattr(self._state, "list_enabled_post_mirror_folder_sources", None)
        if callable(folder_sources):
            for source in folder_sources():
                source_chat_id = int(source["chat_id"])
                if target_chat_id is not None and source_chat_id == int(target_chat_id):
                    continue
                source_ids.append(source_chat_id)
        return sorted(set(source_ids))

    @staticmethod
    def _normalize_delay_range(delay_range: tuple[int, int]) -> tuple[int, int]:
        minimum, maximum = delay_range
        minimum = int(minimum)
        maximum = int(maximum)
        if minimum < 0 or maximum < minimum:
            raise ValueError("history_post_delay_range_seconds must be non-negative and ordered")
        return minimum, maximum

    async def _sleep_between_history_posts(self) -> None:
        minimum, maximum = self._history_post_delay_range_seconds
        if maximum <= 0:
            return
        await self._sleep_during_history_pause(self._randint(minimum, maximum))

    async def _sleep_after_history_topic_create(self, chat_id: int) -> None:
        minimum, maximum = self._history_topic_create_delay_range_seconds
        if maximum <= 0:
            return
        delay = self._randint(minimum, maximum)
        self._logger.info("post_mirror_history_topic_create_cooldown chat_id=%s delay_seconds=%s", chat_id, delay)
        await self._sleep_during_history_pause(delay)

    async def _sleep_during_history_pause(self, delay: float) -> None:
        if self._operation_gate is None:
            await self._sleep(delay)
            return
        await self._operation_gate.sleep_during_history_pause(delay, self._sleep)

    async def _yield_to_realtime_if_pending(self) -> None:
        if self._operation_gate is not None:
            await self._operation_gate.yield_to_realtime_if_pending()

    async def _interruptible_history_await(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        if self._operation_gate is None:
            return await factory()
        return await self._operation_gate.interruptible_history_await(factory)

    @asynccontextmanager
    async def _history_operation(self) -> Any:
        if self._operation_gate is None:
            yield
            return
        async with self._operation_gate.history_operation():
            yield

    async def _iter_history_messages(self, chat_id: int, *, reverse: bool) -> Any:
        last_yielded_id: int | None = None

        def make_iterator() -> Any:
            kwargs: dict[str, Any] = {
                "limit": None,
                "reverse": reverse,
                "wait_time": self._history_fetch_wait_seconds,
            }
            if last_yielded_id is not None:
                if reverse:
                    kwargs["min_id"] = last_yielded_id
                else:
                    kwargs["offset_id"] = last_yielded_id
            return self._client.iter_messages(chat_id, **kwargs).__aiter__()

        iterator = make_iterator()
        try:
            while True:
                await self._yield_to_realtime_if_pending()
                try:
                    message = await self._interruptible_history_await(iterator.__anext__)
                except telethon_errors.FloodWaitError as exc:
                    wait_seconds = post_mirror_flood_wait_delay_seconds(exc)
                    self._logger.warning(
                        "post_mirror_history_fetch_flood_wait chat_id=%s wait_seconds=%s",
                        chat_id,
                        wait_seconds,
                    )
                    await self._sleep_during_history_pause(wait_seconds)
                    continue
                except PostMirrorHistoryPreempted:
                    aclose = getattr(iterator, "aclose", None)
                    if callable(aclose):
                        await aclose()
                    iterator = make_iterator()
                    continue
                except StopAsyncIteration:
                    return
                await self._yield_to_realtime_if_pending()
                last_yielded_id = int(getattr(message, "id"))
                yield message
        finally:
            aclose = getattr(iterator, "aclose", None)
            if callable(aclose):
                await aclose()
            await mark_current_session_offline(self._client, logger_=self._logger)

    async def _handle_history_batch(
        self,
        *,
        feature: PostMirrorFeature,
        context: Any,
        source_chat_id: int,
        settings: Any,
        batch: tuple[Any, ...],
    ) -> str:
        event = PostMirrorEvent(
            chat_id=source_chat_id,
            message_id=int(batch[0].id),
            message_ids=tuple(int(message.id) for message in batch),
            is_channel=settings.kind == "channel",
            is_group=settings.kind == "group",
            grouped_id=(
                int(getattr(batch[0], "grouped_id"))
                if getattr(batch[0], "grouped_id", None) is not None
                else None
            ),
            messages=tuple(batch),
        )
        token = _POST_MIRROR_FLOOD_WAIT_SLEEP.set(self._sleep_during_history_pause)
        yield_token = _POST_MIRROR_REALTIME_YIELD.set(self._yield_to_realtime_if_pending)
        interruptible_token = _POST_MIRROR_INTERRUPTIBLE_HISTORY_AWAIT.set(self._interruptible_history_await)
        try:
            async with self._history_operation():
                while True:
                    try:
                        return await feature.handle(event, context)
                    except telethon_errors.FloodWaitError as exc:
                        wait_seconds = post_mirror_flood_wait_delay_seconds(exc)
                        self._logger.warning(
                            "post_mirror_history_flood_wait chat_id=%s wait_seconds=%s",
                            source_chat_id,
                            wait_seconds,
                        )
                        await self._sleep_during_history_pause(wait_seconds)
        finally:
            _POST_MIRROR_INTERRUPTIBLE_HISTORY_AWAIT.reset(interruptible_token)
            _POST_MIRROR_REALTIME_YIELD.reset(yield_token)
            _POST_MIRROR_FLOOD_WAIT_SLEEP.reset(token)

    async def _latest_post_batches(self, chat_id: int, limit: int | None) -> tuple[list[tuple[Any, ...]], int]:
        batches_by_key: OrderedDict[object, list[Any]] = OrderedDict()
        scanned = 0
        async for message in self._iter_history_messages(chat_id, reverse=False):
            if getattr(message, "action", None) is not None:
                if limit is not None and len(batches_by_key) >= limit:
                    break
                scanned += 1
                continue
            key: object = getattr(message, "grouped_id", None) or int(message.id)
            if limit is not None and len(batches_by_key) >= limit and key not in batches_by_key:
                break
            scanned += 1
            if key not in batches_by_key:
                batches_by_key[key] = []
            batches_by_key[key].append(message)
        newest_first = [tuple(reversed(messages)) for messages in batches_by_key.values()]
        return list(reversed(newest_first)), scanned


class TelethonChannelReactionSender:
    def __init__(
        self,
        client: Any,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        send_delay_seconds: float = 7.0,
        refresh_message_before_send: bool = True,
    ):
        self.client = client
        self.sleep = sleep
        self.send_delay_seconds = send_delay_seconds
        self.refresh_message_before_send = refresh_message_before_send

    async def available_reactions(self, chat_id: int) -> list[ReactionCandidate]:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.types import ChatReactionsAll, ReactionCustomEmoji, ReactionEmoji, ReactionPaid

        try:
            peer = await self.client.get_input_entity(chat_id)
        finally:
            await mark_current_session_offline(self.client)
        try:
            full_channel = await self.client(GetFullChannelRequest(peer))
        finally:
            await mark_current_session_offline(self.client)
        available = getattr(getattr(full_channel, "full_chat", None), "available_reactions", None)
        if not available:
            return []

        if isinstance(available, ChatReactionsAll):
            return [
                ReactionCandidate(kind="emoji", emoji=emoji, value=ReactionEmoji(emoticon=emoji), category=reaction_category(emoji))
                for emoji in DEFAULT_REACTION_EMOJIS
            ]

        reactions = getattr(available, "reactions", None)
        if not reactions:
            return []

        default_emojis = set(DEFAULT_REACTION_EMOJIS)
        candidates: list[ReactionCandidate] = []
        for reaction in reactions:
            if isinstance(reaction, ReactionPaid):
                continue
            if isinstance(reaction, ReactionCustomEmoji):
                document_id = str(getattr(reaction, "document_id", ""))
                candidates.append(
                    ReactionCandidate(
                        kind="custom",
                        emoji=document_id,
                        value=reaction,
                        category="neutral",
                    )
                )
            elif isinstance(reaction, ReactionEmoji) and reaction.emoticon in default_emojis:
                candidates.append(
                    ReactionCandidate(
                        kind="emoji",
                        emoji=reaction.emoticon,
                        value=reaction,
                        category=reaction_category(reaction.emoticon),
                    )
                )
        return candidates

    async def send_reactions(
        self,
        event: ChannelMessageEvent,
        reactions: list[ReactionCandidate],
        *,
        max_reactions: int,
        fallback_reactions: list[ReactionCandidate] | tuple[ReactionCandidate, ...] = (),
    ) -> int | ReactionSendResult:
        try:
            peer = await self.client.get_input_entity(event.chat_id)
        finally:
            await mark_current_session_offline(self.client)
        message = event.message or SimpleNamespace(id=event.message_id, reactions=None)
        result = await smart_set_telethon_reactions(
            self.client,
            peer,
            message,
            [reaction.value for reaction in reactions],
            fallback_reactions=[reaction.value for reaction in fallback_reactions],
            max_reactions=max_reactions,
            sleep=self.sleep,
            send_delay_seconds=self.send_delay_seconds,
            refresh_message_before_send=self.refresh_message_before_send,
            return_sent_reactions=True,
        )
        if not isinstance(result, TelethonReactionSendResult):
            return result
        candidates = [*reactions, *list(fallback_reactions)]
        if 0 <= result.count < max_reactions:
            fresh_candidates = await self.available_reactions(event.chat_id)
            sent_identifiers = {
                ident
                for reaction in result.reactions
                if (ident := telethon_reaction_identifier(reaction)) is not None
            }
            fresh_candidates = [
                candidate
                for candidate in fresh_candidates
                if telethon_reaction_identifier(candidate.value) not in sent_identifiers
            ]
            logger.info(
                "channel_reaction_underfill_refresh chat_id=%s sent=%s max=%s fresh_candidates=%s",
                event.chat_id,
                result.count,
                max_reactions,
                len(fresh_candidates),
            )
            if fresh_candidates:
                retry = await smart_set_telethon_reactions(
                    self.client,
                    peer,
                    message,
                    [reaction.value for reaction in fresh_candidates],
                    max_reactions=max_reactions,
                    sleep=self.sleep,
                    send_delay_seconds=0,
                    refresh_message_before_send=self.refresh_message_before_send,
                    return_sent_reactions=True,
                )
                if isinstance(retry, TelethonReactionSendResult) and retry.count > 0:
                    result = self._merge_telethon_reaction_send_results(
                        result,
                        retry,
                        max_reactions=max_reactions,
                    )
                    logger.info(
                        "channel_reaction_underfill_recovered chat_id=%s retry_sent=%s total=%s",
                        event.chat_id,
                        retry.count,
                        result.count,
                    )
                    candidates.extend(fresh_candidates)
        return self._reaction_send_result_from_telethon(
            result,
            candidates,
        )

    @staticmethod
    def _merge_telethon_reaction_send_results(
        first: TelethonReactionSendResult,
        second: TelethonReactionSendResult,
        *,
        max_reactions: int,
    ) -> TelethonReactionSendResult:
        reactions: list[Any] = []
        seen: set[object] = set()
        for reaction in [*first.reactions, *second.reactions]:
            identifier = telethon_reaction_identifier(reaction)
            if identifier is None or identifier in seen:
                continue
            reactions.append(reaction)
            seen.add(identifier)
        return TelethonReactionSendResult(
            min(max_reactions, len(reactions)),
            tuple(reactions[:max_reactions]),
        )

    @staticmethod
    def _reaction_send_result_from_telethon(
        result: TelethonReactionSendResult,
        candidates: list[ReactionCandidate],
    ) -> ReactionSendResult:
        keys_by_identifier: dict[object, tuple[str, str]] = {}
        for candidate in candidates:
            identifier = telethon_reaction_identifier(candidate.value)
            if identifier is not None and identifier not in keys_by_identifier:
                keys_by_identifier[identifier] = reaction_candidate_key(candidate)

        reaction_keys: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for reaction in result.reactions:
            identifier = telethon_reaction_identifier(reaction)
            key = keys_by_identifier.get(identifier)
            if key is None or key in seen:
                continue
            reaction_keys.append(key)
            seen.add(key)
        return ReactionSendResult(result.count, tuple(reaction_keys))


class CachedTelethonChannelReactionSender:
    def __init__(self, sender: Any, state: Any):
        self.sender = sender
        self.state = state

    async def available_reactions(self, chat_id: int) -> list[ReactionCandidate]:
        if self.state.has_reaction_channel_available_reactions_checked(chat_id):
            return [
                candidate
                for reaction in self.state.list_reaction_channel_available_reactions(chat_id)
                if (candidate := telethon_reaction_candidate_from_cache(reaction)) is not None
            ]
        return await self.sender.available_reactions(chat_id)

    async def send_reactions(
        self,
        event: ChannelMessageEvent,
        reactions: list[ReactionCandidate],
        *,
        max_reactions: int,
        fallback_reactions: list[ReactionCandidate] | tuple[ReactionCandidate, ...] = (),
    ) -> int | ReactionSendResult:
        return await self.sender.send_reactions(
            event,
            reactions,
            max_reactions=max_reactions,
            fallback_reactions=fallback_reactions,
        )


def is_paid_telethon_reaction(reaction: Any) -> bool:
    from telethon.tl.types import ReactionPaid

    return isinstance(reaction, ReactionPaid)


@dataclass(frozen=True)
class TelethonReactionSendResult:
    count: int
    reactions: tuple[Any, ...] = ()


def telethon_reaction_identifier(reaction: Any) -> object | None:
    from telethon.tl.types import ReactionCustomEmoji, ReactionEmoji

    if isinstance(reaction, ReactionCustomEmoji):
        return reaction.document_id
    if isinstance(reaction, ReactionEmoji):
        return reaction.emoticon
    return None


STANDARD_REACTION_DIVERSITY_FAMILIES: dict[str, frozenset[str]] = {
    "❤": frozenset({"heart"}),
    "❤‍🔥": frozenset({"heart", "fire"}),
    "💘": frozenset({"heart"}),
    "💔": frozenset({"heart", "sad"}),
    "🔥": frozenset({"fire"}),
    "👍": frozenset({"positive_hand"}),
    "👏": frozenset({"positive_hand"}),
    "👌": frozenset({"positive_hand"}),
    "🙏": frozenset({"positive_hand"}),
    "🤝": frozenset({"positive_hand"}),
    "👎": frozenset({"negative_hand"}),
    "🖕": frozenset({"negative_hand"}),
    "😁": frozenset({"happy_face"}),
    "🤣": frozenset({"happy_face"}),
    "🤩": frozenset({"happy_face", "star"}),
    "⭐": frozenset({"star"}),
    "😇": frozenset({"happy_face"}),
    "🤗": frozenset({"happy_face"}),
    "🥰": frozenset({"love_face"}),
    "😍": frozenset({"love_face"}),
    "😘": frozenset({"love_face", "kiss"}),
    "💋": frozenset({"kiss"}),
    "🤯": frozenset({"shock_attention"}),
    "😱": frozenset({"shock_attention"}),
    "😨": frozenset({"shock_attention"}),
    "👀": frozenset({"shock_attention"}),
    "🎉": frozenset({"celebration"}),
    "🏆": frozenset({"celebration"}),
    "🍾": frozenset({"celebration"}),
    "😭": frozenset({"sad"}),
    "😢": frozenset({"sad"}),
    "🤬": frozenset({"angry"}),
    "😡": frozenset({"angry"}),
    "🤮": frozenset({"disgust"}),
    "🤔": frozenset({"thinking"}),
    "🤨": frozenset({"thinking"}),
    "😐": frozenset({"neutral_face"}),
    "🥱": frozenset({"tired_face"}),
    "😴": frozenset({"tired_face"}),
    "🥴": frozenset({"dizzy_face"}),
    "😎": frozenset({"cool_face"}),
    "🆒": frozenset({"cool_face"}),
    "🤪": frozenset({"silly_face"}),
    "🤓": frozenset({"nerd_face"}),
    "💩": frozenset({"gross"}),
    "🤡": frozenset({"weird"}),
    "🗿": frozenset({"weird"}),
    "🌚": frozenset({"weird"}),
    "😈": frozenset({"devil"}),
    "👻": frozenset({"spooky"}),
    "🎃": frozenset({"spooky"}),
    "👾": frozenset({"spooky"}),
    "🙈": frozenset({"monkey"}),
    "🙉": frozenset({"monkey"}),
    "🙊": frozenset({"monkey"}),
    "🐳": frozenset({"animal"}),
    "🦄": frozenset({"animal"}),
    "🍌": frozenset({"food"}),
    "🌭": frozenset({"food"}),
    "🍓": frozenset({"food"}),
    "🎅": frozenset({"holiday"}),
    "🎄": frozenset({"holiday"}),
    "☃": frozenset({"holiday"}),
    "⚡": frozenset({"energy"}),
    "💯": frozenset({"approval"}),
    "🕊": frozenset({"peace"}),
    "💊": frozenset({"medical"}),
    "👨‍💻": frozenset({"work"}),
    "✍": frozenset({"writing"}),
    "💅": frozenset({"beauty"}),
    "🫡": frozenset({"salute"}),
    "🤷": frozenset({"shrug"}),
    "🤷‍♂": frozenset({"shrug"}),
    "🤷‍♀": frozenset({"shrug"}),
}


def telethon_reaction_diversity_families(reaction: Any) -> frozenset[str]:
    ident = telethon_reaction_identifier(reaction)
    if not isinstance(ident, str):
        return frozenset()
    return STANDARD_REACTION_DIVERSITY_FAMILIES.get(ident, frozenset())


def message_installed_telethon_reaction_count(message: Any) -> int:
    reactions = getattr(message, "reactions", None)
    if reactions is None or not hasattr(reactions, "results"):
        return 0
    return sum(
        1
        for reaction_count in reactions.results
        if getattr(reaction_count, "chosen_order", None) is not None
    )


def message_has_installed_telethon_reaction(message: Any) -> bool:
    return message_installed_telethon_reaction_count(message) > 0


def order_telethon_reactions_for_diversity(
    reactions: list[Any],
    *,
    visible_ids: set[object],
    visible_reactions: list[Any],
) -> list[Any]:
    used_families: set[str] = set()
    for reaction in visible_reactions:
        used_families.update(telethon_reaction_diversity_families(reaction))

    unseen_reactions = [
        reaction
        for reaction in reactions
        if telethon_reaction_identifier(reaction) not in visible_ids
    ]
    visible_candidates = [
        reaction
        for reaction in reactions
        if telethon_reaction_identifier(reaction) in visible_ids
    ]

    ordered: list[Any] = []
    remaining = list(unseen_reactions)
    while remaining:
        distinct_index = next(
            (
                index
                for index, reaction in enumerate(remaining)
                if not (telethon_reaction_diversity_families(reaction) & used_families)
            ),
            None,
        )
        if distinct_index is None:
            distinct_index = 0
        reaction = remaining.pop(distinct_index)
        ordered.append(reaction)
        used_families.update(telethon_reaction_diversity_families(reaction))
    return [*ordered, *visible_candidates]


def telethon_reaction_candidate_from_cache(reaction: dict[str, str]) -> ReactionCandidate | None:
    from telethon.tl.types import ReactionCustomEmoji, ReactionEmoji

    kind = str(reaction.get("kind") or "emoji")
    emoji = str(reaction.get("emoji") or "")
    category = str(reaction.get("category") or reaction_category(emoji))
    if kind == "emoji":
        return ReactionCandidate(
            kind="emoji",
            emoji=emoji,
            value=ReactionEmoji(emoticon=emoji),
            category=category,
        )
    if kind in {"custom", "premium"}:
        try:
            document_id = int(emoji)
        except ValueError:
            return None
        return ReactionCandidate(
            kind="custom",
            emoji=emoji,
            value=ReactionCustomEmoji(document_id=document_id),
            category=category,
        )
    return None


async def refresh_telethon_message_for_reactions(client: Any, peer: Any, message: Any) -> Any:
    get_messages = getattr(client, "get_messages", None)
    if get_messages is None:
        return message
    try:
        fresh_message = await get_messages(peer, ids=int(message.id))
    finally:
        await mark_current_session_offline(client)
    return fresh_message or message


async def smart_set_telethon_reactions(
    client: Any,
    peer: Any,
    message: Any,
    selected_reactions: list[Any],
    *,
    fallback_reactions: list[Any] | tuple[Any, ...] = (),
    max_reactions: int = 3,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    send_delay_seconds: float = 7.0,
    refresh_message_before_send: bool = True,
    return_sent_reactions: bool = False,
) -> int | TelethonReactionSendResult:
    from telethon.tl.functions.messages import SendReactionRequest

    def result(count: int, reactions: list[Any] | tuple[Any, ...] = ()) -> int | TelethonReactionSendResult:
        normalized_count = max(0, int(count))
        if return_sent_reactions:
            return TelethonReactionSendResult(normalized_count, tuple(reactions))
        return normalized_count

    if send_delay_seconds > 0:
        await sleep(send_delay_seconds)
    if refresh_message_before_send:
        message = await refresh_telethon_message_for_reactions(client, peer, message)

    installed_ids = set()
    visible_ids = set()
    visible_reactions = []
    installed_reactions = []
    if getattr(message, "reactions", None) and hasattr(message.reactions, "results"):
        for reaction_count in message.reactions.results:
            ident = telethon_reaction_identifier(reaction_count.reaction)
            if ident is not None:
                visible_ids.add(ident)
                visible_reactions.append(reaction_count.reaction)
            chosen_order = getattr(reaction_count, "chosen_order", None)
            if chosen_order is None:
                continue
            if ident is not None:
                installed_ids.add(ident)
                installed_reactions.append((int(chosen_order), reaction_count.reaction))

    installed_reactions = [
        reaction
        for _, reaction in sorted(installed_reactions, key=lambda item: item[0])
    ][:max_reactions]
    open_slots = max(0, max_reactions - len(installed_reactions))
    if open_slots <= 0:
        return result(0)

    selected_unique, seen_candidate_ids = unique_telethon_reactions(selected_reactions, installed_ids)
    fallback_unique, _ = unique_telethon_reactions(
        list(fallback_reactions),
        installed_ids | seen_candidate_ids,
    )
    fallback_ids = {
        ident
        for reaction in fallback_unique
        if (ident := telethon_reaction_identifier(reaction)) is not None
    }
    visible_fallback_unique, _ = unique_telethon_reactions(
        visible_reactions,
        installed_ids | seen_candidate_ids | fallback_ids,
    )
    candidate_queue = [*selected_unique, *fallback_unique, *visible_fallback_unique]
    candidate_queue = order_telethon_reactions_for_diversity(
        candidate_queue,
        visible_ids=visible_ids,
        visible_reactions=visible_reactions,
    )
    target_count = min(open_slots, len(candidate_queue))
    active_reactions = candidate_queue[:target_count]
    fallback_queue = candidate_queue[target_count:]
    if not active_reactions:
        return result(0)

    def is_visible_candidate(reaction: Any) -> bool:
        return telethon_reaction_identifier(reaction) in visible_ids

    def replace_unseen_with_visible_fallback() -> bool:
        active_index = next(
            (
                index
                for index, reaction in enumerate(active_reactions)
                if not is_visible_candidate(reaction)
            ),
            None,
        )
        if active_index is None:
            return False
        fallback_index = next(
            (
                index
                for index, reaction in enumerate(fallback_queue)
                if is_visible_candidate(reaction)
            ),
            None,
        )
        if fallback_index is None:
            return False
        active_reactions[active_index] = fallback_queue.pop(fallback_index)
        return True

    while active_reactions:
        reaction_vector = [*installed_reactions, *active_reactions]
        try:
            try:
                await client(
                    SendReactionRequest(peer=peer, msg_id=message.id, reaction=reaction_vector)
                )
            finally:
                await mark_current_session_offline(client)
            return result(len(active_reactions), active_reactions)
        except Exception as error:
            error_text = str(error)
            if "reactions_uniq_max" in error_text.lower() or "CUSTOM_REACTIONS_TOO_MANY" in error_text.upper():
                if visible_ids and replace_unseen_with_visible_fallback():
                    continue
                if len(active_reactions) <= 1:
                    return result(0)
                active_reactions.pop()
                target_count = len(active_reactions)
                continue
            if is_reaction_rejected_error(error_text):
                active_reactions.pop(0)
                while fallback_queue and len(active_reactions) < target_count:
                    active_reactions.append(fallback_queue.pop(0))
                continue
            raise
    return result(0)


def unique_telethon_reactions(
    reactions: list[Any] | tuple[Any, ...],
    installed_ids: set[object],
) -> tuple[list[Any], set[object]]:
    unique: list[Any] = []
    seen = set(installed_ids)
    added: set[object] = set()
    for reaction in reactions:
        if is_paid_telethon_reaction(reaction):
            continue
        ident = telethon_reaction_identifier(reaction)
        if ident is None or ident in seen:
            continue
        unique.append(reaction)
        seen.add(ident)
        added.add(ident)
    return unique, added


def is_reaction_rejected_error(error_text: str) -> bool:
    upper = error_text.upper()
    return any(
        marker in upper
        for marker in (
            "REACTION_INVALID",
            "REACTION_EMPTY",
            "REACTION_FORBIDDEN",
            "REACTION_NOT_ALLOWED",
        )
    )


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
DEFAULT_POST_MIRROR_QUEUE_MAXSIZE = 0


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


class PostMirrorQueueWorker:
    def __init__(
        self,
        handler: Callable[[Any], Awaitable[Any]],
        *,
        maxsize: int = DEFAULT_POST_MIRROR_QUEUE_MAXSIZE,
        operation_gate: PostMirrorOperationGate | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        logger_: logging.Logger | None = None,
    ) -> None:
        if maxsize < 0:
            raise ValueError("maxsize must be non-negative")
        self._handler = handler
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._operation_gate = operation_gate
        self._sleep = sleep
        self._logger = logger_ or logger
        self.dropped_count = 0
        self.processed_count = 0

    def submit(self, event: Any) -> bool:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_count += 1
            self._logger.warning(
                "post_mirror_dropped_queue_full chat_id=%s message_id=%s queue_size=%s",
                getattr(event, "chat_id", "?"),
                getattr(getattr(event, "message", None), "id", "?"),
                self._queue.maxsize,
            )
            return False
        if self._operation_gate is not None:
            self._operation_gate.notify_realtime_queued()
        return True

    async def run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                if self._operation_gate is None:
                    await self._handle_event_with_flood_wait_retry(event)
                else:
                    async with self._operation_gate.realtime_operation():
                        await self._handle_event_with_flood_wait_retry(event)
                self.processed_count += 1
            except Exception:
                self._logger.exception(
                    "post_mirror_consumer_handler_failed chat_id=%s",
                    getattr(event, "chat_id", "?"),
                )
            finally:
                self._queue.task_done()

    async def _handle_event_with_flood_wait_retry(self, event: Any) -> None:
        while True:
            try:
                await self._handler(event)
                return
            except telethon_errors.FloodWaitError as exc:
                wait_seconds = post_mirror_flood_wait_delay_seconds(exc)
                self._logger.warning(
                    "post_mirror_realtime_flood_wait chat_id=%s message_id=%s wait_seconds=%s",
                    getattr(event, "chat_id", "?"),
                    getattr(getattr(event, "message", None), "id", "?"),
                    wait_seconds,
                )
                await self._sleep(wait_seconds)


DEFAULT_CHANNEL_REACTION_QUEUE_MAXSIZE = 100
CHANNEL_REACTION_HISTORY_FETCH_WAIT_SECONDS = 1.0
CHANNEL_REACTION_HISTORY_LIMIT_CHOICES = frozenset({1000, 2000, 5000})
CHANNEL_REACTION_HISTORY_SEND_DELAY_RANGE_SECONDS = (8, 15)
CHANNEL_REACTION_POST_DISPATCH_DELAY_RANGE_SECONDS = (8, 11)


@dataclass(frozen=True)
class ChannelReactionHistoryBackfillResult:
    channel_count: int
    scanned_count: int
    sent_count: int
    skipped_count: int
    limit_per_channel: int | None
    target_chat_id: int | None = None
    reaction_count: int = 0
    failed_count: int = 0
    already_running: bool = False
    request_queued: bool = False
    duplicate_queued: bool = False
    queue_position: int | None = None
    skip_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def queued_count(self) -> int:
        return self.sent_count


@dataclass(frozen=True)
class ChannelReactionHistoryBackfillRequest:
    limit_per_channel: int | None
    chat_id: int | None


class ChannelReactionHistoryBackfill:
    def __init__(
        self,
        *,
        client: Any | None = None,
        state: Any | None = None,
        reaction_sender: Any | None = None,
        history_fetch_wait_seconds: float = CHANNEL_REACTION_HISTORY_FETCH_WAIT_SECONDS,
        send_delay_range_seconds: tuple[int, int] = CHANNEL_REACTION_HISTORY_SEND_DELAY_RANGE_SECONDS,
        randint: Callable[[int, int], int] = random.randint,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        completion_notifier: Callable[[ChannelReactionHistoryBackfillResult], Awaitable[Any]] | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._state = state
        self._reaction_sender = reaction_sender
        self._history_fetch_wait_seconds = float(history_fetch_wait_seconds)
        self._send_delay_range_seconds = send_delay_range_seconds
        self._randint = randint
        self._sleep = sleep
        self._completion_notifier = completion_notifier
        self._logger = logger_ or logger
        self._lock = asyncio.Lock()
        self._queue_lock = asyncio.Lock()
        self._queue: deque[ChannelReactionHistoryBackfillRequest] = deque()
        self._active_request: ChannelReactionHistoryBackfillRequest | None = None
        self._queue_task: asyncio.Task[None] | None = None

    def bind(self, *, client: Any, state: Any, reaction_sender: Any) -> None:
        self._client = client
        self._state = state
        self._reaction_sender = reaction_sender

    def set_completion_notifier(
        self,
        notifier: Callable[[ChannelReactionHistoryBackfillResult], Awaitable[Any]] | None,
    ) -> None:
        self._completion_notifier = notifier

    async def enqueue_recent_posts(
        self,
        *,
        limit_per_channel: int | None,
        chat_id: int | None = None,
    ) -> ChannelReactionHistoryBackfillResult:
        return await self.enqueue_history(limit_per_channel=limit_per_channel, chat_id=chat_id)

    async def enqueue_history(
        self,
        *,
        limit_per_channel: int | None,
        chat_id: int | None = None,
    ) -> ChannelReactionHistoryBackfillResult:
        if self._client is None or self._state is None or self._reaction_sender is None:
            raise RuntimeError("Telegram user client недоступен.")

        limit = self._normalize_limit(limit_per_channel)
        target_chat_id = int(chat_id) if chat_id is not None else None
        channel_count = len(self._target_channel_ids(target_chat_id))
        if channel_count <= 0:
            return ChannelReactionHistoryBackfillResult(
                channel_count=0,
                scanned_count=0,
                sent_count=0,
                skipped_count=0,
                limit_per_channel=limit,
                target_chat_id=target_chat_id,
            )

        request = ChannelReactionHistoryBackfillRequest(
            limit_per_channel=limit,
            chat_id=target_chat_id,
        )
        async with self._queue_lock:
            duplicate_position = self._request_queue_position_locked(request)
            if duplicate_position is not None:
                return ChannelReactionHistoryBackfillResult(
                    channel_count=channel_count,
                    scanned_count=0,
                    sent_count=0,
                    skipped_count=0,
                    limit_per_channel=limit,
                    target_chat_id=target_chat_id,
                    request_queued=True,
                    duplicate_queued=True,
                    queue_position=duplicate_position,
                )

            self._queue.append(request)
            queue_position = self._request_queue_position_locked(request) or len(self._queue)
            if self._queue_task is None or self._queue_task.done():
                self._queue_task = asyncio.create_task(
                    self._run_history_queue(),
                    name="channel-reaction-history-queue",
                )

        self._logger.info(
            "channel_reaction_history_enqueued chat_id=%s limit=%s position=%s channel_count=%s",
            target_chat_id,
            limit,
            queue_position,
            channel_count,
        )
        return ChannelReactionHistoryBackfillResult(
            channel_count=channel_count,
            scanned_count=0,
            sent_count=0,
            skipped_count=0,
            limit_per_channel=limit,
            target_chat_id=target_chat_id,
            request_queued=True,
            queue_position=queue_position,
        )

    async def wait_history_queue_idle(self) -> None:
        task = self._queue_task
        if task is not None:
            await task

    def _request_queue_position_locked(self, request: ChannelReactionHistoryBackfillRequest) -> int | None:
        if self._active_request == request:
            return 1
        offset = 1 if self._active_request is not None else 0
        for index, queued_request in enumerate(self._queue, start=offset + 1):
            if queued_request == request:
                return index
        return None

    async def _run_history_queue(self) -> None:
        while True:
            async with self._queue_lock:
                if not self._queue:
                    self._active_request = None
                    self._queue_task = None
                    return
                request = self._queue.popleft()
                self._active_request = request

            try:
                self._logger.info(
                    "channel_reaction_history_job_started chat_id=%s limit=%s",
                    request.chat_id,
                    request.limit_per_channel,
                )
                async with self._lock:
                    result = await self._process_history_locked(
                        limit_per_channel=request.limit_per_channel,
                        chat_id=request.chat_id,
                    )
                self._logger.info(
                    "channel_reaction_history_job_complete chat_id=%s limit=%s scanned=%s sent=%s reactions=%s skipped=%s failed=%s",
                    request.chat_id,
                    request.limit_per_channel,
                    result.scanned_count,
                    result.sent_count,
                    result.reaction_count,
                    result.skipped_count,
                    result.failed_count,
                )
                await self._notify_history_completion(result)
            except Exception:
                self._logger.exception(
                    "channel_reaction_history_job_failed chat_id=%s limit=%s",
                    request.chat_id,
                    request.limit_per_channel,
                )
                await self._notify_history_completion(
                    ChannelReactionHistoryBackfillResult(
                        channel_count=0,
                        scanned_count=0,
                        sent_count=0,
                        skipped_count=0,
                        failed_count=1,
                        limit_per_channel=request.limit_per_channel,
                        target_chat_id=request.chat_id,
                    )
                )
            finally:
                async with self._queue_lock:
                    if self._active_request == request:
                        self._active_request = None

    async def _notify_history_completion(self, result: ChannelReactionHistoryBackfillResult) -> None:
        if self._completion_notifier is None:
            return
        try:
            await self._completion_notifier(result)
        except Exception:
            self._logger.exception(
                "channel_reaction_history_completion_notify_failed chat_id=%s limit=%s",
                result.target_chat_id,
                result.limit_per_channel,
            )

    async def process_history(
        self,
        *,
        limit_per_channel: int | None,
        chat_id: int | None = None,
    ) -> ChannelReactionHistoryBackfillResult:
        limit = self._normalize_limit(limit_per_channel)
        target_chat_id = int(chat_id) if chat_id is not None else None
        if self._lock.locked():
            return ChannelReactionHistoryBackfillResult(
                channel_count=0,
                scanned_count=0,
                sent_count=0,
                skipped_count=0,
                limit_per_channel=limit,
                target_chat_id=target_chat_id,
                already_running=True,
                skip_reasons={},
            )
        async with self._lock:
            return await self._process_history_locked(limit_per_channel=limit, chat_id=target_chat_id)

    async def _process_history_locked(
        self,
        *,
        limit_per_channel: int | None,
        chat_id: int | None,
    ) -> ChannelReactionHistoryBackfillResult:
        if self._client is None or self._state is None or self._reaction_sender is None:
            raise RuntimeError("Telegram user client недоступен.")

        channel_ids = self._target_channel_ids(chat_id)
        scanned_count = 0
        sent_count = 0
        reaction_count = 0
        skipped_count = 0
        failed_count = 0
        skip_reasons: Counter[str] = Counter()
        last_random_reaction_keys_by_chat: dict[int, frozenset[tuple[str, str]]] = {}
        handled_media_groups_in_run: set[tuple[int, int]] = set()
        manual_target = chat_id is not None
        for target_chat_id in channel_ids:
            channel_scanned_before = scanned_count
            channel_sent_before = sent_count
            channel_reaction_before = reaction_count
            channel_skipped_before = skipped_count
            channel_failed_before = failed_count
            channel_skip_reasons: Counter[str] = Counter()
            try:
                settings = self._state.get_effective_reaction_channel_settings(target_chat_id)
                if settings is None or (not settings.enabled and not manual_target):
                    continue
                if not settings.enabled and manual_target:
                    settings = replace(settings, enabled=True)
                available = await self._available_reactions(target_chat_id)
                is_premium = self._state.is_account_premium()
                max_reactions = effective_max_reactions(settings, is_premium=is_premium)
                target_ordered = order_reaction_candidates(
                    available,
                    settings,
                    is_premium=is_premium,
                    chooser=lambda reactions: reactions,
                )
                target_reaction_count = min(max_reactions, len(target_ordered))
                target_count = 0
                async for message in self._iter_channel_history_messages(target_chat_id):
                    scanned_count += 1
                    event = self._history_event(target_chat_id, message)
                    grouped_id = getattr(message, "grouped_id", None)
                    media_group_key = (
                        (target_chat_id, int(grouped_id))
                        if grouped_id is not None
                        else None
                    )
                    skip_reason = (
                        "media_group_duplicate"
                        if media_group_key in handled_media_groups_in_run
                        else self._history_skip_reason(
                            event,
                            allow_disabled_channel=manual_target,
                            target_reaction_count=target_reaction_count,
                        )
                    )
                    if skip_reason is not None:
                        if (
                            media_group_key is not None
                            and skip_reason in {"already_processed", "media_group_duplicate"}
                        ):
                            handled_media_groups_in_run.add(media_group_key)
                        skip_reasons[skip_reason] += 1
                        channel_skip_reasons[skip_reason] += 1
                        skipped_count += 1
                        continue
                    target_count += 1
                    channel_event = build_channel_message_event(event)
                    ordered = order_reaction_candidates(
                        available,
                        settings,
                        is_premium=is_premium,
                    )
                    avoid_keys = (
                        last_random_reaction_keys_by_chat.get(target_chat_id)
                        if settings.selection_strategy == "random"
                        else None
                    )
                    selected = select_from_ordered_reaction_candidates(
                        ordered,
                        max_reactions,
                        avoid_reaction_keys=avoid_keys,
                    )
                    if not selected:
                        skip_reasons["no_reactions_available"] += 1
                        channel_skip_reasons["no_reactions_available"] += 1
                        skipped_count += 1
                        if limit_per_channel is not None and target_count >= limit_per_channel:
                            break
                        continue
                    try:
                        await self._sleep_before_send()
                        sent_reactions = await self._reaction_sender.send_reactions(
                            channel_event,
                            selected,
                            max_reactions=max_reactions,
                            fallback_reactions=fallback_reaction_candidates(ordered, selected),
                        )
                    except Exception:
                        failed_count += 1
                        self._logger.exception(
                            "channel_reaction_history_message_failed chat_id=%s message_id=%s",
                            target_chat_id,
                            channel_event.message_id,
                        )
                        if limit_per_channel is not None and target_count >= limit_per_channel:
                            break
                        continue
                    sent_reaction_count = resolve_sent_reaction_count(sent_reactions, selected)
                    if sent_reaction_count <= 0:
                        skip_reasons["no_reactions_sent"] += 1
                        channel_skip_reasons["no_reactions_sent"] += 1
                        skipped_count += 1
                        if limit_per_channel is not None and target_count >= limit_per_channel:
                            break
                        continue
                    reaction_count += sent_reaction_count
                    if settings.selection_strategy == "random":
                        actual_reaction_keys = resolve_sent_reaction_keys(sent_reactions)
                        last_random_reaction_keys_by_chat[target_chat_id] = frozenset(
                            reaction_candidate_key(reaction)
                            for reaction in selected
                        ) if actual_reaction_keys is None else actual_reaction_keys
                    self._state.mark_processed(
                        target_chat_id,
                        channel_event.message_id,
                        ChannelReactionFeature.name,
                    )
                    if channel_event.grouped_id is not None:
                        handled_media_groups_in_run.add(
                            (target_chat_id, int(channel_event.grouped_id))
                        )
                        self._state.mark_processed(
                            target_chat_id,
                            int(channel_event.grouped_id),
                            ChannelReactionFeature.media_group_feature,
                        )
                    sent_count += 1
                    if limit_per_channel is not None and target_count >= limit_per_channel:
                        break
            except Exception:
                failed_count += 1
                self._logger.exception("channel_reaction_history_backfill_failed chat_id=%s", target_chat_id)
            finally:
                self._logger.info(
                    "channel_reaction_history_channel_complete chat_id=%s scanned=%s sent=%s reactions=%s skipped=%s failed=%s skip_reasons=%s",
                    target_chat_id,
                    scanned_count - channel_scanned_before,
                    sent_count - channel_sent_before,
                    reaction_count - channel_reaction_before,
                    skipped_count - channel_skipped_before,
                    failed_count - channel_failed_before,
                    dict(channel_skip_reasons),
                )

        return ChannelReactionHistoryBackfillResult(
            channel_count=len(channel_ids),
            scanned_count=scanned_count,
            sent_count=sent_count,
            reaction_count=reaction_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            limit_per_channel=limit_per_channel,
            target_chat_id=chat_id,
            skip_reasons=dict(skip_reasons),
        )

    @staticmethod
    def _normalize_limit(limit_per_channel: int | None) -> int | None:
        if limit_per_channel is None:
            return None
        limit = int(limit_per_channel)
        if limit not in CHANNEL_REACTION_HISTORY_LIMIT_CHOICES:
            raise ValueError("limit_per_channel must be 1000, 2000, 5000 or None")
        return limit

    async def _sleep_before_send(self) -> None:
        minimum, maximum = self._send_delay_range_seconds
        if minimum < 0 or maximum < minimum:
            minimum, maximum = CHANNEL_REACTION_HISTORY_SEND_DELAY_RANGE_SECONDS
        await self._sleep(self._randint(int(minimum), int(maximum)))

    async def _available_reactions(self, chat_id: int) -> list[ReactionCandidate]:
        cached = self._cached_available_reactions(chat_id)
        if cached is not None:
            return cached
        available = await self._reaction_sender.available_reactions(chat_id)
        self._state.replace_reaction_channel_available_reactions(chat_id, available)
        return available

    def _cached_available_reactions(self, chat_id: int) -> list[ReactionCandidate] | None:
        if not self._state.has_reaction_channel_available_reactions_checked(chat_id):
            return None
        return [
            candidate
            for reaction in self._state.list_reaction_channel_available_reactions(chat_id)
            if (candidate := self._reaction_candidate_from_cache(reaction)) is not None
        ]

    @staticmethod
    def _reaction_candidate_from_cache(reaction: dict[str, str]) -> ReactionCandidate | None:
        return telethon_reaction_candidate_from_cache(reaction)

    def _target_channel_ids(self, chat_id: int | None) -> list[int]:
        if not self._state.is_reaction_autolike_enabled():
            return []
        if chat_id is not None:
            settings = self._state.get_effective_reaction_channel_settings(int(chat_id))
            return [int(chat_id)] if settings is not None else []

        chat_ids: list[int] = []
        seen: set[int] = set()

        def add(value: Any) -> None:
            channel_id = int(value)
            if channel_id not in seen:
                seen.add(channel_id)
                chat_ids.append(channel_id)

        for channel in self._state.list_reaction_channels():
            add(channel["chat_id"])
        for chat in self._state.list_known_chats(kind="channel"):
            add(chat["chat_id"])
        for folder in self._state.list_reaction_folders():
            for channel in self._state.list_reaction_folder_channels(int(folder["folder_id"])):
                add(channel["chat_id"])

        enabled_ids: list[int] = []
        for channel_id in chat_ids:
            settings = self._state.get_effective_reaction_channel_settings(channel_id)
            if settings is not None and settings.enabled:
                enabled_ids.append(channel_id)
        return enabled_ids

    async def _iter_channel_history_messages(self, chat_id: int) -> Any:
        iterator = self._client.iter_messages(
            chat_id,
            limit=None,
            wait_time=self._history_fetch_wait_seconds,
        ).__aiter__()
        try:
            while True:
                try:
                    yield await iterator.__anext__()
                except StopAsyncIteration:
                    return
        finally:
            aclose = getattr(iterator, "aclose", None)
            if callable(aclose):
                await aclose()
            await mark_current_session_offline(self._client, logger_=self._logger)

    @staticmethod
    def _history_event(chat_id: int, message: Any) -> Any:
        return SimpleNamespace(
            chat_id=int(chat_id),
            message=message,
            is_channel=True,
            is_group=False,
            is_private=False,
            chat=None,
        )

    def _history_skip_reason(
        self,
        event: Any,
        *,
        allow_disabled_channel: bool = False,
        target_reaction_count: int | None = None,
    ) -> str | None:
        if not is_reactable_channel_message(event):
            return "service_message" if getattr(event.message, "action", None) is not None else "not_reactable"
        if not self._state.is_reaction_autolike_enabled():
            return "global_disabled"
        settings = self._state.get_effective_reaction_channel_settings(int(event.chat_id))
        if settings is None or (not settings.enabled and not allow_disabled_channel):
            return "channel_disabled"
        message = event.message
        message_id = int(message.id)
        installed_reaction_count = message_installed_telethon_reaction_count(message)
        if target_reaction_count is None:
            target_reaction_count = effective_max_reactions(
                settings,
                is_premium=self._state.is_account_premium(),
            )
        has_enough_installed_reactions = (
            target_reaction_count > 0 and installed_reaction_count >= target_reaction_count
        )
        if (
            self._state.is_processed(int(event.chat_id), message_id, ChannelReactionFeature.name)
            and has_enough_installed_reactions
        ):
            return "already_processed"
        grouped_id = getattr(message, "grouped_id", None)
        if (
            grouped_id is not None
            and self._state.is_processed(
                int(event.chat_id),
                int(grouped_id),
                ChannelReactionFeature.media_group_feature,
            )
            and has_enough_installed_reactions
        ):
            return "media_group_duplicate"
        return None


class ChannelReactionQueueWorker:
    def __init__(
        self,
        handler: Callable[[Any], Awaitable[Any]],
        *,
        maxsize: int = DEFAULT_CHANNEL_REACTION_QUEUE_MAXSIZE,
        dedupe_window_size: int = 1000,
        delay_range_seconds: tuple[int, int] = (300, 900),
        delay_range_provider: Callable[[], tuple[int, int]] | None = None,
        post_dispatch_delay_range_seconds: tuple[int, int] = (0, 0),
        randint: Callable[[int, int], int] = random.randint,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        logger_: logging.Logger | None = None,
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be a positive integer")
        if dedupe_window_size <= 0:
            raise ValueError("dedupe_window_size must be a positive integer")
        self._handler = handler
        self._maxsize = maxsize
        self._dedupe_window_size = dedupe_window_size
        self._seen_event_keys: OrderedDict[tuple[int, int], None] = OrderedDict()
        self._pending_tasks: set[asyncio.Task[Any]] = set()
        self._dispatch_lock = asyncio.Lock()
        self._delay_range_seconds = delay_range_seconds
        self._delay_range_provider = delay_range_provider
        self._post_dispatch_delay_range_seconds = post_dispatch_delay_range_seconds
        self._randint = randint
        self._sleep = sleep
        self._logger = logger_ or logger
        self.dropped_count = 0
        self.processed_count = 0

    @property
    def maxsize(self) -> int:
        return self._maxsize

    def qsize(self) -> int:
        self._prune_finished_tasks()
        return len(self._pending_tasks)

    def submit(self, event: Any) -> bool:
        self._prune_finished_tasks()
        event_key = self._event_key(event)
        if event_key in self._seen_event_keys:
            self._logger.info(
                "channel_reaction_duplicate_skipped chat_id=%s message_id=%s",
                event_key[0],
                event_key[1],
            )
            return False
        if len(self._pending_tasks) >= self._maxsize:
            self.dropped_count += 1
            self._logger.warning(
                "channel_reaction_dropped_queue_full chat_id=%s message_id=%s queue_size=%s",
                getattr(event, "chat_id", "?"),
                getattr(getattr(event, "message", None), "id", "?"),
                self._maxsize,
            )
            return False
        self._remember_event_key(event_key)
        task = asyncio.create_task(self._run_delayed(event), name="channel-reaction-delayed")
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return True

    async def run(self) -> None:
        try:
            await asyncio.Future()
        finally:
            pending_tasks = tuple(self._pending_tasks)
            for task in pending_tasks:
                task.cancel()
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)

    async def _run_delayed(self, event: Any) -> None:
        try:
            delay = self._next_delay_seconds()
            self._logger.info(
                "channel_reaction_scheduled chat_id=%s message_id=%s delay_seconds=%s pending_count=%s",
                getattr(event, "chat_id", "?"),
                getattr(getattr(event, "message", None), "id", "?"),
                delay,
                len(self._pending_tasks),
            )
            await self._sleep(delay)
            async with self._dispatch_lock:
                await self._handler(event)
                post_dispatch_delay = self._next_post_dispatch_delay_seconds()
                if post_dispatch_delay > 0:
                    await self._sleep(post_dispatch_delay)
            self.processed_count += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception(
                "channel_reaction_consumer_handler_failed chat_id=%s",
                getattr(event, "chat_id", "?"),
            )

    def _prune_finished_tasks(self) -> None:
        for task in tuple(self._pending_tasks):
            if task.done():
                self._pending_tasks.discard(task)

    def _next_delay_seconds(self) -> int:
        minimum, maximum = (
            self._delay_range_provider()
            if self._delay_range_provider is not None
            else self._delay_range_seconds
        )
        if minimum < 0 or maximum < minimum:
            minimum, maximum = self._delay_range_seconds
        return self._randint(int(minimum), int(maximum))

    def _next_post_dispatch_delay_seconds(self) -> int:
        minimum, maximum = self._post_dispatch_delay_range_seconds
        if minimum < 0 or maximum < minimum:
            return 0
        return self._randint(int(minimum), int(maximum))

    @staticmethod
    def _event_key(event: Any) -> tuple[int, int]:
        return (int(getattr(event, "chat_id")), int(getattr(getattr(event, "message", None), "id")))

    def _remember_event_key(self, event_key: tuple[int, int]) -> None:
        self._seen_event_keys[event_key] = None
        self._seen_event_keys.move_to_end(event_key)
        while len(self._seen_event_keys) > self._dedupe_window_size:
            self._seen_event_keys.popitem(last=False)


async def run_user_client(
    settings: Settings,
    *,
    client: Any | None = None,
    state: Any | None = None,
    reaction_history_backfill: ChannelReactionHistoryBackfill | None = None,
    post_mirror_history_backfill: PostMirrorHistoryBackfill | None = None,
) -> None:  # pragma: no cover - integration only
    from telethon import TelegramClient, events

    ensure_session_parent(settings.telegram_session)
    owns_client = client is None
    if client is None:
        client = TelegramClient(settings.telegram_session, settings.telegram_api_id, settings.telegram_api_hash)
    if state is None:
        state = SQLiteAssistantRepository(settings.database_path)
    reaction_sender = TelethonChannelReactionSender(client, send_delay_seconds=0)
    cached_reaction_sender = CachedTelethonChannelReactionSender(reaction_sender, state)
    post_mirror_delivery_sender = TelethonPostMirrorSender(client)
    post_mirror_realtime_enqueuer = PostMirrorOutboxEnqueuer(state, origin="realtime")
    post_mirror_history_enqueuer = PostMirrorOutboxEnqueuer(state, origin="history")
    post_mirror_topic_manager = TelethonForumTopicManager(client)
    post_mirror_online_gate = TelegramAuthorizationOnlineGate(
        client,
        freshness_seconds=settings.post_mirror_online_freshness_seconds,
    )
    gated_post_mirror_topic_manager = OnlineGatedForumTopicManager(
        post_mirror_topic_manager,
        post_mirror_online_gate,
    )
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
        reaction_settings=state,
        reaction_sender=cached_reaction_sender,
        post_mirror_settings=state,
        post_mirror_sender=post_mirror_realtime_enqueuer,
        post_mirror_topic_manager=gated_post_mirror_topic_manager,
        post_mirror_defer_missing_topics=True,
    )
    registry = FeatureRegistry([VoiceTranscriptionFeature(), ChannelReactionFeature(), PostMirrorFeature()])

    async def handle_voice(event: Any) -> None:
        await dispatch_voice_message(event, registry, context)

    async def handle_channel_reaction(event: Any) -> None:
        await dispatch_channel_message(event, registry, context)

    async def handle_post_mirror(event: Any) -> None:
        await dispatch_post_mirror(event, registry, context)

    post_mirror_operation_gate = PostMirrorOperationGate()
    worker = VoiceQueueWorker(handler=handle_voice)
    reaction_worker = ChannelReactionQueueWorker(
        handler=handle_channel_reaction,
        delay_range_provider=state.get_reaction_delay_range_seconds,
        post_dispatch_delay_range_seconds=CHANNEL_REACTION_POST_DISPATCH_DELAY_RANGE_SECONDS,
    )
    post_mirror_worker = PostMirrorQueueWorker(
        handler=handle_post_mirror,
        operation_gate=post_mirror_operation_gate,
    )
    post_mirror_delivery_worker = PostMirrorOutboxDeliveryWorker(
        state=state,
        client=client,
        post_mirror_sender=post_mirror_delivery_sender,
        online_gate=post_mirror_online_gate,
        post_mirror_topic_manager=post_mirror_topic_manager,
        poll_seconds=settings.post_mirror_outbox_poll_seconds,
        delivery_delay_range_seconds=settings.post_mirror_delivery_delay_range_seconds,
        online_delivery_window_seconds=settings.post_mirror_online_delivery_window_seconds,
    )
    if reaction_history_backfill is not None:
        reaction_history_backfill.bind(
            client=client,
            state=state,
            reaction_sender=TelethonChannelReactionSender(client, send_delay_seconds=0),
        )
    if post_mirror_history_backfill is not None:
        post_mirror_history_backfill.bind(
            client=client,
            state=state,
            post_mirror_sender=post_mirror_history_enqueuer,
            post_mirror_topic_manager=gated_post_mirror_topic_manager,
            operation_gate=post_mirror_operation_gate,
        )
    consumer_task = asyncio.create_task(worker.run(), name="voice-queue-consumer")
    reaction_consumer_task = asyncio.create_task(reaction_worker.run(), name="channel-reaction-queue-consumer")
    post_mirror_consumer_task = asyncio.create_task(post_mirror_worker.run(), name="post-mirror-queue-consumer")
    post_mirror_delivery_task: asyncio.Task[None] | None = None
    premium_refresh_task = asyncio.create_task(
        refresh_account_premium_status_loop(client, state),
        name="telegram-premium-refresh",
    )

    @client.on(events.NewMessage())
    async def on_message(event: Any) -> None:
        title, kind = await remember_chat_from_event(event, state)
        # Only voice/video-note events should occupy the queue — text messages
        # would block voice processing for no reason.
        if is_transcribable_message(event.message):
            worker.submit(event)
        if should_enqueue_channel_reaction(event, state):
            reaction_worker.submit(event)
        if should_enqueue_post_mirror(event, state):
            post_mirror_worker.submit(event)
        try:
            await sync_post_mirror_topic_title(
                state=state,
                topic_manager=gated_post_mirror_topic_manager,
                source_chat_id=int(event.chat_id),
                title=title,
                kind=kind,
            )
        except Exception:
            logger.exception("post_mirror_topic_title_sync_failed chat_id=%s", getattr(event, "chat_id", "?"))
        await remember_group_from_event(event, state)

    @client.on(events.Album())
    async def on_album(event: Any) -> None:
        title, kind = await remember_chat_from_event(event, state)
        if should_enqueue_post_mirror(event, state):
            post_mirror_worker.submit(event)
        try:
            await sync_post_mirror_topic_title(
                state=state,
                topic_manager=gated_post_mirror_topic_manager,
                source_chat_id=int(event.chat_id),
                title=title,
                kind=kind,
            )
        except Exception:
            logger.exception("post_mirror_topic_title_sync_failed chat_id=%s", getattr(event, "chat_id", "?"))
        await remember_group_from_event(event, state)

    try:
        await client.start()
        await mark_current_session_offline(client)
        post_mirror_delivery_task = asyncio.create_task(
            post_mirror_delivery_worker.run(),
            name="post-mirror-outbox-delivery",
        )
        await record_account_premium_status(client, state)
        await client.run_until_disconnected()
    finally:
        for task in tuple(
            task
            for task in (
                consumer_task,
                reaction_consumer_task,
                post_mirror_consumer_task,
                post_mirror_delivery_task,
                premium_refresh_task,
            )
            if task is not None
        ):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - graceful shutdown
                pass
        if owns_client and client.is_connected():
            await client.disconnect()


def main() -> None:  # pragma: no cover - integration only
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_user_client(load_settings()))


if __name__ == "__main__":
    main()
