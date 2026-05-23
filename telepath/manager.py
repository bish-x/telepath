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
            return "Access denied."

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
            return "Assistant manager is running."
        return f"Unknown command: {command}\n\n{self._help()}"

    def _block(self, parts: list[str]) -> str:
        if len(parts) < 2:
            return "Usage: /block <chat_id> [title]"
        try:
            chat_id = int(parts[1])
        except ValueError:
            return "Usage: /block <chat_id> [title]"
        title = " ".join(parts[2:]) or None
        self.blacklist.block_chat(chat_id, title)
        return f"Blocked chat {chat_id}."

    def _unblock(self, parts: list[str]) -> str:
        if len(parts) != 2:
            return "Usage: /unblock <chat_id>"
        try:
            chat_id = int(parts[1])
        except ValueError:
            return "Usage: /unblock <chat_id>"
        self.blacklist.unblock_chat(chat_id)
        return f"Unblocked chat {chat_id}."

    def _list(self) -> str:
        chats = self.blacklist.list_blocked_chats()
        if not chats:
            return "Blocked chats: none"
        lines = ["Blocked chats:"]
        for chat in chats:
            title = chat["title"] or "(no title)"
            lines.append(f"- {chat['chat_id']}: {title}")
        return "\n".join(lines)

    def _allow_group(self, parts: list[str]) -> str:
        if len(parts) < 2:
            return "Usage: /allow_group <chat_id> [title]"
        try:
            chat_id = int(parts[1])
        except ValueError:
            return "Usage: /allow_group <chat_id> [title]"
        title = " ".join(parts[2:]) or None
        self.blacklist.allow_group(chat_id, title)
        return f"Allowed group {chat_id}."

    def _deny_group(self, parts: list[str]) -> str:
        if len(parts) != 2:
            return "Usage: /deny_group <chat_id>"
        try:
            chat_id = int(parts[1])
        except ValueError:
            return "Usage: /deny_group <chat_id>"
        self.blacklist.disallow_group(chat_id)
        return f"Removed group {chat_id}."

    def _groups(self) -> str:
        groups = self.blacklist.list_allowed_groups()
        if not groups:
            return "Allowed groups: none"
        lines = ["Allowed groups:"]
        for group in groups:
            title = group["title"] or "(no title)"
            lines.append(f"- {group['chat_id']}: {title}")
        return "\n".join(lines)

    @staticmethod
    def _help() -> str:
        return "\n".join(
            [
                "Commands:",
                "/block <chat_id> [title] - exclude a private chat from transcription",
                "/unblock <chat_id> - remove chat from blacklist",
                "/list - show blocked chats",
                "/allow_group <chat_id> [title] - enable transcription in a group",
                "/deny_group <chat_id> - remove group from whitelist",
                "/groups - show whitelisted groups",
                "/status - show manager status",
            ]
        )
