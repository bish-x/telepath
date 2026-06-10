from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


# Official Bot API ReactionTypeEmoji set, kept in Bot API order for deterministic
# fallback when Telegram says a channel allows all emoji reactions.
STANDARD_REACTION_CATEGORIES: dict[str, str] = {
    "⭐": "positive",
    "❤": "positive",
    "👍": "positive",
    "👎": "negative",
    "🔥": "positive",
    "🥰": "positive",
    "👏": "positive",
    "😁": "positive",
    "🤔": "neutral",
    "🤯": "negative",
    "😱": "negative",
    "🤬": "negative",
    "😢": "negative",
    "🎉": "positive",
    "🤩": "positive",
    "🤮": "negative",
    "💩": "negative",
    "🙏": "positive",
    "👌": "positive",
    "🕊": "positive",
    "🤡": "negative",
    "🥱": "neutral",
    "🥴": "negative",
    "😍": "positive",
    "🐳": "neutral",
    "❤‍🔥": "positive",
    "🌚": "neutral",
    "🌭": "neutral",
    "💯": "positive",
    "🤣": "negative",
    "⚡": "positive",
    "🍌": "neutral",
    "🏆": "positive",
    "💔": "negative",
    "🤨": "negative",
    "😐": "negative",
    "🍓": "positive",
    "🍾": "positive",
    "💋": "positive",
    "🖕": "negative",
    "😈": "neutral",
    "😴": "neutral",
    "😭": "negative",
    "🤓": "negative",
    "👻": "neutral",
    "👨‍💻": "neutral",
    "👀": "neutral",
    "🎃": "neutral",
    "🙈": "neutral",
    "😇": "positive",
    "😨": "negative",
    "🤝": "positive",
    "✍": "neutral",
    "🤗": "positive",
    "🫡": "positive",
    "🎅": "positive",
    "🎄": "positive",
    "☃": "positive",
    "💅": "positive",
    "🤪": "negative",
    "🗿": "negative",
    "🆒": "positive",
    "💘": "positive",
    "🙉": "neutral",
    "🦄": "neutral",
    "😘": "positive",
    "💊": "neutral",
    "🙊": "neutral",
    "😎": "positive",
    "👾": "neutral",
    "🤷‍♂": "neutral",
    "🤷": "neutral",
    "🤷‍♀": "neutral",
    "😡": "negative",
}

DEFAULT_REACTION_EMOJIS: tuple[str, ...] = tuple(STANDARD_REACTION_CATEGORIES)

VALID_REACTION_MODES: frozenset[str] = frozenset({"all", "positive", "negative", "custom"})
VALID_REACTION_SELECTION_STRATEGIES: frozenset[str] = frozenset({"priority", "random"})
VALID_REACTION_CATEGORIES: frozenset[str] = frozenset({"positive", "negative", "neutral"})
VALID_REACTION_SOURCES: frozenset[str] = frozenset({"mixed", "standard", "premium"})
PREMIUM_REACTION_KINDS: frozenset[str] = frozenset({"custom", "premium"})

ReactionChooser = Callable[[list["ReactionCandidate"]], list["ReactionCandidate"]]
ReactionKey = tuple[str, str]


@dataclass(frozen=True)
class ReactionCandidate:
    kind: str
    emoji: str
    value: Any
    category: str = "positive"


@dataclass(frozen=True)
class ReactionSendResult:
    count: int
    reaction_keys: tuple[ReactionKey, ...] = ()


@dataclass(frozen=True)
class ChannelReactionSettings:
    enabled: bool = False
    mode: str = "positive"
    selected_emojis: tuple[str, ...] = ()
    disabled_emojis: tuple[str, ...] = ()
    max_reactions: int = 3
    selection_strategy: str = "random"
    reaction_source: str = "mixed"
    emoji_categories: dict[str, str] = field(default_factory=dict)
    title: str | None = None


@dataclass(frozen=True)
class ChannelMessageEvent:
    chat_id: int
    message_id: int
    is_channel: bool
    is_group: bool
    grouped_id: int | None = None
    message: Any | None = None


class ReactionSettingsPort(Protocol):
    def is_reaction_autolike_enabled(self) -> bool: ...
    def get_reaction_channel_settings(self, chat_id: int) -> ChannelReactionSettings | None: ...
    def get_effective_reaction_channel_settings(self, chat_id: int) -> ChannelReactionSettings | None: ...
    def is_account_premium(self) -> bool: ...
    def replace_reaction_channel_available_reactions(
        self,
        chat_id: int,
        reactions: list[ReactionCandidate],
    ) -> None: ...


class ReactionSenderPort(Protocol):
    async def available_reactions(self, chat_id: int) -> list[ReactionCandidate]: ...
    async def send_reactions(
        self,
        event: ChannelMessageEvent,
        reactions: list[ReactionCandidate],
        *,
        max_reactions: int,
        fallback_reactions: list[ReactionCandidate] | tuple[ReactionCandidate, ...] = (),
    ) -> int | ReactionSendResult: ...


