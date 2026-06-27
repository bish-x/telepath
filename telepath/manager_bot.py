from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any, Protocol

from telepath.config import Settings, load_settings
from telepath.chat_export import (
    ChatExportDocument,
    ChatMediaArchivePart,
    ExportChat,
    ExportChatPage,
    chat_media_archive_caption,
)
from telepath.panel import PanelButton, PanelView, ControlPanelService
from telepath.premium_emoji import extract_premium_emoji_ids, format_premium_emoji_reply
from telepath.storage import SQLiteAssistantRepository


logger = logging.getLogger(__name__)

POST_MIRROR_HISTORY_PACE_TEXT = (
    "История копируется безопасно: один пост раз в 60-120 сек; "
    "после нового топика пауза 3-6 мин; realtime-посты без искусственной задержки."
)
CHAT_EXPORT_PAGE_SIZE = 8
CHAT_EXPORT_SAFE_LIMIT = 5000
CHAT_ARCHIVE_STANDARD_PART_BYTES = 1536 * 1024 * 1024
CHAT_ARCHIVE_PREMIUM_PART_BYTES = 3584 * 1024 * 1024


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
    def export_chat_media_archives(
        self,
        chat_id: int,
        *,
        limit: int | None = None,
        max_archive_bytes: int,
    ) -> Any: ...
    async def send_chat_archive_part(self, part: ChatMediaArchivePart, *, target_peer: Any) -> None: ...


class ChatMediaExportSendSummary:
    def __init__(
        self,
        *,
        part_count: int = 0,
        message_count: int = 0,
        service_message_count: int = 0,
        media_count: int = 0,
        byte_count: int = 0,
    ):
        self.part_count = part_count
        self.message_count = message_count
        self.service_message_count = service_message_count
        self.media_count = media_count
        self.byte_count = byte_count


class ReactionHistoryBackfillPort(Protocol):
    async def enqueue_history(
        self,
        *,
        limit_per_channel: int | None,
        chat_id: int | None = None,
    ) -> Any: ...
    def set_completion_notifier(self, notifier: Any) -> None: ...


class PostMirrorHistoryBackfillPort(Protocol):
    async def enqueue_history(
        self,
        *,
        limit_per_source: int | None,
        chat_id: int | None = None,
        folder_id: int | None = None,
    ) -> Any: ...


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
        "post_mirror.toggle",
        "post_mirror.source.toggle:",
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


def _parse_post_mirror_history_backfill_action(action: str) -> tuple[int, int | None, int] | None:
    parts = action.split(":")
    if len(parts) != 5 or parts[0] != "pmh" or parts[1] != "ch":
        return None
    try:
        chat_id = int(parts[2])
        if parts[3] == "all":
            limit = None
        else:
            limit = int(parts[3])
            if limit <= 0:
                return None
        page = int(parts[4])
        if page < 0:
            return None
    except ValueError:
        return None
    return chat_id, limit, page


def _parse_post_mirror_folder_history_backfill_action(action: str) -> tuple[int, int | None] | None:
    parts = action.split(":")
    if len(parts) != 4 or parts[0] != "pmh" or parts[1] != "folder":
        return None
    try:
        folder_id = int(parts[2])
        if folder_id <= 0:
            return None
        if parts[3] == "all":
            limit = None
        else:
            limit = int(parts[3])
            if limit <= 0:
                return None
    except ValueError:
        return None
    return folder_id, limit


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


