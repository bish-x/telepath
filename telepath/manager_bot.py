from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from telepath.config import Settings, load_settings
from telepath.chat_export import ChatExportDocument, ExportChat, ExportChatPage
from telepath.panel import PanelButton, PanelView, ControlPanelService
from telepath.premium_emoji import extract_premium_emoji_ids, format_premium_emoji_reply
from telepath.storage import SQLiteAssistantRepository


logger = logging.getLogger(__name__)
CHAT_EXPORT_PAGE_SIZE = 8
CHAT_EXPORT_SAFE_LIMIT = 5000


class ChatExportPort(Protocol):
    async def list_chats(
        self,
        *,
        page: int = 0,
        page_size: int = CHAT_EXPORT_PAGE_SIZE,
        refresh: bool = False,
        query: str | None = None,
    ) -> ExportChatPage: ...
    async def get_chat(self, chat_id: int) -> ExportChat: ...
    async def export_chat_text(self, chat_id: int, *, limit: int | None = None) -> ChatExportDocument: ...


class ReactionHistoryBackfillPort(Protocol):
    async def enqueue_history(
        self,
        *,
        limit_per_channel: int | None,
        chat_id: int | None = None,
    ) -> Any: ...
    def set_completion_notifier(self, notifier: Any) -> None: ...


def _active_llm_model(settings: Settings) -> str | None:
    if settings.llm_provider == "openai":
        return settings.openai_model
    if settings.llm_provider == "anthropic":
        return settings.anthropic_model
    if settings.llm_provider == "copilot":
        return settings.copilot_model
    return None


def _panel_markup(view: PanelView, *, include_custom_icons: bool = True):
    if not view.keyboard:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button.text,
                    callback_data=f"panel:{button.action}",
                    style=button.style,
                    icon_custom_emoji_id=button.icon_custom_emoji_id if include_custom_icons else None,
                )
                for button in row
            ]
            for row in view.keyboard
        ]
    )


def _view_has_custom_button_icons(view: PanelView) -> bool:
    return any(button.icon_custom_emoji_id for row in view.keyboard for button in row)


def _should_retry_panel_without_custom_icons(error: Exception, view: PanelView) -> bool:
    if not _view_has_custom_button_icons(view):
        return False
    message = str(error).casefold()
    return "custom" in message and ("emoji" in message or "icon" in message)


def _is_message_not_modified_error(error: Exception) -> bool:
    return "message is not modified" in str(error).casefold()


def _is_callback_query_expired_error(error: Exception) -> bool:
    message = str(error).casefold()
    return "query is too old" in message or "query id is invalid" in message


async def _answer_callback_query(callback: Any, text: str | None = None) -> None:
    from aiogram.exceptions import TelegramBadRequest

    try:
        if text is None:
            await callback.answer()
        else:
            await callback.answer(text)
    except TelegramBadRequest as error:
        if not _is_callback_query_expired_error(error):
            raise
        logger.info("callback_answer_skipped_expired_query")


def _panel_callback_ack_text(action: str, view: PanelView | None = None) -> str | None:
    state_change_prefixes = (
        "reactions.channel.toggle:",
        "reactions.channel.max1:",
        "reactions.channel.max3:",
        "reactions.channel.strategy:",
        "reactions.channel.source:",
        "reactions.channel.mode:",
        "reactions.channel.emoji:",
        "reactions.folder.toggle:",
        "reactions.folder.max1:",
        "reactions.folder.max3:",
        "reactions.folder.strategy:",
        "reactions.folder.source:",
        "reactions.folder.mode:",
        "rce:",
        "rcc:",
    )
    if action.startswith(state_change_prefixes):
        return "Обновлено."
    return None


def _parse_reaction_available_refresh_action(action: str) -> tuple[int, int] | None:
    parts = action.split(":")
    if len(parts) != 3 or parts[0] != "rcr":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _parse_reaction_channel_detail_action(action: str) -> tuple[int, int] | None:
    parts = action.split(":")
    if len(parts) != 3 or parts[0] != "reactions.channel":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _parse_reaction_history_backfill_action(action: str) -> tuple[int | None, int | None, int] | None:
    parts = action.split(":")

    def parse_limit(value: str) -> int | None:
        if value == "all":
            return None
        limit = int(value)
        if limit not in {1000, 2000, 5000}:
            raise ValueError
        return limit

    try:
        if len(parts) == 3 and parts[0] == "rhb" and parts[1] == "all":
            limit = parse_limit(parts[2])
            return None, limit, 0
        if len(parts) == 5 and parts[0] == "rhb" and parts[1] == "ch":
            chat_id = int(parts[2])
            limit = parse_limit(parts[3])
            page = int(parts[4])
            if page < 0:
                return None
            return chat_id, limit, page
    except ValueError:
        return None
    return None


def _append_panel_feedback(view: PanelView, feedback: str) -> PanelView:
    return PanelView(
        text=f"{view.text}\n\n{feedback}",
        keyboard=view.keyboard,
        input_state=view.input_state,
        action=view.action,
    )


async def _refresh_reaction_channel_available_reactions(
    *,
    chat_id: int,
    state: SQLiteAssistantRepository,
    chat_exporter: ChatExportPort | None,
    sender_factory: Any | None = None,
) -> int:
    client = getattr(chat_exporter, "client", None)
    if client is None:
        raise RuntimeError("Telegram user client недоступен.")

    if sender_factory is None:
        from telepath.user_client import TelethonChannelReactionSender

        sender_factory = TelethonChannelReactionSender
    reactions = await sender_factory(client).available_reactions(chat_id)
    state.replace_reaction_channel_available_reactions(chat_id, reactions)
    return len(reactions)


