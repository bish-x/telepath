from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from telepath.features.channel_reactions import (
    DEFAULT_REACTION_EMOJIS,
    VALID_REACTION_MODES,
    VALID_REACTION_SELECTION_STRATEGIES,
    VALID_REACTION_SOURCES,
    reaction_category,
)
from telepath.manager import ManagerService


VOICE_FEATURE = "voice_transcription"


@dataclass(frozen=True)
class PanelButton:
    text: str
    action: str
    style: str | None = None
    icon_custom_emoji_id: str | None = None


@dataclass(frozen=True)
class PanelView:
    text: str
    keyboard: list[list[PanelButton]]
    input_state: str | None = None
    action: str = "main"


class AssistantState(Protocol):
    def block_chat(self, chat_id: int, title: str | None = None) -> None: ...
    def unblock_chat(self, chat_id: int) -> None: ...
    def list_blocked_chats(self) -> list[dict[str, object]]: ...
    def allow_group(self, chat_id: int, title: str | None = None) -> None: ...
    def disallow_group(self, chat_id: int) -> None: ...
    def is_group_allowed(self, chat_id: int) -> bool: ...
    def list_allowed_groups(self) -> list[dict[str, object]]: ...
    def upsert_known_group(self, chat_id: int, title: str | None = None) -> None: ...
    def list_known_groups(self) -> list[dict[str, object]]: ...
    def upsert_known_chat(self, chat_id: int, title: str | None = None, kind: str = "chat") -> None: ...
    def list_known_chats(self, kind: str | None = None) -> list[dict[str, object]]: ...
    def get_private_chat_message_gate(self, chat_id: int) -> dict[str, object] | None: ...
    def is_private_chat_transcription_enabled(self, chat_id: int) -> bool: ...
    def get_private_chat_transcription_override(self, chat_id: int) -> bool | None: ...
    def set_private_chat_transcription(self, chat_id: int, title: str | None, enabled: bool) -> None: ...
    def is_feature_enabled(self, feature: str) -> bool: ...
    def set_feature_enabled(self, feature: str, enabled: bool) -> None: ...
    def is_transcription_decoration_enabled(self) -> bool: ...
    def set_transcription_decoration_enabled(self, enabled: bool) -> None: ...
    def get_private_chat_min_messages(self) -> int: ...
    def set_private_chat_min_messages(self, minimum_messages: int) -> None: ...
    def get_voice_min_duration_seconds(self) -> int: ...
    def set_voice_min_duration_seconds(self, seconds: int) -> None: ...
    def get_text_polish_prompt(self) -> str: ...
    def set_text_polish_prompt(self, prompt: str) -> None: ...
    def reset_text_polish_prompt(self) -> None: ...
    def is_account_premium(self) -> bool: ...
    def set_account_premium(self, is_premium: bool) -> None: ...
    def is_reaction_autolike_enabled(self) -> bool: ...
    def set_reaction_autolike_enabled(self, enabled: bool) -> None: ...
    def get_reaction_delay_range_seconds(self) -> tuple[int, int]: ...
    def set_reaction_delay_range_seconds(self, minimum_seconds: int, maximum_seconds: int) -> None: ...
    def get_reaction_global_mode(self) -> str: ...
    def set_reaction_global_mode(self, mode: str) -> None: ...
    def upsert_reaction_folder(self, folder_id: int, title: str, *, position: int = 0) -> None: ...
    def get_reaction_folder_settings(self, folder_id: int): ...
    def list_reaction_folders(self) -> list[dict[str, object]]: ...
    def list_reaction_folder_channels(self, folder_id: int) -> list[dict[str, object]]: ...
    def set_reaction_folder_enabled(self, folder_id: int, enabled: bool) -> None: ...
    def set_reaction_folder_mode(self, folder_id: int, mode: str) -> None: ...
    def set_reaction_folder_max_reactions(self, folder_id: int, max_reactions: int) -> None: ...
    def set_reaction_folder_selection_strategy(self, folder_id: int, strategy: str) -> None: ...
    def set_reaction_folder_source(self, folder_id: int, source: str) -> None: ...
    def upsert_reaction_channel(self, chat_id: int, title: str | None = None) -> None: ...
    def get_reaction_channel_settings(self, chat_id: int): ...
    def get_effective_reaction_channel_settings(self, chat_id: int): ...
    def get_effective_reaction_channel_settings_source(self, chat_id: int) -> str | None: ...
    def list_reaction_channels(self) -> list[dict[str, object]]: ...
    def set_reaction_channel_enabled(self, chat_id: int, enabled: bool) -> None: ...
    def set_reaction_channel_mode(self, chat_id: int, mode: str) -> None: ...
    def set_reaction_channel_max_reactions(self, chat_id: int, max_reactions: int) -> None: ...
    def set_reaction_channel_selection_strategy(self, chat_id: int, strategy: str) -> None: ...
    def set_reaction_channel_source(self, chat_id: int, source: str) -> None: ...
    def toggle_reaction_channel_emoji(self, chat_id: int, emoji: str) -> None: ...
    def cycle_reaction_channel_emoji_category(self, chat_id: int, emoji: str) -> str: ...
    def has_reaction_channel_available_reactions_checked(self, chat_id: int) -> bool: ...
    def list_reaction_channel_available_reactions(self, chat_id: int) -> list[dict[str, str]]: ...


