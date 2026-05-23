import pytest

from telepath.llm.base import LLMUnavailable
from telepath.llm.openai_api import OpenAITextPolisher
from telepath.prompts import DEFAULT_TEXT_POLISH_PROMPT


class RecordingChat:
    def __init__(self, output="polished"):
        self.output = output
        self.calls = []

    def complete(self, *, model, system, user):
        self.calls.append({"model": model, "system": system, "user": user})
        return self.output


def test_openai_polisher_sends_system_and_user_messages_with_model():
    chat = RecordingChat(output="Привет, мир.")
    polisher = OpenAITextPolisher(api_key="sk-test", model="gpt-test", chat=chat)

    result = polisher.polish("привет мир")

    assert result == "Привет, мир."
    assert chat.calls == [
        {
            "model": "gpt-test",
            "system": DEFAULT_TEXT_POLISH_PROMPT.strip(),
            "user": "привет мир",
        }
    ]


def test_openai_polisher_uses_custom_prompt_when_provided():
    chat = RecordingChat(output="X")
    polisher = OpenAITextPolisher(api_key="sk-test", chat=chat)

    polisher.polish("  raw  ", prompt="  Custom rules. ")

    assert chat.calls[0]["system"] == "Custom rules."
    assert chat.calls[0]["user"] == "raw"


def test_openai_polisher_strips_output_whitespace():
    chat = RecordingChat(output="\n\n  hello\n")
    polisher = OpenAITextPolisher(api_key="sk-test", chat=chat)

    assert polisher.polish("x") == "hello"


def test_openai_polisher_rejects_empty_api_key():
    with pytest.raises(LLMUnavailable, match="OPENAI_API_KEY"):
        OpenAITextPolisher(api_key="", chat=RecordingChat())


# --- _RealOpenAIChat (the SDK adapter) -------------------------------------
# Tests inject a fake OpenAI class to exercise __init__ and .complete()
# without needing a real API key or network.

class _FakeOpenAIMessage:
    def __init__(self, content):
        self.content = content


class _FakeOpenAIChoice:
    def __init__(self, content):
        self.message = _FakeOpenAIMessage(content)


class _FakeOpenAINoneMessageChoice:
    message = None


class _FakeOpenAIResponse:
    def __init__(self, choices):
        self.choices = choices


class _FakeCompletionsAPI:
    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise = raise_exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return self._response


class _FakeOpenAIChat:
    def __init__(self, response=None, raise_exc=None):
        self.completions = _FakeCompletionsAPI(response=response, raise_exc=raise_exc)


class _FakeOpenAI:
    """Drop-in stand-in for the `openai.OpenAI` class."""
    last_kwargs = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        self.chat = _FakeOpenAIChat(response=_FakeOpenAIResponse([_FakeOpenAIChoice("hello")]))


def _install_fake_openai(monkeypatch, fake_cls):
    import openai
    monkeypatch.setattr(openai, "OpenAI", fake_cls)


def test_real_openai_chat_constructor_passes_kwargs(monkeypatch):
    from telepath.llm.openai_api import _RealOpenAIChat

    _install_fake_openai(monkeypatch, _FakeOpenAI)

    chat = _RealOpenAIChat(api_key="sk-x", base_url="https://api.example.com", timeout_seconds=42)

    assert _FakeOpenAI.last_kwargs == {
        "api_key": "sk-x",
        "base_url": "https://api.example.com",
        "timeout": 42,
    }
    assert chat.complete(model="m", system="s", user="u") == "hello"


def test_real_openai_chat_complete_returns_empty_string_when_content_none(monkeypatch):
    from telepath.llm.openai_api import _RealOpenAIChat

    class FakeOpenAIWithNoneContent(_FakeOpenAI):
        def __init__(self, **kwargs):
            type(self).last_kwargs = kwargs
            self.chat = _FakeOpenAIChat(response=_FakeOpenAIResponse([_FakeOpenAIChoice(None)]))

    _install_fake_openai(monkeypatch, FakeOpenAIWithNoneContent)
    chat = _RealOpenAIChat(api_key="sk-x", base_url=None, timeout_seconds=60)

    assert chat.complete(model="m", system="s", user="u") == ""


def test_real_openai_chat_complete_raises_on_no_choices(monkeypatch):
    from telepath.llm.openai_api import _RealOpenAIChat
    from telepath.llm.base import LLMFailure

    class FakeOpenAINoChoices(_FakeOpenAI):
        def __init__(self, **kwargs):
            type(self).last_kwargs = kwargs
            self.chat = _FakeOpenAIChat(response=_FakeOpenAIResponse([]))

    _install_fake_openai(monkeypatch, FakeOpenAINoChoices)
    chat = _RealOpenAIChat(api_key="sk-x", base_url=None, timeout_seconds=60)

    with pytest.raises(LLMFailure, match="no choices"):
        chat.complete(model="m", system="s", user="u")


def test_real_openai_chat_complete_raises_when_choice_message_is_none(monkeypatch):
    from telepath.llm.openai_api import _RealOpenAIChat
    from telepath.llm.base import LLMFailure

    class FakeOpenAINoneMsg(_FakeOpenAI):
        def __init__(self, **kwargs):
            type(self).last_kwargs = kwargs
            self.chat = _FakeOpenAIChat(response=_FakeOpenAIResponse([_FakeOpenAINoneMessageChoice()]))

    _install_fake_openai(monkeypatch, FakeOpenAINoneMsg)
    chat = _RealOpenAIChat(api_key="sk-x", base_url=None, timeout_seconds=60)

    with pytest.raises(LLMFailure, match="no choices"):
        chat.complete(model="m", system="s", user="u")


def test_real_openai_chat_complete_wraps_sdk_exception_as_llm_failure(monkeypatch):
    from telepath.llm.openai_api import _RealOpenAIChat
    from telepath.llm.base import LLMFailure

    class FakeOpenAIRaising(_FakeOpenAI):
        def __init__(self, **kwargs):
            type(self).last_kwargs = kwargs
            self.chat = _FakeOpenAIChat(raise_exc=RuntimeError("rate limited"))

    _install_fake_openai(monkeypatch, FakeOpenAIRaising)
    chat = _RealOpenAIChat(api_key="sk-x", base_url=None, timeout_seconds=60)

    with pytest.raises(LLMFailure, match="rate limited"):
        chat.complete(model="m", system="s", user="u")


def test_openai_polisher_default_chat_factory_is_real_openai(monkeypatch):
    _install_fake_openai(monkeypatch, _FakeOpenAI)

    polisher = OpenAITextPolisher(api_key="sk-x")  # no chat=  -> auto-construct
    assert polisher.chat is not None
    assert polisher.polish("hello") == "hello"