async def _render_reaction_folder_refresh_action(
    *,
    user_id: int,
    panel: ControlPanelService,
    state: SQLiteAssistantRepository,
    chat_exporter: ChatExportPort | None,
) -> PanelView:
    view = panel.handle_action(user_id=user_id, action="reactions.folders")
    if user_id != panel.owner_id:
        return view

    client = getattr(chat_exporter, "client", None)
    if client is None:
        return _append_panel_feedback(
            view,
            "Не могу обновить из manager-only режима: Telegram user client недоступен.",
        )

    try:
        from telepath.user_client import sync_reaction_folders

        folder_count = await sync_reaction_folders(client, state)
    except Exception as error:
        logger.exception("reaction_folder_refresh_failed")
        refreshed = panel.handle_action(user_id=user_id, action="reactions.folders")
        return _append_panel_feedback(refreshed, f"Не смог обновить папки: {error}")

    refreshed = panel.handle_action(user_id=user_id, action="reactions.folders")
    return _append_panel_feedback(refreshed, f"Папки обновлены: {folder_count}.")


async def _render_reaction_channel_detail_action(
    *,
    user_id: int,
    action: str,
    panel: ControlPanelService,
    state: SQLiteAssistantRepository,
    chat_exporter: ChatExportPort | None,
    sender_factory: Any | None = None,
) -> PanelView:
    parsed = _parse_reaction_channel_detail_action(action)
    if parsed is None:
        return panel.handle_action(user_id=user_id, action="reactions")
    chat_id, page = parsed
    view = panel.handle_action(user_id=user_id, action=action)
    if user_id != panel.owner_id or state.has_reaction_channel_available_reactions_checked(chat_id):
        return view
    if getattr(chat_exporter, "client", None) is None:
        return _append_panel_feedback(
            view,
            "Не могу автоматически обновить из manager-only режима: Telegram user client недоступен.",
        )

    try:
        await _refresh_reaction_channel_available_reactions(
            chat_id=chat_id,
            state=state,
            chat_exporter=chat_exporter,
            sender_factory=sender_factory,
        )
    except Exception as error:
        logger.exception("reaction_available_first_click_refresh_failed chat_id=%s", chat_id)
        refreshed = panel.handle_action(user_id=user_id, action=action)
        return _append_panel_feedback(refreshed, f"Не смог обновить доступные реакции: {error}")

    return panel.handle_action(user_id=user_id, action=f"reactions.channel:{chat_id}:{page}")


async def _render_reaction_available_refresh_action(
    *,
    user_id: int,
    action: str,
    panel: ControlPanelService,
    state: SQLiteAssistantRepository,
    chat_exporter: ChatExportPort | None,
    sender_factory: Any | None = None,
) -> PanelView:
    parsed = _parse_reaction_available_refresh_action(action)
    if parsed is None:
        return panel.handle_action(user_id=user_id, action="reactions")
    chat_id, page = parsed
    detail_action = f"reactions.channel:{chat_id}:{page}"
    view = panel.handle_action(user_id=user_id, action=detail_action)
    if user_id != panel.owner_id:
        return view

    if getattr(chat_exporter, "client", None) is None:
        return _append_panel_feedback(
            view,
            "Не могу обновить из manager-only режима: Telegram user client недоступен.",
        )

    try:
        reaction_count = await _refresh_reaction_channel_available_reactions(
            chat_id=chat_id,
            state=state,
            chat_exporter=chat_exporter,
            sender_factory=sender_factory,
        )
    except Exception as error:
        logger.exception("reaction_available_refresh_failed chat_id=%s", chat_id)
        refreshed = panel.handle_action(user_id=user_id, action=detail_action)
        return _append_panel_feedback(refreshed, f"Не смог обновить доступные реакции: {error}")

    refreshed = panel.handle_action(user_id=user_id, action=detail_action)
    return _append_panel_feedback(refreshed, f"Доступные реакции обновлены: {reaction_count}.")


def _reaction_history_backfill_feedback(result: Any) -> str:
    if bool(getattr(result, "duplicate_queued", False)):
        position = getattr(result, "queue_position", None)
        suffix = f" Позиция: {position}." if position else ""
        return (
            "Такой запуск истории уже есть в очереди."
            f"{suffix}\n"
            "Обработка идет в фоне: один пост раз в 8-15 сек."
        )

    if bool(getattr(result, "request_queued", False)):
        position = getattr(result, "queue_position", None)
        channel_count = int(getattr(result, "channel_count", 0))
        lines = ["История добавлена в очередь."]
        if position:
            lines.append(f"Позиция: {position}.")
        if channel_count:
            lines.append(f"Каналов: {channel_count}.")
        lines.append("Обработка идет в фоне: один пост раз в 8-15 сек.")
        return "\n".join(lines)

    if bool(getattr(result, "already_running", False)):
        return "История уже обрабатывается. Дождись завершения текущего обхода."

    sent_count = int(getattr(result, "sent_count", getattr(result, "queued_count", 0)))
    reaction_count = int(getattr(result, "reaction_count", sent_count))
    scanned_count = int(getattr(result, "scanned_count", 0))
    skipped_count = int(getattr(result, "skipped_count", 0))
    channel_count = int(getattr(result, "channel_count", 0))
    failed_count = int(getattr(result, "failed_count", 0))
    if channel_count <= 0:
        if getattr(result, "target_chat_id", None) is not None:
            return "У канала нет сохраненных настроек автолайка для обработки истории."
        return (
            "Нет включенных каналов для массовой обработки истории.\n"
            "Выключенный realtime-канал можно обработать из его карточки через «История»."
        )

    lines = [
        f"Реакций поставлено: {reaction_count}",
        f"Постов с реакциями: {sent_count}",
        f"Просканировано: {scanned_count}",
        f"Пропущено: {skipped_count}",
        f"Каналов: {channel_count}",
    ]
    skip_reasons = _format_reaction_history_skip_reasons(getattr(result, "skip_reasons", {}))
    if skip_reasons:
        lines.append(f"Причины: {skip_reasons}")
    if failed_count:
        lines.append(f"Ошибок: {failed_count}")
    if sent_count:
        lines.append("История обработана последовательно: один пост раз в 8-15 сек.")
    else:
        lines.append("Новых постов для обработки не нашел.")
    return "\n".join(lines)