def _post_mirror_history_backfill_feedback(result: Any) -> str:
    if bool(getattr(result, "duplicate_queued", False)):
        position = getattr(result, "queue_position", None)
        suffix = f" Позиция: {position}." if position else ""
        return f"Такой запуск истории уже есть в очереди.{suffix}\n{POST_MIRROR_HISTORY_PACE_TEXT}"

    if bool(getattr(result, "request_queued", False)):
        position = getattr(result, "queue_position", None)
        lines = ["История добавлена в очередь."]
        if position:
            lines.append(f"Позиция: {position}.")
        lines.append(POST_MIRROR_HISTORY_PACE_TEXT)
        return "\n".join(lines)

    source_count = int(getattr(result, "source_count", 0))
    if source_count <= 0:
        return "Нет настроенных каналов для копирования истории."

    lines = [
        f"Постов скопировано: {int(getattr(result, 'mirrored_count', 0))}",
        f"Просканировано: {int(getattr(result, 'scanned_count', 0))}",
        f"Пропущено: {int(getattr(result, 'skipped_count', 0))}",
        f"Источников: {source_count}",
    ]
    failed_count = int(getattr(result, "failed_count", 0))
    if failed_count:
        lines.append(f"Ошибок: {failed_count}")
    lines.append("История обработана последовательно: один пост раз в 60-120 сек.")
    return "\n".join(lines)


def _post_mirror_source_title(state: SQLiteAssistantRepository, source_chat_id: int) -> tuple[str, str]:
    for chat in state.list_known_chats():
        if int(chat["chat_id"]) == source_chat_id:
            return str(chat.get("title") or source_chat_id), str(chat.get("kind") or "channel")
    for source in state.list_post_mirror_sources():
        if int(source["source_chat_id"]) == source_chat_id:
            return str(source.get("title") or source_chat_id), str(source.get("kind") or "channel")
    return str(source_chat_id), "channel"


async def _render_post_mirror_topic_create_action(
    *,
    user_id: int,
    action: str,
    panel: ControlPanelService,
    state: SQLiteAssistantRepository,
    chat_exporter: ChatExportPort | None,
    topic_manager_factory: Any | None = None,
) -> PanelView:
    if user_id != panel.owner_id:
        return PanelView(text="Доступ запрещен.", keyboard=[])
    parts = action.split(":")
    if len(parts) != 3 or parts[0] != "pm.topic":
        return panel.handle_action(user_id=user_id, action="post_mirror.sources")
    try:
        source_chat_id = int(parts[1])
        page = int(parts[2])
    except ValueError:
        return panel.handle_action(user_id=user_id, action="post_mirror.sources")

    detail_action = f"post_mirror.source:{source_chat_id}:{page}"
    view = panel.handle_action(user_id=user_id, action=detail_action)
    target_chat_id = state.get_post_mirror_target_chat_id()
    if target_chat_id is None:
        return _append_panel_feedback(view, "Сначала задай группу с топиками.")
    if int(source_chat_id) == int(target_chat_id):
        return _append_panel_feedback(view, "Нельзя копировать группу саму в себя.")
    client = getattr(chat_exporter, "client", None)
    if client is None:
        return _append_panel_feedback(view, "Не могу создать топик: Telegram user client недоступен.")

    title, kind = _post_mirror_source_title(state, source_chat_id)
    try:
        if topic_manager_factory is None:
            from telepath.user_client import TelethonForumTopicManager

            topic_manager_factory = TelethonForumTopicManager
        topic_id = await topic_manager_factory(client).create_topic(target_chat_id, title)
    except Exception as error:
        logger.exception("post_mirror_topic_create_failed source_chat_id=%s", source_chat_id)
        refreshed = panel.handle_action(user_id=user_id, action=detail_action)
        return _append_panel_feedback(refreshed, f"Не смог создать топик: {error}")

    state.upsert_post_mirror_source(source_chat_id, title, kind)
    state.set_post_mirror_source_topic(source_chat_id, topic_id)
    state.set_post_mirror_source_enabled(source_chat_id, True)
    refreshed = panel.handle_action(user_id=user_id, action=detail_action)
    return _append_panel_feedback(refreshed, f"Топик создан: {topic_id}. Источник включен.")


