from __future__ import annotations

import asyncio

from telepath.chat_export import ChatMediaArchivePart
from telepath.chat_export import ChatExportDocument, ExportChat, ExportChatPage
from telepath.manager_bot import (
    CHAT_ARCHIVE_PREMIUM_PART_BYTES,
    CHAT_ARCHIVE_STANDARD_PART_BYTES,
    ChatMediaExportSendSummary,
    _chat_export_caption,
    _chat_export_detail_view,
    _chat_export_done_view,
    _chat_export_full_history_warning_view,
    _chat_export_limit_prompt_view,
    _chat_export_limit_warning_view,
    _chat_export_menu_view,
    _chat_export_page_action,
    _chat_export_prompt_view,
    _chat_export_search_prompt_view,
    _chat_export_exception_message,
    _chat_media_archive_caption,
    _chat_media_archive_limit_bytes,
    _chat_media_export_done_view,
    _chat_media_export_warning_view,
    _chat_archive_upload_target,
    _confirm_media_export_action,
    _export_callback_ack_text,
    _is_user_account_archive_upload_message,
    _parse_confirm_media_export_action,
    _parse_export_limit_action,
    _parse_export_limit_text,
    _parse_full_history_action,
    _parse_confirm_export_action,
    _parse_export_chat_action,
    _send_chat_media_archives_to_peer,
    _should_warn_export_limit,
    _upload_document_action_kwargs,
)


def button_texts(view):
    return [button.text for row in view.keyboard for button in row]


def button_actions(view):
    return [button.action for row in view.keyboard for button in row]


def test_chat_export_menu_view_paginates_dialogs():
    page = ExportChatPage(
        chats=[ExportChat(chat_id=10, title="Alice", kind="private")],
        page=1,
        page_size=8,
        total=9,
    )

    view = _chat_export_menu_view(page)

    assert "Экспорт чата" in view.text
    assert "Страница 2/2" in view.text
    assert "[ЛС] Alice" in button_texts(view)
    assert "export.chat:10:1" in button_actions(view)
    assert "Найти чат" in button_texts(view)
    assert "Обновить список" in button_texts(view)
    assert "‹" in button_texts(view)
    assert "›" in button_texts(view)
    assert "Ввести chat_id" in button_texts(view)


def test_chat_export_menu_view_keeps_search_context():
    page = ExportChatPage(
        chats=[ExportChat(chat_id=-10020, title="Team", kind="group")],
        page=0,
        page_size=8,
        total=1,
    )

    view = _chat_export_menu_view(page, search_query="team")

    assert "Поиск: team" in view.text
    assert view.action == "export.chats.search.page:0"
    assert "export.chat:-10020:0:search" in button_actions(view)
    assert "Сбросить поиск" in button_texts(view)
    assert "export.chats.search.clear" in button_actions(view)


def test_chat_export_detail_view_shows_chat_info_and_requires_confirmation():
    view = _chat_export_detail_view(
        ExportChat(chat_id=-10020, title="Team", kind="group"),
        page=3,
        mode="search",
    )

    assert "Team" in view.text
    assert "-10020" in view.text
    assert "Группа" in view.text
    assert "Выбери объем" in view.text
    assert "Последние 1 000" in button_texts(view)
    assert "Последние 5 000" in button_texts(view)
    assert ".zip с медиа · 1 000" in button_texts(view)
    assert ".zip с медиа · 5 000" in button_texts(view)
    assert "Вся история" in button_texts(view)
    assert "Вся история с медиа" in button_texts(view)
    assert "Другой лимит" in button_texts(view)
    assert "export.confirm:-10020:3:search:1000" in button_actions(view)
    assert "export.confirm:-10020:3:search:5000" in button_actions(view)
    assert "export.media:-10020:3:search:1000" in button_actions(view)
    assert "export.media:-10020:3:search:5000" in button_actions(view)
    assert "export.full:-10020:3:search" in button_actions(view)
    assert "export.media:-10020:3:search:all" in button_actions(view)
    assert "export.limit:-10020:3:search" in button_actions(view)
    assert "К списку чатов" in button_texts(view)
    assert "export.chats.search.page:3" in button_actions(view)


def test_chat_media_export_warning_view_uses_user_account_upload_and_part_limit():
    view = _chat_media_export_warning_view(
        ExportChat(chat_id=-10020, title="Team", kind="group"),
        page=3,
        mode="search",
        limit=5000,
        is_premium=False,
    )

    assert "Архив с медиа" in view.text
    assert "Team" in view.text
    assert "1.5 GB" in view.text
    assert "user account" in view.text
    assert "исходные файлы удаляются" in view.text
    assert "Создать .zip с медиа" in button_texts(view)
    assert "export.media.confirm:-10020:3:search:5000" in button_actions(view)
    assert "export.confirm:-10020:3:search:5000" in button_actions(view)
    assert "export.chat:-10020:3:search" in button_actions(view)