def _reaction_history_backfill_completion_message(result: Any, *, channel_title: str | None = None) -> str:
    failed_count = int(getattr(result, "failed_count", 0))
    title = "История автолайка завершена с ошибками" if failed_count else "История автолайка завершена"
    sent_count = int(getattr(result, "sent_count", getattr(result, "queued_count", 0)))
    reaction_count = int(getattr(result, "reaction_count", sent_count))
    target_chat_id = getattr(result, "target_chat_id", None)
    if target_chat_id is None:
        target_line = "Каналы: все включенные"
    elif channel_title:
        target_line = f"Канал: {channel_title} ({target_chat_id})"
    else:
        target_line = f"Канал: {target_chat_id}"

    limit = getattr(result, "limit_per_channel", None)
    limit_line = "Лимит: все посты" if limit is None else f"Лимит: {int(limit)} новых пригодных постов"
    lines = [
        title,
        "",
        target_line,
        limit_line,
        f"Каналов: {int(getattr(result, 'channel_count', 0))}",
        f"Просканировано: {int(getattr(result, 'scanned_count', 0))}",
        f"Реакций поставлено: {reaction_count}",
        f"Постов с реакциями: {sent_count}",
        f"Пропущено: {int(getattr(result, 'skipped_count', 0))}",
    ]
    skip_reasons = _format_reaction_history_skip_reasons(getattr(result, "skip_reasons", {}))
    if skip_reasons:
        lines.append(f"Причины: {skip_reasons}")
    if failed_count:
        lines.append(f"Ошибок: {failed_count}")
    return "\n".join(lines)


def _reaction_history_channel_title(state: Any, chat_id: int | None) -> str | None:
    if chat_id is None:
        return None
    try:
        channels = state.list_known_chats(kind="channel")
    except Exception:
        logger.exception("reaction_history_completion_channel_title_lookup_failed chat_id=%s", chat_id)
        return None
    for channel in channels:
        if int(channel.get("chat_id", 0)) == int(chat_id):
            title = str(channel.get("title") or "").strip()
            return title or None
    return None


def _bind_reaction_history_completion_notifier(
    *,
    bot: Any,
    owner_id: int,
    state: Any,
    reaction_history_backfill: ReactionHistoryBackfillPort | None,
) -> None:
    if reaction_history_backfill is None:
        return

    async def notify(result: Any) -> None:
        channel_title = _reaction_history_channel_title(
            state,
            getattr(result, "target_chat_id", None),
        )
        await bot.send_message(
            owner_id,
            _reaction_history_backfill_completion_message(result, channel_title=channel_title),
        )

    reaction_history_backfill.set_completion_notifier(notify)


def _format_reaction_history_skip_reasons(skip_reasons: Any) -> str:
    labels = {
        "already_processed": "уже обработаны",
        "media_group_duplicate": "альбомы",
        "service_message": "сервисные",
        "no_reactions_available": "нет реакций",
        "no_reactions_sent": "не поставились",
        "channel_disabled": "канал выключен",
        "global_disabled": "автолайк выключен",
        "not_reactable": "не посты",
    }
    if not isinstance(skip_reasons, dict):
        return ""
    parts = []
    for reason, count in skip_reasons.items():
        try:
            normalized_count = int(count)
        except (TypeError, ValueError):
            continue
        if normalized_count <= 0:
            continue
        parts.append(f"{labels.get(str(reason), str(reason))} {normalized_count}")
    return ", ".join(parts)


async def _render_reaction_history_backfill_action(
    *,
    user_id: int,
    action: str,
    panel: ControlPanelService,
    reaction_history_backfill: ReactionHistoryBackfillPort | None,
) -> PanelView:
    parsed = _parse_reaction_history_backfill_action(action)
    if parsed is None:
        return panel.handle_action(user_id=user_id, action="reactions.history")
    chat_id, limit_per_channel, page = parsed
    target_action = f"reactions.channel.history:{chat_id}:{page}" if chat_id is not None else "reactions.history"
    view = panel.handle_action(user_id=user_id, action=target_action)
    if user_id != panel.owner_id:
        return view
    if reaction_history_backfill is None:
        return _append_panel_feedback(view, "Telegram user client недоступен.")

    try:
        result = await reaction_history_backfill.enqueue_history(
            limit_per_channel=limit_per_channel,
            chat_id=chat_id,
        )
    except Exception as error:
        logger.exception("reaction_history_backfill_start_failed action=%s", action)
        refreshed = panel.handle_action(user_id=user_id, action=target_action)
        return _append_panel_feedback(refreshed, f"Не смог обработать историю: {error}")

    refreshed = panel.handle_action(user_id=user_id, action=target_action)
    return _append_panel_feedback(refreshed, _reaction_history_backfill_feedback(result))


async def _edit_panel_message(message: Any, view: PanelView) -> None:
    from aiogram.exceptions import TelegramBadRequest

    try:
        await message.edit_text(view.text, reply_markup=_panel_markup(view))
    except TelegramBadRequest as error:
        if _is_message_not_modified_error(error):
            return
        if not _should_retry_panel_without_custom_icons(error, view):
            raise
        logger.warning("panel_custom_emoji_button_icons_rejected; retrying_without_icons")
        try:
            await message.edit_text(view.text, reply_markup=_panel_markup(view, include_custom_icons=False))
        except TelegramBadRequest as retry_error:
            if _is_message_not_modified_error(retry_error):
                return
            raise


async def _answer_panel_message(message: Any, view: PanelView) -> None:
    from aiogram.exceptions import TelegramBadRequest

    try:
        await message.answer(view.text, reply_markup=_panel_markup(view))
    except TelegramBadRequest as error:
        if not _should_retry_panel_without_custom_icons(error, view):
            raise
        logger.warning("panel_custom_emoji_button_icons_rejected; retrying_without_icons")
        await message.answer(view.text, reply_markup=_panel_markup(view, include_custom_icons=False))


