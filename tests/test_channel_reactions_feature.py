from dataclasses import dataclass

from telepath.features.channel_reactions import (
    ChannelMessageEvent,
    ChannelReactionFeature,
    ChannelReactionSettings,
    DEFAULT_REACTION_EMOJIS,
    ReactionCandidate,
    ReactionSendResult,
    order_reaction_candidates,
    reaction_category,
    select_reaction_candidates,
)


def candidate(emoji: str, *, category: str = "positive", kind: str = "emoji") -> ReactionCandidate:
    return ReactionCandidate(kind=kind, emoji=emoji, value=emoji, category=category)


def test_select_reaction_candidates_filters_positive_and_caps_non_premium_to_one():
    settings = ChannelReactionSettings(
        enabled=True,
        mode="positive",
        max_reactions=3,
        selection_strategy="priority",
    )
    available = [
        candidate("👍", category="positive"),
        candidate("👎", category="negative"),
        candidate("🔥", category="positive"),
    ]

    selected = select_reaction_candidates(available, settings, is_premium=False)

    assert [reaction.emoji for reaction in selected] == ["👍"]


def test_select_reaction_candidates_allows_three_for_premium_and_prefers_custom_reactions():
    settings = ChannelReactionSettings(enabled=True, mode="all", max_reactions=3, selection_strategy="priority")
    available = [
        candidate("👍"),
        candidate("⭐", kind="custom"),
        candidate("🔥"),
        candidate("👎", category="negative"),
    ]

    selected = select_reaction_candidates(available, settings, is_premium=True)

    assert [reaction.emoji for reaction in selected] == ["⭐", "👍", "🔥"]


def test_default_standard_reaction_categories_include_whale_and_unicorn_as_neutral():
    assert "🐳" in DEFAULT_REACTION_EMOJIS
    assert "🦄" in DEFAULT_REACTION_EMOJIS
    assert reaction_category("🐳") == "neutral"
    assert reaction_category("🦄") == "neutral"
    assert "🐳" in [reaction.emoji for reaction in select_reaction_candidates(
        [candidate("👍"), candidate("🐳", category=reaction_category("🐳"))],
        ChannelReactionSettings(enabled=True, mode="positive", max_reactions=3, selection_strategy="priority"),
        is_premium=True,
    )]


def test_default_standard_reaction_categories_mark_mocking_and_weird_reactions_as_negative():
    for emoji in ("🤣", "🤯", "🤡", "🥴", "🤨", "😐", "🤪", "🗿", "🤓"):
        assert reaction_category(emoji) == "negative"


def test_positive_mode_fills_with_neutral_before_negative_reactions():
    available = [
        candidate("👍", category="positive"),
        candidate("🔥", category="positive"),
        candidate("🐳", category="neutral"),
        candidate("👎", category="negative"),
    ]

    selected = select_reaction_candidates(
        available,
        ChannelReactionSettings(enabled=True, mode="positive", max_reactions=3, selection_strategy="priority"),
        is_premium=True,
    )

    assert [reaction.emoji for reaction in selected] == ["👍", "🔥", "🐳"]


def test_positive_mode_uses_neutral_but_never_negative_reactions():
    available = [
        candidate("👍", category="positive"),
        candidate("🤔", category="neutral"),
        candidate("👎", category="negative"),
        candidate("💩", category="negative"),
    ]

    selected = select_reaction_candidates(
        available,
        ChannelReactionSettings(enabled=True, mode="positive", max_reactions=3, selection_strategy="priority"),
        is_premium=True,
    )

    assert [reaction.emoji for reaction in selected] == ["👍", "🤔"]


def test_negative_mode_fills_with_neutral_and_excludes_positive_reactions():
    available = [
        candidate("👎", category="negative"),
        candidate("🐳", category="neutral"),
        candidate("🔥", category="positive"),
    ]

    selected = select_reaction_candidates(
        available,
        ChannelReactionSettings(enabled=True, mode="negative", max_reactions=3, selection_strategy="priority"),
        is_premium=True,
    )

    assert [reaction.emoji for reaction in selected] == ["👎", "🐳"]


def test_negative_mode_uses_neutral_but_never_positive_reactions():
    available = [
        candidate("👎", category="negative"),
        candidate("🤔", category="neutral"),
        candidate("👍", category="positive"),
        candidate("🔥", category="positive"),
    ]

    selected = select_reaction_candidates(
        available,
        ChannelReactionSettings(enabled=True, mode="negative", max_reactions=3, selection_strategy="priority"),
        is_premium=True,
    )

    assert [reaction.emoji for reaction in selected] == ["👎", "🤔"]


