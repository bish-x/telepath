from pathlib import Path

import pytest

from telepath.config import Settings
from telepath.llm import (
    AnthropicTextPolisher,
    CopilotCliTextPolisher,
    LLMUnavailable,
    OpenAITextPolisher,
    build_polisher,
)


class StubChat:
    def complete(self, **kwargs):  # pragma: no cover - never reached in factory tests
        raise AssertionError("stub chat should not be invoked from factory tests")


def _base_settings(**overrides) -> Settings:
    base = dict(
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_session="telepath",
        manager_bot_token="token",
        owner_id=1,
        database_path=Path("data/assistant.sqlite3"),
    )
    base.update(overrides)
    return Settings(**base)


def test_build_polisher_returns_copilot_by_default():
    settings = _base_settings(
        copilot_command="copilot",
        copilot_model="gpt-test",
        copilot_timeout_seconds=42,
    )

    polisher = build_polisher(settings)

    assert isinstance(polisher, CopilotCliTextPolisher)
    assert polisher.command == "copilot"
    assert polisher.model == "gpt-test"
    assert polisher.timeout_seconds == 42


def test_build_polisher_returns_openai_with_settings(monkeypatch):
    monkeypatch.setattr(
        "telepath.llm.openai_api._RealOpenAIChat.__init__",
        lambda self, *a, **kw: None,
    )
    settings = _base_settings(
        llm_provider="openai",
        openai_api_key="sk-x",
        openai_model="gpt-custom",
        openai_base_url="https://api.example.com",
    )

    polisher = build_polisher(settings)

    assert isinstance(polisher, OpenAITextPolisher)
    assert polisher.api_key == "sk-x"
    assert polisher.model == "gpt-custom"
    assert polisher.base_url == "https://api.example.com"


def test_build_polisher_returns_anthropic_with_settings(monkeypatch):
    monkeypatch.setattr(
        "telepath.llm.anthropic_api._RealAnthropicChat.__init__",
        lambda self, *a, **kw: None,
    )
    settings = _base_settings(
        llm_provider="anthropic",
        anthropic_api_key="ant-x",
        anthropic_model="claude-custom",
    )

    polisher = build_polisher(settings)

    assert isinstance(polisher, AnthropicTextPolisher)
    assert polisher.api_key == "ant-x"
    assert polisher.model == "claude-custom"


def test_build_polisher_rejects_unknown_provider():
    settings = _base_settings(llm_provider="grok")

    with pytest.raises(LLMUnavailable, match="Unknown LLM_PROVIDER"):
        build_polisher(settings)


def test_build_polisher_openai_without_key_raises():
    settings = _base_settings(llm_provider="openai", openai_api_key=None)

    with pytest.raises(LLMUnavailable, match="OPENAI_API_KEY"):
        build_polisher(settings)


def test_build_polisher_anthropic_without_key_raises():
    settings = _base_settings(llm_provider="anthropic", anthropic_api_key=None)

    with pytest.raises(LLMUnavailable, match="ANTHROPIC_API_KEY"):
        build_polisher(settings)
