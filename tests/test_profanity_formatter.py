from telepath.profanity import find_profanity_spans


def covered_words(text, spans):
    return [text[span.start : span.end] for span in spans]


def test_find_profanity_spans_covers_common_russian_forms_and_prefixes():
    text = "Ну блять, это пиздец, я охуел, заебался и нахуячил."

    assert covered_words(text, find_profanity_spans(text)) == [
        "блять",
        "пиздец",
        "охуел",
        "заебался",
        "нахуячил",
    ]


def test_find_profanity_spans_covers_productive_prefixed_forms():
    text = "Подъебал, съебался, спиздил, распиздяй, похуй."

    assert covered_words(text, find_profanity_spans(text)) == [
        "Подъебал",
        "съебался",
        "спиздил",
        "распиздяй",
        "похуй",
    ]


def test_find_profanity_spans_handles_yo_letter_and_suffixes():
    text = "Ёбаный случай, хуйней пахнет, сука."

    assert covered_words(text, find_profanity_spans(text)) == ["Ёбаный", "хуйней", "сука"]


def test_find_profanity_spans_does_not_match_inside_normal_words():
    text = "Благодаря учебе, ребенку, сухому сукну, хулигану и мандату текст обычный."

    assert find_profanity_spans(text) == []
