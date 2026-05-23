from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class TextSpan:
    start: int
    end: int


_WORD_RE = re.compile(r"[А-Яа-яЁё]+")

_EB_PREFIX = r"(?:за|на|по|вы|до|от[ъь]?|под[ъь]?|пере|про|раз[ъь]?|рас|с[ъь]?|у|об[ъь]?|при|в[ъь]?|из[ъь]?|недо|о)?"
_HUY_PREFIX = r"(?:на|по|о|а|за|до|ни|не|вы|при|под|от|пере|про|раз|рас|с|в|у|об|из|без)?"
_PIZD_PREFIX = r"(?:за|на|по|вы|до|рас|раз|с|от|пере|про|при|под|об|у|в|из)?"

_PROFANITY_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        # Four productive core roots: еб-, ху[йяеюи]-, пизд-/пизж-, бляд-/блять.
        rf"^{_EB_PREFIX}еб[а-я]*$",
        r"^епт[а-я]*$",
        rf"^{_HUY_PREFIX}ху(?:й|я|е|ю|и)[а-я]*$",
        r"^(?:на|по|за)?хул[ие]$",
        rf"^{_PIZD_PREFIX}п[ие]з(?:д|ж)[а-я]*$",
        r"^бля(?:д[а-я]*|ть)?$",
        # High-confidence extended obscene roots. Kept tight to avoid words like "мудрый" or "мандат".
        r"^муд(?:а|я|е|о|и|ы|у|ак|ац|ил|ищ|оз|еб)[а-я]*$",
        r"^манд(?:а(?!т)|е|ой|ою|у|ы|ищ|юк|ец|оват|ох)[а-я]*$",
        r"^[еэ]лд(?:а|ак|ой|ою|у|ы|ищ)[а-я]*$",
        # Common obscene insult forms; exact/tight patterns prevent "сукно"/"сукин".
        r"^сук(?:а|и|у|ой|ою|е)$",
        r"^суч(?:к|ар|ий|ья|ье|ьи)[а-я]*$",
        r"^г[ао]ндон[а-я]*$",
        r"^дроч[а-я]*$",
    )
)


def _normalize_word(word: str) -> str:
    return word.casefold().replace("ё", "е")


def _is_profanity(word: str) -> bool:
    normalized = _normalize_word(word)
    return any(pattern.match(normalized) for pattern in _PROFANITY_PATTERNS)


def find_profanity_spans(text: str) -> list[TextSpan]:
    spans: list[TextSpan] = []
    for match in _WORD_RE.finditer(text):
        if _is_profanity(match.group(0)):
            spans.append(TextSpan(match.start(), match.end()))
    return spans
