from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from telepath.llm.base import LLMFailure, LLMUnavailable, format_prompt


DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_ANTHROPIC_MAX_TOKENS = 4096


class AnthropicChatCompleter(Protocol):
    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> str: ...


class _RealAnthropicChat:
    def __init__(self, api_key: str, base_url: str | None, timeout_seconds: int) -> None:
        try:
            from anthropic import Anthropic  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - import guard
            raise LLMUnavailable(
                "anthropic SDK is not installed. Add `anthropic` to dependencies or pick another LLM_PROVIDER."
            ) from exc
        kwargs: dict[str, object] = {"api_key": api_key, "timeout": timeout_seconds}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = Anthropic(**kwargs)

    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> str:
        try:
            message = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:  # noqa: BLE001 - SDK raises many exception types
            raise LLMFailure(f"Anthropic request failed: {exc}") from exc
        parts = [block.text for block in message.content if getattr(block, "type", None) == "text"]
        if not parts:
            raise LLMFailure("Anthropic response contained no text blocks.")
        return "".join(parts)


@dataclass
class AnthropicTextPolisher:
    api_key: str
    model: str = DEFAULT_ANTHROPIC_MODEL
    base_url: str | None = None
    timeout_seconds: int = 60
    max_tokens: int = DEFAULT_ANTHROPIC_MAX_TOKENS
    chat: AnthropicChatCompleter | None = None

    def __post_init__(self) -> None:
        if not self.api_key:
            raise LLMUnavailable("ANTHROPIC_API_KEY is required for the Anthropic polisher.")
        if self.chat is None:
            self.chat = _RealAnthropicChat(self.api_key, self.base_url, self.timeout_seconds)

    def polish(self, text: str, prompt: str | None = None) -> str:
        instructions, user_text = format_prompt(text, prompt)
        assert self.chat is not None
        output = self.chat.complete(
            model=self.model,
            system=instructions,
            user=user_text,
            max_tokens=self.max_tokens,
        )
        return output.strip()
