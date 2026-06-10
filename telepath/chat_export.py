from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from typing import Any


DEFAULT_EXPORT_HISTORY_WAIT_SECONDS = 1.0


@dataclass(frozen=True)
class ExportChat:
    chat_id: int
    title: str
    kind: str


@dataclass(frozen=True)
class ExportChatPage:
    chats: list[ExportChat]
    page: int
    page_size: int
    total: int

    @property
    def total_pages(self) -> int:
        return max(1, ceil(self.total / self.page_size))


@dataclass(frozen=True)
class ChatExportDocument:
    chat_id: int
    title: str
    filename: str
    data: bytes
    message_count: int
    service_message_count: int


class TelethonChatExporter:
    def __init__(self, client: Any, *, history_wait_seconds: float = DEFAULT_EXPORT_HISTORY_WAIT_SECONDS):
        if history_wait_seconds < 0:
            raise ValueError("history_wait_seconds must be non-negative")
        self.client = client
        self.history_wait_seconds = float(history_wait_seconds)
        self._chat_cache: list[ExportChat] | None = None
        self._export_lock = asyncio.Lock()

    async def list_chats(
        self,
        *,
        page: int = 0,
        page_size: int = 8,
        refresh: bool = False,
        query: str | None = None,
    ) -> ExportChatPage:
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        page = max(page, 0)
        chats = await self._get_cached_chats(refresh=refresh)
        chats = _filter_chats(chats, query=query)
        total = len(chats)
        total_pages = max(1, ceil(total / page_size))
        page = min(page, total_pages - 1)
        start = page * page_size
        return ExportChatPage(
            chats=chats[start : start + page_size],
            page=page,
            page_size=page_size,
            total=total,
        )

    async def get_chat(self, chat_id: int) -> ExportChat:
        if self._chat_cache is not None:
            for chat in self._chat_cache:
                if chat.chat_id == chat_id:
                    return chat
        entity = await self.client.get_entity(chat_id)
        return ExportChat(
            chat_id=chat_id,
            title=_entity_title(entity) or str(chat_id),
            kind=_entity_kind(entity),
        )

    async def _get_cached_chats(self, *, refresh: bool) -> list[ExportChat]:
        if self._chat_cache is not None and not refresh:
            return self._chat_cache
        chats = []
        async for dialog in self.client.iter_dialogs():
            chats.append(
                ExportChat(
                    chat_id=int(dialog.id),
                    title=_dialog_title(dialog),
                    kind=_dialog_kind(dialog),
                )
            )
        self._chat_cache = chats
        return chats

    async def export_chat_text(self, chat_id: int, *, limit: int | None = None) -> ChatExportDocument:
        async with self._export_lock:
            return await self._export_chat_text_unlocked(chat_id, limit=limit)

    async def _export_chat_text_unlocked(self, chat_id: int, *, limit: int | None = None) -> ChatExportDocument:
        entity = await self.client.get_entity(chat_id)
        title = _entity_title(entity) or str(chat_id)
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lines = [
            "Telegram chat export",
            f"Chat: {title} ({chat_id})",
            f"Generated at: {generated_at}",
            "",
        ]
        message_count = 0
        service_message_count = 0
        async for message in self._iter_export_messages(entity, limit=limit):
            if _is_service_message(message):
                service_message_count += 1
                continue
            lines.extend(await self._format_message(message))
            message_count += 1

        lines[3:3] = [
            f"Messages exported: {message_count}",
            f"Service messages skipped: {service_message_count}",
        ]
        data = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
        return ChatExportDocument(
            chat_id=chat_id,
            title=title,
            filename=_export_filename(title, chat_id),
            data=data,
            message_count=message_count,
            service_message_count=service_message_count,
        )

    async def _iter_export_messages(self, entity: Any, *, limit: int | None):
        if limit is None:
            async for message in self.client.iter_messages(
                entity,
                limit=None,
                reverse=True,
                wait_time=self.history_wait_seconds,
            ):
                yield message
            return

        if limit <= 0:
            return

        latest_messages = []
        async for message in self.client.iter_messages(
            entity,
            limit=None,
            reverse=False,
            wait_time=self.history_wait_seconds,
        ):
            latest_messages.append(message)
            if not _is_service_message(message):
                limit -= 1
                if limit == 0:
                    break
        for message in reversed(latest_messages):
            yield message

    async def _format_message(self, message: Any) -> list[str]:
        sender_name, sender_id = await _sender_name_and_id(message)
        header = f"#{int(message.id)} {sender_name} ({sender_id or 'unknown'})"
        details = _message_details(message, forwarded_from=await self._forwarded_from_name(message))
        if details:
            header = f"{header} | {' | '.join(details)}"
        text = getattr(message, "message", None) or "[Media/No text]"
        return [
            f"[{_format_datetime(getattr(message, 'date', None))}] {header}",
            str(text),
            "",
        ]

    async def _forwarded_from_name(self, message: Any) -> str | None:
        forward = getattr(message, "fwd_from", None)
        if forward is None:
            return None
        name = getattr(forward, "from_name", None)
        if name:
            return str(name)
        from_id = getattr(forward, "from_id", None)
        if from_id is None:
            return None
        try:
            entity = await self.client.get_entity(from_id)
        except Exception:
            return "unknown"
        return _entity_title(entity) or "unknown"