async def _render_post_mirror_source_refresh_action(
    *,
    user_id: int,
    panel: ControlPanelService,
    state: SQLiteAssistantRepository,
    chat_exporter: ChatExportPort | None,
    topic_manager_factory: Any | None = None,
) -> PanelView:
    view = panel.handle_action(user_id=user_id, action="post_mirror.sources")
    if user_id != panel.owner_id:
        return view
    client = getattr(chat_exporter, "client", None)
    if client is None:
        return _append_panel_feedback(view, "Не могу обновить каталог: Telegram user client недоступен.")
    try:
        from telepath.user_client import TelethonForumTopicManager, sync_chat_catalog

        if topic_manager_factory is None:
            topic_manager_factory = TelethonForumTopicManager
        await sync_chat_catalog(client, state, post_mirror_topic_manager=topic_manager_factory(client))
    except Exception as error:
        logger.exception("post_mirror_source_refresh_failed")
        refreshed = panel.handle_action(user_id=user_id, action="post_mirror.sources")
        return _append_panel_feedback(refreshed, f"Не смог обновить каталог: {error}")
    refreshed = panel.handle_action(user_id=user_id, action="post_mirror.sources")
    return _append_panel_feedback(refreshed, "Каталог обновлен.")


async def _render_post_mirror_folder_refresh_action(
    *,
    user_id: int,
    panel: ControlPanelService,
    state: SQLiteAssistantRepository,
    chat_exporter: ChatExportPort | None,
) -> PanelView:
    view = panel.handle_action(user_id=user_id, action="post_mirror.folders")
    if user_id != panel.owner_id:
        return view
    client = getattr(chat_exporter, "client", None)
    if client is None:
        return _append_panel_feedback(view, "Не могу обновить папки: Telegram user client недоступен.")
    try:
        from telepath.user_client import sync_reaction_folders

        await sync_reaction_folders(client, state)
    except Exception as error:
        logger.exception("post_mirror_folder_refresh_failed")
        refreshed = panel.handle_action(user_id=user_id, action="post_mirror.folders")
        return _append_panel_feedback(refreshed, f"Не смог обновить папки: {error}")
    refreshed = panel.handle_action(user_id=user_id, action="post_mirror.folders")
    return _append_panel_feedback(refreshed, "Папки обновлены.")


async def _render_post_mirror_folder_toggle_action(
    *,
    user_id: int,
    action: str,
    panel: ControlPanelService,
    state: SQLiteAssistantRepository,
) -> PanelView:
    if user_id != panel.owner_id:
        return PanelView(text="Доступ запрещен.", keyboard=[])
    parts = action.split(":")
    if len(parts) != 2 or parts[0] != "pmf.toggle":
        return panel.handle_action(user_id=user_id, action="post_mirror.folders")
    try:
        folder_id = int(parts[1])
    except ValueError:
        return panel.handle_action(user_id=user_id, action="post_mirror.folders")

    folder = next(
        (item for item in state.list_post_mirror_folders() if int(item["folder_id"]) == folder_id),
        None,
    )
    if folder is None:
        return _append_panel_feedback(
            panel.handle_action(user_id=user_id, action="post_mirror.folders"),
            "Папка не найдена. Обнови список папок.",
        )
    detail_action = f"post_mirror.folder:{folder_id}"
    if folder["enabled"]:
        state.set_post_mirror_folder_enabled(folder_id, False)
        return _append_panel_feedback(
            panel.handle_action(user_id=user_id, action=detail_action),
            "Папка выключена.",
        )
    if state.get_post_mirror_target_chat_id() is None:
        return _append_panel_feedback(
            panel.handle_action(user_id=user_id, action=detail_action),
            "Сначала задай группу с топиками.",
        )
    if not state.list_post_mirror_folder_sources(folder_id):
        return _append_panel_feedback(
            panel.handle_action(user_id=user_id, action=detail_action),
            "В папке нет каналов или групп для копирования.",
        )
    state.set_post_mirror_folder_enabled(folder_id, True)
    return _append_panel_feedback(
        panel.handle_action(user_id=user_id, action=detail_action),
        "Папка включена. Топики будут создаваться при первых постах.",
    )


