from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

from telepath.chat_export import ChatMediaArchivePart, TelethonChatExporter


class FakeDialog:
    def __init__(self, chat_id, title, *, is_user=False, is_group=False, is_channel=False):
        self.id = chat_id
        self.title = title
        self.name = title
        self.is_user = is_user
        self.is_group = is_group
        self.is_channel = is_channel


class FakeSender:
    def __init__(self, sender_id, first_name="", last_name="", title=""):
        self.id = sender_id
        self.first_name = first_name
        self.last_name = last_name
        self.title = title


class FakeMessage:
    def __init__(
        self,
        message_id,
        text,
        *,
        sender=None,
        date=None,
        reply_to_id=None,
        forwarded_from=None,
        action=None,
        views=None,
        media=None,
        media_name=None,
        media_bytes=None,
    ):
        self.id = message_id
        self.message = text
        self.sender = sender
        self.date = date or datetime(2026, 5, 27, 1, 2, 3, tzinfo=timezone.utc)
        self.reply_to = (
            type("Reply", (), {"reply_to_msg_id": reply_to_id})()
            if reply_to_id is not None
            else None
        )
        self.fwd_from = (
            type("Forward", (), {"from_name": forwarded_from, "from_id": None})()
            if forwarded_from
            else None
        )
        self.action = action
        self.views = views
        self.media = media
        self.media_name = media_name
        self.media_bytes = media_bytes


class FakeClient:
    def __init__(self):
        self.dialogs = [
            FakeDialog(10, "Alice", is_user=True),
            FakeDialog(-10020, "Team", is_group=True),
            FakeDialog(-10030, "News", is_channel=True),
        ]
        self.iter_dialogs_calls = 0
        self.get_entity_calls = []
        self.iter_messages_calls = []
        self.downloaded_paths = []
        self.sent_files = []
        self.requests = []

    async def iter_dialogs(self):
        self.iter_dialogs_calls += 1
        for dialog in self.dialogs:
            yield dialog

    async def get_entity(self, chat_id):
        self.get_entity_calls.append(chat_id)
        for dialog in self.dialogs:
            if dialog.id == chat_id:
                return type("Entity", (), {"id": dialog.id, "title": dialog.title})()
        raise ValueError("missing chat")

    async def iter_messages(self, entity, *, limit=None, reverse=False, wait_time=None):
        self.iter_messages_calls.append((entity.id, limit, reverse, wait_time))
        messages = [
            FakeMessage(1, "hello", sender=FakeSender(1, "Alice"), views=5),
            FakeMessage(2, None, action=object()),
            FakeMessage(3, "answer", sender=FakeSender(2, "Bish"), reply_to_id=1),
            FakeMessage(4, None, sender=FakeSender(3, title="Channel"), forwarded_from="Source"),
        ]
        for message in messages:
            yield message

    async def download_media(self, message, file):
        if getattr(message, "media_bytes", None) is None:
            return None
        directory = Path(file)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (getattr(message, "media_name", None) or f"{message.id}.bin")
        target.write_bytes(message.media_bytes)
        self.downloaded_paths.append(target)
        return str(target)

    async def send_file(self, target_peer, path, *, caption=None):
        self.sent_files.append((target_peer, Path(path), caption))

    async def __call__(self, request):
        self.requests.append(request)


class SlowFakeClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.active_message_iterators = 0
        self.max_active_message_iterators = 0

    async def iter_messages(self, entity, *, limit=None, reverse=False, wait_time=None):
        self.iter_messages_calls.append((entity.id, limit, reverse, wait_time))
        self.active_message_iterators += 1
        self.max_active_message_iterators = max(
            self.max_active_message_iterators,
            self.active_message_iterators,
        )
        try:
            await asyncio.sleep(0)
            yield FakeMessage(1, "hello", sender=FakeSender(1, "Alice"))
            await asyncio.sleep(0)
        finally:
            self.active_message_iterators -= 1