def test_reaction_source_prioritizes_standard_premium_and_mixed_pools():
    available = [
        candidate("👍", category="positive"),
        candidate("111", kind="custom", category="positive"),
        candidate("222", kind="custom", category="neutral"),
        candidate("🔥", category="positive"),
    ]

    standard = select_reaction_candidates(
        available,
        ChannelReactionSettings(
            enabled=True,
            mode="all",
            max_reactions=3,
            reaction_source="standard",
            selection_strategy="priority",
        ),
        is_premium=True,
    )
    premium = select_reaction_candidates(
        available,
        ChannelReactionSettings(
            enabled=True,
            mode="all",
            max_reactions=3,
            reaction_source="premium",
            selection_strategy="priority",
        ),
        is_premium=True,
    )
    mixed = select_reaction_candidates(
        available,
        ChannelReactionSettings(
            enabled=True,
            mode="all",
            max_reactions=3,
            reaction_source="mixed",
            selection_strategy="priority",
        ),
        is_premium=True,
    )

    assert [reaction.emoji for reaction in standard] == ["👍", "🔥", "111"]
    assert [reaction.emoji for reaction in premium] == ["111", "222", "👍"]
    assert [reaction.emoji for reaction in mixed] == ["111", "222", "👍"]


def test_non_premium_account_excludes_custom_reactions_even_when_premium_source_selected():
    available = [
        candidate("111", kind="custom", category="positive"),
        candidate("👍", category="positive"),
        candidate("🔥", category="positive"),
    ]

    selected = select_reaction_candidates(
        available,
        ChannelReactionSettings(
            enabled=True,
            mode="all",
            max_reactions=3,
            reaction_source="premium",
            selection_strategy="priority",
        ),
        is_premium=False,
    )

    assert [reaction.emoji for reaction in selected] == ["👍"]


def test_disabled_reactions_are_removed_from_available_pool_before_selection():
    available = [
        candidate("👍", category="positive"),
        candidate("🔥", category="positive"),
        candidate("111", kind="custom", category="positive"),
        candidate("👎", category="negative"),
    ]

    selected = select_reaction_candidates(
        available,
        ChannelReactionSettings(
            enabled=True,
            mode="all",
            max_reactions=3,
            selection_strategy="priority",
            disabled_emojis=("🔥", "111"),
        ),
        is_premium=True,
    )

    assert [reaction.emoji for reaction in selected] == ["👍", "👎"]


def test_random_positive_standard_source_uses_premium_fallback_before_neutral_reactions():
    available = [
        candidate("👍", category="positive"),
        candidate("111", kind="custom", category="positive"),
        candidate("🔥", category="positive"),
        candidate("🐳", category="neutral"),
        candidate("👎", category="negative"),
    ]

    ordered = order_reaction_candidates(
        available,
        ChannelReactionSettings(
            enabled=True,
            mode="positive",
            max_reactions=3,
            reaction_source="standard",
            selection_strategy="random",
        ),
        is_premium=True,
        chooser=lambda items: list(reversed(items)),
    )

    assert [reaction.emoji for reaction in ordered] == ["🔥", "👍", "111", "🐳"]
    assert [reaction.emoji for reaction in ordered[:3]] == ["🔥", "👍", "111"]


def test_random_premium_source_uses_standard_fallback_after_premium_reactions():
    available = [
        candidate("👍", category="positive"),
        candidate("111", kind="custom", category="positive"),
        candidate("222", kind="custom", category="neutral"),
        candidate("🔥", category="positive"),
    ]

    ordered = order_reaction_candidates(
        available,
        ChannelReactionSettings(
            enabled=True,
            mode="all",
            max_reactions=3,
            reaction_source="premium",
            selection_strategy="random",
        ),
        is_premium=True,
        chooser=lambda items: list(reversed(items)),
    )

    assert [reaction.emoji for reaction in ordered] == ["222", "111", "🔥", "👍"]


def test_premium_source_fills_short_unique_premium_pool_with_standard_reactions():
    selected = select_reaction_candidates(
        [
            candidate("111", kind="custom"),
            candidate("222", kind="custom"),
            candidate("👍"),
        ],
        ChannelReactionSettings(
            enabled=True,
            mode="all",
            max_reactions=3,
            reaction_source="premium",
            selection_strategy="priority",
        ),
        is_premium=True,
    )

    assert [reaction.emoji for reaction in selected] == ["111", "222", "👍"]