def _chat_export_menu_view(page: ExportChatPage, *, search_query: str | None = None) -> PanelView:
    mode = "search" if (search_query or "").strip() else "all"
    lines = [
        "Экспорт чата",
        "",
        "Выбери чат. Я отправлю .txt с текстовой историей.",
        f"Страница {page.page + 1}/{page.total_pages}, чатов: {page.total}",
    ]
    if mode == "search":
        lines.extend([f"Поиск: {search_query}", f"Найдено: {page.total}"])
    if not page.chats and mode == "search":
        lines.extend(["", "Ничего не найдено. Уточни запрос или сбрось поиск."])
    elif not page.chats:
        lines.extend(["", "Чаты не найдены."])

    keyboard = [
        [
            PanelButton(
                _chat_button_label(chat.kind, chat.title),
                _export_chat_action(chat.chat_id, page.page, mode),
            )
        ]
        for chat in page.chats
    ]
    if page.total_pages > 1:
        prev_page = max(page.page - 1, 0)
        next_page = min(page.page + 1, page.total_pages - 1)
        keyboard.append(
            [
                PanelButton("‹", _chat_export_page_action(prev_page, mode)),
                PanelButton(f"{page.page + 1}/{page.total_pages}", _chat_export_page_action(page.page, mode)),
                PanelButton("›", _chat_export_page_action(next_page, mode)),
            ]
        )
    if mode == "search":
        keyboard.append(
            [
                PanelButton("Найти заново", "export.chats.search"),
                PanelButton("Сбросить поиск", "export.chats.search.clear"),
            ]
        )
        keyboard.append([PanelButton("Обновить список", "export.chats.search.refresh")])
    else:
        keyboard.append([PanelButton("Найти чат", "export.chats.search")])
        keyboard.append([PanelButton("Обновить список", "export.chats.refresh")])
    keyboard.append([PanelButton("Ввести chat_id", "export.chats.input")])
    keyboard.append([PanelButton("Назад", "back")])
    return PanelView(text="\n".join(lines), keyboard=keyboard, action=_chat_export_page_action(page.page, mode))


def _chat_export_detail_view(chat: ExportChat, *, page: int, mode: str = "all") -> PanelView:
    mode = _normalize_export_mode(mode)
    return PanelView(
        text="\n".join(
            [
                "Экспорт чата",
                "",
                f"Название: {chat.title}",
                f"chat_id: {chat.chat_id}",
                f"Тип: {_chat_kind_label(chat.kind)}",
                "",
                "Выбери объем. Последние сообщения обычно быстрее и безопаснее для больших чатов.",
            ]
        ),
        keyboard=[
            [PanelButton("Последние 1 000", _confirm_export_action(chat.chat_id, page, mode, limit=1000))],
            [PanelButton("Последние 5 000", _confirm_export_action(chat.chat_id, page, mode, limit=5000))],
            [PanelButton("Вся история", _full_history_action(chat.chat_id, page, mode))],
            [PanelButton("Другой лимит", _export_limit_action(chat.chat_id, page, mode))],
            [PanelButton("К списку чатов", _chat_export_page_action(page, mode))],
            [PanelButton("Назад", "back")],
        ],
        action=_export_chat_action(chat.chat_id, page, mode),
    )


def _chat_export_full_history_warning_view(chat: ExportChat, *, page: int, mode: str = "all") -> PanelView:
    mode = _normalize_export_mode(mode)
    return PanelView(
        text="\n".join(
            [
                "Экспорт всей истории",
                "",
                f"Название: {chat.title}",
                f"chat_id: {chat.chat_id}",
                "",
                "Ты собираешься выгрузить всю историю чата. Это может занять заметное время, "
                "а Telegram может поставить паузу по лимитам API.",
            ]
        ),
        keyboard=[
            [PanelButton("Экспортировать всю историю", _confirm_export_action(chat.chat_id, page, mode, limit=None))],
            [PanelButton("Лучше последние 5 000", _confirm_export_action(chat.chat_id, page, mode, limit=5000))],
            [PanelButton("К карточке чата", _export_chat_action(chat.chat_id, page, mode))],
            [PanelButton("К списку чатов", _chat_export_page_action(page, mode))],
        ],
        action=_full_history_action(chat.chat_id, page, mode),
    )


def _chat_export_limit_warning_view(
    chat: ExportChat,
    *,
    page: int,
    mode: str = "all",
    limit: int | None,
) -> PanelView | None:
    if not _should_warn_export_limit(limit):
        return None
    return _chat_export_required_limit_warning_view(chat, page=page, mode=mode, limit=limit)


def _chat_export_required_limit_warning_view(
    chat: ExportChat,
    *,
    page: int,
    mode: str = "all",
    limit: int | None,
) -> PanelView:
    if limit is not None:
        return _chat_export_large_limit_warning_view(chat, page=page, mode=mode, limit=limit)
    return _chat_export_full_history_warning_view(chat, page=page, mode=mode)


def _should_warn_export_limit(limit: int | None) -> bool:
    return limit is None or limit > CHAT_EXPORT_SAFE_LIMIT


def _chat_export_large_limit_warning_view(chat: ExportChat, *, page: int, mode: str = "all", limit: int) -> PanelView:
    mode = _normalize_export_mode(mode)
    limit_label = _format_int_grouped(limit)
    safe_limit_label = _format_int_grouped(CHAT_EXPORT_SAFE_LIMIT)
    return PanelView(
        text="\n".join(
            [
                f"Экспорт {limit_label} сообщений",
                "",
                f"Название: {chat.title}",
                f"chat_id: {chat.chat_id}",
                "",
                "Это большой ручной лимит. Выгрузка может занять заметное время, "
                "а Telegram может поставить паузу по лимитам API.",
            ]
        ),
        keyboard=[
            [
                PanelButton(
                    f"Экспортировать {limit_label}",
                    _confirm_export_action(chat.chat_id, page, mode, limit=limit),
                )
            ],
            [
                PanelButton(
                    f"Лучше последние {safe_limit_label}",
                    _confirm_export_action(chat.chat_id, page, mode, limit=CHAT_EXPORT_SAFE_LIMIT),
                )
            ],
            [PanelButton("К карточке чата", _export_chat_action(chat.chat_id, page, mode))],
            [PanelButton("К списку чатов", _chat_export_page_action(page, mode))],
        ],
        action=_export_limit_action(chat.chat_id, page, mode),
    )


