from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


async def mark_current_session_offline(client: Any, *, logger_: logging.Logger | None = None) -> bool:
    call = getattr(client, "__call__", None)
    if not callable(call):
        return False

    from telethon import functions

    try:
        await call(functions.account.UpdateStatusRequest(offline=True))
    except Exception:
        (logger_ or logger).warning("telegram_current_session_offline_update_failed", exc_info=True)
        return False
    return True