class ProcessedMessagesPort(Protocol):
    def is_processed(self, chat_id: int, message_id: int, feature: str) -> bool: ...
    def mark_processed(self, chat_id: int, message_id: int, feature: str) -> bool: ...


class ChannelReactionContext(Protocol):
    reaction_settings: ReactionSettingsPort
    reaction_sender: ReactionSenderPort
    processed: ProcessedMessagesPort


def reaction_category(emoji: str) -> str:
    return STANDARD_REACTION_CATEGORIES.get(emoji, "neutral")


def effective_reaction_category(reaction: ReactionCandidate, settings: ChannelReactionSettings) -> str:
    category = settings.emoji_categories.get(reaction.emoji, reaction.category)
    return category if category in VALID_REACTION_CATEGORIES else "neutral"


def is_premium_reaction(reaction: ReactionCandidate) -> bool:
    return reaction.kind in PREMIUM_REACTION_KINDS


def _normalized_reaction_source(settings: ChannelReactionSettings) -> str:
    return settings.reaction_source if settings.reaction_source in VALID_REACTION_SOURCES else "mixed"


def effective_max_reactions(settings: ChannelReactionSettings, *, is_premium: bool) -> int:
    requested = settings.max_reactions if settings.max_reactions in {1, 3} else 1
    return min(requested, 3 if is_premium else 1)


def _filtered_reaction_candidates(
    available: list[ReactionCandidate],
    settings: ChannelReactionSettings,
) -> list[ReactionCandidate]:
    if not settings.enabled:
        return []

    mode = settings.mode if settings.mode in VALID_REACTION_MODES else "positive"
    selected = set(settings.selected_emojis)
    disabled = set(settings.disabled_emojis)
    filtered = []
    for reaction in available:
        if reaction.emoji in disabled:
            continue
        if mode == "custom" and reaction.emoji not in selected:
            continue
        category = effective_reaction_category(reaction, settings)
        if mode == "positive" and category == "negative":
            continue
        if mode == "negative" and category == "positive":
            continue
        filtered.append(reaction)
    return filtered


def _category_priority(category: str, mode: str) -> int:
    if mode == "positive":
        return {"positive": 0, "neutral": 1, "negative": 2}.get(category, 1)
    if mode == "negative":
        return {"negative": 0, "neutral": 1, "positive": 2}.get(category, 1)
    return 0


def _source_priority(reaction: ReactionCandidate, source: str) -> int:
    premium = is_premium_reaction(reaction)
    if source == "standard":
        return 0 if not premium else 1
    return 0 if premium else 1


def _priority_key(reaction: ReactionCandidate, settings: ChannelReactionSettings, mode: str) -> tuple[int, int]:
    category = effective_reaction_category(reaction, settings)
    return (
        _category_priority(category, mode),
        _source_priority(reaction, _normalized_reaction_source(settings)),
    )


def _randomized_group(
    reactions: list[ReactionCandidate],
    chooser: ReactionChooser | None,
) -> list[ReactionCandidate]:
    if chooser is None:
        return random.sample(reactions, k=len(reactions))

    chosen = chooser(list(reactions))
    ordered: list[ReactionCandidate] = []
    seen: set[tuple[str, str]] = set()
    for reaction in chosen:
        key = (reaction.kind, reaction.emoji)
        if key in seen or reaction not in reactions:
            continue
        ordered.append(reaction)
        seen.add(key)
    for reaction in reactions:
        key = (reaction.kind, reaction.emoji)
        if key not in seen:
            ordered.append(reaction)
            seen.add(key)
    return ordered


def order_reaction_candidates(
    available: list[ReactionCandidate],
    settings: ChannelReactionSettings,
    *,
    is_premium: bool,
    chooser: ReactionChooser | None = None,
) -> list[ReactionCandidate]:
    filtered = _filtered_reaction_candidates(available, settings)
    if not is_premium:
        filtered = [reaction for reaction in filtered if not is_premium_reaction(reaction)]
    if not filtered:
        return []

    mode = settings.mode if settings.mode in VALID_REACTION_MODES else "positive"
    strategy = (
        settings.selection_strategy
        if settings.selection_strategy in VALID_REACTION_SELECTION_STRATEGIES
        else "priority"
    )
    indexed = list(enumerate(filtered))
    tiers = sorted({_priority_key(reaction, settings, mode) for reaction in filtered})
    ordered: list[ReactionCandidate] = []
    for tier in tiers:
        tier_reactions = [
            reaction
            for _, reaction in indexed
            if _priority_key(reaction, settings, mode) == tier
        ]
        if strategy == "random":
            ordered.extend(_randomized_group(tier_reactions, chooser))
        else:
            ordered.extend(tier_reactions)
    return ordered


def reaction_candidate_key(reaction: ReactionCandidate) -> ReactionKey:
    return (reaction.kind, reaction.emoji)


