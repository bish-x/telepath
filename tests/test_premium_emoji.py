from aiogram.types import MessageEntity

from telepath.premium_emoji import extract_premium_emoji_ids, format_premium_emoji_reply


class FakeMessage:
    def __init__(self, *, text=None, caption=None, entities=None, caption_entities=None):
        self.text = text
        self.caption = caption
        self.entities = entities
        self.caption_entities = caption_entities


def test_extract_premium_emoji_ids_from_message_entities():
    message = FakeMessage(
        text="A ⭐ B",
        entities=[
            MessageEntity(type="custom_emoji", offset=2, length=1, custom_emoji_id="1234567890"),
        ],
    )

    emojis = extract_premium_emoji_ids(message)

    assert [(emoji.fallback, emoji.custom_emoji_id) for emoji in emojis] == [("⭐", "1234567890")]


def test_extract_premium_emoji_ids_supports_caption_entities_and_deduplicates():
    message = FakeMessage(
        caption="⭐ ⭐",
        caption_entities=[
            MessageEntity(type="custom_emoji", offset=0, length=1, custom_emoji_id="123"),
            MessageEntity(type="custom_emoji", offset=2, length=1, custom_emoji_id="123"),
        ],
    )

    emojis = extract_premium_emoji_ids(message)

    assert [(emoji.fallback, emoji.custom_emoji_id) for emoji in emojis] == [("⭐", "123")]


def test_format_premium_emoji_reply_for_one_and_many():
    one = format_premium_emoji_reply([("⭐", "123")])
    many = format_premium_emoji_reply([("⭐", "123"), ("🔥", "456")])

    assert one == "custom_emoji_id: 123"
    assert many == "custom_emoji_ids:\n- ⭐ 123\n- 🔥 456"


def test_extract_premium_emoji_ids_returns_empty_for_blank_message():
    # No text/caption → tuple is ("", []) → top-level returns []
    assert extract_premium_emoji_ids(FakeMessage()) == []


def test_extract_premium_emoji_ids_returns_empty_for_text_without_entities():
    assert extract_premium_emoji_ids(FakeMessage(text="hi", entities=None)) == []


def test_extract_premium_emoji_ids_skips_non_custom_emoji_entities():
    message = FakeMessage(
        text="ABC",
        entities=[MessageEntity(type="bold", offset=0, length=1)],
    )

    assert extract_premium_emoji_ids(message) == []


def test_extract_premium_emoji_ids_skips_entities_with_empty_emoji_id():
    message = FakeMessage(
        text="X",
        entities=[MessageEntity(type="custom_emoji", offset=0, length=1, custom_emoji_id="")],
    )

    assert extract_premium_emoji_ids(message) == []


def test_format_premium_emoji_reply_returns_empty_for_no_emojis():
    assert format_premium_emoji_reply([]) == ""


def test_format_premium_emoji_reply_passes_through_PremiumEmoji_objects():
    from telepath.premium_emoji import PremiumEmoji

    out = format_premium_emoji_reply([PremiumEmoji(fallback="⭐", custom_emoji_id="999")])
    assert out == "custom_emoji_id: 999"


def test_format_premium_emoji_reply_omits_fallback_when_empty():
    out = format_premium_emoji_reply([("⭐", "1"), ("", "2")])
    assert "- ⭐ 1" in out
    assert "- 2" in out


def test_slice_telegram_entity_text_returns_empty_for_nonpositive_length():
    from telepath.premium_emoji import _slice_telegram_entity_text

    assert _slice_telegram_entity_text("ABC", 0, 0) == ""
    assert _slice_telegram_entity_text("ABC", 0, -1) == ""
