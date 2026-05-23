import pytest

from telepath.llm.anthropic_api import (
    DEFAULT_ANTHROPIC_MAX_TOKENS,
    AnthropicTextPolisher,
)
from telepath.llm.base import LLMUnavailable
from telepath.prompts import DEFAULT_TEXT_POLISH_PROMPT


class RecordingChat:
    def __init__(self, output="polished"):
        self.output = output
        self.calls = []

    def complete(self, *, model, system, user, max_tokens):
        self.calls.append(
            {"model": model, "system": system, "user": user, "max_tokens": max_tokens}
        )
        return self.output


def test_anthropic_polisher_passes_system_user_model_and_max_tokens():
    chat = RecordingChat(output="Привет, мир.")
    polisher = AnthropicTextPolisher(
        api_key="ant-test", model="claude-test", chat=chat, max_tokens=512
    )

    result = polisher.polish("привет мир")

    assert result == "Привет, мир."
    assert chat.calls == [
        {
            "model": "claude-test",
            "system": DEFAULT_TEXT_POLISH_PROMPT.strip(),
            "user": "привет мир",
            "max_tokens": 512,
        }
    ]


def test_anthropic_polisher_uses_default_max_tokens_when_unspecified():
    chat = RecordingChat(output="X")
    polisher = AnthropicTextPolisher(api_key="ant-test", chat=chat)

    polisher.polish("raw")

    assert chat.calls[0]["max_tokens"] == DEFAULT_ANTHROPIC_MAX_TOKENS


def test_anthropic_polisher_accepts_runtime_prompt_override():
    chat = RecordingChat(output="ok")
    polisher = AnthropicTextPolisher(api_key="ant-test", chat=chat)

    polisher.polish("  raw  ", prompt="  Custom rules. ")

    assert chat.calls[0]["system"] == "Custom rules."
    assert chat.calls[0]["user"] == "raw"


def test_anthropic_polisher_strips_output_whitespace():
    chat = RecordingChat(output="\n  hello \n")
    polisher = AnthropicTextPolisher(api_key="ant-test", chat=chat)

    assert polisher.polish("x") == "hello"


def test_anthropic_polisher_rejects_empty_api_key():
    with pytest.raises(LLMUnavailable, match="ANTHROPIC_API_KEY"):
        AnthropicTextPolisher(api_key="", chat=RecordingChat())


# --- _RealAnthropicChat (the SDK adapter) ----------------------------------
# Tests inject a fake Anthropic class to exercise __init__ and .complete()
# without needing a real API key or network.

class _FakeBlock:
    def __init__(self, text, type_="text"):
        self.text = text
        self.type = type_


class _FakeAnthropicMessage:
    def __init__(self, content):
        self.content = content


class _FakeMessagesAPI:
    def __init__(self, message=None, raise_exc=None):
        self._message = message
        self._raise = raise_exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return self._message


class _FakeAnthropic:
    """Drop-in stand-in for the `anthropic.Anthropic` class."""
    last_kwargs = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        self.messages = _FakeMessagesAPI(
            message=_FakeAnthropicMessage([_FakeBlock("hello")]),
        )


def _install_fake_anthropic(monkeypatch, fake_cls):
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", fake_cls)


def test_real_anthropic_chat_constructor_passes_kwargs_with_base_url(monkeypatch):
    from telepath.llm.anthropic_api import _RealAnthropicChat

    _install_fake_anthropic(monkeypatch, _FakeAnthropic)

    chat = _RealAnthropicChat(api_key="ant-x", base_url="https://api.example.com", timeout_seconds=42)

    assert _FakeAnthropic.last_kwargs == {
        "api_key": "ant-x",
        "timeout": 42,
        "base_url": "https://api.example.com",
    }
    assert chat.complete(model="m", system="s", user="u", max_tokens=64) == "hello"


def test_real_anthropic_chat_constructor_skips_base_url_when_none(monkeypatch):
    from telepath.llm.anthropic_api import _RealAnthropicChat

    _install_fake_anthropic(monkeypatch, _FakeAnthropic)

    _RealAnthropicChat(api_key="ant-x", base_url=None, timeout_seconds=42)

    assert "base_url" not in _FakeAnthropic.last_kwargs


def test_real_anthropic_chat_complete_joins_multiple_text_blocks(monkeypatch):
    from telepath.llm.anthropic_api import _RealAnthropicChat

    class FakeAnthropicMultiBlock(_FakeAnthropic):
        def __init__(self, **kwargs):
            type(self).last_kwargs = kwargs
            self.messages = _FakeMessagesAPI(
                message=_FakeAnthropicMessage([_FakeBlock("foo "), _FakeBlock("bar")]),
            )

    _install_fake_anthropic(monkeypatch, FakeAnthropicMultiBlock)
    chat = _RealAnthropicChat(api_key="ant-x", base_url=None, timeout_seconds=60)

    assert chat.complete(model="m", system="s", user="u", max_tokens=128) == "foo bar"


def test_real_anthropic_chat_complete_skips_non_text_blocks(monkeypatch):
    from telepath.llm.anthropic_api import _RealAnthropicChat

    class FakeAnthropicMixed(_FakeAnthropic):
        def __init__(self, **kwargs):
            type(self).last_kwargs = kwargs
            self.messages = _FakeMessagesAPI(
                message=_FakeAnthropicMessage([
                    _FakeBlock("X", type_="tool_use"),
                    _FakeBlock("OK", type_="text"),
                ]),
            )

    _install_fake_anthropic(monkeypatch, FakeAnthropicMixed)
    chat = _RealAnthropicChat(api_key="ant-x", base_url=None, timeout_seconds=60)

    assert chat.complete(model="m", system="s", user="u", max_tokens=128) == "OK"


def test_real_anthropic_chat_complete_raises_when_no_text_blocks(monkeypatch):
    from telepath.llm.anthropic_api import _RealAnthropicChat
    from telepath.llm.base import LLMFailure

    class FakeAnthropicNoText(_FakeAnthropic):
        def __init__(self, **kwargs):
            type(self).last_kwargs = kwargs
            self.messages = _FakeMessagesAPI(
                message=_FakeAnthropicMessage([_FakeBlock("X", type_="tool_use")]),
            )

    _install_fake_anthropic(monkeypatch, FakeAnthropicNoText)
    chat = _RealAnthropicChat(api_key="ant-x", base_url=None, timeout_seconds=60)

    with pytest.raises(LLMFailure, match="no text blocks"):
        chat.complete(model="m", system="s", user="u", max_tokens=128)


def test_real_anthropic_chat_complete_wraps_sdk_exception(monkeypatch):
    from telepath.llm.anthropic_api import _RealAnthropicChat
    from telepath.llm.base import LLMFailure

    class FakeAnthropicRaising(_FakeAnthropic):
        def __init__(self, **kwargs):
            type(self).last_kwargs = kwargs
            self.messages = _FakeMessagesAPI(raise_exc=RuntimeError("rate limited"))

    _install_fake_anthropic(monkeypatch, FakeAnthropicRaising)
    chat = _RealAnthropicChat(api_key="ant-x", base_url=None, timeout_seconds=60)

    with pytest.raises(LLMFailure, match="rate limited"):
        chat.complete(model="m", system="s", user="u", max_tokens=128)


def test_anthropic_polisher_default_chat_factory_is_real_anthropic(monkeypatch):
    _install_fake_anthropic(monkeypatch, _FakeAnthropic)

    polisher = AnthropicTextPolisher(api_key="ant-x")  # no chat= → auto-construct
    assert polisher.chat is not None
    assert polisher.polish("hello") == "hello"
