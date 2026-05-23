from pathlib import Path

import pytest

from telepath import auth


def test_auth_cli_reports_missing_env_without_traceback(monkeypatch, capsys):
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)

    with pytest.raises(SystemExit) as exc:
        auth.main()

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "Missing required environment variable: TG_API_ID" in captured.err
    assert "Fill .env" in captured.err


def test_auth_cli_main_runs_authorize_when_env_is_present(monkeypatch):
    """Happy path: load settings then invoke authorize_telegram_user via asyncio.run."""
    captured = {}

    monkeypatch.setattr(
        auth,
        "load_telegram_user_settings",
        lambda: auth.TelegramUserSettings(
            telegram_api_id=1, telegram_api_hash="hash", telegram_session="data/x"
        ),
    )

    def fake_asyncio_run(coro):
        captured["coro_name"] = type(coro).__name__
        coro.close()

    monkeypatch.setattr(auth.asyncio, "run", fake_asyncio_run)

    auth.main()

    assert captured["coro_name"] == "coroutine"


def test_persist_owner_id_writes_dotfile_in_session_directory(tmp_path):
    session_path = tmp_path / "data" / "telepath"

    written = auth._persist_owner_id(str(session_path), 9876543)

    assert written == tmp_path / "data" / ".owner_id"
    assert written.read_text(encoding="utf-8").strip() == "9876543"
    # parent dir was created on demand
    assert (tmp_path / "data").is_dir()


def test_persist_owner_id_handles_bare_session_name_with_no_parent(tmp_path, monkeypatch):
    # When the session is a bare name (no directory component), the dotfile
    # lands in the current working directory.
    monkeypatch.chdir(tmp_path)

    written = auth._persist_owner_id("telepath", 42)

    assert written == Path(".owner_id")
    assert (tmp_path / ".owner_id").read_text(encoding="utf-8").strip() == "42"


def test_persist_owner_id_overwrites_previous_value(tmp_path):
    session_path = tmp_path / "data" / "telepath"
    auth._persist_owner_id(str(session_path), 1)
    auth._persist_owner_id(str(session_path), 2)

    assert (tmp_path / "data" / ".owner_id").read_text(encoding="utf-8").strip() == "2"