def _chat_export_prompt_view() -> PanelView:
    return PanelView(
        text="Пришли chat_id чата для выгрузки.\n\nПример: -1001234567890",
        keyboard=[[PanelButton("К списку чатов", "export.chats")], [PanelButton("Назад", "back")]],
        input_state="export_chat_id",
        action="export.chats.input",
    )


def _chat_export_search_prompt_view() -> PanelView:
    return PanelView(
        text="Пришли часть названия чата или chat_id для поиска.",
        keyboard=[[PanelButton("К списку чатов", "export.chats")], [PanelButton("Назад", "back")]],
        input_state="export_chat_search",
        action="export.chats.search",
    )


def _chat_export_limit_prompt_view(*, chat_id: int, page: int, mode: str = "all") -> PanelView:
    mode = _normalize_export_mode(mode)
    return PanelView(
        text=(
            "Пришли число сообщений для экспорта.\n\n"
            "Пример: 2500\n"
            "Можно написать: все"
        ),
        keyboard=[
            [PanelButton("К карточке чата", _export_chat_action(chat_id, page, mode))],
            [PanelButton("К списку чатов", _chat_export_page_action(page, mode))],
        ],
        input_state="export_chat_limit",
        action=_export_limit_action(chat_id, page, mode),
    )


def _chat_export_done_view(
    *,
    chat_id: int,
    page: int,
    filename: str,
    message_count: int,
    service_message_count: int,
    byte_count: int,
    mode: str = "all",
) -> PanelView:
    return PanelView(
        text="\n".join(
            [
                f"Готово. Отправил {filename}",
                f"Сообщений в файле: {_format_int_grouped(message_count)}",
                f"Сервисных событий пропущено: {_format_int_grouped(service_message_count)}",
                f"Размер файла: {_format_bytes(byte_count)}",
            ]
        ),
        keyboard=[
            [PanelButton("К карточке чата", _export_chat_action(chat_id, page, mode))],
            [PanelButton("К списку чатов", _chat_export_page_action(page, mode))],
            [PanelButton("Назад", "back")],
        ],
        action="export.chats",
    )


def _chat_export_error_view(message: str) -> PanelView:
    return PanelView(
        text=f"Не смог выгрузить чат.\n\n{message}",
        keyboard=[[PanelButton("К списку чатов", "export.chats")], [PanelButton("Назад", "back")]],
        action="export.chats",
    )


def _chat_export_exception_message(error: Exception) -> str:
    wait_seconds = _flood_wait_seconds(error)
    if wait_seconds is not None:
        return (
            f"Telegram поставил паузу на {_format_wait_duration(wait_seconds)} из-за лимитов API. "
            "Экспорт остановлен, чтобы не флудить запросами. Попробуй позже."
        )
    return str(error)


def _flood_wait_seconds(error: Exception) -> int | None:
    error_name = getattr(error, "name", "")
    class_name = error.__class__.__name__
    if "FloodWait" not in class_name and error_name != "FLOOD_WAIT":
        return None
    seconds = getattr(error, "seconds", None)
    if seconds is None:
        seconds = getattr(error, "value", None)
    try:
        return max(0, int(seconds))
    except (TypeError, ValueError):
        return None


def _format_wait_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, tail_seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if tail_seconds or not parts:
        parts.append(f"{tail_seconds} сек")
    return " ".join(parts)


def _parse_export_chat_action(action: str) -> tuple[int, int, str] | None:
    parts = action.split(":")
    if len(parts) not in {3, 4} or parts[0] != "export.chat":
        return None
    try:
        mode = _normalize_export_mode(parts[3] if len(parts) == 4 else "all")
        return int(parts[1]), int(parts[2]), mode
    except ValueError:
        return None


def _parse_confirm_export_action(action: str) -> tuple[int, int, str, int | None] | None:
    parts = action.split(":")
    if len(parts) not in {3, 4, 5} or parts[0] != "export.confirm":
        return None
    try:
        mode = _normalize_export_mode(parts[3] if len(parts) == 4 else "all")
        if len(parts) == 5:
            mode = _normalize_export_mode(parts[3])
            parsed_limit = _parse_export_limit_token(parts[4])
            if not parsed_limit[0]:
                return None
            limit = parsed_limit[1]
        else:
            limit = None
        return int(parts[1]), int(parts[2]), mode, limit
    except ValueError:
        return None


def _parse_export_limit_action(action: str) -> tuple[int, int, str] | None:
    parts = action.split(":")
    if len(parts) != 4 or parts[0] != "export.limit":
        return None
    try:
        return int(parts[1]), int(parts[2]), _normalize_export_mode(parts[3])
    except ValueError:
        return None


def _parse_full_history_action(action: str) -> tuple[int, int, str] | None:
    parts = action.split(":")
    if len(parts) not in {3, 4} or parts[0] != "export.full":
        return None
    try:
        mode = _normalize_export_mode(parts[3] if len(parts) == 4 else "all")
        return int(parts[1]), int(parts[2]), mode
    except ValueError:
        return None


def _parse_export_limit_text(text: str) -> tuple[bool, int | None]:
    value = text.strip().casefold().replace(" ", "")
    if value in {"all", "все", "всё", "вся"}:
        return True, None
    if not value.isdigit():
        return False, None
    limit = int(value)
    if limit <= 0:
        return False, None
    return True, limit


def _parse_export_limit_token(value: str) -> tuple[bool, int | None]:
    return _parse_export_limit_text(value)