def test_standard_source_fills_short_unique_standard_pool_with_premium_reactions():
    selected = select_reaction_candidates(
        [
            candidate("👍"),
            candidate("🔥"),
            candidate("111", kind="custom"),
        ],
        ChannelReactionSettings(
            enabled=True,
            mode="all",
            max_reactions=3,
            reaction_source="standard",
            selection_strategy="priority",
        ),
        is_premium=True,
    )

    assert [reaction.emoji for reaction in selected] == ["👍", "🔥", "111"]


def test_positive_mode_keeps_positive_standard_before_neutral_premium_fallback():
    available = [
        candidate("👍", category="positive"),
        candidate("111", kind="custom", category="neutral"),
        candidate("👎", category="negative"),
    ]

    selected = select_reaction_candidates(
        available,
        ChannelReactionSettings(enabled=True, mode="positive", max_reactions=3, selection_strategy="priority"),
        is_premium=True,
    )

    assert [reaction.emoji for reaction in selected] == ["👍", "111"]


def test_positive_mixed_mode_uses_premium_neutral_fallback_before_standard_neutral_reactions():
    available = [
        candidate("⚡", category="positive"),
        candidate("👍", category="positive"),
        candidate("🎉", category="positive"),
        candidate("111", kind="custom", category="neutral"),
        candidate("🤔", category="neutral"),
    ]

    ordered = order_reaction_candidates(
        available,
        ChannelReactionSettings(
            enabled=True,
            mode="positive",
            max_reactions=3,
            reaction_source="mixed",
            selection_strategy="priority",
        ),
        is_premium=True,
    )
    selected = ordered[:3]

    assert [reaction.emoji for reaction in selected] == ["⚡", "👍", "🎉"]
    assert [reaction.emoji for reaction in ordered[3:]] == ["111", "🤔"]


def test_select_reaction_candidates_requires_manual_custom_category_for_positive_negative_filters():
    available = [
        candidate("👍", category="positive"),
        candidate("111", kind="custom", category="neutral"),
        candidate("222", kind="custom", category="negative"),
    ]

    positive = select_reaction_candidates(
        available,
        ChannelReactionSettings(
            enabled=True,
            mode="positive",
            max_reactions=3,
            selection_strategy="priority",
            emoji_categories={"111": "positive"},
        ),
        is_premium=True,
    )
    negative = select_reaction_candidates(
        available,
        ChannelReactionSettings(enabled=True, mode="negative", max_reactions=3, selection_strategy="priority"),
        is_premium=True,
    )

    assert [reaction.emoji for reaction in positive] == ["111", "👍"]
    assert [reaction.emoji for reaction in negative] == ["222", "111"]


def test_order_reaction_candidates_randomizes_within_premium_first_tiers():
    available = [
        candidate("👍"),
        candidate("111", kind="custom", category="neutral"),
        candidate("🔥"),
        candidate("222", kind="custom", category="neutral"),
        candidate("👎", category="negative"),
    ]

    ordered = order_reaction_candidates(
        available,
        ChannelReactionSettings(enabled=True, mode="all", selection_strategy="random", max_reactions=3),
        is_premium=True,
        chooser=lambda items: list(reversed(items)),
    )

    assert [reaction.emoji for reaction in ordered] == ["222", "111", "👎", "🔥", "👍"]
    assert [reaction.emoji for reaction in ordered[:3]] == ["222", "111", "👎"]


def test_select_reaction_candidates_supports_negative_and_custom_channel_filters():
    available = [
        candidate("👍", category="positive"),
        candidate("👎", category="negative"),
        candidate("💩", category="negative"),
        candidate("🔥", category="positive"),
    ]

    negative = select_reaction_candidates(
        available,
        ChannelReactionSettings(enabled=True, mode="negative", max_reactions=3, selection_strategy="priority"),
        is_premium=True,
    )
    custom = select_reaction_candidates(
        available,
        ChannelReactionSettings(
            enabled=True,
            mode="custom",
            selected_emojis=("🔥", "💩"),
            max_reactions=3,
            selection_strategy="priority",
        ),
        is_premium=True,
    )

    assert [reaction.emoji for reaction in negative] == ["👎", "💩"]
    assert [reaction.emoji for reaction in custom] == ["💩", "🔥"]


