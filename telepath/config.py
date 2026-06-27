from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


DEFAULT_COPILOT_MODEL = "gpt-5-mini"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_TRANSCRIPTION_DECORATION_CUSTOM_EMOJI_ID = "5460795800101594035"

SUPPORTED_LLM_PROVIDERS = ("copilot", "openai", "anthropic")


@dataclass(frozen=True)
class TelegramUserSettings:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session: str


@dataclass(frozen=True)
class Settings:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session: str
    manager_bot_token: str
    owner_id: int
    database_path: Path
    llm_provider: str = "copilot"
    copilot_command: str = "copilot"
    copilot_model: str | None = DEFAULT_COPILOT_MODEL
    copilot_timeout_seconds: int = 300
    openai_api_key: str | None = None
    openai_model: str = DEFAULT_OPENAI_MODEL
    openai_base_url: str | None = None
    openai_timeout_seconds: int = 60
    anthropic_api_key: str | None = None
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    anthropic_base_url: str | None = None
    anthropic_timeout_seconds: int = 60
    private_history_throttle_seconds: float = 5.0
    transcription_decoration_custom_emoji_id: str | None = DEFAULT_TRANSCRIPTION_DECORATION_CUSTOM_EMOJI_ID
    post_mirror_online_freshness_seconds: int = 180
    post_mirror_outbox_poll_seconds: float = 30.0
    post_mirror_delivery_delay_range_seconds: tuple[int, int] = (60, 120)


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _resolve_llm_provider() -> str:
    raw = (os.getenv("LLM_PROVIDER") or "copilot").strip().lower()
    if raw not in SUPPORTED_LLM_PROVIDERS:
        raise RuntimeError(
            f"Unsupported LLM_PROVIDER={raw!r}. Supported: {', '.join(SUPPORTED_LLM_PROVIDERS)}."
        )
    return raw


def _validate_provider_credentials(provider: str, openai_key: str | None, anthropic_key: str | None) -> None:
    if provider == "openai" and not openai_key:
        raise RuntimeError("LLM_PROVIDER=openai requires OPENAI_API_KEY to be set.")
    if provider == "anthropic" and not anthropic_key:
        raise RuntimeError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY to be set.")


def load_settings() -> Settings:
    database_path = Path(os.getenv("TG_ASSISTANT_DB", "data/assistant.sqlite3"))
    provider = _resolve_llm_provider()
    openai_api_key = _optional_env("OPENAI_API_KEY")
    anthropic_api_key = _optional_env("ANTHROPIC_API_KEY")
    _validate_provider_credentials(provider, openai_api_key, anthropic_api_key)
    owner_id = int(_require_env("TG_OWNER_ID"))
    if owner_id <= 0:
        raise RuntimeError(
            "TG_OWNER_ID must be a positive integer (your Telegram user ID). "
            "Run ./scripts/setup.sh or ./scripts/auth.sh to set it from the auth step."
        )
    return Settings(
        telegram_api_id=int(_require_env("TG_API_ID")),
        telegram_api_hash=_require_env("TG_API_HASH"),
        telegram_session=os.getenv("TG_SESSION", "telepath"),
        manager_bot_token=_require_env("TG_MANAGER_BOT_TOKEN"),
        owner_id=owner_id,
        database_path=database_path,
        llm_provider=provider,
        copilot_command=os.getenv("COPILOT_COMMAND", "copilot"),
        copilot_model=os.getenv("COPILOT_MODEL") or DEFAULT_COPILOT_MODEL,
        copilot_timeout_seconds=int(os.getenv("COPILOT_TIMEOUT_SECONDS", "300")),
        openai_api_key=openai_api_key,
        openai_model=os.getenv("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL,
        openai_base_url=_optional_env("OPENAI_BASE_URL"),
        openai_timeout_seconds=int(os.getenv("OPENAI_TIMEOUT_SECONDS", "60")),
        anthropic_api_key=anthropic_api_key,
        anthropic_model=os.getenv("ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL,
        anthropic_base_url=_optional_env("ANTHROPIC_BASE_URL"),
        anthropic_timeout_seconds=int(os.getenv("ANTHROPIC_TIMEOUT_SECONDS", "60")),
        private_history_throttle_seconds=float(os.getenv("PRIVATE_HISTORY_THROTTLE_SECONDS", "5")),
        transcription_decoration_custom_emoji_id=os.getenv(
            "TRANSCRIPTION_DECORATION_CUSTOM_EMOJI_ID",
            DEFAULT_TRANSCRIPTION_DECORATION_CUSTOM_EMOJI_ID,
        ) or None,
        post_mirror_online_freshness_seconds=int(os.getenv("POST_MIRROR_ONLINE_FRESHNESS_SECONDS", "180")),
        post_mirror_outbox_poll_seconds=float(os.getenv("POST_MIRROR_OUTBOX_POLL_SECONDS", "30")),
        post_mirror_delivery_delay_range_seconds=(
            int(os.getenv("POST_MIRROR_DELIVERY_DELAY_MIN_SECONDS", "60")),
            int(os.getenv("POST_MIRROR_DELIVERY_DELAY_MAX_SECONDS", "120")),
        ),
    )


def load_telegram_user_settings() -> TelegramUserSettings:
    return TelegramUserSettings(
        telegram_api_id=int(_require_env("TG_API_ID")),
        telegram_api_hash=_require_env("TG_API_HASH"),
        telegram_session=os.getenv("TG_SESSION", "telepath"),
    )
