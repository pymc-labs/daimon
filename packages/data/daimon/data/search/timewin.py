"""Rule-based time windows in queries ("last week", "in March", "Q3 2026", "4 weeks ago").

No model and no date library."""
# pyright: basic
# ruff: noqa: E501

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        1,
    )
}
MONTHS |= {m[:3]: i for m, i in list(MONTHS.items())} | {"sept": 9}
NUMS = {
    w: i
    for i, w in enumerate(
        [
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
        ],
        1,
    )
}
NUMS["a"] = NUMS["an"] = 1
NOT_TIME = re.compile(r"\b(incomplete|partial|final|first|second|each|every|per)\s+$", re.I)
UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}

_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_NUM = r"(\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_DATE = (
    rf"(\d{{4}}-\d{{2}}-\d{{2}}|{_MON}\s+\d{{4}}|{_MON}\s+\d{{1,2}}(?!\d)(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MON}(?:\s+\d{{4}})?|{_MON})"
)


@dataclass
class Window:
    start: datetime | None  # None: open
    end: datetime | None
    text: str  # the phrase matched
    span: tuple[int, int]


def _day(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _add_months(d: datetime, n: int) -> datetime:
    y, m = divmod(d.month - 1 + n, 12)
    return d.replace(year=d.year + y, month=m + 1, day=1)


def _month(m: int, year: int | None, now: datetime) -> tuple[datetime, datetime]:
    y = year or (now.year if m <= now.month else now.year - 1)  # most recent, never in the future
    s = datetime(y, m, 1, tzinfo=now.tzinfo)
    return s, _add_months(s, 1)


def _date(s: str, now: datetime) -> datetime | None:
    s = s.lower().replace(",", "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return datetime.fromisoformat(s).replace(tzinfo=now.tzinfo)
    parts = re.findall(r"[a-z]+|\d+", s)
    mon = next((MONTHS[p] for p in parts if p in MONTHS), None)
    nums = [int(p) for p in parts if p.isdigit()]
    if mon is None:
        return None
    year = next((n for n in nums if n > 31), None)
    day = next((n for n in nums if n <= 31), None)
    if day is None:
        return _month(mon, year, now)[0]
    y = year or (now.year if (mon, day) <= (now.month, now.day) else now.year - 1)
    try:
        return datetime(y, mon, day, tzinfo=now.tzinfo)
    except ValueError:
        return None


def _num(s: str) -> int:
    return int(s) if s.isdigit() else NUMS[s]


def _rules(now: datetime):
    today = _day(now)
    week0 = today - timedelta(days=today.weekday())
    month0 = today.replace(day=1)
    q0 = today.replace(month=(today.month - 1) // 3 * 3 + 1, day=1)
    year0 = today.replace(month=1, day=1)
    starts = {
        "week": (week0, lambda d, n: d + timedelta(weeks=n)),
        "month": (month0, _add_months),
        "quarter": (q0, lambda d, n: _add_months(d, 3 * n)),
        "year": (year0, lambda d, n: d.replace(year=d.year + n)),
    }

    def calendar(m):
        which, unit = m.group(1), m.group(2)
        s, add = starts[unit]
        return (add(s, -1), s) if which in ("last", "previous") else (s, add(s, 1))

    def rolling(m):
        n, unit = _num(m.group(1) or "1"), m.group(2)
        return today + timedelta(days=1) - timedelta(
            days=n * (91 if unit == "quarter" else UNIT_DAYS[unit])
        ), now

    def ago(m):
        n, unit = _num(m.group(1)), m.group(2)
        if unit == "day":
            d = today - timedelta(days=n)
            return d, d + timedelta(days=1)
        centre, half = (
            now - timedelta(days=n * UNIT_DAYS[unit]),
            timedelta(days=UNIT_DAYS[unit] / 2),
        )
        return centre - half, centre + half

    def quarter(m):
        q, year = int(m.group(1)), int(m.group(2)) if m.group(2) else None
        if year is None:
            year = now.year if q <= (now.month - 1) // 3 + 1 else now.year - 1
        s = datetime(year, 3 * q - 2, 1, tzinfo=now.tzinfo)
        return s, _add_months(s, 3)

    def month(m):
        word = m.group(1)  # "may" and "march" are ordinary words: need "in", a year or a capital
        if not (
            m.group(0).lower().startswith("in ")
            or m.group(2)
            or (word[0].isupper() and word != "May")
        ):
            return None
        return _month(
            MONTHS.get(word.lower()) or MONTHS[word.lower()[:3]],
            int(m.group(2)) if m.group(2) else None,
            now,
        )

    def since(m):
        d = _date(m.group(2), now)
        return (d, None) if m.group(1) in ("since", "after") else (None, d)

    def on(m):
        d = _date(m.group(1), now)
        return (d, d + timedelta(days=1)) if d else None

    return [
        (r"\b(since|after|before|until)\s+" + _DATE, since),
        (r"\b(\d{4}-\d{2}-\d{2})\b", on),
        (r"\btoday\b", lambda m: (today, today + timedelta(days=1))),
        (r"\byesterday\b", lambda m: (today - timedelta(days=1), today)),
        (r"\b(last|previous|this)\s+(week|month|quarter|year)\b", calendar),
        (
            rf"\b(?:in\s+the\s+|over\s+the\s+)?(?:past|last)\s+{_NUM}?\s*(day|week|month|quarter|year)s?\b",
            rolling,
        ),
        (rf"\b{_NUM}\s+(day|week|month|year)s?\s+ago\b", ago),
        (r"\bq([1-4])(?:\s+(\d{4}))?\b", quarter),
        (rf"\b(?:in\s+)?{_MON}(?:\s+(\d{{4}}))?\b(?!\s+\d)", month),
        (rf"\bon\s+{_DATE}", on),
    ]


def parse(text: str, now: datetime | None = None) -> Window | None:
    """The first time phrase in `text`, as a [start, end) window in now's timezone."""
    now = now or datetime.now(UTC)
    for rx, fn in _rules(now):
        for m in re.finditer(rx, text, re.I):
            if NOT_TIME.search(
                text[: m.start()]
            ):  # "an incomplete last week" is a thing, not a time
                continue
            r = fn(m)
            if r and any(r):
                return Window(r[0], r[1], m.group(0), m.span())
    return None


def strip(text: str, w: Window | None) -> str:
    """The query without its time phrase, for the lexical and semantic arms."""
    if not w:
        return text
    out = re.sub(r"^'s\b", "", (text[: w.span[0]] + text[w.span[1] :]).strip(" ,?"))
    out = re.sub(r"\b(on|in|from|during|for)\s*$", "", out.strip()).strip()
    return re.sub(r"\s{2,}", " ", out) or text