def test_chat_media_archive_limit_bytes_respects_premium_status():
    assert _chat_media_archive_limit_bytes(False) == CHAT_ARCHIVE_STANDARD_PART_BYTES
    assert _chat_media_archive_limit_bytes(True) == CHAT_ARCHIVE_PREMIUM_PART_BYTES


def test_chat_export_full_history_warning_requires_explicit_start():
    view = _chat_export_full_history_warning_view(
        ExportChat(chat_id=-10020, title="Team", kind="group"),
        page=3,
        mode="search",
    )

    assert "Team" in view.text
    assert "всю историю" in view.text
    assert "Telegram может поставить паузу" in view.text
    assert "Экспортировать всю историю" in button_texts(view)
    assert "Лучше последние 5 000" in button_texts(view)
    assert "export.confirm:-10020:3:search:all" in button_actions(view)
    assert "export.confirm:-10020:3:search:5000" in button_actions(view)
    assert "export.chat:-10020:3:search" in button_actions(view)


def test_chat_export_prompt_view_requests_chat_id():
    view = _chat_export_prompt_view()

    assert view.input_state == "export_chat_id"
    assert "chat_id" in view.text


def test_chat_export_search_prompt_view_requests_query():
    view = _chat_export_search_prompt_view()

    assert view.input_state == "export_chat_search"
    assert "названия" in view.text


def test_chat_export_limit_prompt_view_requests_custom_limit():
    view = _chat_export_limit_prompt_view(chat_id=-10020, page=3, mode="search")

    assert view.input_state == "export_chat_limit"
    assert "число сообщений" in view.text
    assert "export.chat:-10020:3:search" in button_actions(view)


def test_chat_export_limit_warning_view_only_intercepts_all_history():
    chat = ExportChat(chat_id=-10020, title="Team", kind="group")

    warning = _chat_export_limit_warning_view(chat, page=3, mode="search", limit=None)

    assert warning is not None
    assert "Экспорт всей истории" in warning.text
    assert "export.confirm:-10020:3:search:all" in button_actions(warning)
    assert _chat_export_limit_warning_view(chat, page=3, mode="search", limit=5000) is None


def test_chat_export_limit_warning_view_intercepts_large_manual_limit():
    chat = ExportChat(chat_id=-10020, title="Team", kind="group")

    warning = _chat_export_limit_warning_view(chat, page=3, mode="search", limit=5001)

    assert warning is not None
    assert "Экспорт 5 001 сообщений" in warning.text
    assert "Telegram может поставить паузу" in warning.text
    assert "Экспортировать 5 001" in button_texts(warning)
    assert "Лучше последние 5 000" in button_texts(warning)
    assert "export.confirm:-10020:3:search:5001" in button_actions(warning)
    assert "export.confirm:-10020:3:search:5000" in button_actions(warning)


def test_should_warn_export_limit_only_flags_risky_manual_exports():
    assert _should_warn_export_limit(None)
    assert _should_warn_export_limit(5001)
    assert not _should_warn_export_limit(5000)
    assert not _should_warn_export_limit(1)


def test_parse_export_chat_action_reads_chat_id_and_page():
    assert _parse_export_chat_action("export.chat:-10020:3") == (-10020, 3, "all")
    assert _parse_export_chat_action("export.chat:-10020:3:search") == (-10020, 3, "search")
    assert _parse_export_chat_action("export.chat:bad:3") is None
    assert _parse_export_chat_action("export.chats") is None


def test_parse_confirm_export_action_reads_chat_id_and_page():
    assert _parse_confirm_export_action("export.confirm:-10020:3") == (-10020, 3, "all", None)
    assert _parse_confirm_export_action("export.confirm:-10020:3:search") == (-10020, 3, "search", None)
    assert _parse_confirm_export_action("export.confirm:-10020:3:search:1000") == (-10020, 3, "search", 1000)
    assert _parse_confirm_export_action("export.confirm:-10020:3:search:all") == (-10020, 3, "search", None)
    assert _parse_confirm_export_action("export.confirm:bad:3") is None
    assert _parse_confirm_export_action("export.chat:-10020:3") is None


def test_parse_confirm_media_export_action_reads_chat_id_page_and_limit():
    assert _parse_confirm_media_export_action("export.media.confirm:-10020:3:search:1000") == (
        -10020,
        3,
        "search",
        1000,
    )
    assert _parse_confirm_media_export_action("export.media.confirm:-10020:3:search:all") == (
        -10020,
        3,
        "search",
        None,
    )
    assert _parse_confirm_media_export_action("export.media.confirm:bad:3:search:1000") is None
    assert _parse_confirm_media_export_action("export.confirm:-10020:3:search:1000") is None