def _parse_export_page_action(action: str) -> int:
    try:
        return int(action.rsplit(":", 1)[1])
    except ValueError:
        return 0


def _normalize_export_mode(mode: str) -> str:
    return mode if mode in {"all", "search"} else "all"


def _export_chat_action(chat_id: int, page: int, mode: str = "all") -> str:
    mode = _normalize_export_mode(mode)
    suffix = f":{mode}" if mode != "all" else ""
    return f"export.chat:{chat_id}:{page}{suffix}"


def _confirm_export_action(chat_id: int, page: int, mode: str = "all", *, limit: int | None = None) -> str:
    mode = _normalize_export_mode(mode)
    limit_part = str(limit) if limit is not None else "all"
    return f"export.confirm:{chat_id}:{page}:{mode}:{limit_part}"


def _export_limit_action(chat_id: int, page: int, mode: str = "all") -> str:
    return f"export.limit:{chat_id}:{page}:{_normalize_export_mode(mode)}"


def _full_history_action(chat_id: int, page: int, mode: str = "all") -> str:
    mode = _normalize_export_mode(mode)
    suffix = f":{mode}" if mode != "all" else ""
    return f"export.full:{chat_id}:{page}{suffix}"


def _export_limit_label(limit: int | None) -> str:
    if limit is None:
        return "вся история"
    return f"последние {_format_int_grouped(limit)} сообщений"


