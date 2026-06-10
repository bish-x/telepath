from __future__ import annotations

import shlex
from typing import Protocol


class BlacklistRepository(Protocol):
    def block_chat(self, chat_id: int, title: str | None = None) -> None: ...
    def unblock_chat(self, chat_id: int) -> None: ...
    def is_blocked(self, chat_id: int) -> bool: ...
    def list_blocked_chats(self) -> list[dict[str, object]]: ...
    def allow_group(self, chat_id: int, title: str | None = None) -> None: ...
    def disallow_group(self, chat_id: int) -> None: ...
    def list_allowed_groups(self) -> list[dict[str, object]]: ...


class ManagerService:
    def __init__(self, *, owner_id: int, blacklist: BlacklistRepository):
        self.owner_id = owner_id
        self.blacklist = blacklist

    def handle_command(self, *, user_id: int, text: str) -> str:
        if user_id != self.owner_id:
            return "Доступ запрещен."

        parts = shlex.split(text.strip())
        if not parts:
            return self._help()

        command = parts[0].split("@", 1)[0]
        if command == "/start" or command == "/help":
            return self._help()
        if command in {"/block", "/deny"}:
            return self._block(parts)
        if command in {"/unblock", "/allow"}:
            return self._unblock(parts)
        if command == "/allow_group":
            return self._allow_group(parts)
        if command == "/deny_group":
            return self._deny_group(parts)
        if command == "/groups":
            return self._groups()
        if command == "/list":
            return self._list()
        if command == "/status":
            return "Панель управления работает."
        return f"Неизвестная команда: {command}\n\n{self._help()}"

    def _block(self, parts: list[str]) -> str:
        if len(parts) < 2:
            return "Формат: /block <chat_id> [название]"
        try:
            chat_id = int(parts[1])
        except ValueError:
            return "Формат: /block <chat_id> [название]"
        title = " ".join(parts[2:]) or None
        self.blacklist.block_chat(chat_id, title)
        return f"Чат {chat_id} добавлен в исключения."

    def _unblock(self, parts: list[str]) -> str:
        if len(parts) != 2:
            return "Формат: /unblock <chat_id>"
        try:
            chat_id = int(parts[1])
        except ValueError:
            return "Формат: /unblock <chat_id>"
        self.blacklist.unblock_chat(chat_id)
        return f"Чат {chat_id} убран из исключений."

    def _list(self) -> str:
        chats = self.blacklist.list_blocked_chats()
        if not chats:
            return "Исключения: нет"
        lines = ["Исключения:"]
        for chat in chats:
            title = chat["title"] or "без названия"
            lines.append(f"- {chat['chat_id']}: {title}")
        return "\n".join(lines)

    def _allow_group(self, parts: list[str]) -> str:
        if len(parts) < 2:
            return "Формат: /allow_group <chat_id> [название]"
        try:
            chat_id = int(parts[1])
        except ValueError:
            return "Формат: /allow_group <chat_id> [название]"
        title = " ".join(parts[2:]) or None
        self.blacklist.allow_group(chat_id, title)
        return f"Группа {chat_id} включена."

    def _deny_group(self, parts: list[str]) -> str:
        if len(parts) != 2:
            return "Формат: /deny_group <chat_id>"
        try:
            chat_id = int(parts[1])
        except ValueError:
            return "Формат: /deny_group <chat_id>"
        self.blacklist.disallow_group(chat_id)
        return f"Группа {chat_id} выключена."

    def _groups(self) -> str:
        groups = self.blacklist.list_allowed_groups()
        if not groups:
            return "Включенные группы: нет"
        lines = ["Включенные группы:"]
        for group in groups:
            title = group["title"] or "без названия"
            lines.append(f"- {group['chat_id']}: {title}")
        return "\n".join(lines)

    @staticmethod
    def _help() -> str:
        return "\n".join(
            [
                "Команды:",
                "/block <chat_id> [название] - добавить личный чат в исключения",
                "/unblock <chat_id> - убрать личный чат из исключений",
                "/list - показать исключения",
                "/allow_group <chat_id> [название] - включить группу",
                "/deny_group <chat_id> - выключить группу",
                "/groups - показать включенные группы",
                "/status - проверить панель",
            ]
        )