async def _render_post_mirror_source_add_text(
    *,
    user_id: int,
    text: str,
    panel: ControlPanelService,
    state: SQLiteAssistantRepository,
    chat_exporter: ChatExportPort | None,
    topic_manager_factory: Any | None = None,
) -> PanelView:
    if user_id != panel.owner_id:
        return PanelView(text="Доступ запрещен.", keyboard=[])
    try:
        source_chat_id = int(text.strip())
    except ValueError:
        return PanelView(
            text="channel_id должен быть числом.\n\nПример: -1001234567890",
            keyboard=[[PanelButton("Повторить", "post_mirror.source.add")], [PanelButton("Назад", "back")]],
            input_state="post_mirror_source_add",
            action="post_mirror.source.add",
        )
    target_chat_id = state.get_post_mirror_target_chat_id()
    if target_chat_id is None:
        return _append_panel_feedback(
            panel.handle_action(user_id=user_id, action="post_mirror"),
            "Сначала задай группу с топиками.",
        )
    if int(source_chat_id) == int(target_chat_id):
        return PanelView(
            text="Нельзя копировать группу саму в себя.\n\nПришли другой channel_id.",
            keyboard=[[PanelButton("Повторить", "post_mirror.source.add")], [PanelButton("К списку", "post_mirror.sources")]],
            input_state="post_mirror_source_add",
            action="post_mirror.source.add",
        )
    client = getattr(chat_exporter, "client", None)
    if chat_exporter is None or client is None:
        return _append_panel_feedback(
            panel.handle_action(user_id=user_id, action="post_mirror.sources"),
            "Не могу проверить канал: Telegram user client недоступен.",
        )
    try:
        chat = await chat_exporter.get_chat(source_chat_id)
    except Exception as error:
        logger.exception("post_mirror_source_add_get_chat_failed source_chat_id=%s", source_chat_id)
        return PanelView(
            text=f"Не смог найти канал: {error}\n\nПришли другой channel_id.",
            keyboard=[[PanelButton("Повторить", "post_mirror.source.add")], [PanelButton("К списку", "post_mirror.sources")]],
            input_state="post_mirror_source_add",
            action="post_mirror.source.add",
        )
    if chat.kind not in {"channel", "group"}:
        return PanelView(
            text="Этот чат нельзя включить для копирования постов.\n\nНужен Telegram-канал или группа.",
            keyboard=[[PanelButton("Повторить", "post_mirror.source.add")], [PanelButton("К списку", "post_mirror.sources")]],
            input_state="post_mirror_source_add",
            action="post_mirror.source.add",
        )

    state.upsert_known_chat(chat.chat_id, chat.title, chat.kind)
    state.upsert_post_mirror_source(chat.chat_id, chat.title, chat.kind)
    try:
        if topic_manager_factory is None:
            from telepath.user_client import TelethonForumTopicManager

            topic_manager_factory = TelethonForumTopicManager
        topic_id = await topic_manager_factory(client).create_topic(target_chat_id, chat.title)
    except Exception as error:
        logger.exception("post_mirror_source_add_topic_failed source_chat_id=%s", source_chat_id)
        view = panel.handle_action(user_id=user_id, action=f"post_mirror.source:{chat.chat_id}:0")
        return _append_panel_feedback(view, f"Канал добавлен, но топик не создан: {error}")

    state.set_post_mirror_source_topic(chat.chat_id, topic_id)
    state.set_post_mirror_source_enabled(chat.chat_id, True)
    return _append_panel_feedback(
        panel.handle_action(user_id=user_id, action=f"post_mirror.source:{chat.chat_id}:0"),
        f"Канал добавлен. Топик создан: {topic_id}.",
    )


