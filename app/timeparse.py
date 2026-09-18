"""Natural-language time-window extraction.

Used by the deterministic fallback interpreter and by the guardrail layer that
repairs partially-malformed LLM output.

Convention from the problem statement: windows are whole-hour intervals with an
inclusive start and an *exclusive* end -- "1 PM to 3 PM" is hours [13, 14].
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "twentyone": 21, "twentytwo": 22, "twentythree": 23,
}

# A clock token: 13, 13:00, 1 PM, 1p.m., noon, midnight
_TIME = r"(?P<{n}>(?:\d{{1,2}}(?::\d{{2}})?\s*(?:a\.?m\.?|p\.?m\.?)?)|noon|midday|midnight)"
_CONNECT_ANY = r"(?:\s*(?:to|till|til|until|through|thru|and|-|–|—)\s*)"
_CONNECT_STRICT = r"(?:\s*(?:to|till|til|until|through|thru|-|–|—)\s*)"

_RANGE_LEAD = re.compile(
    r"\b(?:from|between|during|over|for|in|within|across)\s+"
    + _TIME.format(n="a") + _CONNECT_ANY + _TIME.format(n="b"),
    re.IGNORECASE,
)
_RANGE_BARE = re.compile(
    _TIME.format(n="a") + _CONNECT_STRICT + _TIME.format(n="b"),
    re.IGNORECASE,
)
_SINGLE = re.compile(
    r"\b(?:at|during|for|in)\s+(?:the\s+)?(?:hour\s+(?:of\s+|starting\s+(?:at\s+)?)?)?"
    + _TIME.format(n="a"),
    re.IGNORECASE,
)
_HOUR_LIST = re.compile(r"\bhours?\s+((?:\d{1,2})(?:\s*(?:,|and|&|/)\s*\d{1,2})+)\b", re.IGNORECASE)

ALL_DAY = re.compile(
    r"\b(?:all\s+day|entire\s+day|whole\s+day|throughout\s+the\s+day|"
    r"round[-\s]the[-\s]clock|24\s*h(?:ou)?rs?|every\s+hour|full\s+day)\b",
    re.IGNORECASE,
)


class _Tok:
    __slots__ = ("hour", "minute", "meridiem", "explicit24")

    def __init__(self, hour: int, minute: int, meridiem: Optional[str], explicit24: bool):
        self.hour = hour
        self.minute = minute
        self.meridiem = meridiem
        self.explicit24 = explicit24


def _normalize(text: str) -> str:
    t = text.lower()
    t = t.replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")
    t = re.sub(r"\b(o'clock|oclock)\b", "", t)
    t = re.sub(r"\btwenty[-\s]?(one|two|three|four)\b", lambda m: "twenty" + m.group(1), t)
    # Word numbers -> digits, but never inside fraction words (one-fifth, two-thirds).
    def _sub_word(m: re.Match) -> str:
        word = m.group(0)
        tail = t[m.end():m.end() + 10]
        if re.match(r"\s*-?\s*(half|third|quarter|fourth|fifth|sixth|eighth|tenth)", tail):
            return word
        return str(WORD_NUMBERS[word])

    t = re.sub(r"\b(" + "|".join(WORD_NUMBERS) + r")\b", _sub_word, t)
    t = re.sub(r"\s+", " ", t)
    return t


def _tok(raw: Optional[str]) -> Optional[_Tok]:
    if not raw:
        return None
    s = raw.strip().lower().replace(".", "")
    if s in ("noon", "midday"):
        return _Tok(12, 0, "pm", True)
    if s == "midnight":
        return _Tok(0, 0, "am", True)
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    mer = m.group(3)
    if hour > 24 or minute > 59:
        return None
    explicit24 = mer is None and (hour > 12 or m.group(2) is not None)
    return _Tok(hour, minute, mer, explicit24)


def _apply_meridiem(tok: _Tok, mer: Optional[str]) -> int:
    h = tok.hour
    if tok.explicit24 or mer is None:
        return h % 24
    if mer == "am":
        return 0 if h == 12 else h % 24
    return 12 if h == 12 else (h + 12) % 24 if h < 12 else h % 24


def _resolve_pair(a: _Tok, b: _Tok) -> Tuple[int, int]:
    """Resolve two clock tokens into 24h start/end hours, inferring AM/PM."""
    a_mer, b_mer = a.meridiem, b.meridiem

    if a_mer is None and b_mer is not None and not a.explicit24:
        a_mer = b_mer
    if b_mer is None and a_mer is not None and not b.explicit24:
        b_mer = a_mer
    if a_mer is None and b_mer is None and not a.explicit24 and not b.explicit24:
        # Bare small numbers ("from one until three") read as afternoon hours.
        if 1 <= a.hour <= 6:
            a_mer = b_mer = "pm"

    start = _apply_meridiem(a, a_mer)
    end = _apply_meridiem(b, b_mer)

    # "11 to 2 PM" -> the start is morning, not 23:00.
    if start > end and a.meridiem is None and not a.explicit24 and b_mer == "pm":
        start = a.hour % 12
    # "2 PM to 4" -> end is still afternoon.
    if end <= start and b.meridiem is None and not b.explicit24 and start >= 12 and b.hour < 12:
        end = (b.hour + 12) % 24

    # A minutes component past the hour extends the window into that hour.
    if b.minute > 0:
        end = (end + 1) % 24
    return start, end


def _span_to_hours(start: int, end: int) -> List[int]:
    start %= 24
    end %= 24
    if start == end:
        return [start]
    if end > start:
        return list(range(start, end))
    return list(range(start, 24)) + list(range(0, end))


def extract_hours(text: str) -> Optional[List[int]]:
    """Return the hour list described by ``text``, or None when none is stated."""
    t = _normalize(text)
    hours: List[int] = []
    consumed: List[Tuple[int, int]] = []

    for m in _RANGE_LEAD.finditer(t):
        a, b = _tok(m.group("a")), _tok(m.group("b"))
        if a and b:
            hours += _span_to_hours(*_resolve_pair(a, b))
            consumed.append(m.span())

    for m in _RANGE_BARE.finditer(t):
        if any(s <= m.start() < e or s < m.end() <= e for s, e in consumed):
            continue
        a, b = _tok(m.group("a")), _tok(m.group("b"))
        if a and b:
            hours += _span_to_hours(*_resolve_pair(a, b))
            consumed.append(m.span())

    if not hours:
        for m in _HOUR_LIST.finditer(t):
            nums = [int(x) for x in re.findall(r"\d{1,2}", m.group(1))]
            hours += [n for n in nums if 0 <= n <= 23]

    if not hours:
        for m in _SINGLE.finditer(t):
            a = _tok(m.group("a"))
            if a:
                mer = a.meridiem
                if mer is None and not a.explicit24 and 1 <= a.hour <= 6:
                    mer = "pm"
                hours.append(_apply_meridiem(a, mer))

    if not hours and ALL_DAY.search(t):
        hours = list(range(24))

    if not hours:
        return None
    return sorted({h for h in hours if 0 <= h <= 23})


def has_all_day(text: str) -> bool:
    return bool(ALL_DAY.search(_normalize(text)))
