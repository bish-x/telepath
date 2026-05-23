import asyncio

from telepath import app


async def test_run_cancels_sibling_runtime_when_one_runtime_stops(monkeypatch):
    events = []
    settings = object()

    async def fake_user_client(received_settings):
        assert received_settings is settings
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            events.append("user_cancelled")
            raise

    async def fake_manager_bot(received_settings):
        assert received_settings is settings
        events.append("manager_stopped")

    monkeypatch.setattr(app, "load_settings", lambda: settings)
    monkeypatch.setattr(app, "run_user_client", fake_user_client)
    monkeypatch.setattr(app, "run_manager_bot", fake_manager_bot)

    await app.run()

    assert events == ["manager_stopped", "user_cancelled"]