def test_legacy_custom_whitelist_still_combines_with_disabled_reaction_overrides():
    available = [
        candidate("👍", category="positive"),
        candidate("👎", category="negative"),
        candidate("🔥", category="positive"),
        candidate("💩", category="negative"),
    ]

    selected = select_reaction_candidates(
        available,
        ChannelReactionSettings(
            enabled=True,
            mode="custom",
            selected_emojis=("🔥", "💩"),
            disabled_emojis=("💩",),
            max_reactions=3,
            selection_strategy="priority",
        ),
        is_premium=True,
    )

    assert [reaction.emoji for reaction in selected] == ["🔥"]


class FakeReactionSettings:
    def __init__(self, channel_settings=None, *, premium=False, effective_settings=None):
        self.channel_settings = channel_settings or {}
        self.effective_settings = effective_settings if effective_settings is not None else self.channel_settings
        self.premium = premium
        self.premium_calls = 0
        self.available_reactions_updates = []
        self.autolike_enabled = True

    def is_reaction_autolike_enabled(self):
        return self.autolike_enabled

    def get_reaction_channel_settings(self, chat_id):
        return self.channel_settings.get(chat_id)

    def get_effective_reaction_channel_settings(self, chat_id):
        return self.effective_settings.get(chat_id)

    def is_account_premium(self):
        self.premium_calls += 1
        return self.premium

    def replace_reaction_channel_available_reactions(self, chat_id, reactions):
        self.available_reactions_updates.append((chat_id, tuple((r.kind, r.emoji, r.category) for r in reactions)))


class FakeProcessed:
    def __init__(self, already_processed=False):
        self.already_processed = already_processed
        self.is_processed_calls = []
        self.mark_processed_calls = []

    def is_processed(self, chat_id, message_id, feature):
        self.is_processed_calls.append((chat_id, message_id, feature))
        return self.already_processed

    def mark_processed(self, chat_id, message_id, feature):
        self.mark_processed_calls.append((chat_id, message_id, feature))
        return True


class FakeReactionSender:
    def __init__(self, available=None):
        self.available = [candidate("👍")] if available is None else available
        self.sent = []

    async def available_reactions(self, chat_id):
        return self.available

    async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
        self.sent.append((event.chat_id, event.message_id, tuple(r.emoji for r in reactions), max_reactions, ()))
        return len(reactions)


class FakeFallbackReactionSender(FakeReactionSender):
    async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
        self.sent.append(
            (
                event.chat_id,
                event.message_id,
                tuple(r.emoji for r in reactions),
                max_reactions,
                tuple(r.emoji for r in fallback_reactions),
            )
        )
        return len(reactions)


class ZeroReactionSender(FakeFallbackReactionSender):
    async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
        await super().send_reactions(
            event,
            reactions,
            max_reactions=max_reactions,
            fallback_reactions=fallback_reactions,
        )
        return 0


class ActualResultReactionSender(FakeFallbackReactionSender):
    def __init__(self, available=None, actual_results=()):
        super().__init__(available)
        self.actual_results = list(actual_results)

    async def send_reactions(self, event, reactions, *, max_reactions, fallback_reactions=()):
        await super().send_reactions(
            event,
            reactions,
            max_reactions=max_reactions,
            fallback_reactions=fallback_reactions,
        )
        if self.actual_results:
            return self.actual_results.pop(0)
        return len(reactions)


@dataclass
class Context:
    reaction_settings: FakeReactionSettings
    reaction_sender: FakeReactionSender
    processed: FakeProcessed


def make_event(*, chat_id=-100123, message_id=50, is_channel=True, is_group=False, grouped_id=None):
    return ChannelMessageEvent(
        chat_id=chat_id,
        message_id=message_id,
        is_channel=is_channel,
        is_group=is_group,
        grouped_id=grouped_id,
        message=object(),
    )


async def test_channel_reaction_feature_sends_configured_channel_reactions():
    context = Context(
        FakeReactionSettings(
            {
                -100123: ChannelReactionSettings(
                    enabled=True,
                    mode="all",
                    max_reactions=3,
                    selection_strategy="priority",
                )
            },
            premium=True,
        ),
        FakeReactionSender([candidate("👍"), candidate("🔥"), candidate("👎", category="negative")]),
        FakeProcessed(),
    )

    result = await ChannelReactionFeature().handle(make_event(), context)

    assert result == "channel_reactions_sent"
    assert context.reaction_sender.sent == [(-100123, 50, ("👍", "🔥", "👎"), 3, ())]
    assert context.processed.mark_processed_calls == [(-100123, 50, "channel_reactions")]


