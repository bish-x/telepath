"""Pluggable LLM text polishers."""
from __future__ import annotations

from typing import TYPE_CHECKING

from telepath.llm.anthropic_api import AnthropicTextPolisher
from telepath.llm.base import (
    LLMError,
    LLMFailure,
    LLMUnavailable,
    TextPolisher,
)
from telepath.llm.copilot_cli import CopilotCliTextPolisher
from telepath.llm.openai_api import OpenAITextPolisher

if TYPE_CHECKING:
    from telepath.config import Settings


SUPPORTED_PROVIDERS = ("copilot", "openai", "anthropic")


def build_polisher(settings: "Settings") -> TextPolisher:
    provider = (settings.llm_provider or "copilot").strip().lower()
    if provider == "copilot":
        return CopilotCliTextPolisher(
            command=settings.copilot_command,
            model=settings.copilot_model,
            timeout_seconds=settings.copilot_timeout_seconds,
        )
    if provider == "openai":
        return OpenAITextPolisher(
            api_key=settings.openai_api_key or "",
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            timeout_seconds=settings.openai_timeout_seconds,
        )
    if provider == "anthropic":
        return AnthropicTextPolisher(
            api_key=settings.anthropic_api_key or "",
            model=settings.anthropic_model,
            base_url=settings.anthropic_base_url,
            timeout_seconds=settings.anthropic_timeout_seconds,
        )
    raise LLMUnavailable(
        f"Unknown LLM_PROVIDER={settings.llm_provider!r}. Supported: {', '.join(SUPPORTED_PROVIDERS)}."
    )


__all__ = [
    "AnthropicTextPolisher",
    "CopilotCliTextPolisher",
    "LLMError",
    "LLMFailure",
    "LLMUnavailable",
    "OpenAITextPolisher",
    "SUPPORTED_PROVIDERS",
    "TextPolisher",
    "build_polisher",
]