class ControlPanelService:
    group_page_size = 8
    chat_page_size = 8
    reaction_channel_page_size = 8
    reaction_emoji_page_size = 16

    def __init__(
        self,
        *,
        owner_id: int,
        state: AssistantState,
        llm_provider: str = "copilot",
        llm_model: str | None = None,
        chat_export_enabled: bool = False,
    ):
        self.owner_id = owner_id
        self.state = state
        self.llm_provider = llm_provider
        self.llm_model = llm_model
        self.chat_export_enabled = chat_export_enabled
        self.command_service = ManagerService(owner_id=owner_id, blacklist=state)
        self._chat_search_queries: dict[int, str] = {}
        self._group_search_queries: dict[int, str] = {}
        self._reaction_channel_search_queries: dict[int, str] = {}
        self._reaction_channel_enabled_filters: dict[int, bool] = {}
        self._reaction_channel_mode_filters: dict[int, str] = {}
        self._reaction_channel_source_filters: dict[int, str] = {}

    def main(self, *, user_id: int) -> PanelView:
        if not self._is_owner(user_id):
            return self._denied()
        enabled = self.state.is_feature_enabled(VOICE_FEATURE)
        status = "включена" if enabled else "выключена"
        text = "\n".join(
            [
                "Главное меню",
                "",
                f"Транскрибация: {status}",
                "Личные чаты: по лимиту или вручную включенные.",
                "Группы: только выбранные.",
                "Изменения применяются сразу, без перезапуска worker.",
            ]
        )
        return PanelView(
            text=text,
            keyboard=[
                [PanelButton("Транскрибация", "transcription")],
                [PanelButton("Автолайк ТГК", "reactions")],
                *([[PanelButton("Экспорт чатов", "export.chats")]] if self.chat_export_enabled else []),
                [PanelButton("Статус", "status"), PanelButton("Помощь", "help")],
            ],
            action="main",
        )

    def handle_action(self, *, user_id: int, action: str) -> PanelView:
        if not self._is_owner(user_id):
            return self._denied()

        action = self._normalize_action(action)
        if action in {"main", "home", "back"}:
            return self.main(user_id=user_id)
        if action == "help":
            return self._help()
        if action in {"status", "status.refresh"}:
            return self._status()
        if action == "transcription":
            return self._transcription()
        if action == "transcription.toggle":
            current = self.state.is_feature_enabled(VOICE_FEATURE)
            self.state.set_feature_enabled(VOICE_FEATURE, not current)
            return self._transcription()
        if action == "transcription.decoration.toggle":
            current = self.state.is_transcription_decoration_enabled()
            self.state.set_transcription_decoration_enabled(not current)
            return self._transcription()
        if action == "transcription.why":
            return self._transcription_why()
        if action == "transcription.blacklist":
            return self._blacklist()
        if action == "transcription.blacklist.add":
            return PanelView(
                text="Пришли chat_id личного чата и опциональное название.\n\nПример: 123456789 Иван",
                keyboard=self._back_keyboard(),
                input_state="block_chat",
                action="transcription.blacklist.add",
            )
        if action == "transcription.blacklist.remove":
            return PanelView(
                text="Пришли chat_id, который нужно убрать из исключений.",
                keyboard=self._back_keyboard(),
                input_state="unblock_chat",
                action="transcription.blacklist.remove",
            )
        if action == "transcription.chats":
            return self._transcription_chats()
        if action == "transcription.chats.search":
            return self._transcription_chat_search_prompt()
        if action == "transcription.chats.search.clear":
            self._chat_search_queries.pop(user_id, None)
            return self._transcription_chats()
        if action.startswith("transcription.chats.search.page:"):
            query = self._chat_search_queries.get(user_id)
            if not query:
                return self._transcription_chat_search_prompt()
            return self._transcription_chats(page=self._parse_page(action), query=query)
        if action == "transcription.chats.enabled":
            return self._transcription_chats(only_enabled=True)
        if action.startswith("transcription.chats.page:"):
            return self._transcription_chats(page=self._parse_page(action))
        if action.startswith("transcription.chats.enabled.page:"):
            return self._transcription_chats(page=self._parse_page(action), only_enabled=True)
        if action.startswith("transcription.chats.toggle:"):
            return self._toggle_transcription_chat(action, user_id=user_id)
        if action == "transcription.settings":
            return self._transcription_settings()
        if action == "transcription.settings.private_limit":
            return PanelView(
                text="Пришли лимит сообщений для авто-включения личного чата.\n\nПример: 250",
                keyboard=self._back_keyboard(),
                input_state="private_min_messages",
                action="transcription.settings.private_limit",
            )
        if action == "transcription.settings.voice_min_duration":
            return PanelView(
                text="Пришли минимальную длину голосового в секундах.\n\nПример: 12",
                keyboard=self._back_keyboard(),
                input_state="voice_min_duration",
                action="transcription.settings.voice_min_duration",
            )
        if action == "transcription.groups":
            return self._groups()
        if action == "transcription.groups.search":
            return self._transcription_group_search_prompt()
        if action == "transcription.groups.search.clear":
            self._group_search_queries.pop(user_id, None)
            return self._groups()
        if action.startswith("transcription.groups.search.page:"):
            query = self._group_search_queries.get(user_id)
            if not query:
                return self._transcription_group_search_prompt()
            return self._groups_view(page=self._parse_page(action), query=query)
        if action == "transcription.groups.enabled":
            return self._groups_view(only_allowed=True)
        if action.startswith("transcription.groups.page:"):
            return self._groups_view(page=self._parse_page(action))
        if action.startswith("transcription.groups.enabled.page:"):
            return self._groups_view(page=self._parse_page(action), only_allowed=True)
        if action.startswith("transcription.groups.toggle:"):
            return self._toggle_group(action, user_id=user_id)
        if action == "transcription.groups.add":
            return PanelView(
                text="Пришли chat_id группы и название.\n\nПример: -1001234567890 Рабочий чат",
                keyboard=self._back_keyboard(),
                input_state="allow_group",
                action="transcription.groups.add",
            )
        if action == "transcription.groups.remove":
            return self._groups()
        if action == "transcription.prompt":
            return self._prompt()
        if action == "transcription.prompt.edit":
            return PanelView(
                text="Пришли новый промпт для нейросети. Он будет применяться к следующим голосовым сразу.",
                keyboard=self._back_keyboard(),
                input_state="text_polish_prompt",
                action="transcription.prompt.edit",
            )
        if action == "transcription.prompt.reset":
            self.state.reset_text_polish_prompt()
            return PanelView(
                text="Промпт сброшен.\n\n" + self._prompt_text(),
                keyboard=self._prompt_keyboard(),
                action="transcription.prompt",
            )
        if action == "reactions":
            self._clear_reaction_channel_filters(user_id)
            return self._reaction_channels()
        if action == "reactions.enabled":
            self._reaction_channel_enabled_filters[user_id] = True
            return self._reaction_channels_for_user(user_id)
        if action == "reactions.search":
            return self._reaction_channel_search_prompt()
        if action == "reactions.search.clear":
            self._reaction_channel_search_queries.pop(user_id, None)
            return self._reaction_channels_for_user(user_id)
        if action == "reactions.filter.enabled:on":
            self._reaction_channel_enabled_filters[user_id] = True
            return self._reaction_channels_for_user(user_id)
        if action == "reactions.filter.enabled:any":
            self._reaction_channel_enabled_filters.pop(user_id, None)
            return self._reaction_channels_for_user(user_id)
        if action.startswith("reactions.filter.mode:"):
            mode = action.rsplit(":", 1)[1]
            if mode == "any":
                self._reaction_channel_mode_filters.pop(user_id, None)
            elif mode in VALID_REACTION_MODES - {"custom"}:
                self._reaction_channel_mode_filters[user_id] = mode
            return self._reaction_channels_for_user(user_id)
        if action.startswith("reactions.filter.source:"):
            source = action.rsplit(":", 1)[1]
            if source == "any":
                self._reaction_channel_source_filters.pop(user_id, None)
            elif source in VALID_REACTION_SOURCES:
                self._reaction_channel_source_filters[user_id] = source
            return self._reaction_channels_for_user(user_id)
        if action == "reactions.toggle":
            self.state.set_reaction_autolike_enabled(not self.state.is_reaction_autolike_enabled())
            return self._reaction_channels_for_user(user_id)
        if action == "reactions.delay":
            return PanelView(
                text="Пришли диапазон задержки автолайка в секундах.\n\nПример: 240-900",
                keyboard=self._back_keyboard(),
                input_state="reaction_delay",
                action="reactions.delay",
            )
        if action == "reactions.history":
            return self._reaction_history()
        if action.startswith("reactions.global.mode:"):
            mode = action.rsplit(":", 1)[1]
            if mode not in VALID_REACTION_MODES - {"custom"}:
                return self._reaction_channels_for_user(user_id, feedback="Неизвестный глобальный фильтр.")
            self.state.set_reaction_global_mode(mode)
            return self._reaction_channels_for_user(user_id, feedback=f"Глобальный фильтр: {self._reaction_mode_label(mode)}")
        if action == "reactions.folders":
            return self._reaction_folders()
        if action == "rfr":
            return self._reaction_folders()
        if action.startswith("reactions.folder.toggle:"):
            return self._toggle_reaction_folder(action)
        if action.startswith("reactions.folder.max1:"):
            return self._set_reaction_folder_max(action, max_reactions=1)
        if action.startswith("reactions.folder.max3:"):
            return self._set_reaction_folder_max(action, max_reactions=3)
        if action.startswith("reactions.folder.strategy:"):
            return self._set_reaction_folder_strategy(action)
        if action.startswith("reactions.folder.source:"):
            return self._set_reaction_folder_source(action)
        if action.startswith("reactions.folder.mode:"):
            return self._set_reaction_folder_mode(action)
        if action.startswith("reactions.folder:"):
            folder_id = self._parse_folder_id(action)
            if folder_id is None:
                return self._reaction_folders()
            return self._reaction_folder_detail(folder_id)
        if action.startswith("reactions.page:"):
            return self._reaction_channels_for_user(user_id, page=self._parse_page(action))
        if action.startswith("reactions.enabled.page:"):
            self._reaction_channel_enabled_filters[user_id] = True
            return self._reaction_channels_for_user(user_id, page=self._parse_page(action))
        if action.startswith("reactions.search.page:"):
            query = self._reaction_channel_search_queries.get(user_id)
            if not query:
                return self._reaction_channel_search_prompt()
            return self._reaction_channels_for_user(user_id, page=self._parse_page(action))
        if action.startswith("reactions.channel.customize:"):
            parsed = self._parse_channel_action(action, "reactions.channel.customize")
            if parsed is None:
                return self._reaction_channels()
            chat_id, page = parsed
            self._create_reaction_channel_override(chat_id)
            return self._reaction_channel_detail(chat_id, page=page)
        if action.startswith("reactions.channel.history:"):
            parsed = self._parse_channel_action(action, "reactions.channel.history")
            if parsed is None:
                return self._reaction_channels()
            chat_id, page = parsed
            return self._reaction_channel_history(chat_id, page=page)
        if action.startswith("reactions.channel:"):
            parsed = self._parse_channel_action(action, "reactions.channel")
            if parsed is None:
                return self._reaction_channels()
            chat_id, page = parsed
            return self._reaction_channel_detail(chat_id, page=page)
        if action.startswith("reactions.channel.toggle:"):
            return self._toggle_reaction_channel(action)
        if action.startswith("reactions.channel.max1:"):
            return self._set_reaction_channel_max(action, max_reactions=1)
        if action.startswith("reactions.channel.max3:"):
            return self._set_reaction_channel_max(action, max_reactions=3)
        if action.startswith("reactions.channel.strategy:"):
            return self._set_reaction_channel_strategy(action)
        if action.startswith("reactions.channel.source:"):
            return self._set_reaction_channel_source(action)
        if action.startswith("reactions.channel.mode:"):
            return self._set_reaction_channel_mode(action)
        if action.startswith("reactions.channel.emojis:"):
            parsed = self._parse_channel_action(action, "reactions.channel.emojis")
            if parsed is None:
                return self._reaction_channels()
            chat_id, page = parsed
            return self._reaction_emoji_picker(chat_id, page=page)
        if action.startswith("reactions.channel.emoji:"):
            return self._toggle_reaction_channel_emoji(action)
        if action.startswith("rce:"):
            return self._toggle_reaction_channel_emoji(action)
        if action.startswith("rcc:"):
            return self._cycle_reaction_channel_emoji_category(action)
        if action == "reactions.channel.add":
            return PanelView(
                text="Пришли chat_id канала и название.\n\nПример: -1001234567890 Новости",
                keyboard=self._back_keyboard(),
                input_state="reaction_channel",
                action="reactions.channel.add",
            )
        return PanelView(
            text="Неизвестное действие. Возвращаю в главное меню.",
            keyboard=self._back_keyboard(),
            action="main",
        )

    def handle_text(self, *, user_id: int, state: str | None, text: str) -> PanelView:
        if not self._is_owner(user_id):
            return self._denied()
        if state == "block_chat":
            return self._handle_block_chat_text(text)
        if state == "unblock_chat":
            return self._handle_unblock_chat_text(text)
        if state == "allow_group":
            return self._handle_allow_group_text(text)
        if state == "disallow_group":
            return self._handle_disallow_group_text(text)
        if state == "text_polish_prompt":
            return self._handle_prompt_text(text)
        if state == "private_min_messages":
            return self._handle_private_min_messages_text(text)
        if state == "voice_min_duration":
            return self._handle_voice_min_duration_text(text)
        if state == "transcription_chat_search":
            return self._handle_transcription_chat_search_text(user_id, text)
        if state == "transcription_group_search":
            return self._handle_transcription_group_search_text(user_id, text)
        if state == "reaction_channel_search":
            return self._handle_reaction_channel_search_text(user_id, text)
        if state == "reaction_channel":
            return self._handle_reaction_channel_text(text)
        if state == "reaction_delay":
            return self._handle_reaction_delay_text(text)
        if text.strip().startswith("/"):
            response = self.command_service.handle_command(user_id=user_id, text=text)
            return PanelView(text=response, keyboard=self._back_keyboard(), action="main")
        return self.main(user_id=user_id)

    def _transcription(self) -> PanelView:
        enabled = self.state.is_feature_enabled(VOICE_FEATURE)
        status = "включена" if enabled else "выключена"
        toggle = "Выключить" if enabled else "Включить"
        decoration_enabled = self.state.is_transcription_decoration_enabled()
        decoration_status = "вкл" if decoration_enabled else "выкл"
        decoration_toggle = "Выключить смайлы" if decoration_enabled else "Включить смайлы"
        blocked = self.state.list_blocked_chats()
        groups = self.state.list_allowed_groups()
        text = "\n".join(
            [
                f"Транскрибация: {status}",
                "",
                "Личные чаты: по лимиту или вручную",
                f"Лимит личного чата: {self.state.get_private_chat_min_messages()} сообщений",
                f"ГС от: {self.state.get_voice_min_duration_seconds()} сек",
                f"Исключения: {len(blocked)}",
                f"Группы: выбрано {len(groups)}",
                f"Смайлы: {decoration_status}",
                "Каналы не обрабатываются.",
            ]
        )
        return PanelView(
            text=text,
            keyboard=[
                [PanelButton(toggle, "transcription.toggle")],
                [PanelButton(decoration_toggle, "transcription.decoration.toggle")],
                [PanelButton("Чаты", "transcription.chats"), PanelButton("Настройки", "transcription.settings")],
                [PanelButton("Исключения", "transcription.blacklist"), PanelButton("Группы", "transcription.groups")],
                [PanelButton("Промпт", "transcription.prompt")],
                [PanelButton("Почему не работает", "transcription.why")],
                [PanelButton("Статус", "status"), PanelButton("Помощь", "help")],
                *self._back_keyboard(),
            ],
            action="transcription",
        )

    def _transcription_why(self) -> PanelView:
        return PanelView(
            text="\n".join(
                [
                    "Почему чат не обрабатывается",
                    "",
                    "Личные чаты: по лимиту сообщений или вручную.",
                    "Группы: только выбранные.",
                    "Каналы не транскрибируются.",
                    "Исключения выключают личный чат.",
                    "ГС короче минимума или длиннее 5 мин пропускаются.",
                ]
            ),
            keyboard=[
                [PanelButton("Чаты", "transcription.chats"), PanelButton("Группы", "transcription.groups")],
                [PanelButton("Исключения", "transcription.blacklist")],
                *self._back_keyboard(),
            ],
            action="transcription.why",
        )

    def _blacklist(self) -> PanelView:
        chats = self.state.list_blocked_chats()
        if chats:
            lines = ["Исключения:"]
            for chat in chats:
                title = chat["title"] or "без названия"
                lines.append(f"- {chat['chat_id']}: {title}")
        else:
            lines = ["Исключения пусты.", "Все личные чаты сейчас будут транскрибироваться."]
        return PanelView(
            text="\n".join(lines),
            keyboard=[
                [PanelButton("Заблокировать чат", "transcription.blacklist.add")],
                [PanelButton("Разблокировать чат", "transcription.blacklist.remove")],
                *self._back_keyboard(),
            ],
            action="transcription.blacklist",
        )

    def _transcription_chat_search_prompt(self, *, error: str | None = None) -> PanelView:
        text = "Пришли часть названия, chat_id или тип чата.\n\nПример: alice"
        if error:
            text = f"{error}\n\n{text}"
        return PanelView(
            text=text,
            keyboard=[[PanelButton("К списку чатов", "transcription.chats")], *self._back_keyboard()],
            input_state="transcription_chat_search",
            action="transcription.chats.search",
        )

    def _handle_transcription_chat_search_text(self, user_id: int, text: str) -> PanelView:
        query = self._normalize_search_query(text)
        if query is None:
            return self._transcription_chat_search_prompt(error="Поиск не должен быть пустым.")
        self._chat_search_queries[user_id] = query
        return self._transcription_chats(query=query)

    def _transcription_chats(
        self,
        *,
        page: int = 0,
        feedback: str | None = None,
        only_enabled: bool = False,
        query: str | None = None,
    ) -> PanelView:
        all_chats = self._transcription_chat_items()
        enabled_count = sum(1 for chat in all_chats if chat["enabled"])
        query = self._normalize_search_query(query)
        search_mode = query is not None
        if search_mode:
            chats = [chat for chat in all_chats if self._matches_query(chat, query)]
        elif only_enabled:
            chats = [chat for chat in all_chats if chat["enabled"]]
        else:
            chats = all_chats
        total_pages = max(1, (len(chats) + self.chat_page_size - 1) // self.chat_page_size)
        page = min(max(page, 0), total_pages - 1)
        page_chats = chats[page * self.chat_page_size : (page + 1) * self.chat_page_size]
        if search_mode:
            lines = [
                "Чаты",
                "",
                f"Поиск: {query}",
                f"Найдено: {len(chats)}",
                f"Включено: {enabled_count}",
            ]
        else:
            lines = [
                "Чаты",
                "",
                f"Включено: {enabled_count}",
                f"Фильтр: {'только включенные' if only_enabled else 'все чаты'}",
                "Личные чаты включаются по лимиту сообщений или вручную.",
                "Группы включаются только явно.",
            ]
        if not chats:
            if search_mode:
                lines = [
                    "Чаты",
                    "",
                    f"Поиск: {query}",
                    "Найдено: 0",
                    "Ничего не найдено. Уточни запрос или сбрось поиск.",
                ]
            elif only_enabled:
                lines = [
                    "Чаты",
                    "",
                    f"Включено: {enabled_count}",
                    "Фильтр: только включенные",
                    "Пока нет включенных личных чатов и групп.",
                    "Открой общий список и включи нужный чат.",
                ]
            else:
                lines = [
                    "Чаты",
                    "",
                    "Пока нет найденных личных чатов и групп.",
                    "Список пополняется автоматически из Telegram аккаунта.",
                ]
        if feedback:
            lines.extend(["", feedback])

        if search_mode:
            mode_suffix = ":search"
        elif only_enabled:
            mode_suffix = ":enabled"
        else:
            mode_suffix = ""
        keyboard = [
            [
                PanelButton(
                    f"{'✅' if chat['enabled'] else '❌'} {self._chat_kind_prefix(str(chat['kind']))} "
                    f"{self._compact_button_title(str(chat['title']))}",
                    f"transcription.chats.toggle:{chat['chat_id']}:{page}{mode_suffix}",
                )
            ]
            for chat in page_chats
        ]
        if total_pages > 1:
            prev_page = max(page - 1, 0)
            next_page = min(page + 1, total_pages - 1)
            if search_mode:
                page_prefix = "transcription.chats.search.page"
            elif only_enabled:
                page_prefix = "transcription.chats.enabled.page"
            else:
                page_prefix = "transcription.chats.page"
            keyboard.append(
                [
                    PanelButton("‹", f"{page_prefix}:{prev_page}"),
                    PanelButton(f"{page + 1}/{total_pages}", f"{page_prefix}:{page}"),
                    PanelButton("›", f"{page_prefix}:{next_page}"),
                ]
            )
        if search_mode:
            keyboard.append(
                [
                    PanelButton("Найти заново", "transcription.chats.search"),
                    PanelButton("Сбросить поиск", "transcription.chats.search.clear"),
                ]
            )
        elif only_enabled:
            keyboard.append([PanelButton("Все чаты", "transcription.chats")])
        else:
            keyboard.append([PanelButton("Найти чат", "transcription.chats.search")])
            keyboard.append([PanelButton("Только включенные", "transcription.chats.enabled")])
        keyboard.extend(self._back_keyboard())
        if search_mode:
            action = f"transcription.chats.search.page:{page}"
        elif only_enabled:
            action = f"transcription.chats.enabled.page:{page}"
        else:
            action = "transcription.chats"
        return PanelView(text="\n".join(lines), keyboard=keyboard, action=action)

    def _transcription_chat_items(self) -> list[dict[str, object]]:
        items = []
        for chat in self.state.list_known_chats():
            kind = str(chat["kind"])
            if kind == "channel":
                continue
            chat_id = int(chat["chat_id"])
            title = str(chat.get("title") or chat_id)
            if kind == "private":
                gate = self.state.get_private_chat_message_gate(chat_id)
                override = self.state.get_private_chat_transcription_override(chat_id)
                enabled = override if override is not None else bool(gate and gate.get("is_allowed"))
            elif kind == "group":
                enabled = self.state.is_group_allowed(chat_id)
            else:
                enabled = False
            items.append({"chat_id": chat_id, "title": title, "kind": kind, "enabled": enabled})
        return items

    def _toggle_transcription_chat(self, action: str, *, user_id: int) -> PanelView:
        parts = action.split(":")
        if len(parts) not in {3, 4}:
            return self._transcription_chats()
        try:
            chat_id = int(parts[1])
            page = int(parts[2])
        except ValueError:
            return self._transcription_chats()
        mode = parts[3] if len(parts) == 4 else "all"
        only_enabled = mode == "enabled"
        query = self._chat_search_queries.get(user_id) if mode == "search" else None
        chat = self._known_chat(chat_id)
        if chat is None:
            return self._transcription_chats(
                page=page,
                feedback=f"Чат {chat_id} не найден.",
                only_enabled=only_enabled,
                query=query,
            )
        title = str(chat["title"])
        kind = str(chat["kind"])
        if kind == "private":
            enabled = not bool(chat["enabled"])
            self.state.set_private_chat_transcription(chat_id, title, enabled)
        elif kind == "group":
            enabled = not self.state.is_group_allowed(chat_id)
            if enabled:
                self.state.allow_group(chat_id, title)
            else:
                self.state.disallow_group(chat_id)
        else:
            return self._transcription_chats(
                page=page,
                feedback=f"Чат {title} нельзя включить для транскрибации.",
                only_enabled=only_enabled,
                query=query,
            )
        return self._transcription_chats(
            page=page,
            feedback=f"{'Включено' if enabled else 'Выключено'}: {title}",
            only_enabled=only_enabled,
            query=query,
        )

    def _known_chat(self, chat_id: int) -> dict[str, object] | None:
        for chat in self._transcription_chat_items() + self._reaction_channel_items():
            if int(chat["chat_id"]) == chat_id:
                return chat
        return None

    def _transcription_settings(self) -> PanelView:
        return PanelView(
            text="\n".join(
                [
                    "Настройки транскрибации",
                    "",
                    f"Лимит личного чата: {self.state.get_private_chat_min_messages()} сообщений",
                    f"ГС от: {self.state.get_voice_min_duration_seconds()} сек",
                ]
            ),
            keyboard=[
                [PanelButton("Лимит сообщений", "transcription.settings.private_limit")],
                [PanelButton("Минимум ГС", "transcription.settings.voice_min_duration")],
                *self._back_keyboard(),
            ],
            action="transcription.settings",
        )

    def _groups(self) -> PanelView:
        return self._groups_view()

    def _transcription_group_search_prompt(self, *, error: str | None = None) -> PanelView:
        text = "Пришли часть названия или chat_id группы.\n\nПример: team"
        if error:
            text = f"{error}\n\n{text}"
        return PanelView(
            text=text,
            keyboard=[[PanelButton("К списку групп", "transcription.groups")], *self._back_keyboard()],
            input_state="transcription_group_search",
            action="transcription.groups.search",
        )

    def _handle_transcription_group_search_text(self, user_id: int, text: str) -> PanelView:
        query = self._normalize_search_query(text)
        if query is None:
            return self._transcription_group_search_prompt(error="Поиск не должен быть пустым.")
        self._group_search_queries[user_id] = query
        return self._groups_view(query=query)

    def _groups_view(
        self,
        *,
        page: int = 0,
        feedback: str | None = None,
        only_allowed: bool = False,
        query: str | None = None,
    ) -> PanelView:
        all_groups = self._group_items()
        selected_count = sum(1 for group in all_groups if group["allowed"])
        query = self._normalize_search_query(query)
        search_mode = query is not None
        if search_mode:
            groups = [group for group in all_groups if self._matches_query(group, query)]
        elif only_allowed:
            groups = [group for group in all_groups if group["allowed"]]
        else:
            groups = all_groups
        total_pages = max(1, (len(groups) + self.group_page_size - 1) // self.group_page_size)
        page = min(max(page, 0), total_pages - 1)
        page_groups = groups[page * self.group_page_size : (page + 1) * self.group_page_size]
        if search_mode:
            lines = [
                "Группы",
                "",
                f"Поиск: {query}",
                f"Найдено: {len(groups)}",
                f"Выбрано: {selected_count}",
            ]
        else:
            lines = [
                "Группы",
                "",
                f"Выбрано: {selected_count}",
                f"Фильтр: {'только выбранные' if only_allowed else 'все группы'}",
                "Нажми на группу, чтобы включить или выключить транскрибацию.",
            ]
        if not groups:
            if search_mode:
                lines = [
                    "Группы",
                    "",
                    f"Поиск: {query}",
                    "Найдено: 0",
                    "Ничего не найдено. Уточни запрос или сбрось поиск.",
                ]
            elif only_allowed:
                lines = [
                    "Группы",
                    "",
                    f"Выбрано: {selected_count}",
                    "Фильтр: только выбранные",
                    "Пока нет выбранных групп.",
                    "Открой общий список или введи chat_id группы.",
                ]
            else:
                lines = [
                    "Группы",
                    "",
                    "Пока нет найденных групп.",
                    "Список пополняется автоматически из Telegram аккаунта.",
                ]
        if feedback:
            lines.extend(["", feedback])

        if search_mode:
            mode_suffix = ":search"
        elif only_allowed:
            mode_suffix = ":enabled"
        else:
            mode_suffix = ""
        keyboard = [
            [
                PanelButton(
                    f"{'✅' if group['allowed'] else '○'} {self._compact_button_title(str(group['title']))}",
                    f"transcription.groups.toggle:{group['chat_id']}:{page}{mode_suffix}",
                )
            ]
            for group in page_groups
        ]
        if total_pages > 1:
            prev_page = max(page - 1, 0)
            next_page = min(page + 1, total_pages - 1)
            if search_mode:
                page_prefix = "transcription.groups.search.page"
            elif only_allowed:
                page_prefix = "transcription.groups.enabled.page"
            else:
                page_prefix = "transcription.groups.page"
            keyboard.append(
                [
                    PanelButton("‹", f"{page_prefix}:{prev_page}"),
                    PanelButton(f"{page + 1}/{total_pages}", f"{page_prefix}:{page}"),
                    PanelButton("›", f"{page_prefix}:{next_page}"),
                ]
            )
        if search_mode:
            keyboard.append(
                [
                    PanelButton("Найти заново", "transcription.groups.search"),
                    PanelButton("Сбросить поиск", "transcription.groups.search.clear"),
                ]
            )
        elif only_allowed:
            keyboard.append([PanelButton("Все группы", "transcription.groups")])
        else:
            keyboard.append([PanelButton("Найти группу", "transcription.groups.search")])
            keyboard.append([PanelButton("Только выбранные", "transcription.groups.enabled")])
        keyboard.append([PanelButton("Ввести chat_id", "transcription.groups.add")])
        keyboard.extend(self._back_keyboard())
        if search_mode:
            action = f"transcription.groups.search.page:{page}"
        elif only_allowed:
            action = f"transcription.groups.enabled.page:{page}"
        else:
            action = "transcription.groups"
        return PanelView(
            text="\n".join(lines),
            keyboard=keyboard,
            action=action,
        )

    def _group_items(self) -> list[dict[str, object]]:
        groups: dict[int, dict[str, object]] = {}
        ordered_chat_ids: list[int] = []
        for group in self.state.list_known_groups():
            chat_id = int(group["chat_id"])
            ordered_chat_ids.append(chat_id)
            groups[chat_id] = {
                "chat_id": chat_id,
                "title": self._group_title(group),
                "allowed": self.state.is_group_allowed(chat_id),
            }
        for group in self.state.list_allowed_groups():
            chat_id = int(group["chat_id"])
            if chat_id not in groups:
                ordered_chat_ids.append(chat_id)
            groups[chat_id] = {
                "chat_id": chat_id,
                "title": self._group_title(group),
                "allowed": True,
            }
        return [groups[chat_id] for chat_id in ordered_chat_ids]

    @staticmethod
    def _group_title(group: dict[str, object]) -> str:
        title = group.get("title")
        return str(title) if title else str(group["chat_id"])

    def _toggle_group(self, action: str, *, user_id: int) -> PanelView:
        parts = action.split(":")
        if len(parts) not in {3, 4}:
            return self._groups()
        try:
            chat_id = int(parts[1])
            page = int(parts[2])
        except ValueError:
            return self._groups()
        mode = parts[3] if len(parts) == 4 else "all"
        only_allowed = mode == "enabled"
        query = self._group_search_queries.get(user_id) if mode == "search" else None
        title = self._known_group_title(chat_id)
        if self.state.is_group_allowed(chat_id):
            self.state.disallow_group(chat_id)
            feedback = f"Выключено: {title}"
        else:
            self.state.allow_group(chat_id, title)
            feedback = f"Включено: {title}"
        return self._groups_view(page=page, feedback=feedback, only_allowed=only_allowed, query=query)

    def _known_group_title(self, chat_id: int) -> str:
        for group in self._group_items():
            if int(group["chat_id"]) == chat_id:
                return str(group["title"])
        return str(chat_id)

    @staticmethod
    def _normalize_search_query(query: str | None) -> str | None:
        if query is None:
            return None
        query = query.strip()
        return query.casefold() if query else None

    @classmethod
    def _matches_query(cls, item: dict[str, object], query: str) -> bool:
        values = [str(item.get("chat_id", "")), str(item.get("title", ""))]
        kind = item.get("kind")
        if kind is not None:
            values.extend([str(kind), cls._chat_kind_prefix(str(kind))])
        return query in " ".join(values).casefold()

    @staticmethod
    def _compact_button_title(title: str, *, max_length: int = 40) -> str:
        title = title.strip() or "без названия"
        if len(title) <= max_length:
            return title
        return title[: max_length - 3].rstrip() + "..."

    def _prompt(self) -> PanelView:
        return PanelView(text=self._prompt_text(), keyboard=self._prompt_keyboard(), action="transcription.prompt")

    def _prompt_text(self) -> str:
        prompt = self.state.get_text_polish_prompt()
        return f"Текущий промпт:\n\n{prompt}"

    @staticmethod
    def _prompt_keyboard() -> list[list[PanelButton]]:
        return [
            [PanelButton("Изменить промпт", "transcription.prompt.edit")],
            [PanelButton("Сбросить промпт", "transcription.prompt.reset")],
            [PanelButton("Назад", "back")],
        ]

    def _reaction_channel_search_prompt(self, *, error: str | None = None) -> PanelView:
        text = "Пришли часть названия или channel_id.\n\nПример: news"
        if error:
            text = f"{error}\n\n{text}"
        return PanelView(
            text=text,
            keyboard=[[PanelButton("К списку каналов", "reactions")], *self._back_keyboard()],
            input_state="reaction_channel_search",
            action="reactions.search",
        )

    def _handle_reaction_channel_search_text(self, user_id: int, text: str) -> PanelView:
        query = self._normalize_search_query(text)
        if query is None:
            return self._reaction_channel_search_prompt(error="Поиск не должен быть пустым.")
        self._reaction_channel_search_queries[user_id] = query
        return self._reaction_channels_for_user(user_id)

    def _reaction_channels_for_user(
        self,
        user_id: int,
        *,
        page: int = 0,
        feedback: str | None = None,
    ) -> PanelView:
        return self._reaction_channels(
            page=page,
            feedback=feedback,
            only_enabled=self._reaction_channel_enabled_filters.get(user_id, False),
            query=self._reaction_channel_search_queries.get(user_id),
            mode_filter=self._reaction_channel_mode_filters.get(user_id),
            source_filter=self._reaction_channel_source_filters.get(user_id),
        )

    def _clear_reaction_channel_filters(self, user_id: int) -> None:
        self._reaction_channel_search_queries.pop(user_id, None)
        self._reaction_channel_enabled_filters.pop(user_id, None)
        self._reaction_channel_mode_filters.pop(user_id, None)
        self._reaction_channel_source_filters.pop(user_id, None)

    def _reaction_folders(self, *, feedback: str | None = None) -> PanelView:
        folders = self.state.list_reaction_folders()
        enabled_count = sum(1 for folder in folders if folder["enabled"])
        lines = [
            "Папки автолайка",
            "",
            f"Включено: {enabled_count}/{len(folders)}",
            "Папка работает как дефолт для каналов без ручной настройки.",
        ]
        if feedback:
            lines.extend(["", feedback])
        if not folders:
            lines.extend(
                [
                    "",
                    "Папки еще не синхронизированы. Нажми «Обновить папки», чтобы прочитать их из Telegram.",
                ]
            )
        keyboard = [
            [
                PanelButton(
                    (
                        f"{'✅' if folder['enabled'] else '○'} "
                        f"{self._compact_button_title(str(folder['title']))} · "
                        f"{self._reaction_channel_count_label(int(folder['channel_count']))}"
                    ),
                    f"reactions.folder:{folder['folder_id']}",
                    style="primary" if folder["enabled"] else None,
                )
            ]
            for folder in folders
        ]
        keyboard.append([PanelButton("Обновить папки", "rfr")])
        keyboard.append([PanelButton("К автолайку", "reactions")])
        keyboard.extend(self._back_keyboard())
        return PanelView(text="\n".join(lines), keyboard=keyboard, action="reactions.folders")

    def _reaction_folder_detail(self, folder_id: int, *, feedback: str | None = None) -> PanelView:
        settings = self.state.get_reaction_folder_settings(folder_id)
        folder = self._reaction_folder_item(folder_id)
        if settings is None or folder is None:
            return self._reaction_folders(feedback="Папка не найдена. Обнови список папок.")
        channels = self.state.list_reaction_folder_channels(folder_id)
        channel_preview = [self._compact_button_title(str(channel.get("title") or channel["chat_id"])) for channel in channels[:5]]
        lines = [
            f"Папка: {folder['title']}",
            "",
            f"Автолайк: {'включен' if settings.enabled else 'выключен'}",
            f"Каналов: {folder['channel_count']}",
            f"Фильтр: {self._reaction_mode_label(settings.mode)}",
            f"Выбор: {self._reaction_strategy_label(settings.selection_strategy)}",
            f"Приоритет: {self._reaction_source_label(settings.reaction_source)}",
            f"Реакций на пост: {settings.max_reactions}",
            "При конфликте побеждает источник, который включили позже.",
        ]
        if channel_preview:
            lines.extend(["", "Каналы: " + ", ".join(channel_preview)])
            if len(channels) > len(channel_preview):
                lines.append(f"Еще: {len(channels) - len(channel_preview)}")
        if feedback:
            lines.extend(["", feedback])
        keyboard = [
            [
                PanelButton(
                    "Выключить папку" if settings.enabled else "Включить папку",
                    f"reactions.folder.toggle:{folder_id}",
                    style="danger" if settings.enabled else "success",
                )
            ],
            [
                PanelButton(
                    "1 реакция",
                    f"reactions.folder.max1:{folder_id}",
                    style="primary" if settings.max_reactions == 1 else None,
                ),
                PanelButton(
                    "3 реакции",
                    f"reactions.folder.max3:{folder_id}",
                    style="primary" if settings.max_reactions == 3 else None,
                ),
            ],
            [
                PanelButton(
                    "По порядку",
                    f"reactions.folder.strategy:{folder_id}:priority",
                    style="primary" if settings.selection_strategy == "priority" else None,
                ),
                PanelButton(
                    "Случайно",
                    f"reactions.folder.strategy:{folder_id}:random",
                    style="primary" if settings.selection_strategy == "random" else None,
                ),
            ],
            [
                PanelButton(
                    "Хорошие",
                    f"reactions.folder.mode:{folder_id}:positive",
                    style="primary" if settings.mode == "positive" else None,
                ),
                PanelButton(
                    "Плохие",
                    f"reactions.folder.mode:{folder_id}:negative",
                    style="primary" if settings.mode == "negative" else None,
                ),
                PanelButton(
                    "Все",
                    f"reactions.folder.mode:{folder_id}:all",
                    style="primary" if settings.mode == "all" else None,
                ),
            ],
            [
                PanelButton(
                    "Обычные",
                    f"reactions.folder.source:{folder_id}:standard",
                    style="primary" if settings.reaction_source == "standard" else None,
                ),
                PanelButton(
                    "Премиум",
                    f"reactions.folder.source:{folder_id}:premium",
                    style="primary" if settings.reaction_source == "premium" else None,
                ),
                PanelButton(
                    "Смесь",
                    f"reactions.folder.source:{folder_id}:mixed",
                    style="primary" if settings.reaction_source == "mixed" else None,
                ),
            ],
            [PanelButton("К папкам", "reactions.folders")],
            [PanelButton("Назад", "back")],
        ]
        return PanelView(text="\n".join(lines), keyboard=keyboard, action=f"reactions.folder:{folder_id}")

    def _reaction_folder_item(self, folder_id: int) -> dict[str, object] | None:
        for folder in self.state.list_reaction_folders():
            if int(folder["folder_id"]) == folder_id:
                return folder
        return None

    def _toggle_reaction_folder(self, action: str) -> PanelView:
        folder_id = self._parse_folder_id(action)
        if folder_id is None:
            return self._reaction_folders()
        settings = self.state.get_reaction_folder_settings(folder_id)
        if settings is None:
            return self._reaction_folders(feedback="Папка не найдена.")
        enabled = not bool(settings.enabled)
        self.state.set_reaction_folder_enabled(folder_id, enabled)
        return self._reaction_folder_detail(folder_id)

    def _set_reaction_folder_max(self, action: str, *, max_reactions: int) -> PanelView:
        folder_id = self._parse_folder_id(action)
        if folder_id is None:
            return self._reaction_folders()
        self.state.set_reaction_folder_max_reactions(folder_id, max_reactions)
        return self._reaction_folder_detail(folder_id)

    def _set_reaction_folder_mode(self, action: str) -> PanelView:
        parts = action.split(":")
        if len(parts) != 3:
            return self._reaction_folders()
        try:
            folder_id = int(parts[1])
        except ValueError:
            return self._reaction_folders()
        mode = parts[2]
        if mode not in VALID_REACTION_MODES - {"custom"}:
            return self._reaction_folder_detail(folder_id, feedback="Неизвестный фильтр.")
        self.state.set_reaction_folder_mode(folder_id, mode)
        return self._reaction_folder_detail(folder_id)

    def _set_reaction_folder_strategy(self, action: str) -> PanelView:
        parts = action.split(":")
        if len(parts) != 3:
            return self._reaction_folders()
        try:
            folder_id = int(parts[1])
        except ValueError:
            return self._reaction_folders()
        strategy = parts[2]
        if strategy not in VALID_REACTION_SELECTION_STRATEGIES:
            return self._reaction_folder_detail(folder_id, feedback="Неизвестный способ выбора.")
        self.state.set_reaction_folder_selection_strategy(folder_id, strategy)
        return self._reaction_folder_detail(folder_id)

    def _set_reaction_folder_source(self, action: str) -> PanelView:
        parts = action.split(":")
        if len(parts) != 3:
            return self._reaction_folders()
        try:
            folder_id = int(parts[1])
        except ValueError:
            return self._reaction_folders()
        source = parts[2]
        if source not in VALID_REACTION_SOURCES:
            return self._reaction_folder_detail(folder_id, feedback="Неизвестный источник реакций.")
        self.state.set_reaction_folder_source(folder_id, source)
        return self._reaction_folder_detail(folder_id)

    def _reaction_channels(
        self,
        *,
        page: int = 0,
        feedback: str | None = None,
        only_enabled: bool = False,
        query: str | None = None,
        mode_filter: str | None = None,
        source_filter: str | None = None,
    ) -> PanelView:
        all_channels = self._reaction_channel_items()
        enabled_count = sum(1 for channel in all_channels if channel["enabled"])
        inherited_count = sum(1 for channel in all_channels if channel.get("settings_source") == "folder" and channel["enabled"])
        folders = self.state.list_reaction_folders()
        enabled_folders = sum(1 for folder in folders if folder["enabled"])
        query = self._normalize_search_query(query)
        search_mode = query is not None
        mode_filter = mode_filter if mode_filter in VALID_REACTION_MODES - {"custom"} else None
        source_filter = source_filter if source_filter in VALID_REACTION_SOURCES else None
        channels = all_channels
        if search_mode:
            channels = [channel for channel in channels if self._matches_query(channel, query)]
        if only_enabled:
            channels = [channel for channel in channels if channel["enabled"]]
        if mode_filter:
            channels = [channel for channel in channels if channel["mode"] == mode_filter]
        if source_filter:
            channels = [channel for channel in channels if channel["reaction_source"] == source_filter]
        total_pages = max(1, (len(channels) + self.reaction_channel_page_size - 1) // self.reaction_channel_page_size)
        page = min(max(page, 0), total_pages - 1)
        page_channels = channels[page * self.reaction_channel_page_size : (page + 1) * self.reaction_channel_page_size]
        min_delay, max_delay = self.state.get_reaction_delay_range_seconds()
        autolike_enabled = self.state.is_reaction_autolike_enabled()
        lines = [
            "Автолайк ТГК",
            "",
            f"Автолайк: {'включен' if autolike_enabled else 'выключен'}",
            f"Включено каналов: {enabled_count}" + (f" (из папок: {inherited_count})" if inherited_count else ""),
            f"Папки: {enabled_folders}/{len(folders)}",
        ]
        if search_mode:
            lines.extend([f"Поиск: {query}", f"Найдено: {len(channels)}"])
        lines.extend(
            [
                f"Фильтр каналов: {'включенные' if only_enabled else 'все'}",
                f"Тип реакций: {self._reaction_mode_label(mode_filter) if mode_filter else 'любой'}",
                f"Приоритет: {self._reaction_source_label(source_filter) if source_filter else 'любой'}",
            ]
        )
        lines.extend(
            [
                f"Глобальный фильтр: {self._reaction_mode_label(self.state.get_reaction_global_mode())}",
                f"Пауза: {min_delay}-{max_delay} сек",
                f"Аккаунт Premium: {'да' if self.state.is_account_premium() else 'нет'}",
            ]
        )
        if feedback:
            lines.extend(["", feedback])
        if not channels:
            if search_mode:
                lines.extend(["", "Ничего не найдено. Уточни запрос или сбрось поиск."])
            elif only_enabled:
                lines.extend(["", "Пока нет каналов под выбранные фильтры. Открой общий список или сбрось фильтры."])
            else:
                lines.extend(["", "Пока нет каналов под выбранные фильтры."])

        keyboard = [
            [
                PanelButton(
                    (
                        f"{self._reaction_channel_state_icon(channel)} "
                        f"{self._compact_button_title(str(channel['title']))} · "
                        f"{self._reaction_mode_label(str(channel['mode']))} · "
                        f"{self._reaction_source_short_label(str(channel['reaction_source']))}"
                    ),
                    f"reactions.channel:{channel['chat_id']}:{page}",
                )
            ]
            for channel in page_channels
        ]
        if total_pages > 1:
            prev_page = max(page - 1, 0)
            next_page = min(page + 1, total_pages - 1)
            if search_mode:
                page_prefix = "reactions.search.page"
            elif only_enabled:
                page_prefix = "reactions.enabled.page"
            else:
                page_prefix = "reactions.page"
            keyboard.append(
                [
                    PanelButton("‹", f"{page_prefix}:{prev_page}"),
                    PanelButton(f"{page + 1}/{total_pages}", f"{page_prefix}:{page}"),
                    PanelButton("›", f"{page_prefix}:{next_page}"),
                ]
            )
        if search_mode:
            keyboard.append(
                [
                    PanelButton("Найти заново", "reactions.search"),
                    PanelButton("Сбросить поиск", "reactions.search.clear"),
                ]
            )
        else:
            keyboard.append([PanelButton("Найти канал", "reactions.search")])
        keyboard.append(
            [
                PanelButton(
                    "Все каналы",
                    "reactions.filter.enabled:any",
                    style="primary" if not only_enabled else None,
                ),
                PanelButton(
                    "Включенные",
                    "reactions.filter.enabled:on",
                    style="primary" if only_enabled else None,
                ),
            ]
        )
        keyboard.append(
            [
                PanelButton("Тип: любой", "reactions.filter.mode:any", style="primary" if mode_filter is None else None),
                PanelButton("Хорошие", "reactions.filter.mode:positive", style="primary" if mode_filter == "positive" else None),
                PanelButton("Плохие", "reactions.filter.mode:negative", style="primary" if mode_filter == "negative" else None),
                PanelButton("Все", "reactions.filter.mode:all", style="primary" if mode_filter == "all" else None),
            ]
        )
        keyboard.append(
            [
                PanelButton(
                    "Приоритет: любой",
                    "reactions.filter.source:any",
                    style="primary" if source_filter is None else None,
                ),
                PanelButton(
                    "Обычные",
                    "reactions.filter.source:standard",
                    style="primary" if source_filter == "standard" else None,
                ),
                PanelButton(
                    "Премиум",
                    "reactions.filter.source:premium",
                    style="primary" if source_filter == "premium" else None,
                ),
                PanelButton(
                    "Смесь",
                    "reactions.filter.source:mixed",
                    style="primary" if source_filter == "mixed" else None,
                ),
            ]
        )
        keyboard.append(
            [
                PanelButton(
                    "Хорошие",
                    "reactions.global.mode:positive",
                    style="primary" if self.state.get_reaction_global_mode() == "positive" else None,
                ),
                PanelButton(
                    "Плохие",
                    "reactions.global.mode:negative",
                    style="primary" if self.state.get_reaction_global_mode() == "negative" else None,
                ),
                PanelButton(
                    "Все",
                    "reactions.global.mode:all",
                    style="primary" if self.state.get_reaction_global_mode() == "all" else None,
                ),
            ]
        )
        keyboard.append(
            [
                PanelButton(
                    "Выключить автолайк" if autolike_enabled else "Включить автолайк",
                    "reactions.toggle",
                )
            ]
        )
        keyboard.append(
            [
                PanelButton("Папки", "reactions.folders"),
                PanelButton("Пауза", "reactions.delay"),
                PanelButton("История", "reactions.history"),
            ]
        )
        keyboard.append([PanelButton("Ввести channel_id", "reactions.channel.add")])
        keyboard.extend(self._back_keyboard())
        if search_mode:
            action = f"reactions.search.page:{page}"
        elif only_enabled:
            action = f"reactions.enabled.page:{page}"
        else:
            action = "reactions"
        return PanelView(text="\n".join(lines), keyboard=keyboard, action=action)

    def _reaction_history(self, *, feedback: str | None = None) -> PanelView:
        enabled_count = sum(1 for channel in self._reaction_channel_items() if channel["enabled"])
        lines = [
            "История автолайка",
            "",
            f"Каналов для массового запуска: {enabled_count}",
            "Ритм истории: 8-15 сек на пост",
            "Массовый запуск берет только включенные realtime-каналы.",
            "Выключенный канал можно обработать из карточки канала.",
            "Берет текущие настройки каждого канала.",
            "Лимит считается по новым пригодным постам.",
            "Сервисные события, повторы альбомов и уже обработанные не съедают лимит.",
        ]
        if feedback:
            lines.extend(["", feedback])
        return PanelView(
            text="\n".join(lines),
            keyboard=[
                [PanelButton("1000", "rhb:all:1000"), PanelButton("2000", "rhb:all:2000")],
                [
                    PanelButton("5000", "rhb:all:5000"),
                    PanelButton("Все посты", "rhb:all:all"),
                ],
                [PanelButton("К автолайку", "reactions")],
                [PanelButton("Назад", "back")],
            ],
            action="reactions.history",
        )

    def _reaction_channel_history(self, chat_id: int, *, page: int = 0, feedback: str | None = None) -> PanelView:
        title = self._known_reaction_channel_title(chat_id)
        settings = self.state.get_effective_reaction_channel_settings(chat_id)
        if settings is None:
            return self._reaction_channel_detail(chat_id, page=page, feedback="Сначала включи автолайк для канала или папки.")
        effective_source = self.state.get_effective_reaction_channel_settings_source(chat_id)
        settings_source = "канал" if effective_source == "channel" else "папка"
        lines = [
            f"История канала: {title}",
            "",
            f"Автолайк: {'включен' if settings.enabled else 'выключен'}",
            f"Источник настройки: {settings_source}",
            f"Фильтр: {self._reaction_mode_label(settings.mode)}",
            f"Выбор: {self._reaction_strategy_label(settings.selection_strategy)}",
            f"Приоритет: {self._reaction_source_label(settings.reaction_source)}",
            f"Реакций на пост: {settings.max_reactions}",
            "Ритм истории: 8-15 сек на пост",
            "Лимит считается по новым пригодным постам.",
            "Сервисные события, повторы альбомов и уже обработанные не съедают лимит.",
        ]
        if feedback:
            lines.extend(["", feedback])
        return PanelView(
            text="\n".join(lines),
            keyboard=[
                [
                    PanelButton("1000", f"rhb:ch:{chat_id}:1000:{page}"),
                    PanelButton("2000", f"rhb:ch:{chat_id}:2000:{page}"),
                ],
                [
                    PanelButton("5000", f"rhb:ch:{chat_id}:5000:{page}"),
                    PanelButton("Все посты", f"rhb:ch:{chat_id}:all:{page}"),
                ],
                [PanelButton("К настройкам", f"reactions.channel:{chat_id}:{page}")],
                [PanelButton("К каналам", f"reactions.page:{page}")],
                [PanelButton("Назад", "back")],
            ],
            action=f"reactions.channel.history:{chat_id}:{page}",
        )

    def _reaction_channel_items(self) -> list[dict[str, object]]:
        channels: dict[int, dict[str, object]] = {}
        ordered_chat_ids: list[int] = []
        for chat in self.state.list_known_chats(kind="channel"):
            chat_id = int(chat["chat_id"])
            ordered_chat_ids.append(chat_id)
            settings = self.state.get_reaction_channel_settings(chat_id)
            effective_settings = self.state.get_effective_reaction_channel_settings(chat_id)
            display_settings = effective_settings or settings
            mode = str(display_settings.mode) if display_settings else self.state.get_reaction_global_mode()
            reaction_source = str(display_settings.reaction_source) if display_settings else "mixed"
            source = self.state.get_effective_reaction_channel_settings_source(chat_id) or "none"
            channels[chat_id] = {
                "chat_id": chat_id,
                "title": str(chat.get("title") or chat_id),
                "kind": "channel",
                "enabled": bool(display_settings.enabled) if display_settings else False,
                "mode": mode,
                "reaction_source": reaction_source,
                "settings_source": source,
            }
        for channel in self.state.list_reaction_channels():
            chat_id = int(channel["chat_id"])
            if chat_id not in channels:
                ordered_chat_ids.append(chat_id)
            known_title = channels.get(chat_id, {}).get("title")
            settings = self.state.get_reaction_channel_settings(chat_id)
            effective_settings = self.state.get_effective_reaction_channel_settings(chat_id)
            display_settings = effective_settings or settings
            mode = str(display_settings.mode) if display_settings else str(channel["mode"])
            reaction_source = (
                str(display_settings.reaction_source)
                if display_settings
                else str(channel["reaction_source"])
            )
            source = (
                self.state.get_effective_reaction_channel_settings_source(chat_id)
                or ("channel" if settings is not None else "none")
            )
            channels[chat_id] = {
                "chat_id": chat_id,
                "title": str(channel.get("title") or known_title or chat_id),
                "kind": "channel",
                "enabled": bool(display_settings.enabled) if display_settings else bool(channel["enabled"]),
                "mode": mode,
                "reaction_source": reaction_source,
                "settings_source": source,
            }
        return [channels[chat_id] for chat_id in ordered_chat_ids]

    def _reaction_channel_detail(self, chat_id: int, *, page: int = 0, feedback: str | None = None) -> PanelView:
        title = self._known_reaction_channel_title(chat_id)
        explicit_settings = self.state.get_reaction_channel_settings(chat_id)
        settings = self.state.get_effective_reaction_channel_settings(chat_id)
        if settings is None:
            self.state.upsert_reaction_channel(chat_id, title)
            settings = self.state.get_reaction_channel_settings(chat_id)
            explicit_settings = settings
        effective_source = self.state.get_effective_reaction_channel_settings_source(chat_id)
        settings_source = "канал" if effective_source == "channel" else "папка"
        folder_title = settings.title if effective_source == "folder" else None
        mode_label = self._reaction_mode_label(settings.mode)
        strategy_label = self._reaction_strategy_label(settings.selection_strategy)
        source_label = self._reaction_source_label(settings.reaction_source)
        observed_count = len(self.state.list_reaction_channel_available_reactions(chat_id))
        available_checked = self.state.has_reaction_channel_available_reactions_checked(chat_id)
        available_line = (
            f"Доступных реакций: {observed_count}"
            if available_checked
            else "Доступные реакции: еще не проверялись"
        )
        lines = [
            f"Канал: {title}",
            "",
            f"Автолайк: {'включен' if settings.enabled else 'выключен'}",
            f"Источник настройки: {settings_source}",
            f"Фильтр: {mode_label}",
            f"Выбор: {strategy_label}",
            f"Приоритет: {source_label}",
            f"Реакций на пост: {settings.max_reactions}",
            available_line,
            f"Аккаунт Premium: {'да' if self.state.is_account_premium() else 'нет'}",
        ]
        if folder_title:
            lines.append(f"Папка: {folder_title}")
            lines.append("Ручная настройка канала станет активной, если включить ее позже папки.")
        if not available_checked:
            lines.append("Открой канал в панели, нажми «Обновить доступные» или дождись первой обработки нового поста.")
        if feedback:
            lines.extend(["", feedback])
        if explicit_settings is None:
            keyboard = [
                [
                    PanelButton("Настроить вручную", f"reactions.channel.customize:{chat_id}:{page}", style="primary")
                ],
                [
                    PanelButton(
                        "Выключить для канала" if settings.enabled else "Включить для канала",
                        f"reactions.channel.toggle:{chat_id}:{page}",
                        style="danger" if settings.enabled else "success",
                    )
                ],
                [PanelButton("История", f"reactions.channel.history:{chat_id}:{page}")],
                [PanelButton("К папкам", "reactions.folders"), PanelButton("К каналам", f"reactions.page:{page}")],
                [PanelButton("Назад", "back")],
            ]
            return PanelView(text="\n".join(lines), keyboard=keyboard, action=f"reactions.channel:{chat_id}:{page}")

        keyboard = [
            [
                PanelButton(
                    "Выключить" if settings.enabled else "Включить",
                    f"reactions.channel.toggle:{chat_id}:{page}",
                    style="danger" if settings.enabled else "success",
                )
            ],
            [
                PanelButton(
                    "1 реакция",
                    f"reactions.channel.max1:{chat_id}:{page}",
                    style="primary" if settings.max_reactions == 1 else None,
                ),
                PanelButton(
                    "3 реакции",
                    f"reactions.channel.max3:{chat_id}:{page}",
                    style="primary" if settings.max_reactions == 3 else None,
                ),
            ],
            [
                PanelButton(
                    "По порядку",
                    f"reactions.channel.strategy:{chat_id}:priority:{page}",
                    style="primary" if settings.selection_strategy == "priority" else None,
                ),
                PanelButton(
                    "Случайно",
                    f"reactions.channel.strategy:{chat_id}:random:{page}",
                    style="primary" if settings.selection_strategy == "random" else None,
                ),
            ],
            [
                PanelButton(
                    "Хорошие",
                    f"reactions.channel.mode:{chat_id}:positive:{page}",
                    style="primary" if settings.mode == "positive" else None,
                ),
                PanelButton(
                    "Плохие",
                    f"reactions.channel.mode:{chat_id}:negative:{page}",
                    style="primary" if settings.mode == "negative" else None,
                ),
                PanelButton(
                    "Все",
                    f"reactions.channel.mode:{chat_id}:all:{page}",
                    style="primary" if settings.mode == "all" else None,
                ),
            ],
            [
                PanelButton(
                    "Обычные",
                    f"reactions.channel.source:{chat_id}:standard:{page}",
                    style="primary" if settings.reaction_source == "standard" else None,
                ),
                PanelButton(
                    "Премиум",
                    f"reactions.channel.source:{chat_id}:premium:{page}",
                    style="primary" if settings.reaction_source == "premium" else None,
                ),
                PanelButton(
                    "Смесь",
                    f"reactions.channel.source:{chat_id}:mixed:{page}",
                    style="primary" if settings.reaction_source == "mixed" else None,
                ),
            ],
            [
                PanelButton("Реакции", f"reactions.channel.emojis:{chat_id}:0"),
                PanelButton("Обновить доступные", f"rcr:{chat_id}:{page}"),
            ],
            [PanelButton("История", f"reactions.channel.history:{chat_id}:{page}")],
            [PanelButton("К каналам", f"reactions.page:{page}")],
            [PanelButton("Назад", "back")],
        ]
        return PanelView(text="\n".join(lines), keyboard=keyboard, action=f"reactions.channel:{chat_id}:{page}")

    def _known_reaction_channel_title(self, chat_id: int) -> str:
        for channel in self._reaction_channel_items():
            if int(channel["chat_id"]) == chat_id:
                return str(channel["title"])
        return str(chat_id)

    def _reaction_folder_title_for_channel(self, chat_id: int) -> str | None:
        settings = self.state.get_effective_reaction_channel_settings(chat_id)
        return str(settings.title) if settings is not None else None

    def _create_reaction_channel_override(self, chat_id: int, *, enabled: bool | None = None) -> None:
        title = self._known_reaction_channel_title(chat_id)
        base_settings = self.state.get_effective_reaction_channel_settings(chat_id)
        self.state.upsert_reaction_channel(chat_id, title)
        if base_settings is not None:
            self.state.set_reaction_channel_mode(chat_id, base_settings.mode)
            self.state.set_reaction_channel_max_reactions(chat_id, base_settings.max_reactions)
            self.state.set_reaction_channel_selection_strategy(chat_id, base_settings.selection_strategy)
            self.state.set_reaction_channel_source(chat_id, base_settings.reaction_source)
        if enabled is None:
            enabled = bool(base_settings.enabled) if base_settings is not None else True
        self.state.set_reaction_channel_enabled(chat_id, enabled)

    def _toggle_reaction_channel(self, action: str) -> PanelView:
        parsed = self._parse_channel_action(action, "reactions.channel.toggle")
        if parsed is None:
            return self._reaction_channels()
        chat_id, page = parsed
        title = self._known_reaction_channel_title(chat_id)
        settings = self.state.get_reaction_channel_settings(chat_id)
        if settings is None:
            inherited = self.state.get_effective_reaction_channel_settings(chat_id)
            enabled = not bool(inherited.enabled) if inherited is not None else True
            self._create_reaction_channel_override(chat_id, enabled=enabled)
            return self._reaction_channels(page=page, feedback=f"{'Включено' if enabled else 'Выключено'}: {title}")
        enabled = not bool(settings.enabled)
        self.state.set_reaction_channel_enabled(chat_id, enabled)
        return self._reaction_channels(page=page, feedback=f"{'Включено' if enabled else 'Выключено'}: {title}")

    def _set_reaction_channel_max(self, action: str, *, max_reactions: int) -> PanelView:
        prefix = f"reactions.channel.max{max_reactions}"
        parsed = self._parse_channel_action(action, prefix)
        if parsed is None:
            return self._reaction_channels()
        chat_id, page = parsed
        self.state.set_reaction_channel_max_reactions(chat_id, max_reactions)
        return self._reaction_channel_detail(chat_id, page=page)

    def _set_reaction_channel_mode(self, action: str) -> PanelView:
        parts = action.split(":")
        if len(parts) != 4:
            return self._reaction_channels()
        try:
            chat_id = int(parts[1])
            mode = parts[2]
            page = int(parts[3])
        except ValueError:
            return self._reaction_channels()
        if mode not in VALID_REACTION_MODES:
            return self._reaction_channel_detail(chat_id, page=page, feedback="Неизвестный фильтр.")
        self.state.set_reaction_channel_mode(chat_id, mode)
        return self._reaction_channel_detail(chat_id, page=page)

    def _set_reaction_channel_strategy(self, action: str) -> PanelView:
        parts = action.split(":")
        if len(parts) != 4:
            return self._reaction_channels()
        try:
            chat_id = int(parts[1])
            strategy = parts[2]
            page = int(parts[3])
        except ValueError:
            return self._reaction_channels()
        if strategy not in VALID_REACTION_SELECTION_STRATEGIES:
            return self._reaction_channel_detail(chat_id, page=page, feedback="Неизвестный способ выбора.")
        self.state.set_reaction_channel_selection_strategy(chat_id, strategy)
        return self._reaction_channel_detail(chat_id, page=page)

    def _set_reaction_channel_source(self, action: str) -> PanelView:
        parts = action.split(":")
        if len(parts) != 4:
            return self._reaction_channels()
        try:
            chat_id = int(parts[1])
            source = parts[2]
            page = int(parts[3])
        except ValueError:
            return self._reaction_channels()
        if source not in VALID_REACTION_SOURCES:
            return self._reaction_channel_detail(chat_id, page=page, feedback="Неизвестный источник реакций.")
        self.state.set_reaction_channel_source(chat_id, source)
        return self._reaction_channel_detail(chat_id, page=page)

    def _reaction_emoji_picker(self, chat_id: int, *, page: int = 0) -> PanelView:
        settings = self.state.get_reaction_channel_settings(chat_id)
        disabled = set(settings.disabled_emojis if settings else ())
        reactions = self._reaction_picker_items(chat_id)
        total_pages = max(1, (len(reactions) + self.reaction_emoji_page_size - 1) // self.reaction_emoji_page_size)
        page = min(max(page, 0), total_pages - 1)
        page_reactions = reactions[
            page * self.reaction_emoji_page_size : (page + 1) * self.reaction_emoji_page_size
        ]
        keyboard = [
            [
                PanelButton(
                    f"{'❌' if reaction['emoji'] in disabled else '✅'} {self._reaction_picker_label(reaction)}",
                    f"rce:{chat_id}:{reaction['emoji']}:{page}",
                    style=None if reaction["emoji"] in disabled else "primary",
                    icon_custom_emoji_id=self._reaction_button_icon(reaction),
                ),
                PanelButton(
                    f"Кат: {self._reaction_category_label(reaction.get('category', 'neutral'))}",
                    f"rcc:{chat_id}:{reaction['emoji']}:{page}",
                    style=self._reaction_category_style(reaction.get("category", "neutral")),
                ),
            ]
            for reaction in page_reactions
        ]
        if not reactions and self.state.has_reaction_channel_available_reactions_checked(chat_id):
            keyboard.append([PanelButton("Обновить доступные", f"rcr:{chat_id}:0", style="primary")])
        if total_pages > 1:
            prev_page = max(page - 1, 0)
            next_page = min(page + 1, total_pages - 1)
            keyboard.append(
                [
                    PanelButton("‹", f"reactions.channel.emojis:{chat_id}:{prev_page}"),
                    PanelButton(f"{page + 1}/{total_pages}", f"reactions.channel.emojis:{chat_id}:{page}"),
                    PanelButton("›", f"reactions.channel.emojis:{chat_id}:{next_page}"),
                ]
            )
        keyboard.append([PanelButton("К каналу", f"reactions.channel:{chat_id}:0")])
        keyboard.append([PanelButton("Назад", "back")])
        text_lines = [
            "Выбор реакций",
            "",
            "Все показанные реакции включены по умолчанию. Нажатие отключает или возвращает реакцию.",
        ]
        if not reactions and self.state.has_reaction_channel_available_reactions_checked(chat_id):
            text_lines.extend(
                [
                    "",
                    "Доступные реакции не найдены. Обнови доступные реакции или проверь настройки реакций в канале.",
                ]
            )
        return PanelView(
            text="\n".join(text_lines),
            keyboard=keyboard,
            action=f"reactions.channel.emojis:{chat_id}:{page}",
        )

    def _reaction_picker_items(self, chat_id: int) -> list[dict[str, str]]:
        observed = self.state.list_reaction_channel_available_reactions(chat_id)
        if observed or self.state.has_reaction_channel_available_reactions_checked(chat_id):
            return observed
        return [
            {"emoji": emoji, "kind": "emoji", "category": reaction_category(emoji)}
            for emoji in DEFAULT_REACTION_EMOJIS
        ]

    @staticmethod
    def _reaction_picker_label(reaction: dict[str, str]) -> str:
        emoji = str(reaction["emoji"])
        if reaction.get("kind") == "custom":
            return f"⭐ {emoji[:8]}…"
        return emoji

    @staticmethod
    def _reaction_button_icon(reaction: dict[str, str]) -> str | None:
        if reaction.get("kind") == "custom":
            return str(reaction["emoji"])
        return None

    def _toggle_reaction_channel_emoji(self, action: str) -> PanelView:
        parts = action.split(":")
        if action.startswith("reactions.channel.emoji:"):
            if len(parts) != 4:
                return self._reaction_channels()
            chat_id_index = 1
        else:
            if len(parts) != 4:
                return self._reaction_channels()
            chat_id_index = 1
        try:
            chat_id = int(parts[chat_id_index])
            emoji = parts[chat_id_index + 1]
            page = int(parts[chat_id_index + 2])
        except ValueError:
            return self._reaction_channels()
        self.state.toggle_reaction_channel_emoji(chat_id, emoji)
        return self._reaction_emoji_picker(chat_id, page=page)

    def _cycle_reaction_channel_emoji_category(self, action: str) -> PanelView:
        parts = action.split(":")
        if len(parts) != 4:
            return self._reaction_channels()
        try:
            chat_id = int(parts[1])
            emoji = parts[2]
            page = int(parts[3])
        except ValueError:
            return self._reaction_channels()
        self.state.cycle_reaction_channel_emoji_category(chat_id, emoji)
        return self._reaction_emoji_picker(chat_id, page=page)

    def _handle_reaction_channel_text(self, text: str) -> PanelView:
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return PanelView(
                text="Не понял channel_id. Пример: -1001234567890 Новости",
                keyboard=self._retry_keyboard("reactions.channel.add"),
                input_state="reaction_channel",
                action="reactions.channel.add",
            )
        try:
            chat_id = int(parts[0])
        except ValueError:
            return PanelView(
                text="channel_id должен быть числом. Пример: -1001234567890 Новости",
                keyboard=self._retry_keyboard("reactions.channel.add"),
                input_state="reaction_channel",
                action="reactions.channel.add",
            )
        title = parts[1] if len(parts) > 1 else None
        self.state.upsert_reaction_channel(chat_id, title)
        return self._reaction_channel_detail(chat_id, feedback=f"Канал добавлен: {title or chat_id}")

    def _handle_reaction_delay_text(self, text: str) -> PanelView:
        normalized = text.strip().replace("–", "-").replace("—", "-").replace(",", " ")
        parts = [part for part in normalized.replace("-", " ").split() if part]
        try:
            if len(parts) == 1:
                minimum = maximum = int(parts[0])
            elif len(parts) == 2:
                minimum, maximum = int(parts[0]), int(parts[1])
            else:
                raise ValueError
            self.state.set_reaction_delay_range_seconds(minimum, maximum)
        except ValueError:
            return PanelView(
                text="Не понял диапазон. Пример: 240-900",
                keyboard=self._retry_keyboard("reactions.delay"),
                input_state="reaction_delay",
                action="reactions.delay",
            )
        return self._reaction_channels(feedback=f"Пауза обновлена: {minimum}-{maximum} сек")

    def _status(self) -> PanelView:
        enabled = self.state.is_feature_enabled(VOICE_FEATURE)
        decoration_enabled = self.state.is_transcription_decoration_enabled()
        blocked = self.state.list_blocked_chats()
        groups = self.state.list_allowed_groups()
        reaction_channels = [channel for channel in self._reaction_channel_items() if channel["enabled"]]
        keyboard = [
            [PanelButton("Обновить", "status.refresh")],
            [PanelButton("Транскрибация", "transcription"), PanelButton("Автолайк ТГК", "reactions")],
            *([[PanelButton("Экспорт чатов", "export.chats")]] if self.chat_export_enabled else []),
            [PanelButton("Помощь", "help")],
            [PanelButton("Назад", "back")],
        ]
        text = "\n".join(
            [
                "Статус",
                "",
                "Manager bot: работает",
                "User client: неизвестно",
                "Очередь ГС: неизвестно",
                f"Транскрибация: {'включена' if enabled else 'выключена'}",
                f"Смайлы: {'включены' if decoration_enabled else 'выключены'}",
                f"Исключения: {len(blocked)}",
                f"Группы: {len(groups)}",
                f"Автолайк ТГК: {len(reaction_channels)} каналов",
                f"Premium: {'да' if self.state.is_account_premium() else 'нет'}",
                f"Экспорт чатов: {'доступен' if self.chat_export_enabled else 'недоступен'}",
                self._llm_status_line(),
                "Последняя ошибка: нет данных",
            ]
        )
        return PanelView(text=text, keyboard=keyboard, action="status")

    def _llm_status_line(self) -> str:
        provider_label = {
            "copilot": "GitHub Copilot CLI",
            "openai": "OpenAI",
            "anthropic": "Anthropic",
        }.get(self.llm_provider, self.llm_provider or "—")
        if self.llm_model:
            return f"LLM: {provider_label} ({self.llm_model})"
        return f"LLM: {provider_label}"

    def _help(self) -> PanelView:
        lines = [
            "Помощь",
            "",
            "Транскрибация обрабатывает голосовые в выбранных чатах.",
            "Личные чаты включаются по лимиту или вручную.",
            "Группы включаются только явно. Каналы не транскрибируются.",
            "Автолайк ТГК ставит реакции от аккаунта с паузой.",
        ]
        if self.chat_export_enabled:
            lines.extend(
                [
                    "Экспорт чатов отправляет .txt с выбранным объемом истории.",
                ]
            )
        lines.extend(["", "Если что-то не работает, открой Статус."])
        keyboard = [
            [PanelButton("Транскрибация", "transcription")],
            [PanelButton("Чаты", "transcription.chats"), PanelButton("Группы", "transcription.groups")],
            [PanelButton("Автолайк ТГК", "reactions")],
            *([[PanelButton("Экспорт чатов", "export.chats")]] if self.chat_export_enabled else []),
            [PanelButton("Статус", "status")],
            [PanelButton("Назад", "back")],
        ]
        return PanelView(
            text="\n".join(lines),
            keyboard=keyboard,
            action="help",
        )

    def _handle_block_chat_text(self, text: str) -> PanelView:
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return PanelView(
                text="Не понял chat_id. Пример: 123456789 Иван",
                keyboard=self._retry_keyboard("transcription.blacklist.add"),
                input_state="block_chat",
                action="transcription.blacklist.add",
            )
        try:
            chat_id = int(parts[0])
        except ValueError:
            return PanelView(
                text="chat_id должен быть числом. Пример: 123456789 Иван",
                keyboard=self._retry_keyboard("transcription.blacklist.add"),
                input_state="block_chat",
                action="transcription.blacklist.add",
            )
        title = parts[1] if len(parts) > 1 else None
        self.state.block_chat(chat_id, title)
        return PanelView(
            text=f"Чат {chat_id} добавлен в исключения.",
            keyboard=[[PanelButton("Исключения", "transcription.blacklist")], *self._back_keyboard()],
            action="transcription.blacklist",
        )

    def _handle_unblock_chat_text(self, text: str) -> PanelView:
        try:
            chat_id = int(text.strip())
        except ValueError:
            return PanelView(
                text="chat_id должен быть числом.",
                keyboard=self._retry_keyboard("transcription.blacklist.remove"),
                input_state="unblock_chat",
                action="transcription.blacklist.remove",
            )
        self.state.unblock_chat(chat_id)
        return PanelView(
            text=f"Чат {chat_id} убран из исключений.",
            keyboard=[[PanelButton("Исключения", "transcription.blacklist")], *self._back_keyboard()],
            action="transcription.blacklist",
        )

    def _handle_allow_group_text(self, text: str) -> PanelView:
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return PanelView(
                text="Не понял chat_id группы. Пример: -1001234567890 Рабочий чат",
                keyboard=self._retry_keyboard("transcription.groups.add"),
                input_state="allow_group",
                action="transcription.groups.add",
            )
        try:
            chat_id = int(parts[0])
        except ValueError:
            return PanelView(
                text="chat_id группы должен быть числом. Пример: -1001234567890 Рабочий чат",
                keyboard=self._retry_keyboard("transcription.groups.add"),
                input_state="allow_group",
                action="transcription.groups.add",
            )
        title = parts[1] if len(parts) > 1 else None
        self.state.upsert_known_group(chat_id, title)
        self.state.allow_group(chat_id, title)
        return self._groups_view(feedback=f"Включено: {title or chat_id}")

    def _handle_disallow_group_text(self, text: str) -> PanelView:
        try:
            chat_id = int(text.strip())
        except ValueError:
            return PanelView(
                text="chat_id группы должен быть числом.",
                keyboard=self._retry_keyboard("transcription.groups.remove"),
                input_state="disallow_group",
                action="transcription.groups.remove",
            )
        self.state.disallow_group(chat_id)
        return PanelView(
            text=f"Группа {chat_id} выключена.",
            keyboard=[[PanelButton("Группы", "transcription.groups")], *self._back_keyboard()],
            action="transcription.groups",
        )

    def _handle_prompt_text(self, text: str) -> PanelView:
        prompt = text.strip()
        if len(prompt) < 20:
            return PanelView(
                text="Промпт слишком короткий. Пришли полноценную инструкцию для редактирования текста.",
                keyboard=self._retry_keyboard("transcription.prompt.edit"),
                input_state="text_polish_prompt",
                action="transcription.prompt.edit",
            )
        self.state.set_text_polish_prompt(prompt)
        return PanelView(text="Промпт обновлен.\n\n" + self._prompt_text(), keyboard=self._prompt_keyboard(), action="transcription.prompt")

    def _handle_private_min_messages_text(self, text: str) -> PanelView:
        try:
            minimum_messages = int(text.strip())
            self.state.set_private_chat_min_messages(minimum_messages)
        except ValueError:
            return PanelView(
                text="Лимит должен быть положительным числом.\n\nПример: 250",
                keyboard=self._retry_keyboard("transcription.settings.private_limit"),
                input_state="private_min_messages",
                action="transcription.settings.private_limit",
            )
        return self._transcription_settings()

    def _handle_voice_min_duration_text(self, text: str) -> PanelView:
        try:
            seconds = int(text.strip())
            self.state.set_voice_min_duration_seconds(seconds)
        except ValueError:
            return PanelView(
                text="Минимальная длина должна быть числом от 0.\n\nПример: 12",
                keyboard=self._retry_keyboard("transcription.settings.voice_min_duration"),
                input_state="voice_min_duration",
                action="transcription.settings.voice_min_duration",
            )
        return self._transcription_settings()

    def _is_owner(self, user_id: int) -> bool:
        return user_id == self.owner_id

    @staticmethod
    def _denied() -> PanelView:
        return PanelView(text="Доступ запрещен.", keyboard=[])

    @staticmethod
    def _back_keyboard() -> list[list[PanelButton]]:
        return [[PanelButton("Назад", "back")]]

    @classmethod
    def _retry_keyboard(cls, retry_action: str) -> list[list[PanelButton]]:
        return [[PanelButton("Повторить", retry_action)], *cls._back_keyboard()]

    @staticmethod
    def _normalize_action(action: str) -> str:
        aliases = {
            "transcription.whitelist": "transcription.blacklist",
            "transcription.whitelist.add": "transcription.blacklist.add",
            "transcription.whitelist.remove": "transcription.blacklist.remove",
        }
        return aliases.get(action, action)

    @staticmethod
    def _parse_page(action: str) -> int:
        try:
            return int(action.rsplit(":", 1)[1])
        except ValueError:
            return 0

    @staticmethod
    def _parse_channel_action(action: str, prefix: str) -> tuple[int, int] | None:
        parts = action.split(":")
        if len(parts) != 3 or parts[0] != prefix:
            return None
        try:
            return int(parts[1]), int(parts[2])
        except ValueError:
            return None

    @staticmethod
    def _parse_folder_id(action: str) -> int | None:
        parts = action.split(":")
        if len(parts) < 2:
            return None
        try:
            return int(parts[1])
        except ValueError:
            return None

    @staticmethod
    def _chat_kind_prefix(kind: str) -> str:
        return {
            "private": "[ЛС]",
            "group": "[ГР]",
            "chat": "[ЧАТ]",
        }.get(kind, "[ЧАТ]")

    @staticmethod
    def _reaction_mode_label(mode: str) -> str:
        return {
            "positive": "хорошие",
            "negative": "плохие",
            "all": "все",
            "custom": "свои",
        }.get(mode, mode)

    @staticmethod
    def _reaction_strategy_label(strategy: str) -> str:
        return {
            "priority": "по порядку",
            "random": "случайно",
        }.get(strategy, strategy)

    @staticmethod
    def _reaction_source_label(source: str) -> str:
        return {
            "mixed": "смесь",
            "standard": "обычные",
            "premium": "премиум",
        }.get(source, "смесь")

    @staticmethod
    def _reaction_source_short_label(source: str) -> str:
        return {
            "mixed": "смесь",
            "standard": "обычные",
            "premium": "премиум",
        }.get(source, "смесь")

    @staticmethod
    def _reaction_channel_state_icon(channel: dict[str, object]) -> str:
        if channel.get("settings_source") == "folder" and channel.get("enabled"):
            return "📁"
        return "✅" if channel.get("enabled") else "❌"

    @staticmethod
    def _reaction_channel_count_label(count: int) -> str:
        if count % 10 == 1 and count % 100 != 11:
            noun = "канал"
        elif count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
            noun = "канала"
        else:
            noun = "каналов"
        return f"{count} {noun}"

    @staticmethod
    def _reaction_category_label(category: str) -> str:
        return {
            "positive": "+",
            "negative": "-",
            "neutral": "?",
        }.get(category, "?")

    @staticmethod
    def _reaction_category_style(category: str) -> str:
        return {
            "positive": "success",
            "negative": "danger",
            "neutral": "primary",
        }.get(category, "primary")
