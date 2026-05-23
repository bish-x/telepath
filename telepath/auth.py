from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from telepath.config import TelegramUserSettings, load_telegram_user_settings
from telepath.session_paths import ensure_session_parent


def _persist_owner_id(session_path: str, owner_id: int) -> Path:
    """Write the resolved owner id next to the session file so a host-side script
    (setup.sh / auth.sh) can read it without parsing stdout."""
    parent = Path(session_path).expanduser().parent
    if str(parent) not in {"", "."}:
        parent.mkdir(parents=True, exist_ok=True)
        target = parent / ".owner_id"
    else:
        target = Path(".owner_id")
    target.write_text(f"{owner_id}\n", encoding="utf-8")
    return target


async def authorize_telegram_user(settings: TelegramUserSettings) -> None:  # pragma: no cover - integration only
    from telethon import TelegramClient

    ensure_session_parent(settings.telegram_session)
    client = TelegramClient(settings.telegram_session, settings.telegram_api_id, settings.telegram_api_hash)
    await client.start()
    me = await client.get_me()
    owner_file = _persist_owner_id(settings.telegram_session, int(me.id))
    print("Telegram user session is authorized.")
    print(f"TG_OWNER_ID={me.id}")
    print(f"Session file: {settings.telegram_session}.session")
    print(f"Owner id written to: {owner_file}")
    await client.disconnect()


def main() -> None:
    try:
        settings = load_telegram_user_settings()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        print("Fill .env from .env.example, then run telepath-auth again.", file=sys.stderr)
        raise SystemExit(2) from exc
    asyncio.run(authorize_telegram_user(settings))


if __name__ == "__main__":
    main()
