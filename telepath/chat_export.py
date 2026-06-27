from __future__ import annotations

import asyncio
import shutil
import tempfile
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from typing import Any
from zipfile import ZIP_STORED, ZipFile

from telepath.presence import mark_current_session_offline


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


@dataclass(frozen=True)
class ChatMediaArchivePart:
    chat_id: int
    title: str
    filename: str
    path: Path
    part_index: int
    message_count: int
    service_message_count: int
    media_count: int
    byte_count: int
    temporary_parent: Path | None = None


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
        try:
            entity = await self.client.get_entity(chat_id)
        finally:
            await mark_current_session_offline(self.client)
        return ExportChat(
            chat_id=chat_id,
            title=_entity_title(entity) or str(chat_id),
            kind=_entity_kind(entity),
        )

    async def _get_cached_chats(self, *, refresh: bool) -> list[ExportChat]:
        if self._chat_cache is not None and not refresh:
            return self._chat_cache
        chats = []
        try:
            async for dialog in self.client.iter_dialogs():
                chats.append(
                    ExportChat(
                        chat_id=int(dialog.id),
                        title=_dialog_title(dialog),
                        kind=_dialog_kind(dialog),
                    )
                )
        finally:
            await mark_current_session_offline(self.client)
        self._chat_cache = chats
        return chats

    async def export_chat_text(self, chat_id: int, *, limit: int | None = None) -> ChatExportDocument:
        async with self._export_lock:
            return await self._export_chat_text_unlocked(chat_id, limit=limit)

    async def export_chat_media_archives(
        self,
        chat_id: int,
        *,
        limit: int | None = None,
        max_archive_bytes: int,
        work_dir: str | Path | None = None,
    ):
        if max_archive_bytes <= 0:
            raise ValueError("max_archive_bytes must be positive")
        async with self._export_lock:
            async for part in self._export_chat_media_archives_unlocked(
                chat_id,
                limit=limit,
                max_archive_bytes=max_archive_bytes,
                work_dir=work_dir,
            ):
                yield part

    async def _export_chat_text_unlocked(self, chat_id: int, *, limit: int | None = None) -> ChatExportDocument:
        try:
            entity = await self.client.get_entity(chat_id)
        finally:
            await mark_current_session_offline(self.client)
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

    async def _export_chat_media_archives_unlocked(
        self,
        chat_id: int,
        *,
        limit: int | None,
        max_archive_bytes: int,
        work_dir: str | Path | None,
    ):
        try:
            entity = await self.client.get_entity(chat_id)
        finally:
            await mark_current_session_offline(self.client)
        title = _entity_title(entity) or str(chat_id)
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        archive_root, temporary_parent = _archive_work_root(work_dir)
        download_root = archive_root / "downloads"
        download_root.mkdir(parents=True, exist_ok=True)
        filename_stem = _archive_filename_stem(title, chat_id)
        part_index = 1
        yielded_part = False
        part = _ChatMediaArchivePartBuilder(
            archive_root=archive_root,
            temporary_parent=temporary_parent,
            chat_id=chat_id,
            title=title,
            filename_stem=filename_stem,
            part_index=part_index,
            generated_at=generated_at,
            max_archive_bytes=max_archive_bytes,
        )

        try:
            async for message in self._iter_export_messages(entity, limit=limit):
                if _is_service_message(message):
                    part.add_service_message()
                    continue

                media_path: Path | None = None
                media_name: str | None = None
                try:
                    if getattr(message, "media", None) is not None:
                        media_path = await self._download_media_to_path(message, download_root)
                        if media_path is not None:
                            media_name = part.preview_media_name(message, media_path)

                    message_lines = await self._format_archive_message(message, media_name=media_name)
                    media_size = media_path.stat().st_size if media_path is not None else 0
                    if part.has_content and not part.can_fit(message_lines=message_lines, media_size=media_size):
                        closed_part = part.close()
                        yielded_part = True
                        yield closed_part
                        part_index += 1
                        part = _ChatMediaArchivePartBuilder(
                            archive_root=archive_root,
                            temporary_parent=temporary_parent,
                            chat_id=chat_id,
                            title=title,
                            filename_stem=filename_stem,
                            part_index=part_index,
                            generated_at=generated_at,
                            max_archive_bytes=max_archive_bytes,
                        )
                        if media_path is not None:
                            media_name = part.preview_media_name(message, media_path)
                            message_lines = await self._format_archive_message(message, media_name=media_name)

                    part.add_message(message_lines=message_lines, media_path=media_path, media_name=media_name)
                finally:
                    if media_path is not None:
                        _remove_downloaded_media(media_path)

            if part.has_content:
                closed_part = part.close()
                yielded_part = True
                yield closed_part
            else:
                part.discard()
        finally:
            if temporary_parent is not None and not yielded_part:
                shutil.rmtree(temporary_parent, ignore_errors=True)

    async def send_chat_archive_part(self, part: ChatMediaArchivePart, *, target_peer: Any) -> None:
        try:
            await self.client.send_file(target_peer, part.path, caption=chat_media_archive_caption(part))
        finally:
            await mark_current_session_offline(self.client)

    async def _iter_export_messages(self, entity: Any, *, limit: int | None):
        if limit is None:
            try:
                async for message in self.client.iter_messages(
                    entity,
                    limit=None,
                    reverse=True,
                    wait_time=self.history_wait_seconds,
                ):
                    yield message
            finally:
                await mark_current_session_offline(self.client)
            return

        if limit <= 0:
            return

        latest_messages = []
        try:
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
        finally:
            await mark_current_session_offline(self.client)
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

    async def _format_archive_message(self, message: Any, *, media_name: str | None) -> list[str]:
        lines = await self._format_message(message)
        if not media_name:
            return lines
        return [*lines[:-1], f"Media: {media_name}", ""]

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
            try:
                entity = await self.client.get_entity(from_id)
            finally:
                await mark_current_session_offline(self.client)
        except Exception:
            return "unknown"
        return _entity_title(entity) or "unknown"

    async def _download_media_to_path(self, message: Any, directory: Path) -> Path | None:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            downloaded = await self.client.download_media(message, file=directory)
        finally:
            await mark_current_session_offline(self.client)
        if downloaded is None:
            return None
        if isinstance(downloaded, bytes):
            path = directory / f"{int(message.id):06d}.bin"
            path.write_bytes(downloaded)
            return path
        path = Path(downloaded)
        return path if path.exists() else None