async def _render_post_mirror_history_backfill_action(
    *,
    user_id: int,
    action: str,
    panel: ControlPanelService,
    post_mirror_history_backfill: PostMirrorHistoryBackfillPort | None,
) -> PanelView:
    if user_id != panel.owner_id:
        return PanelView(text="Доступ запрещен.", keyboard=[])
    parsed = _parse_post_mirror_history_backfill_action(action)
    if parsed is None:
        return panel.handle_action(user_id=user_id, action="post_mirror.sources")
    source_chat_id, limit, page = parsed
    detail_action = f"post_mirror.source:{source_chat_id}:{page}"
    view = panel.handle_action(user_id=user_id, action=detail_action)
    if post_mirror_history_backfill is None:
        return _append_panel_feedback(view, "Telegram user client недоступен: история не запущена.")
    try:
        result = await post_mirror_history_backfill.enqueue_history(
            limit_per_source=limit,
            chat_id=source_chat_id,
        )
    except Exception as error:
        logger.exception("post_mirror_history_enqueue_failed source_chat_id=%s", source_chat_id)
        refreshed = panel.handle_action(user_id=user_id, action=detail_action)
        return _append_panel_feedback(refreshed, f"Не смог запустить историю: {error}")
    refreshed = panel.handle_action(user_id=user_id, action=detail_action)
    return _append_panel_feedback(refreshed, _post_mirror_history_backfill_feedback(result))


async def _render_post_mirror_folder_history_backfill_action(
    *,
    user_id: int,
    action: str,
    panel: ControlPanelService,
    post_mirror_history_backfill: PostMirrorHistoryBackfillPort | None,
) -> PanelView:
    if user_id != panel.owner_id:
        return PanelView(text="Доступ запрещен.", keyboard=[])
    parsed = _parse_post_mirror_folder_history_backfill_action(action)
    if parsed is None:
        return panel.handle_action(user_id=user_id, action="post_mirror.folders")
    folder_id, limit = parsed
    detail_action = f"post_mirror.folder:{folder_id}"
    view = panel.handle_action(user_id=user_id, action=detail_action)
    folder = next(
        (item for item in panel.state.list_post_mirror_folders() if int(item["folder_id"]) == folder_id),
        None,
    )
    if folder is None:
        return _append_panel_feedback(
            panel.handle_action(user_id=user_id, action="post_mirror.folders"),
            "Папка не найдена. Обнови список папок.",
        )
    if not folder["enabled"]:
        return _append_panel_feedback(view, "Сначала включи папку.")
    if post_mirror_history_backfill is None:
        return _append_panel_feedback(view, "Telegram user client недоступен: история не запущена.")
    try:
        result = await post_mirror_history_backfill.enqueue_history(
            limit_per_source=limit,
            folder_id=folder_id,
        )
    except Exception as error:
        logger.exception("post_mirror_folder_history_enqueue_failed folder_id=%s", folder_id)
        refreshed = panel.handle_action(user_id=user_id, action=detail_action)
        return _append_panel_feedback(refreshed, f"Не смог запустить историю: {error}")
    refreshed = panel.handle_action(user_id=user_id, action=detail_action)
    return _append_panel_feedback(refreshed, _post_mirror_history_backfill_feedback(result))


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
            [PanelButton(".zip с медиа · 1 000", _media_export_action(chat.chat_id, page, mode, limit=1000))],
            [PanelButton(".zip с медиа · 5 000", _media_export_action(chat.chat_id, page, mode, limit=5000))],
            [PanelButton("Вся история", _full_history_action(chat.chat_id, page, mode))],
            [PanelButton("Вся история с медиа", _media_export_action(chat.chat_id, page, mode, limit=None))],
            [PanelButton("Другой лимит", _export_limit_action(chat.chat_id, page, mode))],
            [PanelButton("К списку чатов", _chat_export_page_action(page, mode))],
            [PanelButton("Назад", "back")],
        ],
        action=_export_chat_action(chat.chat_id, page, mode),
    )


