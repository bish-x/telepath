from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from telepath.llm.base import LLMFailure, LLMUnavailable, format_prompt


DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


class OpenAIChatCompleter(Protocol):
    def complete(self, *, model: str, system: str, user: str) -> str: ...


class _RealOpenAIChat:
    def __init__(self, api_key: str, base_url: str | None, timeout_seconds: int) -> None:
        try:
            from openai import OpenAI  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - import guard
            raise LLMUnavailable(
                "openai SDK is not installed. Add `openai` to dependencies or pick another LLM_PROVIDER."
            ) from exc
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)

    def complete(self, *, model: str, system: str, user: str) -> str:
        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
            )
        except Exception as exc:  # noqa: BLE001 - SDK raises many exception types
            raise LLMFailure(f"OpenAI request failed: {exc}") from exc
        choice = response.choices[0] if response.choices else None
        if choice is None or choice.message is None:
            raise LLMFailure("OpenAI response contained no choices.")
        return choice.message.content or ""


@dataclass
class OpenAITextPolisher:
    api_key: str
    model: str = DEFAULT_OPENAI_MODEL
    base_url: str | None = None
    timeout_seconds: int = 60
    chat: OpenAIChatCompleter | None = None

    def __post_init__(self) -> None:
        if not self.api_key:
            raise LLMUnavailable("OPENAI_API_KEY is required for the OpenAI polisher.")
        if self.chat is None:
            self.chat = _RealOpenAIChat(self.api_key, self.base_url, self.timeout_seconds)

    def polish(self, text: str, prompt: str | None = None) -> str:
        instructions, user_text = format_prompt(text, prompt)
        assert self.chat is not None
        output = self.chat.complete(model=self.model, system=instructions, user=user_text)
        return output.strip()
