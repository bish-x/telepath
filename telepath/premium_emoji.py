from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class PremiumEmoji:
    fallback: str
    custom_emoji_id: str


def extract_premium_emoji_ids(message: Any) -> list[PremiumEmoji]:
    text, entities = _message_text_and_entities(message)
    if not text or not entities:
        return []

    seen: set[str] = set()
    emojis: list[PremiumEmoji] = []
    for entity in entities:
        if not _is_custom_emoji_entity(entity):
            continue
        emoji_id = str(getattr(entity, "custom_emoji_id", "") or "")
        if not emoji_id or emoji_id in seen:
            continue
        seen.add(emoji_id)
        fallback = _slice_telegram_entity_text(
            text,
            int(getattr(entity, "offset", 0) or 0),
            int(getattr(entity, "length", 0) or 0),
        )
        emojis.append(PremiumEmoji(fallback=fallback, custom_emoji_id=emoji_id))
    return emojis


def format_premium_emoji_reply(emojis: Iterable[PremiumEmoji | tuple[str, str]]) -> str:
    rows = [_coerce_emoji(emoji) for emoji in emojis]
    if not rows:
        return ""
    if len(rows) == 1:
        return f"custom_emoji_id: {rows[0].custom_emoji_id}"
    lines = ["custom_emoji_ids:"]
    for emoji in rows:
        prefix = f"{emoji.fallback} " if emoji.fallback else ""
        lines.append(f"- {prefix}{emoji.custom_emoji_id}")
    return "\n".join(lines)


def _message_text_and_entities(message: Any) -> tuple[str, list[Any]]:
    text = getattr(message, "text", None)
    entities = getattr(message, "entities", None)
    if text and entities:
        return str(text), list(entities)

    caption = getattr(message, "caption", None)
    caption_entities = getattr(message, "caption_entities", None)
    if caption and caption_entities:
        return str(caption), list(caption_entities)

    return "", []


def _is_custom_emoji_entity(entity: Any) -> bool:
    entity_type = getattr(entity, "type", "")
    value = getattr(entity_type, "value", entity_type)
    return str(value) == "custom_emoji"


def _slice_telegram_entity_text(text: str, offset: int, length: int) -> str:
    if length <= 0:
        return ""
    encoded = text.encode("utf-16-le")
    start = offset * 2
    end = (offset + length) * 2
    return encoded[start:end].decode("utf-16-le", errors="ignore")


def _coerce_emoji(emoji: PremiumEmoji | tuple[str, str]) -> PremiumEmoji:
    if isinstance(emoji, PremiumEmoji):
        return emoji
    fallback, emoji_id = emoji
    return PremiumEmoji(fallback=str(fallback), custom_emoji_id=str(emoji_id))