class _ChatMediaArchivePartBuilder:
    _zip_overhead_bytes = 4096

    def __init__(
        self,
        *,
        archive_root: Path,
        temporary_parent: Path | None,
        chat_id: int,
        title: str,
        filename_stem: str,
        part_index: int,
        generated_at: str,
        max_archive_bytes: int,
    ):
        self.chat_id = chat_id
        self.title = title
        self.part_index = part_index
        self.generated_at = generated_at
        self.max_archive_bytes = max_archive_bytes
        self.temporary_parent = temporary_parent
        self.filename = f"{filename_stem}-part{part_index:03d}.zip"
        self.path = archive_root / self.filename
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._zip = ZipFile(self.path, "w", compression=ZIP_STORED)
        self._message_lines: list[str] = []
        self._used_names: set[str] = set()
        self._payload_bytes = 0
        self.message_count = 0
        self.service_message_count = 0
        self.media_count = 0
        self._closed = False

    @property
    def has_content(self) -> bool:
        return self.message_count > 0 or self.service_message_count > 0 or self.media_count > 0

    def add_service_message(self) -> None:
        self.service_message_count += 1

    def can_fit(self, *, message_lines: list[str], media_size: int) -> bool:
        if not self.has_content:
            return True
        manifest_size = self._manifest_size(extra_lines=message_lines)
        estimated_size = self._payload_bytes + media_size + manifest_size + self._zip_overhead_bytes
        return estimated_size <= self.max_archive_bytes

    def preview_media_name(self, message: Any, media_path: Path) -> str:
        return self._unique_media_name(_media_archive_name(message, media_path))

    def add_message(self, *, message_lines: list[str], media_path: Path | None, media_name: str | None) -> None:
        if media_path is not None and media_name is not None:
            self._zip.write(media_path, media_name)
            self._payload_bytes += media_path.stat().st_size
            self._used_names.add(media_name)
            self.media_count += 1
        self._message_lines.extend(message_lines)
        self.message_count += 1

    def close(self) -> ChatMediaArchivePart:
        if not self._closed:
            self._zip.writestr("messages.txt", self._manifest_text())
            self._zip.close()
            self._closed = True
        return ChatMediaArchivePart(
            chat_id=self.chat_id,
            title=self.title,
            filename=self.filename,
            path=self.path,
            part_index=self.part_index,
            message_count=self.message_count,
            service_message_count=self.service_message_count,
            media_count=self.media_count,
            byte_count=self.path.stat().st_size,
            temporary_parent=self.temporary_parent,
        )

    def discard(self) -> None:
        if not self._closed:
            self._zip.close()
            self._closed = True
        self.path.unlink(missing_ok=True)

    def _manifest_size(self, *, extra_lines: list[str] | None = None) -> int:
        lines = self._manifest_lines(extra_lines=extra_lines)
        return len(("\n".join(lines).rstrip() + "\n").encode("utf-8"))

    def _manifest_text(self) -> str:
        return "\n".join(self._manifest_lines()).rstrip() + "\n"

    def _manifest_lines(self, *, extra_lines: list[str] | None = None) -> list[str]:
        lines = [
            "Telegram chat media archive",
            f"Chat: {self.title} ({self.chat_id})",
            f"Generated at: {self.generated_at}",
            f"Archive part: {self.part_index:03d}",
            f"Messages exported: {self.message_count + (1 if extra_lines else 0)}",
            f"Service messages skipped: {self.service_message_count}",
            f"Media files exported: {self.media_count}",
            "",
            *self._message_lines,
        ]
        if extra_lines:
            lines.extend(extra_lines)
        return lines

    def _unique_media_name(self, candidate: str) -> str:
        if candidate not in self._used_names:
            return candidate
        path = Path(candidate)
        stem = str(path.with_suffix(""))
        suffix = path.suffix
        index = 2
        while True:
            name = f"{stem}-{index}{suffix}"
            if name not in self._used_names:
                return name
            index += 1


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