class OrderedHistoryClient(FakeClient):
    async def iter_messages(self, entity, *, limit=None, reverse=False, wait_time=None):
        self.iter_messages_calls.append((entity.id, limit, reverse, wait_time))
        messages = [
            FakeMessage(1, "oldest", sender=FakeSender(1, "Alice")),
            FakeMessage(2, "middle", sender=FakeSender(1, "Alice")),
            FakeMessage(3, "newest", sender=FakeSender(1, "Alice")),
        ]
        if reverse:
            selected = messages[:limit] if limit is not None else messages
        else:
            newest_first = list(reversed(messages))
            selected = newest_first[:limit] if limit is not None else newest_first
        for message in selected:
            yield message


class ServiceGapHistoryClient(FakeClient):
    async def iter_messages(self, entity, *, limit=None, reverse=False, wait_time=None):
        self.iter_messages_calls.append((entity.id, limit, reverse, wait_time))
        messages = [
            FakeMessage(1, "oldest", sender=FakeSender(1, "Alice")),
            FakeMessage(2, "middle", sender=FakeSender(1, "Alice")),
            FakeMessage(3, None, action=object()),
            FakeMessage(4, "newest", sender=FakeSender(1, "Alice")),
        ]
        if reverse:
            selected = messages if limit is None else messages[:limit]
        else:
            newest_first = list(reversed(messages))
            selected = newest_first if limit is None else newest_first[:limit]
        for message in selected:
            yield message


class MediaHistoryClient(FakeClient):
    async def iter_messages(self, entity, *, limit=None, reverse=False, wait_time=None):
        self.iter_messages_calls.append((entity.id, limit, reverse, wait_time))
        messages = [
            FakeMessage(
                1,
                "photo caption",
                sender=FakeSender(1, "Alice"),
                media=object(),
                media_name="photo one.jpg",
                media_bytes=b"a" * 128,
            ),
            FakeMessage(2, None, action=object()),
            FakeMessage(
                3,
                "video caption",
                sender=FakeSender(2, "Bish"),
                media=object(),
                media_name="clip.mp4",
                media_bytes=b"b" * 128,
            ),
        ]
        if reverse:
            selected = messages if limit is None else messages[:limit]
        else:
            selected = list(reversed(messages)) if limit is None else list(reversed(messages))[:limit]
        for message in selected:
            yield message


async def test_chat_exporter_lists_dialogs_with_pagination():
    client = FakeClient()
    exporter = TelethonChatExporter(client)

    page = await exporter.list_chats(page=1, page_size=2)

    assert page.page == 1
    assert page.total_pages == 2
    assert [(chat.chat_id, chat.title, chat.kind) for chat in page.chats] == [
        (-10030, "News", "channel")
    ]
    assert [request.__class__.__name__ for request in client.requests] == ["UpdateStatusRequest"]
    assert client.requests[0].offline is True


async def test_chat_exporter_reuses_cached_dialogs_for_pagination():
    client = FakeClient()
    exporter = TelethonChatExporter(client)

    first_page = await exporter.list_chats(page=0, page_size=1)
    second_page = await exporter.list_chats(page=1, page_size=1)

    assert client.iter_dialogs_calls == 1
    assert first_page.chats[0].title == "Alice"
    assert second_page.chats[0].title == "Team"


async def test_chat_exporter_can_refresh_cached_dialogs():
    client = FakeClient()
    exporter = TelethonChatExporter(client)

    await exporter.list_chats(page=0, page_size=1)
    client.dialogs.insert(0, FakeDialog(99, "New", is_user=True))
    refreshed = await exporter.list_chats(page=0, page_size=1, refresh=True)

    assert client.iter_dialogs_calls == 2
    assert refreshed.chats[0].title == "New"


async def test_chat_exporter_filters_cached_dialogs_by_title_and_id():
    client = FakeClient()
    exporter = TelethonChatExporter(client)

    title_page = await exporter.list_chats(query="team")
    id_page = await exporter.list_chats(query="10030")

    assert client.iter_dialogs_calls == 1
    assert [(chat.chat_id, chat.title) for chat in title_page.chats] == [(-10020, "Team")]
    assert [(chat.chat_id, chat.title) for chat in id_page.chats] == [(-10030, "News")]


