from __future__ import annotations

from pathlib import Path


def ensure_session_parent(session: str) -> None:
    # Telethon accepts both file-backed session names and long StringSession
    # payloads. StringSession values are not filesystem paths.
    if len(session) > 128 and "/" not in session and "\\" not in session:
        return
    parent = Path(session).expanduser().parent
    if str(parent) not in {"", "."}:
        parent.mkdir(parents=True, exist_ok=True)