def _archive_filename_stem(title: str, chat_id: int) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_title = _safe_filename_part(title) or "chat"
    return f"telegram-{safe_title}-{chat_id}-{now}"


def _archive_work_root(work_dir: str | Path | None) -> tuple[Path, Path | None]:
    if work_dir is not None:
        root = Path(work_dir)
        root.mkdir(parents=True, exist_ok=True)
        return root, None
    root = Path(tempfile.mkdtemp(prefix="telepath-chat-export-"))
    return root, root


def _media_archive_name(message: Any, media_path: Path) -> str:
    filename = _safe_media_filename(media_path.name)
    return f"media/{int(message.id):06d}-{filename}"


def _safe_media_filename(value: str) -> str:
    path = Path(value)
    suffix = re.sub(r"[^0-9A-Za-zА-Яа-яёЁ._-]+", "", path.suffix)[:16]
    stem = _safe_filename_part(path.stem) or "media"
    return f"{stem}{suffix}"


def _safe_filename_part(value: str) -> str:
    value = re.sub(r"\s+", "-", value.strip())
    value = re.sub(r"[^0-9A-Za-zА-Яа-яёЁ._-]+", "", value)
    return value[:48].strip(".-_")


def chat_media_archive_caption(part: ChatMediaArchivePart) -> str:
    return "\n".join(
        [
            "Telepath archive export",
            part.title,
            f"Часть: {part.part_index:03d}",
            f"Сообщений: {_format_int_grouped(part.message_count)}",
            f"Сервисных событий пропущено: {_format_int_grouped(part.service_message_count)}",
            f"Медиафайлов: {_format_int_grouped(part.media_count)}",
            f"Размер: {_format_archive_bytes(part.byte_count)}",
        ]
    )


def _format_int_grouped(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _format_archive_bytes(byte_count: int) -> str:
    if byte_count < 1024:
        return f"{byte_count} B"
    kib = byte_count / 1024
    if kib < 1024:
        return f"{kib:.1f} KB"
    mib = kib / 1024
    if mib < 1024:
        return f"{mib:.1f} MB"
    gib = mib / 1024
    return f"{gib:.1f} GB"


def _remove_downloaded_media(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        return


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
