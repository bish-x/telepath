from telethon import types

from telepath.user_client import TelethonReplies, utf16_len


class FakeClient:
    def __init__(self):
        self.sent = []
        self.requests = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))

    async def __call__(self, request):
        self.requests.append(request)


async def test_telethon_replies_wraps_transcription_with_custom_emoji_entities():
    client = FakeClient()
    replies = TelethonReplies(client, custom_emoji_id="5460795800101594035")

    await replies.reply(chat_id=100, message_id=50, text="Пошел текст", decorate=True)

    assert len(client.sent) == 1
    chat_id, text, kwargs = client.sent[0]
    assert chat_id == 100
    assert text == "⭐\nПошел текст\n⭐"
    assert kwargs["reply_to"] == 50
    assert kwargs["parse_mode"] is None

    entities = kwargs["formatting_entities"]
    custom_emojis = [entity for entity in entities if isinstance(entity, types.MessageEntityCustomEmoji)]
    quotes = [entity for entity in entities if isinstance(entity, types.MessageEntityBlockquote)]
    assert [entity.document_id for entity in custom_emojis] == [5460795800101594035, 5460795800101594035]
    assert [(entity.offset, entity.length) for entity in custom_emojis] == [
        (0, utf16_len("⭐")),
        (utf16_len("⭐\nПошел текст\n"), utf16_len("⭐")),
    ]
    assert [(entity.offset, entity.length) for entity in quotes] == [
        (utf16_len("⭐\n"), utf16_len("Пошел текст")),
    ]
    assert [entity.collapsed for entity in quotes] == [True]


async def test_telethon_replies_adds_italic_entities_for_russian_profanity():
    client = FakeClient()
    replies = TelethonReplies(client, custom_emoji_id="5460795800101594035")

    await replies.reply(chat_id=100, message_id=50, text="Ну блять, это пиздец.", decorate=True)

    _, text, kwargs = client.sent[0]
    entities = kwargs["formatting_entities"]
    italics = [entity for entity in entities if isinstance(entity, types.MessageEntityItalic)]
    quotes = [entity for entity in entities if isinstance(entity, types.MessageEntityBlockquote)]

    assert text == "⭐\nНу блять, это пиздец.\n⭐"
    assert [(entity.offset, entity.length) for entity in italics] == [
        (utf16_len("⭐\nНу "), utf16_len("блять")),
        (utf16_len("⭐\nНу блять, это "), utf16_len("пиздец")),
    ]
    assert [(entity.offset, entity.length) for entity in quotes] == [
        (utf16_len("⭐\n"), utf16_len("Ну блять, это пиздец.")),
    ]
    assert [entity.collapsed for entity in quotes] == [True]


async def test_telethon_replies_sends_plain_text_without_custom_emoji_id():
    client = FakeClient()
    replies = TelethonReplies(client, custom_emoji_id=None)

    await replies.reply(chat_id=100, message_id=50, text="Блять, пошел текст")

    assert len(client.sent) == 1
    chat_id, text, kwargs = client.sent[0]
    assert chat_id == 100
    assert text == "Блять, пошел текст"
    assert kwargs["reply_to"] == 50
    assert kwargs["parse_mode"] is None
    italics = [entity for entity in kwargs["formatting_entities"] if isinstance(entity, types.MessageEntityItalic)]
    quotes = [entity for entity in kwargs["formatting_entities"] if isinstance(entity, types.MessageEntityBlockquote)]
    assert [(entity.offset, entity.length) for entity in italics] == [
        (0, utf16_len("Блять")),
    ]
    assert [(entity.offset, entity.length) for entity in quotes] == [
        (0, utf16_len("Блять, пошел текст")),
    ]
    assert [entity.collapsed for entity in quotes] == [True]


async def test_telethon_replies_keeps_custom_emoji_disabled_by_default():
    client = FakeClient()
    replies = TelethonReplies(client, custom_emoji_id="5460795800101594035")

    await replies.reply(chat_id=100, message_id=50, text="Пошел текст")

    _, text, kwargs = client.sent[0]
    entities = kwargs["formatting_entities"]
    assert text == "Пошел текст"
    assert not [entity for entity in entities if isinstance(entity, types.MessageEntityCustomEmoji)]
    quotes = [entity for entity in entities if isinstance(entity, types.MessageEntityBlockquote)]
    assert [(entity.offset, entity.length, entity.collapsed) for entity in quotes] == [
        (0, utf16_len("Пошел текст"), True),
    ]


async def test_telethon_replies_falls_back_to_plain_send_when_no_entities():
    client = FakeClient()
    replies = TelethonReplies(client, custom_emoji_id=None)

    # Empty text → transcription_quote_entities returns [] and there are no
    # profanity spans → the bare `send_message` branch (no formatting_entities)
    # fires.
    await replies.reply(chat_id=100, message_id=50, text="")

    assert len(client.sent) == 1
    chat_id, text, kwargs = client.sent[0]
    assert text == ""
    assert kwargs == {"reply_to": 50}


async def test_telethon_replies_marks_current_session_offline_after_send():
    client = FakeClient()
    replies = TelethonReplies(client, custom_emoji_id=None)

    await replies.reply(chat_id=100, message_id=50, text="")

    assert [request.__class__.__name__ for request in client.requests] == ["UpdateStatusRequest"]
    assert client.requests[0].offline is True


async def test_telethon_replies_adds_custom_emoji_when_decoration_enabled():
    client = FakeClient()
    replies = TelethonReplies(client, custom_emoji_id="5460795800101594035")

    await replies.reply(chat_id=100, message_id=50, text="Пошел текст", decorate=True)

    _, text, kwargs = client.sent[0]
    entities = kwargs["formatting_entities"]
    custom_emojis = [entity for entity in entities if isinstance(entity, types.MessageEntityCustomEmoji)]
    quotes = [entity for entity in entities if isinstance(entity, types.MessageEntityBlockquote)]
    assert text == "⭐\nПошел текст\n⭐"
    assert len(custom_emojis) == 2
    assert [(entity.offset, entity.length, entity.collapsed) for entity in quotes] == [
        (utf16_len("⭐\n"), utf16_len("Пошел текст"), True),
    ]