def select_from_ordered_reaction_candidates(
    ordered: list[ReactionCandidate],
    max_reactions: int,
    *,
    avoid_reaction_keys: frozenset[ReactionKey] | None = None,
) -> list[ReactionCandidate]:
    selected = ordered[:max_reactions]
    if not selected or not avoid_reaction_keys:
        return selected

    selected_keys = frozenset(reaction_candidate_key(reaction) for reaction in selected)
    if selected_keys != avoid_reaction_keys:
        return selected

    for candidate in ordered[max_reactions:]:
        if reaction_candidate_key(candidate) not in avoid_reaction_keys:
            return [*selected[:-1], candidate]
    return selected


def fallback_reaction_candidates(
    ordered: list[ReactionCandidate],
    selected: list[ReactionCandidate],
) -> list[ReactionCandidate]:
    selected_keys = {reaction_candidate_key(reaction) for reaction in selected}
    return [
        reaction
        for reaction in ordered
        if reaction_candidate_key(reaction) not in selected_keys
    ]


def select_reaction_candidates(
    available: list[ReactionCandidate],
    settings: ChannelReactionSettings,
    *,
    is_premium: bool,
    chooser: ReactionChooser | None = None,
    avoid_reaction_keys: frozenset[ReactionKey] | None = None,
) -> list[ReactionCandidate]:
    ordered = order_reaction_candidates(
        available,
        settings,
        is_premium=is_premium,
        chooser=chooser,
    )
    return select_from_ordered_reaction_candidates(
        ordered,
        effective_max_reactions(settings, is_premium=is_premium),
        avoid_reaction_keys=avoid_reaction_keys,
    )


def sent_reaction_count(result: Any, selected: list[ReactionCandidate]) -> int:
    if isinstance(result, int):
        return max(0, result)
    count = getattr(result, "count", None)
    if count is not None:
        try:
            return max(0, int(count))
        except (TypeError, ValueError):
            return 0
    return len(selected)


def sent_reaction_keys(result: Any) -> frozenset[ReactionKey] | None:
    reaction_keys = getattr(result, "reaction_keys", None)
    if reaction_keys is None:
        return None
    normalized: set[ReactionKey] = set()
    for key in reaction_keys:
        if not isinstance(key, tuple) or len(key) != 2:
            continue
        kind, emoji = key
        normalized.add((str(kind), str(emoji)))
    return frozenset(normalized) if normalized else None


class ChannelReactionFeature:
    name = "channel_reactions"
    media_group_feature = "channel_reactions_media_group"

    def __init__(self, *, chooser: ReactionChooser | None = None):
        self.chooser = chooser
        self._last_random_reaction_keys_by_chat: dict[int, frozenset[ReactionKey]] = {}

    def can_handle(self, event: object) -> bool:
        return isinstance(event, ChannelMessageEvent)

    async def handle(self, event: ChannelMessageEvent, context: ChannelReactionContext) -> str:
        if not event.is_channel or event.is_group:
            return "not_a_channel_post"

        if not context.reaction_settings.is_reaction_autolike_enabled():
            return "channel_reactions_globally_disabled"

        if event.grouped_id is not None and context.processed.is_processed(
            event.chat_id,
            int(event.grouped_id),
            self.media_group_feature,
        ):
            return "already_processed_media_group"

        settings = context.reaction_settings.get_effective_reaction_channel_settings(event.chat_id)
        if settings is None or not settings.enabled:
            return "channel_reactions_disabled"

        if context.processed.is_processed(event.chat_id, event.message_id, self.name):
            return "already_processed"

        available = await context.reaction_sender.available_reactions(event.chat_id)
        context.reaction_settings.replace_reaction_channel_available_reactions(event.chat_id, available)
        is_premium = context.reaction_settings.is_account_premium()
        max_reactions = effective_max_reactions(settings, is_premium=is_premium)
        ordered = order_reaction_candidates(
            available,
            settings,
            is_premium=is_premium,
            chooser=self.chooser,
        )
        avoid_keys = (
            self._last_random_reaction_keys_by_chat.get(event.chat_id)
            if settings.selection_strategy == "random"
            else None
        )
        selected = select_from_ordered_reaction_candidates(
            ordered,
            max_reactions,
            avoid_reaction_keys=avoid_keys,
        )
        if not selected:
            return "no_reactions_available"

        sent_reactions = await context.reaction_sender.send_reactions(
            event,
            selected,
            max_reactions=max_reactions,
            fallback_reactions=fallback_reaction_candidates(ordered, selected),
        )
        sent_count = sent_reaction_count(sent_reactions, selected)
        if sent_count <= 0:
            return "no_reactions_sent"
        if settings.selection_strategy == "random":
            actual_reaction_keys = sent_reaction_keys(sent_reactions)
            self._last_random_reaction_keys_by_chat[event.chat_id] = frozenset(
                reaction_candidate_key(reaction)
                for reaction in selected
            ) if actual_reaction_keys is None else actual_reaction_keys
        context.processed.mark_processed(event.chat_id, event.message_id, self.name)
        if event.grouped_id is not None:
            context.processed.mark_processed(event.chat_id, int(event.grouped_id), self.media_group_feature)
        return "channel_reactions_sent"