def test_parse_export_limit_action_reads_context():
    assert _parse_export_limit_action("export.limit:-10020:3:search") == (-10020, 3, "search")
    assert _parse_export_limit_action("export.limit:bad:3:search") is None
    assert _parse_export_limit_action("export.confirm:-10020:3:search") is None


def test_parse_full_history_action_reads_context():
    assert _parse_full_history_action("export.full:-10020:3:search") == (-10020, 3, "search")
    assert _parse_full_history_action("export.full:-10020:3") == (-10020, 3, "all")
    assert _parse_full_history_action("export.full:bad:3:search") is None
    assert _parse_full_history_action("export.confirm:-10020:3:search") is None


def test_parse_export_limit_text_accepts_number_and_all():
    assert _parse_export_limit_text("2500") == (True, 2500)
    assert _parse_export_limit_text("  2 500  ") == (True, 2500)
    assert _parse_export_limit_text("все") == (True, None)
    assert _parse_export_limit_text("0") == (False, None)
    assert _parse_export_limit_text("abc") == (False, None)


def test_chat_export_done_view_links_back_to_same_page():
    view = _chat_export_done_view(
        chat_id=-10020,
        page=2,
        mode="search",
        filename="x.txt",
        message_count=42,
        service_message_count=3,
        byte_count=1536,
    )

    assert "x.txt" in view.text
    assert "Сообщений в файле: 42" in view.text
    assert "Сервисных событий пропущено: 3" in view.text
    assert "Размер файла: 1.5 KB" in view.text
    assert button_actions(view)[0] == "export.chat:-10020:2:search"
    assert button_actions(view)[1] == "export.chats.search.page:2"


def test_chat_export_exception_message_explains_flood_wait():
    error = type("FloodWaitError", (Exception,), {"seconds": 125})("raw flood")

    message = _chat_export_exception_message(error)

    assert "Telegram поставил паузу" in message
    assert "2 мин 5 сек" in message
    assert "raw flood" not in message


def test_chat_export_page_action_uses_current_mode():
    assert _chat_export_page_action(4, "all") == "export.chats.page:4"
    assert _chat_export_page_action(4, "search") == "export.chats.search.page:4"
    assert _chat_export_page_action(4, "bad") == "export.chats.page:4"


def test_export_callback_ack_text_does_not_show_navigation_toasts():
    assert _export_callback_ack_text("export.chats.refresh") is None
    assert _export_callback_ack_text("export.chats.search.refresh") is None
    assert _export_callback_ack_text("export.chats.page:2") is None
    assert _export_callback_ack_text("export.chat:-10020:3") is None
    assert _export_callback_ack_text("export.limit:-10020:3") is None
    assert _export_callback_ack_text("export.full:-10020:3") is None
    assert _export_callback_ack_text("export.confirm:-10020:3") is None
    assert _export_callback_ack_text("export.media:-10020:3:search:1000") is None
    assert _export_callback_ack_text("export.media.confirm:-10020:3:search:1000") is None


def test_upload_document_action_kwargs_targets_current_chat_thread():
    bot = object()
    chat = type("Chat", (), {"id": -10020})()
    message = type(
        "Message",
        (),
        {
            "bot": bot,
            "chat": chat,
            "message_thread_id": 77,
        },
    )()

    assert _upload_document_action_kwargs(message) == {
        "bot": bot,
        "chat_id": -10020,
        "message_thread_id": 77,
        "interval": 4.0,
        "initial_sleep": 0.0,
    }


def test_chat_export_caption_shows_clear_export_metadata():
    document = ChatExportDocument(
        chat_id=-10020,
        title="Евгений",
        filename="evgeniy.txt",
        data=b"x" * 1536,
        message_count=1000,
        service_message_count=2,
    )

    assert _chat_export_caption(document) == (
        "Евгений\n"
        "Сообщений в файле: 1 000\n"
        "Сервисных событий пропущено: 2\n"
        "Размер: 1.5 KB"
    )


def test_chat_media_archive_caption_shows_clear_metadata(tmp_path):
    path = tmp_path / "part.zip"
    path.write_bytes(b"zip")
    part = ChatMediaArchivePart(
        chat_id=-10020,
        title="Евгений",
        filename="part.zip",
        path=path,
        part_index=2,
        message_count=1000,
        service_message_count=2,
        media_count=9,
        byte_count=1536,
    )

    assert _chat_media_archive_caption(part) == (
        "Telepath archive export\n"
        "Евгений\n"
        "Часть: 002\n"
        "Сообщений: 1 000\n"
        "Сервисных событий пропущено: 2\n"
        "Медиафайлов: 9\n"
        "Размер: 1.5 KB"
    )


