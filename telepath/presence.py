from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any


logger = logging.getLogger(__name__)
_offline_update_suppression_depth: ContextVar[int] = ContextVar("offline_update_suppression_depth", default=0)


@contextmanager
def suppress_current_session_offline_updates() -> Iterator[None]:
    token = _offline_update_suppression_depth.set(_offline_update_suppression_depth.get() + 1)
    try:
        yield
    finally:
        _offline_update_suppression_depth.reset(token)


async def mark_current_session_offline(client: Any, *, logger_: logging.Logger | None = None) -> bool:
    if _offline_update_suppression_depth.get() > 0:
        return False

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
