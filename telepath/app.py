from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from telepath.chat_export import TelethonChatExporter
from telepath.config import load_settings
from telepath.manager_bot import run_manager_bot
from telepath.session_paths import ensure_session_parent
from telepath.user_client import ChannelReactionHistoryBackfill, run_user_client


def build_telegram_client(settings: Any) -> Any:  # pragma: no cover - integration only
    from telethon import TelegramClient

    ensure_session_parent(settings.telegram_session)
    return TelegramClient(settings.telegram_session, settings.telegram_api_id, settings.telegram_api_hash)


async def run() -> None:
    settings = load_settings()
    telegram_client = build_telegram_client(settings)
    chat_exporter = TelethonChatExporter(telegram_client)
    reaction_history_backfill = ChannelReactionHistoryBackfill()
    tasks = [
        asyncio.create_task(
            run_user_client(
                settings,
                client=telegram_client,
                reaction_history_backfill=reaction_history_backfill,
            )
        ),
        asyncio.create_task(
            run_manager_bot(
                settings,
                chat_exporter=chat_exporter,
                reaction_history_backfill=reaction_history_backfill,
            )
        ),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        is_connected = getattr(telegram_client, "is_connected", None)
        disconnect = getattr(telegram_client, "disconnect", None)
        if callable(is_connected) and callable(disconnect) and is_connected():
            result = disconnect()
            if inspect.isawaitable(result):
                await result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
