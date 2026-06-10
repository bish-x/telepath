import asyncio

import pytest

import telepath.app as app_module


class _FakeTask:
    """Minimal awaitable task stub for testing app.run's lifecycle."""

    def __init__(self, *, never_completes: bool = False, raises: BaseException | None = None):
        self._never_completes = never_completes
        self._raises = raises
        self._cancelled = False
        self._done = False

    def cancel(self):
        self._cancelled = True
        self._done = True

    def done(self):
        return self._done

    def result(self):
        if self._raises is not None:
            raise self._raises
        return None


async def _fast_user(settings, **kwargs):
    return None


async def _slow_user(settings, **kwargs):
    await asyncio.sleep(10)


async def _fast_manager(settings, **kwargs):
    return None


async def _failing_user(settings, **kwargs):
    raise RuntimeError("user client died")


def _patch_loaders(monkeypatch, *, user_coro, manager_coro):
    telegram_client = type(
        "FakeTelegramClient",
        (),
        {"is_connected": lambda self: False, "disconnect": lambda self: None},
    )()
    monkeypatch.setattr(app_module, "load_settings", lambda: object())
    monkeypatch.setattr(app_module, "build_telegram_client", lambda settings: telegram_client)
    monkeypatch.setattr(app_module, "run_user_client", user_coro)
    monkeypatch.setattr(app_module, "run_manager_bot", manager_coro)


def test_app_run_cancels_pending_when_first_task_finishes(monkeypatch):
    _patch_loaders(monkeypatch, user_coro=_fast_user, manager_coro=_slow_user)

    asyncio.run(app_module.run())


def test_app_run_propagates_failure_from_finished_task(monkeypatch):
    _patch_loaders(monkeypatch, user_coro=_failing_user, manager_coro=_slow_user)

    with pytest.raises(RuntimeError, match="user client died"):
        asyncio.run(app_module.run())


def test_app_run_cancels_unfinished_tasks_in_finally(monkeypatch):
    """If asyncio.wait raises (e.g. CancelledError), both tasks are still pending
    and the finally block must cancel them."""
    _patch_loaders(monkeypatch, user_coro=_slow_user, manager_coro=_slow_user)

    async def boom(tasks, **kwargs):
        raise RuntimeError("wait failed")

    monkeypatch.setattr(app_module.asyncio, "wait", boom)

    with pytest.raises(RuntimeError, match="wait failed"):
        asyncio.run(app_module.run())


def test_app_main_configures_logging_and_runs(monkeypatch):
    called = {"basic_config": False, "asyncio_run_with": None}

    def fake_basic_config(**kwargs):
        called["basic_config"] = True
        assert kwargs.get("level") is not None
        assert kwargs.get("format")

    def fake_asyncio_run(coro):
        called["asyncio_run_with"] = type(coro).__name__
        coro.close()  # drain the coroutine to avoid "never awaited" warning

    import logging

    monkeypatch.setattr(logging, "basicConfig", fake_basic_config)
    monkeypatch.setattr(app_module.asyncio, "run", fake_asyncio_run)

    app_module.main()

    assert called["basic_config"] is True
    assert called["asyncio_run_with"] == "coroutine"