async def test_channel_reaction_feature_uses_folder_fallback_reaction_settings():
    context = Context(
        FakeReactionSettings(
            channel_settings={},
            effective_settings={
                -100123: ChannelReactionSettings(
                    enabled=True,
                    mode="all",
                    max_reactions=3,
                    selection_strategy="priority",
                )
            },
            premium=True,
        ),
        FakeReactionSender([candidate("👍"), candidate("🔥"), candidate("👎", category="negative")]),
        FakeProcessed(),
    )

    result = await ChannelReactionFeature().handle(make_event(), context)

    assert result == "channel_reactions_sent"
    assert context.reaction_sender.sent == [(-100123, 50, ("👍", "🔥", "👎"), 3, ())]


async def test_channel_reaction_feature_skips_disabled_channels_and_groups():
    context = Context(
        FakeReactionSettings({-100123: ChannelReactionSettings(enabled=False)}),
        FakeReactionSender(),
        FakeProcessed(),
    )
    feature = ChannelReactionFeature()

    assert await feature.handle(make_event(), context) == "channel_reactions_disabled"
    assert await feature.handle(make_event(is_channel=True, is_group=True), context) == "not_a_channel_post"
    assert context.reaction_sender.sent == []


async def test_channel_reaction_feature_skips_when_autolike_is_globally_disabled():
    settings = FakeReactionSettings({-100123: ChannelReactionSettings(enabled=True)})
    settings.autolike_enabled = False
    context = Context(settings, FakeReactionSender(), FakeProcessed())

    result = await ChannelReactionFeature().handle(make_event(), context)

    assert result == "channel_reactions_globally_disabled"
    assert context.reaction_sender.sent == []


async def test_channel_reaction_feature_caps_to_one_reaction_when_account_is_not_premium():
    context = Context(
        FakeReactionSettings(
            {
                -100123: ChannelReactionSettings(
                    enabled=True,
                    mode="all",
                    max_reactions=3,
                    selection_strategy="priority",
                )
            },
            premium=False,
        ),
        FakeReactionSender([candidate("👍"), candidate("🔥"), candidate("👎", category="negative")]),
        FakeProcessed(),
    )

    result = await ChannelReactionFeature().handle(make_event(), context)

    assert result == "channel_reactions_sent"
    assert context.reaction_sender.sent == [(-100123, 50, ("👍",), 1, ())]


async def test_channel_reaction_feature_passes_random_fallback_reactions():
    context = Context(
        FakeReactionSettings(
            {
                -100123: ChannelReactionSettings(
                    enabled=True,
                    mode="all",
                    selection_strategy="random",
                    max_reactions=1,
                )
            },
            premium=False,
        ),
        FakeFallbackReactionSender([candidate("👍"), candidate("🔥"), candidate("🎉")]),
        FakeProcessed(),
    )

    result = await ChannelReactionFeature(chooser=lambda items: list(reversed(items))).handle(
        make_event(),
        context,
    )

    assert result == "channel_reactions_sent"
    assert context.reaction_sender.sent == [(-100123, 50, ("🎉",), 1, ("🔥", "👍"))]


async def test_channel_reaction_feature_avoids_repeating_same_random_set_for_channel():
    context = Context(
        FakeReactionSettings(
            {
                -100123: ChannelReactionSettings(
                    enabled=True,
                    mode="all",
                    selection_strategy="random",
                    max_reactions=3,
                )
            },
            premium=True,
        ),
        FakeFallbackReactionSender(
            [
                candidate("👍"),
                candidate("🔥"),
                candidate("🎉"),
                candidate("🤔", category="neutral"),
            ]
        ),
        FakeProcessed(),
    )
    feature = ChannelReactionFeature(chooser=lambda items: list(items))

    first = await feature.handle(make_event(message_id=50), context)
    second = await feature.handle(make_event(message_id=51), context)

    assert first == "channel_reactions_sent"
    assert second == "channel_reactions_sent"
    assert context.reaction_sender.sent == [
        (-100123, 50, ("👍", "🔥", "🎉"), 3, ("🤔",)),
        (-100123, 51, ("👍", "🔥", "🤔"), 3, ("🎉",)),
    ]


