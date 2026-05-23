from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from telepath.manager import ManagerService


VOICE_FEATURE = "voice_transcription"


@dataclass(frozen=True)
class PanelButton:
    text: str
    action: str


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
    def is_feature_enabled(self, feature: str) -> bool: ...
    def set_feature_enabled(self, feature: str, enabled: bool) -> None: ...
    def is_transcription_decoration_enabled(self) -> bool: ...
    def set_transcription_decoration_enabled(self, enabled: bool) -> None: ...
    def get_text_polish_prompt(self) -> str: ...
    def set_text_polish_prompt(self, prompt: str) -> None: ...
    def reset_text_polish_prompt(self) -> None: ...


class ControlPanelService:
    group_page_size = 8

    def __init__(
        self,
        *,
        owner_id: int,
        state: AssistantState,
        llm_provider: str = "copilot",
        llm_model: str | None = None,
    ):
        self.owner_id = owner_id
        self.state = state
        self.llm_provider = llm_provider
        self.llm_model = llm_model
        self.command_service = ManagerService(owner_id=owner_id, blacklist=state)

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
                "Личные чаты: все, кроме исключений.",
                "Группы: только выбранные.",
                "Изменения применяются сразу, без перезапуска worker.",
            ]
        )
        return PanelView(
            text=text,
            keyboard=[
                [PanelButton("Транскрибация", "transcription")],
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
        if action == "status":
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
        if action == "transcription.groups":
            return self._groups()
        if action.startswith("transcription.groups.page:"):
            return self._groups_view(page=self._parse_page(action))
        if action.startswith("transcription.groups.toggle:"):
            return self._toggle_group(action)
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
                "Личные чаты: включены по умолчанию",
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
                [PanelButton("Исключения", "transcription.blacklist"), PanelButton("Группы", "transcription.groups")],
                [PanelButton("Промпт", "transcription.prompt")],
                [PanelButton("Статус", "status"), PanelButton("Помощь", "help")],
                *self._back_keyboard(),
            ],
            action="transcription",
        )

    def _blacklist(self) -> PanelView:
        chats = self.state.list_blocked_chats()
        if chats:
            lines = ["Исключения:"]
            for chat in chats:
                title = chat["title"] or "(no title)"
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

    def _groups(self) -> PanelView:
        return self._groups_view()

    def _groups_view(self, *, page: int = 0, feedback: str | None = None) -> PanelView:
        groups = self._group_items()
        selected_count = sum(1 for group in groups if group["allowed"])
        total_pages = max(1, (len(groups) + self.group_page_size - 1) // self.group_page_size)
        page = min(max(page, 0), total_pages - 1)
        page_groups = groups[page * self.group_page_size : (page + 1) * self.group_page_size]
        lines = [
            "Группы",
            "",
            f"Выбрано: {selected_count}",
            "Нажми на группу, чтобы включить или выключить транскрибацию.",
        ]
        if feedback:
            lines.extend(["", feedback])
        if not groups:
            lines = [
                "Группы",
                "",
                "Пока нет найденных групп.",
                "Список пополняется автоматически из Telegram аккаунта.",
            ]

        keyboard = [
            [PanelButton(f"{'✅' if group['allowed'] else '○'} {group['title']}", f"transcription.groups.toggle:{group['chat_id']}:{page}")]
            for group in page_groups
        ]
        if total_pages > 1:
            prev_page = max(page - 1, 0)
            next_page = min(page + 1, total_pages - 1)
            keyboard.append(
                [
                    PanelButton("‹", f"transcription.groups.page:{prev_page}"),
                    PanelButton(f"{page + 1}/{total_pages}", f"transcription.groups.page:{page}"),
                    PanelButton("›", f"transcription.groups.page:{next_page}"),
                ]
            )
        keyboard.append([PanelButton("Ввести chat_id", "transcription.groups.add")])
        keyboard.extend(self._back_keyboard())
        return PanelView(
            text="\n".join(lines),
            keyboard=keyboard,
            action="transcription.groups",
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

    def _toggle_group(self, action: str) -> PanelView:
        parts = action.split(":")
        if len(parts) < 3:
            return self._groups()
        try:
            chat_id = int(parts[1])
            page = int(parts[2])
        except ValueError:
            return self._groups()
        title = self._known_group_title(chat_id)
        if self.state.is_group_allowed(chat_id):
            self.state.disallow_group(chat_id)
            feedback = f"Выключено: {title}"
        else:
            self.state.allow_group(chat_id, title)
            feedback = f"Включено: {title}"
        return self._groups_view(page=page, feedback=feedback)

    def _known_group_title(self, chat_id: int) -> str:
        for group in self._group_items():
            if int(group["chat_id"]) == chat_id:
                return str(group["title"])
        return str(chat_id)

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

    def _status(self) -> PanelView:
        enabled = self.state.is_feature_enabled(VOICE_FEATURE)
        decoration_enabled = self.state.is_transcription_decoration_enabled()
        blocked = self.state.list_blocked_chats()
        groups = self.state.list_allowed_groups()
        text = "\n".join(
            [
                "Статус",
                "",
                f"Транскрибация: {'включена' if enabled else 'выключена'}",
                f"Смайлы: {'включены' if decoration_enabled else 'выключены'}",
                "Область: личные чаты + выбранные группы",
                f"Исключения: {len(blocked)}",
                f"Группы: {len(groups)}",
                self._llm_status_line(),
            ]
        )
        return PanelView(text=text, keyboard=self._back_keyboard(), action="status")

    def _llm_status_line(self) -> str:
        provider_label = {
            "copilot": "GitHub Copilot CLI",
            "openai": "OpenAI",
            "anthropic": "Anthropic",
        }.get(self.llm_provider, self.llm_provider or "—")
        if self.llm_model:
            return f"LLM: {provider_label} ({self.llm_model})"
        return f"LLM: {provider_label}"

    @classmethod
    def _help(cls) -> PanelView:
        return PanelView(
            text="\n".join(
                [
                    "Как пользоваться",
                    "",
                    "1. Открой Транскрибация.",
                    "2. При необходимости добавь личный чат в исключения.",
                    "3. Выбери нужные группы в списке.",
                    "4. При необходимости измени промпт.",
                    "5. Включи или выключи обработку кнопкой.",
                    "",
                    "Все изменения применяются к следующим голосовым сразу.",
                ]
            ),
            keyboard=cls._back_keyboard(),
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
            text=f"Blocked chat {chat_id}.",
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
            text=f"Unblocked chat {chat_id}.",
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
            text=f"Removed group {chat_id}.",
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

    def _is_owner(self, user_id: int) -> bool:
        return user_id == self.owner_id

    @staticmethod
    def _denied() -> PanelView:
        return PanelView(text="Access denied.", keyboard=[])

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