async def test_chat_exporter_get_chat_reuses_cached_dialog():
    client = FakeClient()
    exporter = TelethonChatExporter(client)

    await exporter.list_chats(page=0, page_size=1)
    chat = await exporter.get_chat(-10020)

    assert chat.title == "Team"
    assert client.iter_dialogs_calls == 1
    assert client.get_entity_calls == []


async def test_chat_exporter_get_chat_uses_entity_without_warming_dialog_cache():
    client = FakeClient()
    exporter = TelethonChatExporter(client)

    chat = await exporter.get_chat(-10020)

    assert chat.title == "Team"
    assert client.iter_dialogs_calls == 0
    assert client.get_entity_calls == [-10020]
    assert [request.__class__.__name__ for request in client.requests] == ["UpdateStatusRequest"]
    assert client.requests[0].offline is True


async def test_chat_exporter_exports_text_history_as_chronological_txt():
    client = FakeClient()
    exporter = TelethonChatExporter(client)

    document = await exporter.export_chat_text(-10020)

    assert client.iter_messages_calls == [(-10020, None, True, 1.0)]
    assert document.filename.startswith("telegram-Team--10020-")
    assert document.filename.endswith(".txt")
    text = document.data.decode("utf-8")
    assert "Chat: Team (-10020)" in text
    assert "Messages exported: 3" in text
    assert "Service messages skipped: 1" in text
    assert "[2026-05-27 01:02:03+00:00] #1 Alice (1) | views=5" in text
    assert "hello" in text
    assert "[2026-05-27 01:02:03+00:00] #3 Bish (2) | reply_to=1" in text
    assert "[Forwarded from: Source]" in text
    assert "[Media/No text]" in text
    assert [request.__class__.__name__ for request in client.requests] == [
        "UpdateStatusRequest",
        "UpdateStatusRequest",
    ]


async def test_chat_exporter_uses_configured_history_wait_time():
    client = FakeClient()
    exporter = TelethonChatExporter(client, history_wait_seconds=2.5)

    await exporter.export_chat_text(-10020, limit=200)

    assert client.iter_messages_calls == [(-10020, None, False, 2.5)]


async def test_chat_exporter_limited_export_uses_latest_messages_in_chronological_order():
    client = OrderedHistoryClient()
    exporter = TelethonChatExporter(client)

    document = await exporter.export_chat_text(-10020, limit=2)

    text = document.data.decode("utf-8")
    assert client.iter_messages_calls == [(-10020, None, False, 1.0)]
    assert "oldest" not in text
    assert text.index("middle") < text.index("newest")


async def test_chat_exporter_limited_export_does_not_count_service_messages_against_limit():
    client = ServiceGapHistoryClient()
    exporter = TelethonChatExporter(client)

    document = await exporter.export_chat_text(-10020, limit=2)

    text = document.data.decode("utf-8")
    assert document.message_count == 2
    assert document.service_message_count == 1
    assert "Messages exported: 2" in text
    assert "Service messages skipped: 1" in text
    assert "oldest" not in text
    assert "middle" in text
    assert "newest" in text


async def test_chat_exporter_serializes_parallel_exports():
    client = SlowFakeClient()
    exporter = TelethonChatExporter(client)

    await asyncio.gather(
        exporter.export_chat_text(-10020),
        exporter.export_chat_text(-10030),
    )

    assert client.max_active_message_iterators == 1