async def test_channel_reaction_feature_tracks_actual_random_reactions_after_sender_fallback():
    context = Context(
        FakeReactionSettings(
            {
                -100123: ChannelReactionSettings(
                    enabled=True,
                    mode="all",
                    selection_strategy="random",
                    max_reactions=3,
                )
            },
            premium=True,
        ),
        ActualResultReactionSender(
            [
                candidate("👍"),
                candidate("🔥"),
                candidate("🎉"),
                candidate("🤔", category="neutral"),
            ],
            actual_results=[
                ReactionSendResult(
                    count=3,
                    reaction_keys=(("emoji", "👍"), ("emoji", "🔥"), ("emoji", "🤔")),
                )
            ],
        ),
        FakeProcessed(),
    )
    feature = ChannelReactionFeature(chooser=lambda items: list(items))

    first = await feature.handle(make_event(message_id=50), context)
    second = await feature.handle(make_event(message_id=51), context)

    assert first == "channel_reactions_sent"
    assert second == "channel_reactions_sent"
    assert context.reaction_sender.sent == [
        (-100123, 50, ("👍", "🔥", "🎉"), 3, ("🤔",)),
        (-100123, 51, ("👍", "🔥", "🎉"), 3, ("🤔",)),
    ]


async def test_channel_reaction_feature_reads_premium_status_once_per_message():
    settings = FakeReactionSettings(
        {
            -100123: ChannelReactionSettings(
                enabled=True,
                mode="all",
                max_reactions=3,
                selection_strategy="priority",
            )
        },
        premium=True,
    )
    context = Context(
        settings,
        FakeReactionSender([candidate("👍"), candidate("🔥"), candidate("👎", category="negative")]),
        FakeProcessed(),
    )

    result = await ChannelReactionFeature().handle(make_event(), context)

    assert result == "channel_reactions_sent"
    assert settings.premium_calls == 1


async def test_channel_reaction_feature_does_not_mark_processed_when_no_reactions_available():
    processed = FakeProcessed()
    context = Context(
        FakeReactionSettings(
            {
                -100123: ChannelReactionSettings(
                    enabled=True,
                    mode="all",
                    max_reactions=3,
                    selection_strategy="priority",
                )
            },
            premium=True,
        ),
        FakeReactionSender([]),
        processed,
    )

    result = await ChannelReactionFeature().handle(make_event(), context)

    assert result == "no_reactions_available"
    assert processed.mark_processed_calls == []


async def test_channel_reaction_feature_does_not_mark_processed_when_sender_sends_zero_reactions():
    processed = FakeProcessed()
    context = Context(
        FakeReactionSettings(
            {
                -100123: ChannelReactionSettings(
                    enabled=True,
                    mode="positive",
                    max_reactions=3,
                    selection_strategy="priority",
                )
            },
            premium=True,
        ),
        ZeroReactionSender(
            [
                candidate("👍", category="positive"),
                candidate("🔥", category="positive"),
                candidate("🤔", category="neutral"),
            ]
        ),
        processed,
    )

    result = await ChannelReactionFeature().handle(make_event(), context)

    assert result == "no_reactions_sent"
    assert context.reaction_sender.sent == [(-100123, 50, ("👍", "🔥", "🤔"), 3, ())]
    assert processed.mark_processed_calls == []


async def test_channel_reaction_feature_records_observed_available_reactions():
    settings = FakeReactionSettings(
        {
            -100123: ChannelReactionSettings(
                enabled=True,
                mode="all",
                max_reactions=3,
                selection_strategy="priority",
            )
        },
        premium=True,
    )
    context = Context(
        settings,
        FakeReactionSender(
            [
                candidate("👍"),
                candidate("👎", category="negative"),
                candidate("1234567890123", kind="custom"),
            ]
        ),
        FakeProcessed(),
    )

    result = await ChannelReactionFeature().handle(make_event(), context)

    assert result == "channel_reactions_sent"
    assert settings.available_reactions_updates == [
        (
            -100123,
            (
                ("emoji", "👍", "positive"),
                ("emoji", "👎", "negative"),
                ("custom", "1234567890123", "positive"),
            ),
        )
    ]


async def test_channel_reaction_feature_skips_duplicate_media_groups():
    processed = FakeProcessed(already_processed=True)
    context = Context(
        FakeReactionSettings({-100123: ChannelReactionSettings(enabled=True)}, premium=True),
        FakeReactionSender(),
        processed,
    )

    result = await ChannelReactionFeature().handle(make_event(grouped_id=999), context)

    assert result == "already_processed_media_group"
    assert processed.is_processed_calls == [(-100123, 999, "channel_reactions_media_group")]
    assert context.reaction_sender.sent == []
