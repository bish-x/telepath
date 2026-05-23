import pytest

from telepath.config import load_telegram_user_settings


def test_load_telegram_user_settings_requires_only_user_client_env(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_SESSION", "custom-session")
    monkeypatch.delenv("TG_MANAGER_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_OWNER_ID", raising=False)

    settings = load_telegram_user_settings()

    assert settings.telegram_api_id == 123
    assert settings.telegram_api_hash == "hash"
    assert settings.telegram_session == "custom-session"


def test_full_settings_can_load_from_env_without_manager_defaults(monkeypatch, tmp_path):
    from telepath.config import load_settings

    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_MANAGER_BOT_TOKEN", "token")
    monkeypatch.setenv("TG_OWNER_ID", "456")
    monkeypatch.setenv("TG_ASSISTANT_DB", str(tmp_path / "assistant.sqlite3"))
    monkeypatch.delenv("COPILOT_MODEL", raising=False)

    settings = load_settings()

    assert settings.owner_id == 456
    assert settings.database_path == tmp_path / "assistant.sqlite3"
    assert settings.copilot_command == "copilot"
    assert settings.copilot_model == "gpt-5-mini"
    assert settings.private_history_throttle_seconds == 5
    assert settings.transcription_decoration_custom_emoji_id == "5460795800101594035"


def test_full_settings_allows_private_history_throttle_override(monkeypatch, tmp_path):
    from telepath.config import load_settings

    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_MANAGER_BOT_TOKEN", "token")
    monkeypatch.setenv("TG_OWNER_ID", "456")
    monkeypatch.setenv("TG_ASSISTANT_DB", str(tmp_path / "assistant.sqlite3"))
    monkeypatch.setenv("PRIVATE_HISTORY_THROTTLE_SECONDS", "7.5")

    settings = load_settings()

    assert settings.private_history_throttle_seconds == 7.5


def test_full_settings_allows_transcription_decoration_emoji_override(monkeypatch, tmp_path):
    from telepath.config import load_settings

    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_MANAGER_BOT_TOKEN", "token")
    monkeypatch.setenv("TG_OWNER_ID", "456")
    monkeypatch.setenv("TG_ASSISTANT_DB", str(tmp_path / "assistant.sqlite3"))
    monkeypatch.setenv("TRANSCRIPTION_DECORATION_CUSTOM_EMOJI_ID", "111")
    monkeypatch.delenv("COPILOT_MODEL", raising=False)

    settings = load_settings()

    assert settings.transcription_decoration_custom_emoji_id == "111"


def _set_required_telegram_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_MANAGER_BOT_TOKEN", "token")
    monkeypatch.setenv("TG_OWNER_ID", "456")
    monkeypatch.setenv("TG_ASSISTANT_DB", str(tmp_path / "assistant.sqlite3"))


def test_default_llm_provider_is_copilot(monkeypatch, tmp_path):
    from telepath.config import load_settings

    _set_required_telegram_env(monkeypatch, tmp_path)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    settings = load_settings()

    assert settings.llm_provider == "copilot"


def test_llm_provider_openai_requires_api_key(monkeypatch, tmp_path):
    from telepath.config import load_settings

    _set_required_telegram_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        load_settings()


def test_llm_provider_openai_with_api_key_loads_overrides(monkeypatch, tmp_path):
    from telepath.config import load_settings

    _set_required_telegram_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-custom")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "42")

    settings = load_settings()

    assert settings.llm_provider == "openai"
    assert settings.openai_api_key == "sk-test"
    assert settings.openai_model == "gpt-custom"
    assert settings.openai_base_url == "https://api.example.com"
    assert settings.openai_timeout_seconds == 42


def test_llm_provider_anthropic_requires_api_key(monkeypatch, tmp_path):
    from telepath.config import load_settings

    _set_required_telegram_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        load_settings()


def test_llm_provider_unknown_rejected(monkeypatch, tmp_path):
    from telepath.config import load_settings

    _set_required_telegram_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "grok")

    with pytest.raises(RuntimeError, match="Unsupported LLM_PROVIDER"):
        load_settings()


def test_llm_provider_is_case_insensitive(monkeypatch, tmp_path):
    from telepath.config import load_settings

    _set_required_telegram_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "OpenAI")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    settings = load_settings()

    assert settings.llm_provider == "openai"


def test_tg_owner_id_zero_rejected(monkeypatch, tmp_path):
    from telepath.config import load_settings

    _set_required_telegram_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TG_OWNER_ID", "0")

    with pytest.raises(RuntimeError, match="TG_OWNER_ID must be a positive integer"):
        load_settings()


def test_tg_owner_id_negative_rejected(monkeypatch, tmp_path):
    from telepath.config import load_settings

    _set_required_telegram_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TG_OWNER_ID", "-5")

    with pytest.raises(RuntimeError, match="TG_OWNER_ID must be a positive integer"):
        load_settings()
