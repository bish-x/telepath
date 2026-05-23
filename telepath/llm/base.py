from __future__ import annotations

from typing import Protocol

from telepath.prompts import DEFAULT_TEXT_POLISH_PROMPT


class LLMError(RuntimeError):
    """Base class for LLM provider failures."""


class LLMUnavailable(LLMError):
    """LLM provider cannot be reached (missing credentials, binary, or transport)."""


class LLMFailure(LLMError):
    """LLM provider returned an error or invalid response."""


class TextPolisher(Protocol):
    def polish(self, text: str, prompt: str | None = None) -> str: ...


PROMPT_TEMPLATE = """{instructions}

Исходная расшифровка:
{text}
"""


def format_prompt(text: str, prompt: str | None) -> tuple[str, str]:
    """Return (instructions, user_text) pair ready for chat-style LLMs."""
    instructions = (prompt or DEFAULT_TEXT_POLISH_PROMPT).strip()
    return instructions, text.strip()


def format_single_prompt(text: str, prompt: str | None) -> str:
    """Render the legacy single-string prompt used by the Copilot CLI."""
    instructions, user_text = format_prompt(text, prompt)
    return PROMPT_TEMPLATE.format(instructions=instructions, text=user_text)