def _chat_media_export_warning_view(
    chat: ExportChat,
    *,
    page: int,
    mode: str = "all",
    limit: int | None,
    is_premium: bool,
) -> PanelView:
    mode = _normalize_export_mode(mode)
    limit_label = _export_limit_label(limit)
    part_limit = _format_bytes(_chat_media_archive_limit_bytes(is_premium))
    return PanelView(
        text="\n".join(
            [
                "Архив с медиа",
                "",
                f"Название: {chat.title}",
                f"chat_id: {chat.chat_id}",
                f"Объем: {limit_label}",
                f"Размер части: до {part_limit}",
                "",
                "Файлы отправляет Telegram user account в личный чат с manager-ботом. "
                "После отправки zip-части и исходные файлы удаляются с сервера.",
            ]
        ),
        keyboard=[
            [
                PanelButton(
                    "Создать .zip с медиа",
                    _confirm_media_export_action(chat.chat_id, page, mode, limit=limit),
                )
            ],
            [PanelButton("Лучше .txt без медиа", _confirm_export_action(chat.chat_id, page, mode, limit=limit))],
            [PanelButton("К карточке чата", _export_chat_action(chat.chat_id, page, mode))],
            [PanelButton("К списку чатов", _chat_export_page_action(page, mode))],
        ],
        action=_media_export_action(chat.chat_id, page, mode, limit=limit),
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


def _chat_media_export_done_view(
    *,
    chat_id: int,
    page: int,
    summary: ChatMediaExportSendSummary,
    mode: str = "all",
) -> PanelView:
    return PanelView(
        text="\n".join(
            [
                "Отправил архивы с медиа.",
                f"Частей: {_format_int_grouped(summary.part_count)}",
                f"Сообщений: {_format_int_grouped(summary.message_count)}",
                f"Сервисных событий пропущено: {_format_int_grouped(summary.service_message_count)}",
                f"Медиафайлов: {_format_int_grouped(summary.media_count)}",
                f"Общий размер: {_format_bytes(summary.byte_count)}",
                "Файлы на сервере удалены.",
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


def _parse_media_export_action(action: str) -> tuple[int, int, str, int | None] | None:
    parts = action.split(":")
    if len(parts) != 5 or parts[0] != "export.media":
        return None
    try:
        parsed_limit = _parse_export_limit_token(parts[4])
        if not parsed_limit[0]:
            return None
        return int(parts[1]), int(parts[2]), _normalize_export_mode(parts[3]), parsed_limit[1]
    except ValueError:
        return None


def _parse_confirm_media_export_action(action: str) -> tuple[int, int, str, int | None] | None:
    parts = action.split(":")
    if len(parts) != 5 or parts[0] != "export.media.confirm":
        return None
    try:
        parsed_limit = _parse_export_limit_token(parts[4])
        if not parsed_limit[0]:
            return None
        return int(parts[1]), int(parts[2]), _normalize_export_mode(parts[3]), parsed_limit[1]
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


def _media_export_action(chat_id: int, page: int, mode: str = "all", *, limit: int | None = None) -> str:
    mode = _normalize_export_mode(mode)
    limit_part = str(limit) if limit is not None else "all"
    return f"export.media:{chat_id}:{page}:{mode}:{limit_part}"


def _confirm_media_export_action(chat_id: int, page: int, mode: str = "all", *, limit: int | None = None) -> str:
    mode = _normalize_export_mode(mode)
    limit_part = str(limit) if limit is not None else "all"
    return f"export.media.confirm:{chat_id}:{page}:{mode}:{limit_part}"


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


def _chat_media_archive_limit_bytes(is_premium: bool) -> int:
    return CHAT_ARCHIVE_PREMIUM_PART_BYTES if is_premium else CHAT_ARCHIVE_STANDARD_PART_BYTES


def _format_int_grouped(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _format_bytes(byte_count: int) -> str:
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


def _chat_export_caption(document: ChatExportDocument) -> str:
    lines = [
        document.title,
        f"Сообщений в файле: {_format_int_grouped(document.message_count)}",
        f"Сервисных событий пропущено: {_format_int_grouped(document.service_message_count)}",
        f"Размер: {_format_bytes(len(document.data))}",
    ]
    return "\n".join(lines)


def _chat_media_archive_caption(part: ChatMediaArchivePart) -> str:
    return chat_media_archive_caption(part)


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


async def _chat_archive_upload_target(bot: Any) -> str:
    me = await bot.get_me()
    username = (getattr(me, "username", None) or "").strip()
    if not username:
        raise RuntimeError("Manager bot username is unavailable; cannot send archive from user account.")
    return f"@{username}"


async def _send_chat_media_archives_to_peer(
    chat_exporter: ChatExportPort,
    *,
    target_peer: Any,
    chat_id: int,
    limit: int | None,
    max_archive_bytes: int,
) -> ChatMediaExportSendSummary:
    queue: asyncio.Queue[ChatMediaArchivePart | object] = asyncio.Queue(maxsize=1)
    finished = object()
    producer_errors: list[BaseException] = []
    temporary_parents: set[Any] = set()

    async def produce() -> None:
        try:
            async for part in chat_exporter.export_chat_media_archives(
                chat_id,
                limit=limit,
                max_archive_bytes=max_archive_bytes,
            ):
                await queue.put(part)
        except BaseException as error:
            producer_errors.append(error)
        finally:
            await queue.put(finished)

    producer = asyncio.create_task(produce(), name="chat-media-export-producer")
    summary = ChatMediaExportSendSummary()
    try:
        while True:
            item = await queue.get()
            if item is finished:
                break
            part = item
            if not isinstance(part, ChatMediaArchivePart):
                continue
            if part.temporary_parent is not None:
                temporary_parents.add(part.temporary_parent)
            try:
                await chat_exporter.send_chat_archive_part(part, target_peer=target_peer)
                summary.part_count += 1
                summary.message_count += part.message_count
                summary.service_message_count += part.service_message_count
                summary.media_count += part.media_count
                summary.byte_count += part.byte_count
            finally:
                part.path.unlink(missing_ok=True)
        await producer
        if producer_errors:
            raise producer_errors[0]
        return summary
    except BaseException:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
        await _delete_queued_archive_parts(queue, temporary_parents=temporary_parents)
        raise
    finally:
        for parent in temporary_parents:
            shutil.rmtree(parent, ignore_errors=True)


async def _delete_queued_archive_parts(
    queue: asyncio.Queue[ChatMediaArchivePart | object],
    *,
    temporary_parents: set[Any],
) -> None:
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        if isinstance(item, ChatMediaArchivePart):
            if item.temporary_parent is not None:
                temporary_parents.add(item.temporary_parent)
            item.path.unlink(missing_ok=True)


def _is_user_account_archive_upload_message(message: Any) -> bool:
    caption = (getattr(message, "caption", None) or "").strip()
    document = getattr(message, "document", None)
    filename = (getattr(document, "file_name", None) or "").strip()
    return (
        caption.startswith("Telepath archive export")
        and filename.startswith("telegram-")
        and "-part" in filename
        and filename.endswith(".zip")
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
    post_mirror_history_backfill: PostMirrorHistoryBackfillPort | None = None,
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
        if action.startswith("export.media:"):
            parsed = _parse_media_export_action(action)
            if parsed is None:
                return _chat_export_error_view("Некорректный chat_id.")
            chat_id, page, mode, limit = parsed
            try:
                chat = await chat_exporter.get_chat(chat_id)
            except Exception as error:
                logger.exception("chat_export_get_chat_failed chat_id=%s", chat_id)
                return _chat_export_error_view(_chat_export_exception_message(error))
            return _chat_media_export_warning_view(
                chat,
                page=page,
                mode=mode,
                limit=limit,
                is_premium=state.is_account_premium(),
            )
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

    async def export_chat_media_to_user_account_peer(
        message,
        *,
        chat_id: int,
        page: int,
        mode: str,
        limit: int | None,
    ) -> PanelView:
        if chat_exporter is None:
            return _chat_export_error_view("Экспорт не подключен.")
        try:
            target_peer = await _chat_archive_upload_target(bot)
            summary = await _send_chat_media_archives_to_peer(
                chat_exporter,
                target_peer=target_peer,
                chat_id=chat_id,
                limit=limit,
                max_archive_bytes=_chat_media_archive_limit_bytes(state.is_account_premium()),
            )
        except Exception as error:
            logger.exception("chat_media_export_failed chat_id=%s", chat_id)
            return _chat_export_error_view(_chat_export_exception_message(error))
        return _chat_media_export_done_view(
            chat_id=chat_id,
            page=page,
            mode=mode,
            summary=summary,
        )

    @dispatcher.callback_query(F.data.startswith("panel:"))
    async def handle_panel_callback(callback: types.CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else 0
        requested_action = (callback.data or "").removeprefix("panel:")
        if requested_action.startswith("pm.topic:"):
            await _answer_callback_query(callback, "Создаю топик...")
            view = await _render_post_mirror_topic_create_action(
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
        if requested_action == "pm.refresh":
            await _answer_callback_query(callback, "Обновляю каталог...")
            view = await _render_post_mirror_source_refresh_action(
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
        if requested_action == "pmf.refresh":
            await _answer_callback_query(callback, "Обновляю папки...")
            view = await _render_post_mirror_folder_refresh_action(
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
        if requested_action.startswith("pmf.toggle:"):
            await _answer_callback_query(callback, "Настраиваю папку...")
            view = await _render_post_mirror_folder_toggle_action(
                user_id=user_id,
                action=requested_action,
                panel=panel,
                state=state,
            )
            navigation.visit(user_id=user_id, action=view.action)
            remember_input(user_id, view)
            if callback.message:
                await _edit_panel_message(callback.message, view)
            return
        if requested_action.startswith("pmh:folder:"):
            await _answer_callback_query(callback, "Запускаю историю папки...")
            view = await _render_post_mirror_folder_history_backfill_action(
                user_id=user_id,
                action=requested_action,
                panel=panel,
                post_mirror_history_backfill=post_mirror_history_backfill,
            )
            navigation.visit(user_id=user_id, action=view.action)
            remember_input(user_id, view)
            if callback.message:
                await _edit_panel_message(callback.message, view)
            return
        if requested_action.startswith("pmh:"):
            await _answer_callback_query(callback, "Запускаю историю...")
            view = await _render_post_mirror_history_backfill_action(
                user_id=user_id,
                action=requested_action,
                panel=panel,
                post_mirror_history_backfill=post_mirror_history_backfill,
            )
            navigation.visit(user_id=user_id, action=view.action)
            remember_input(user_id, view)
            if callback.message:
                await _edit_panel_message(callback.message, view)
            return
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
            if requested_action.startswith("export.media.confirm:"):
                parsed = _parse_confirm_media_export_action(requested_action)
                if parsed is None:
                    view = _chat_export_error_view("Некорректный chat_id.")
                elif callback.message is None:
                    view = _chat_export_error_view("Нет сообщения для ответа файлом.")
                else:
                    chat_id, page, mode, limit = parsed
                    await _answer_callback_query(callback, "Готовлю .zip...")
                    callback_answered = True
                    await callback.message.edit_text(
                        f"Готовлю .zip с медиа: {_export_limit_label(limit)}..."
                    )
                    view = await export_chat_media_to_user_account_peer(
                        callback.message,
                        chat_id=chat_id,
                        page=page,
                        mode=mode,
                        limit=limit,
                    )
                    remember_input(user_id, view)
                    await _edit_panel_message(callback.message, view)
                    return
            elif requested_action.startswith("export.confirm:"):
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
            if user_id == settings.owner_id and _is_user_account_archive_upload_message(message):
                await message.answer("Файл отправлен. Архив уже в этом чате, его можно скачать или переслать.")
                return
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
            elif pending_input.get(user_id) == "post_mirror_source_add":
                view = await _render_post_mirror_source_add_text(
                    user_id=user_id,
                    text=text,
                    panel=panel,
                    state=state,
                    chat_exporter=chat_exporter,
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