async def test_chat_exporter_exports_media_archive_with_manifest_and_cleans_downloads(tmp_path):
    client = MediaHistoryClient()
    exporter = TelethonChatExporter(client)

    parts = [
        part
        async for part in exporter.export_chat_media_archives(
            -10020,
            limit=2,
            max_archive_bytes=1024 * 1024,
            work_dir=tmp_path,
        )
    ]

    assert len(parts) == 1
    part = parts[0]
    assert part.filename.startswith("telegram-Team--10020-")
    assert part.filename.endswith("-part001.zip")
    assert part.message_count == 2
    assert part.service_message_count == 1
    assert part.media_count == 2
    assert part.byte_count == part.path.stat().st_size
    with ZipFile(part.path) as archive:
        names = archive.namelist()
        assert "messages.txt" in names
        assert "media/000001-photo-one.jpg" in names
        assert "media/000003-clip.mp4" in names
        manifest = archive.read("messages.txt").decode("utf-8")
    assert "Telegram chat media archive" in manifest
    assert "Messages exported: 2" in manifest
    assert "Service messages skipped: 1" in manifest
    assert "Media files exported: 2" in manifest
    assert "Media: media/000001-photo-one.jpg" in manifest
    assert "photo caption" in manifest
    assert all(not path.exists() for path in client.downloaded_paths)
    assert [request.__class__.__name__ for request in client.requests] == [
        "UpdateStatusRequest",
        "UpdateStatusRequest",
        "UpdateStatusRequest",
        "UpdateStatusRequest",
    ]


async def test_chat_exporter_splits_media_archives_by_part_budget(tmp_path):
    client = MediaHistoryClient()
    exporter = TelethonChatExporter(client)

    parts = [
        part
        async for part in exporter.export_chat_media_archives(
            -10020,
            limit=2,
            max_archive_bytes=760,
            work_dir=tmp_path,
        )
    ]

    assert [part.part_index for part in parts] == [1, 2]
    assert [part.media_count for part in parts] == [1, 1]
    assert [part.message_count for part in parts] == [1, 1]
    assert all(part.byte_count <= 760 for part in parts)


async def test_chat_exporter_sends_archive_part_via_user_client(tmp_path):
    client = FakeClient()
    exporter = TelethonChatExporter(client)
    path = tmp_path / "telegram-Team--10020-20260610-010203-part001.zip"
    path.write_bytes(b"zip")
    part = ChatMediaArchivePart(
        chat_id=-10020,
        title="Team",
        filename=path.name,
        path=path,
        part_index=1,
        message_count=2,
        service_message_count=1,
        media_count=2,
        byte_count=3,
    )

    await exporter.send_chat_archive_part(part, target_peer="@telepath_manager_bot")

    assert client.sent_files == [
        (
            "@telepath_manager_bot",
            path,
            "Telepath archive export\n"
            "Team\n"
            "Часть: 001\n"
            "Сообщений: 2\n"
            "Сервисных событий пропущено: 1\n"
            "Медиафайлов: 2\n"
            "Размер: 3 B",
        )
    ]


async def test_chat_exporter_removes_temp_root_when_media_archive_has_no_messages(tmp_path, monkeypatch):
    import telepath.chat_export as chat_export_module

    temp_root = tmp_path / "archive-job"
    monkeypatch.setattr(chat_export_module.tempfile, "mkdtemp", lambda prefix: str(temp_root))
    exporter = TelethonChatExporter(FakeClient())

    parts = [
        part
        async for part in exporter.export_chat_media_archives(
            -10020,
            limit=0,
            max_archive_bytes=1024,
        )
    ]

    assert parts == []
    assert not temp_root.exists()


async def test_chat_exporter_marks_current_session_offline_after_sending_archive(tmp_path):
    client = FakeClient()
    exporter = TelethonChatExporter(client)
    path = tmp_path / "archive.zip"
    path.write_bytes(b"archive")
    part = ChatMediaArchivePart(
        chat_id=-10020,
        title="Team",
        filename="team.zip",
        path=path,
        part_index=1,
        message_count=1,
        service_message_count=0,
        media_count=1,
        byte_count=path.stat().st_size,
    )

    await exporter.send_chat_archive_part(part, target_peer=10)

    assert len(client.sent_files) == 1
    assert [request.__class__.__name__ for request in client.requests] == ["UpdateStatusRequest"]
    assert client.requests[0].offline is True
