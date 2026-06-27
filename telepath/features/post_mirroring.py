from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol


POST_MIRROR_FEATURE = "post_mirroring"


@dataclass(frozen=True)
class PostMirrorSourceSettings:
    enabled: bool = False
    target_thread_id: int | None = None
    title: str | None = None
    kind: str = "channel"


@dataclass(frozen=True)
class PostMirrorEvent:
    chat_id: int
    message_id: int
    message_ids: tuple[int, ...]
    is_channel: bool
    is_group: bool
    grouped_id: int | None = None
    messages: tuple[Any, ...] = ()


@dataclass(frozen=True)
class PostMirrorSendResult:
    message_count: int = 0
    media_count: int = 0


@dataclass(frozen=True)
class PostMirrorQueuedDelivery:
    id: int
    source_chat_id: int
    message_ids: tuple[int, ...]
    is_channel: bool
    is_group: bool
    grouped_id: int | None
    target_chat_id: int
    target_thread_id: int | None
    origin: str
    ready_at: int
    attempts: int = 0
    last_error: str | None = None


class PostMirrorSettingsPort(Protocol):
    def is_post_mirroring_enabled(self) -> bool: ...
    def get_post_mirror_target_chat_id(self) -> int | None: ...
    def get_post_mirror_source_settings(self, chat_id: int) -> PostMirrorSourceSettings | None: ...
    def upsert_post_mirror_source(self, source_chat_id: int, title: str | None = None, kind: str = "channel") -> None: ...
    def set_post_mirror_source_topic(self, source_chat_id: int, target_thread_id: int | None) -> None: ...


class PostMirrorSenderPort(Protocol):
    async def copy_post(
        self,
        event: PostMirrorEvent,
        *,
        target_chat_id: int,
        target_thread_id: int | None,
    ) -> PostMirrorSendResult: ...


class PostMirrorTopicManagerPort(Protocol):
    async def create_topic(self, target_chat_id: int, title: str) -> int: ...


class ProcessedMessagesPort(Protocol):
    def is_processed(self, chat_id: int, message_id: int, feature: str) -> bool: ...
    def mark_processed(self, chat_id: int, message_id: int, feature: str) -> bool: ...


class PostMirrorContext(Protocol):
    post_mirror_settings: PostMirrorSettingsPort
    post_mirror_sender: PostMirrorSenderPort
    post_mirror_topic_manager: PostMirrorTopicManagerPort | None
    processed: ProcessedMessagesPort


class PostMirrorFeature:
    name = POST_MIRROR_FEATURE

    def can_handle(self, event: Any) -> bool:
        return isinstance(event, PostMirrorEvent)

    async def handle(self, event: PostMirrorEvent, context: PostMirrorContext) -> str:
        if not (event.is_channel or event.is_group):
            return "skipped_unsupported_chat"
        if not context.post_mirror_settings.is_post_mirroring_enabled():
            return "skipped_disabled"

        settings = context.post_mirror_settings.get_post_mirror_source_settings(event.chat_id)
        if settings is None:
            return "skipped_unconfigured"
        if not settings.enabled:
            return "skipped_source_disabled"

        target_chat_id = context.post_mirror_settings.get_post_mirror_target_chat_id()
        if target_chat_id is None:
            return "skipped_no_target_group"
        if event.chat_id == target_chat_id:
            return "skipped_target_group"

        message_ids = event.message_ids or (event.message_id,)
        if any(
            context.processed.is_processed(event.chat_id, message_id, POST_MIRROR_FEATURE)
            for message_id in message_ids
        ):
            return "skipped_processed"

        claim = getattr(context.processed, "try_claim_processing", None)
        release = getattr(context.processed, "release_processing_claims", None)
        if callable(claim) and not claim(event.chat_id, message_ids, POST_MIRROR_FEATURE):
            return "skipped_processing"

        try:
            defer_missing_topics = bool(getattr(context, "post_mirror_defer_missing_topics", False))
            topic_was_created = settings.target_thread_id is None
            if settings.target_thread_id is None:
                if defer_missing_topics:
                    settings = replace(settings, target_thread_id=None)
                else:
                    settings = await self._ensure_topic(event, context, settings, target_chat_id)
                    if settings is None or settings.target_thread_id is None:
                        if callable(release):
                            release(event.chat_id, message_ids, POST_MIRROR_FEATURE)
                        return "skipped_no_topic"
                    cooldown = getattr(context, "post_mirror_topic_create_cooldown", None)
                    if topic_was_created and callable(cooldown):
                        await cooldown(event.chat_id)

            result = await context.post_mirror_sender.copy_post(
                event,
                target_chat_id=target_chat_id,
                target_thread_id=settings.target_thread_id,
            )
            if result.message_count <= 0 and result.media_count <= 0:
                if callable(release):
                    release(event.chat_id, message_ids, POST_MIRROR_FEATURE)
                return "skipped_empty"

            mark_many = getattr(context.processed, "mark_many_processed", None)
            if callable(mark_many):
                mark_many(event.chat_id, message_ids, POST_MIRROR_FEATURE)
            else:
                for message_id in message_ids:
                    context.processed.mark_processed(event.chat_id, message_id, POST_MIRROR_FEATURE)
            return "mirrored"
        except Exception:
            if callable(release):
                release(event.chat_id, message_ids, POST_MIRROR_FEATURE)
            raise

    async def _ensure_topic(
        self,
        event: PostMirrorEvent,
        context: PostMirrorContext,
        settings: PostMirrorSourceSettings,
        target_chat_id: int,
    ) -> PostMirrorSourceSettings | None:
        topic_manager = getattr(context, "post_mirror_topic_manager", None)
        if topic_manager is None:
            return None
        title = (settings.title or "").strip() or str(event.chat_id)
        kind = settings.kind if settings.kind in {"channel", "group"} else ("group" if event.is_group else "channel")
        topic_id = await topic_manager.create_topic(target_chat_id, title)
        context.post_mirror_settings.upsert_post_mirror_source(event.chat_id, title, kind)
        context.post_mirror_settings.set_post_mirror_source_topic(event.chat_id, int(topic_id))
        return replace(settings, target_thread_id=int(topic_id), title=title, kind=kind)