def _dialog_title(dialog: Any) -> str:
    title = getattr(dialog, "title", None) or getattr(dialog, "name", None)
    if title:
        return str(title)
    entity = getattr(dialog, "entity", None)
    return _entity_title(entity) or str(getattr(dialog, "id", "unknown"))


def _dialog_kind(dialog: Any) -> str:
    if getattr(dialog, "is_user", False):
        return "private"
    if getattr(dialog, "is_group", False):
        return "group"
    if getattr(dialog, "is_channel", False):
        return "channel"
    return "chat"


def _is_service_message(message: Any) -> bool:
    return getattr(message, "action", None) is not None


def _filter_chats(chats: list[ExportChat], *, query: str | None) -> list[ExportChat]:
    normalized_query = (query or "").strip().casefold()
    if not normalized_query:
        return chats
    return [
        chat
        for chat in chats
        if normalized_query in chat.title.casefold()
        or normalized_query in str(chat.chat_id)
        or normalized_query in chat.kind.casefold()
    ]


def _entity_kind(entity: Any) -> str:
    if getattr(entity, "bot", False) or entity.__class__.__name__ == "User":
        return "private"
    if getattr(entity, "broadcast", False):
        return "channel"
    if getattr(entity, "megagroup", False) or entity.__class__.__name__ == "Chat":
        return "group"
    return "chat"


def _entity_title(entity: Any) -> str | None:
    if entity is None:
        return None
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    first_name = getattr(entity, "first_name", None) or ""
    last_name = getattr(entity, "last_name", None) or ""
    full_name = f"{first_name} {last_name}".strip()
    if full_name:
        return full_name
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    return None


async def _sender_name_and_id(message: Any) -> tuple[str, int | None]:
    sender = getattr(message, "sender", None)
    if sender is None:
        get_sender = getattr(message, "get_sender", None)
        if callable(get_sender):
            sender = await get_sender()
    return _entity_title(sender) or "unknown", _int_or_none(getattr(sender, "id", None))


def _message_details(message: Any, *, forwarded_from: str | None = None) -> list[str]:
    details = []
    reply_to_id = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
    if reply_to_id is not None:
        details.append(f"reply_to={reply_to_id}")
    if forwarded_from:
        details.append(f"[Forwarded from: {forwarded_from}]")
    views = getattr(message, "views", None)
    if views is not None:
        details.append(f"views={views}")
    forwards = getattr(message, "forwards", None)
    if forwards is not None:
        details.append(f"forwards={forwards}")
    replies = getattr(getattr(message, "replies", None), "replies", None)
    if replies is not None:
        details.append(f"replies={replies}")
    reactions = _reaction_count(message)
    if reactions:
        details.append(f"reactions={reactions}")
    return details


def _reaction_count(message: Any) -> int:
    reactions = getattr(message, "reactions", None)
    total = 0
    for reaction in getattr(reactions, "results", []) or []:
        total += int(getattr(reaction, "count", 0) or 0)
    return total


def _format_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return "unknown-date"


def _export_filename(title: str, chat_id: int) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_title = _safe_filename_part(title) or "chat"
    return f"telegram-{safe_title}-{chat_id}-{now}.txt"


def _safe_filename_part(value: str) -> str:
    value = re.sub(r"\s+", "-", value.strip())
    value = re.sub(r"[^0-9A-Za-zА-Яа-яёЁ._-]+", "", value)
    return value[:48].strip(".-_")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
