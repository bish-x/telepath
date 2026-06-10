from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from telepath.chat_export import TelethonChatExporter


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


async def test_chat_exporter_lists_dialogs_with_pagination():
    exporter = TelethonChatExporter(FakeClient())

    page = await exporter.list_chats(page=1, page_size=2)

    assert page.page == 1
    assert page.total_pages == 2
    assert [(chat.chat_id, chat.title, chat.kind) for chat in page.chats] == [
        (-10030, "News", "channel")
    ]


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