def _format_int_grouped(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _format_bytes(byte_count: int) -> str:
    if byte_count < 1024:
        return f"{byte_count} B"
    kib = byte_count / 1024
    if kib < 1024:
        return f"{kib:.1f} KB"
    mib = kib / 1024
    return f"{mib:.1f} MB"


def _chat_export_caption(document: ChatExportDocument) -> str:
    lines = [
        document.title,
        f"Сообщений в файле: {_format_int_grouped(document.message_count)}",
        f"Сервисных событий пропущено: {_format_int_grouped(document.service_message_count)}",
        f"Размер: {_format_bytes(len(document.data))}",
    ]
    return "\n".join(lines)


def _chat_export_page_action(page: int, mode: str = "all") -> str:
    if _normalize_export_mode(mode) == "search":
        return f"export.chats.search.page:{page}"
    return f"export.chats.page:{page}"


def _export_callback_ack_text(action: str) -> str | None:
    if action.startswith("export.confirm:"):
        return None
    return None


def _chat_button_label(kind: str, title: str) -> str:
    prefix = {
        "private": "[ЛС]",
        "group": "[ГР]",
        "channel": "[КН]",
    }.get(kind, "[ЧАТ]")
    title = title.strip() or "Без названия"
    if len(title) > 42:
        title = title[:39].rstrip() + "..."
    return f"{prefix} {title}"


def _chat_kind_label(kind: str) -> str:
    return {
        "private": "Личный чат",
        "group": "Группа",
        "channel": "Канал",
        "chat": "Чат",
    }.get(kind, kind)


async def _send_chat_export_document(message, document: ChatExportDocument) -> None:
    from aiogram.types import BufferedInputFile

    await message.answer_document(
        BufferedInputFile(document.data, filename=document.filename),
        caption=_chat_export_caption(document),
    )


def _upload_document_action_kwargs(message) -> dict[str, Any]:
    return {
        "bot": message.bot,
        "chat_id": message.chat.id,
        "message_thread_id": getattr(message, "message_thread_id", None),
        "interval": 4.0,
        "initial_sleep": 0.0,
    }


def _upload_document_action_sender(message):
    from aiogram.utils.chat_action import ChatActionSender

    return ChatActionSender.upload_document(**_upload_document_action_kwargs(message))


class PanelNavigation:
    def __init__(self):
        self._current: dict[int, str] = {}
        self._history: dict[int, list[str]] = {}

    def reset(self, *, user_id: int) -> None:
        self._current[user_id] = "main"
        self._history[user_id] = []

    def visit(self, *, user_id: int, action: str) -> None:
        current = self._current.get(user_id, "main")
        if action != current:
            self._history.setdefault(user_id, []).append(current)
        self._current[user_id] = action

    def back(self, *, user_id: int) -> str:
        history = self._history.setdefault(user_id, [])
        action = history.pop() if history else "main"
        self._current[user_id] = action
        return action


async def run_manager_bot(
    settings: Settings,
    *,
    chat_exporter: ChatExportPort | None = None,
    reaction_history_backfill: ReactionHistoryBackfillPort | None = None,
) -> None:  # pragma: no cover - integration only
    from aiogram import Bot, Dispatcher, F, types

    bot = Bot(token=settings.manager_bot_token)
    dispatcher = Dispatcher()
    state = SQLiteAssistantRepository(settings.database_path)
    panel = ControlPanelService(
        owner_id=settings.owner_id,
        state=state,
        llm_provider=settings.llm_provider,
        llm_model=_active_llm_model(settings),
        chat_export_enabled=chat_exporter is not None,
    )
    _bind_reaction_history_completion_notifier(
        bot=bot,
        owner_id=settings.owner_id,
        state=state,
        reaction_history_backfill=reaction_history_backfill,
    )
    pending_input: dict[int, str] = {}
    export_search_query: dict[int, str] = {}
    export_limit_context: dict[int, tuple[int, int, str]] = {}
    navigation = PanelNavigation()

    def remember_input(user_id: int, view: PanelView) -> None:
        if view.input_state:
            pending_input[user_id] = view.input_state
        else:
            pending_input.pop(user_id, None)

    def render_action(user_id: int, action: str) -> PanelView:
        if action == "main":
            return panel.main(user_id=user_id)
        return panel.handle_action(user_id=user_id, action=action)

    async def render_export_action(user_id: int, action: str) -> PanelView:
        if user_id != settings.owner_id:
            return PanelView(text="Доступ запрещен.", keyboard=[])
        if chat_exporter is None:
            return PanelView(
                text="Экспорт чатов недоступен: manager bot запущен без Telegram user client.",
                keyboard=[[PanelButton("Назад", "back")]],
                action="main",
            )
        if action == "export.chats":
            page = await chat_exporter.list_chats(page=0, page_size=CHAT_EXPORT_PAGE_SIZE)
            return _chat_export_menu_view(page)
        if action == "export.chats.refresh":
            page = await chat_exporter.list_chats(page=0, page_size=CHAT_EXPORT_PAGE_SIZE, refresh=True)
            return _chat_export_menu_view(page)
        if action == "export.chats.search":
            return _chat_export_search_prompt_view()
        if action == "export.chats.search.clear":
            export_search_query.pop(user_id, None)
            page = await chat_exporter.list_chats(page=0, page_size=CHAT_EXPORT_PAGE_SIZE)
            return _chat_export_menu_view(page)
        if action == "export.chats.search.refresh":
            query = export_search_query.get(user_id, "").strip()
            if not query:
                return _chat_export_search_prompt_view()
            page = await chat_exporter.list_chats(
                page=0,
                page_size=CHAT_EXPORT_PAGE_SIZE,
                refresh=True,
                query=query,
            )
            return _chat_export_menu_view(page, search_query=query)
        if action.startswith("export.chats.search.page:"):
            query = export_search_query.get(user_id, "").strip()
            if not query:
                return _chat_export_search_prompt_view()
            page = await chat_exporter.list_chats(
                page=_parse_export_page_action(action),
                page_size=CHAT_EXPORT_PAGE_SIZE,
                query=query,
            )
            return _chat_export_menu_view(page, search_query=query)
        if action.startswith("export.chats.page:"):
            page = await chat_exporter.list_chats(
                page=_parse_export_page_action(action),
                page_size=CHAT_EXPORT_PAGE_SIZE,
            )
            return _chat_export_menu_view(page)
        if action == "export.chats.input":
            return _chat_export_prompt_view()
        if action.startswith("export.limit:"):
            parsed_limit_context = _parse_export_limit_action(action)
            if parsed_limit_context is None:
                return _chat_export_error_view("Некорректный лимит экспорта.")
            export_limit_context[user_id] = parsed_limit_context
            chat_id, page, mode = parsed_limit_context
            return _chat_export_limit_prompt_view(chat_id=chat_id, page=page, mode=mode)
        if action.startswith("export.full:"):
            parsed = _parse_full_history_action(action)
            if parsed is None:
                return _chat_export_error_view("Некорректный chat_id.")
            chat_id, page, mode = parsed
            try:
                chat = await chat_exporter.get_chat(chat_id)
            except Exception as error:
                logger.exception("chat_export_get_chat_failed chat_id=%s", chat_id)
                return _chat_export_error_view(_chat_export_exception_message(error))
            return _chat_export_full_history_warning_view(chat, page=page, mode=mode)
        if action.startswith("export.chat:"):
            parsed = _parse_export_chat_action(action)
            if parsed is None:
                return _chat_export_error_view("Некорректный chat_id.")
            chat_id, page, mode = parsed
            try:
                chat = await chat_exporter.get_chat(chat_id)
            except Exception as error:
                logger.exception("chat_export_get_chat_failed chat_id=%s", chat_id)
                return _chat_export_error_view(_chat_export_exception_message(error))
            return _chat_export_detail_view(chat, page=page, mode=mode)
        return _chat_export_error_view("Неизвестное действие экспорта.")

    async def export_chat_to_message(message, *, chat_id: int, page: int, mode: str, limit: int | None) -> PanelView:
        if chat_exporter is None:
            return _chat_export_error_view("Экспорт не подключен.")
        try:
            async with _upload_document_action_sender(message):
                document = await chat_exporter.export_chat_text(chat_id, limit=limit)
                await _send_chat_export_document(message, document)
        except Exception as error:
            logger.exception("chat_export_failed chat_id=%s", chat_id)
            return _chat_export_error_view(_chat_export_exception_message(error))
        return _chat_export_done_view(
            chat_id=chat_id,
            page=page,
            mode=mode,
            filename=document.filename,
            message_count=document.message_count,
            service_message_count=document.service_message_count,
            byte_count=len(document.data),
        )

    @dispatcher.callback_query(F.data.startswith("panel:"))
    async def handle_panel_callback(callback: types.CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else 0
        requested_action = (callback.data or "").removeprefix("panel:")
        if requested_action.startswith("rhb:"):
            await _answer_callback_query(callback, "Запускаю историю...")
            view = await _render_reaction_history_backfill_action(
                user_id=user_id,
                action=requested_action,
                panel=panel,
                reaction_history_backfill=reaction_history_backfill,
            )
            navigation.visit(user_id=user_id, action=view.action)
            remember_input(user_id, view)
            if callback.message:
                await _edit_panel_message(callback.message, view)
            return
        if requested_action == "rfr":
            await _answer_callback_query(callback, "Обновляю папки...")
            view = await _render_reaction_folder_refresh_action(
                user_id=user_id,
                panel=panel,
                state=state,
                chat_exporter=chat_exporter,
            )
            navigation.visit(user_id=user_id, action=view.action)
            remember_input(user_id, view)
            if callback.message:
                await _edit_panel_message(callback.message, view)
            return
        if requested_action.startswith("rcr:"):
            await _answer_callback_query(callback, "Проверяю реакции...")
            view = await _render_reaction_available_refresh_action(
                user_id=user_id,
                action=requested_action,
                panel=panel,
                state=state,
                chat_exporter=chat_exporter,
            )
            navigation.visit(user_id=user_id, action=view.action)
            remember_input(user_id, view)
            if callback.message:
                await _edit_panel_message(callback.message, view)
            return
        if requested_action.startswith("reactions.channel:"):
            view = await _render_reaction_channel_detail_action(
                user_id=user_id,
                action=requested_action,
                panel=panel,
                state=state,
                chat_exporter=chat_exporter,
            )
            navigation.visit(user_id=user_id, action=view.action)
            remember_input(user_id, view)
            if callback.message:
                await _edit_panel_message(callback.message, view)
            await _answer_callback_query(callback)
            return
        if requested_action.startswith("export."):
            callback_answered = False
            if requested_action.startswith("export.confirm:"):
                parsed = _parse_confirm_export_action(requested_action)
                if parsed is None:
                    view = _chat_export_error_view("Некорректный chat_id.")
                elif callback.message is None:
                    view = _chat_export_error_view("Нет сообщения для ответа файлом.")
                else:
                    chat_id, page, mode, limit = parsed
                    await _answer_callback_query(callback, "Готовлю .txt...")
                    callback_answered = True
                    await callback.message.edit_text(
                        f"Готовлю .txt: {_export_limit_label(limit)}..."
                    )
                    view = await export_chat_to_message(
                        callback.message,
                        chat_id=chat_id,
                        page=page,
                        mode=mode,
                        limit=limit,
                    )
                    remember_input(user_id, view)
                    await _edit_panel_message(callback.message, view)
                    return
            else:
                ack_text = _export_callback_ack_text(requested_action)
                await _answer_callback_query(callback, ack_text)
                callback_answered = True
                view = await render_export_action(user_id, requested_action)
            navigation.visit(user_id=user_id, action=view.action)
            remember_input(user_id, view)
            if callback.message:
                await _edit_panel_message(callback.message, view)
            if not callback_answered:
                await _answer_callback_query(callback)
            return
        if requested_action == "back":
            target_action = navigation.back(user_id=user_id)
            if target_action.startswith("export."):
                view = await render_export_action(user_id, target_action)
            else:
                view = render_action(user_id, target_action)
        else:
            view = panel.handle_action(user_id=user_id, action=requested_action)
            navigation.visit(user_id=user_id, action=view.action)
        remember_input(user_id, view)
        if callback.message:
            await _edit_panel_message(callback.message, view)
        ack_text = _panel_callback_ack_text(requested_action, view)
        if ack_text is None:
            await _answer_callback_query(callback)
        else:
            await _answer_callback_query(callback, ack_text)

    @dispatcher.message()
    async def handle_message(message: types.Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        text = message.text or ""
        if text.startswith("/start") or text.startswith("/menu"):
            view = panel.main(user_id=user_id)
            navigation.reset(user_id=user_id)
        else:
            premium_emojis = extract_premium_emoji_ids(message)
            if user_id == settings.owner_id and premium_emojis:
                await message.answer(format_premium_emoji_reply(premium_emojis))
                return
            if pending_input.get(user_id) == "export_chat_id":
                try:
                    chat_id = int(text.strip())
                except ValueError:
                    view = _chat_export_prompt_view()
                    view = PanelView(
                        text="chat_id должен быть числом.\n\n" + view.text,
                        keyboard=view.keyboard,
                        input_state=view.input_state,
                        action=view.action,
                    )
                else:
                    if chat_exporter is None:
                        view = _chat_export_error_view("Экспорт не подключен.")
                    else:
                        try:
                            chat = await chat_exporter.get_chat(chat_id)
                        except Exception as error:
                            logger.exception("chat_export_get_chat_failed chat_id=%s", chat_id)
                            view = _chat_export_error_view(_chat_export_exception_message(error))
                        else:
                            view = _chat_export_detail_view(chat, page=0)
            elif pending_input.get(user_id) == "export_chat_search":
                query = text.strip()
                if not query:
                    view = _chat_export_search_prompt_view()
                    view = PanelView(
                        text="Поиск не должен быть пустым.\n\n" + view.text,
                        keyboard=view.keyboard,
                        input_state=view.input_state,
                        action=view.action,
                    )
                elif chat_exporter is None:
                    view = _chat_export_error_view("Экспорт не подключен.")
                else:
                    export_search_query[user_id] = query
                    page = await chat_exporter.list_chats(
                        page=0,
                        page_size=CHAT_EXPORT_PAGE_SIZE,
                        query=query,
                    )
                    view = _chat_export_menu_view(page, search_query=query)
            elif pending_input.get(user_id) == "export_chat_limit":
                ok, limit = _parse_export_limit_text(text)
                context = export_limit_context.get(user_id)
                if not ok or context is None:
                    if context is None:
                        view = _chat_export_error_view("Не нашел контекст экспорта. Открой карточку чата заново.")
                    else:
                        chat_id, page, mode = context
                        view = _chat_export_limit_prompt_view(chat_id=chat_id, page=page, mode=mode)
                        view = PanelView(
                            text="Лимит должен быть положительным числом или словом «все».\n\n" + view.text,
                            keyboard=view.keyboard,
                            input_state=view.input_state,
                            action=view.action,
                        )
                else:
                    chat_id, page, mode = context
                    export_limit_context.pop(user_id, None)
                    if chat_exporter is None:
                        view = _chat_export_error_view("Экспорт не подключен.")
                    elif _should_warn_export_limit(limit):
                        try:
                            chat = await chat_exporter.get_chat(chat_id)
                        except Exception as error:
                            logger.exception("chat_export_get_chat_failed chat_id=%s", chat_id)
                            view = _chat_export_error_view(_chat_export_exception_message(error))
                        else:
                            view = _chat_export_required_limit_warning_view(chat, page=page, mode=mode, limit=limit)
                    else:
                        await message.answer(f"Готовлю .txt: {_export_limit_label(limit)}...")
                        view = await export_chat_to_message(
                            message,
                            chat_id=chat_id,
                            page=page,
                            mode=mode,
                            limit=limit,
                        )
            else:
                view = panel.handle_text(user_id=user_id, state=pending_input.get(user_id), text=text)
            navigation.visit(user_id=user_id, action=view.action)
        remember_input(user_id, view)
        await _answer_panel_message(message, view)

    await dispatcher.start_polling(bot)


def main() -> None:  # pragma: no cover - integration only
    asyncio.run(run_manager_bot(load_settings()))


if __name__ == "__main__":
    main()
