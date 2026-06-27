import asyncio

from telepath import app


async def test_run_cancels_sibling_runtime_when_one_runtime_stops(monkeypatch):
    events = []
    settings = object()
    telegram_client = type(
        "FakeTelegramClient",
        (),
        {"is_connected": lambda self: False, "disconnect": lambda self: None},
    )()

    shared_backfill = {}
    shared_post_mirror_backfill = {}

    async def fake_user_client(
        received_settings,
        *,
        client=None,
        reaction_history_backfill=None,
        post_mirror_history_backfill=None,
    ):
        assert received_settings is settings
        assert client is telegram_client
        assert reaction_history_backfill is not None
        assert post_mirror_history_backfill is not None
        shared_backfill["user"] = reaction_history_backfill
        shared_post_mirror_backfill["user"] = post_mirror_history_backfill
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            events.append("user_cancelled")
            raise

    async def fake_manager_bot(
        received_settings,
        *,
        chat_exporter=None,
        reaction_history_backfill=None,
        post_mirror_history_backfill=None,
    ):
        assert received_settings is settings
        assert chat_exporter.client is telegram_client
        assert reaction_history_backfill is not None
        assert post_mirror_history_backfill is not None
        shared_backfill["manager"] = reaction_history_backfill
        shared_post_mirror_backfill["manager"] = post_mirror_history_backfill
        events.append("manager_stopped")

    monkeypatch.setattr(app, "load_settings", lambda: settings)
    monkeypatch.setattr(app, "build_telegram_client", lambda received_settings: telegram_client)
    monkeypatch.setattr(app, "run_user_client", fake_user_client)
    monkeypatch.setattr(app, "run_manager_bot", fake_manager_bot)

    await app.run()

    assert events == ["manager_stopped", "user_cancelled"]
    assert shared_backfill["user"] is shared_backfill["manager"]
    assert shared_post_mirror_backfill["user"] is shared_post_mirror_backfill["manager"]