def test_chat_media_export_done_view_reports_parts_and_cleanup():
    view = _chat_media_export_done_view(
        chat_id=-10020,
        page=2,
        mode="search",
        summary=ChatMediaExportSendSummary(
            part_count=2,
            message_count=1000,
            service_message_count=2,
            media_count=9,
            byte_count=1536,
        ),
    )

    assert "Отправил архивы с медиа" in view.text
    assert "Частей: 2" in view.text
    assert "Медиафайлов: 9" in view.text
    assert "Файлы на сервере удалены" in view.text
    assert button_actions(view)[0] == "export.chat:-10020:2:search"
    assert button_actions(view)[1] == "export.chats.search.page:2"


async def test_send_chat_media_archives_to_peer_uploads_and_deletes_each_part(tmp_path):
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    first.write_bytes(b"1" * 10)
    second.write_bytes(b"2" * 20)

    class FakeExporter:
        def __init__(self):
            self.sent = []

        async def export_chat_media_archives(self, chat_id, *, limit, max_archive_bytes):
            assert chat_id == -10020
            assert limit == 1000
            assert max_archive_bytes == 123
            yield ChatMediaArchivePart(
                chat_id=chat_id,
                title="Team",
                filename=first.name,
                path=first,
                part_index=1,
                message_count=1,
                service_message_count=0,
                media_count=1,
                byte_count=10,
            )
            yield ChatMediaArchivePart(
                chat_id=chat_id,
                title="Team",
                filename=second.name,
                path=second,
                part_index=2,
                message_count=2,
                service_message_count=1,
                media_count=3,
                byte_count=20,
            )

        async def send_chat_archive_part(self, part, *, target_peer):
            self.sent.append((part.filename, target_peer, part.path.exists()))

    exporter = FakeExporter()

    summary = await _send_chat_media_archives_to_peer(
        exporter,
        target_peer="@telepath_manager_bot",
        chat_id=-10020,
        limit=1000,
        max_archive_bytes=123,
    )

    assert exporter.sent == [
        ("first.zip", "@telepath_manager_bot", True),
        ("second.zip", "@telepath_manager_bot", True),
    ]
    assert not first.exists()
    assert not second.exists()
    assert summary.part_count == 2
    assert summary.message_count == 3
    assert summary.service_message_count == 1
    assert summary.media_count == 4
    assert summary.byte_count == 30


async def test_send_chat_media_archives_to_peer_builds_next_part_while_first_uploads(tmp_path):
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    first.write_bytes(b"1")
    second.write_bytes(b"2")

    class PipelineExporter:
        def __init__(self):
            self.first_send_started = asyncio.Event()
            self.continued_during_first_send = asyncio.Event()
            self.finish_first_send = asyncio.Event()

        async def export_chat_media_archives(self, chat_id, *, limit, max_archive_bytes):
            yield ChatMediaArchivePart(chat_id, "Team", first.name, first, 1, 1, 0, 1, 1)
            await self.first_send_started.wait()
            self.continued_during_first_send.set()
            yield ChatMediaArchivePart(chat_id, "Team", second.name, second, 2, 1, 0, 1, 1)

        async def send_chat_archive_part(self, part, *, target_peer):
            if part.filename == first.name:
                self.first_send_started.set()
                await self.finish_first_send.wait()

    exporter = PipelineExporter()
    task = asyncio.create_task(
        _send_chat_media_archives_to_peer(
            exporter,
            target_peer="@telepath_manager_bot",
            chat_id=-10020,
            limit=1000,
            max_archive_bytes=123,
        )
    )

    await asyncio.wait_for(exporter.continued_during_first_send.wait(), timeout=1)
    exporter.finish_first_send.set()
    await task


async def test_chat_archive_upload_target_uses_manager_bot_username():
    class FakeBot:
        async def get_me(self):
            return type("BotUser", (), {"username": "telepath_manager_bot"})()

    assert await _chat_archive_upload_target(FakeBot()) == "@telepath_manager_bot"


def test_is_user_account_archive_upload_message_recognizes_own_archive_zip():
    document = type("Document", (), {"file_name": "telegram-Team--10020-20260610-part001.zip"})()
    message = type(
        "Message",
        (),
        {
            "caption": "Telepath archive export\nTeam",
            "document": document,
        },
    )()

    assert _is_user_account_archive_upload_message(message)
    assert not _is_user_account_archive_upload_message(type("Message", (), {"caption": "", "document": document})())
