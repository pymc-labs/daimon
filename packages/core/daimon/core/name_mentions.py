"""Plain-text "@<bot name>" mentions: the pure matcher behind the per-tenant toggle.

A real mention on Teams mobile means typing part of the name, waiting for the
suggestion list and tapping the bot, and a pasted prompt that says "@Daimon"
is only text. With ``tenants.plain_text_mentions`` on, each adapter treats a
human channel or thread message that names the bot this way like a real
mention. The adapters run this regex before any I/O, so only a matching
message pays the read of the flag.

Matching is case-insensitive and Unicode-aware, and any run of whitespace
matches a space in the name. The "@" must not follow a word character, "@"
or "/" (``me@daimon.example``, ``https://example.com/@daimon``), and the name
must not run on into a word character, a hyphen, a dotted domain or a
"(" ("@Daimonic", "@Daimon-dev", "@daimon.example.com", "@Daimon (Staging)"
for a bot named "Daimon"). A following word is prose ("@Daimon please"), so
bots whose names share a prefix ("Daimon", "Daimon Helper") all answer
"@Daimon Helper". Code spans and blocks are not special: a name inside
backticks still matches, which keeps the rule one sentence long for admins.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

_TRAILING_PARENTHETICAL = re.compile(r"\s*\([^()]*\)\s*$")


def name_variants(names: Iterable[str | None]) -> tuple[str, ...]:
    """Every name the bot answers to: each name, plus it without a trailing "(...)".

    A deployment named "Daimon (Staging)" still answers to "@Daimon". Longest
    first, so the alternation consumes "Daimon (Staging)" whole before "Daimon".
    Casefolding only dedupes: it rewrites "ß" as "ss", which the pattern would
    then fail to find in "@Straße".
    """
    variants: dict[str, str] = {}
    for name in names:
        cleaned = (name or "").strip()
        short = _TRAILING_PARENTHETICAL.sub("", cleaned).strip()
        for variant in (cleaned, short):
            if variant:
                variants.setdefault(variant.casefold(), variant)
    return tuple(sorted(variants.values(), key=lambda variant: (-len(variant), variant)))


def _name_pattern(variant: str) -> str:
    return r"\s+".join(re.escape(word) for word in variant.split())


@lru_cache(maxsize=64)
def _pattern(variants: tuple[str, ...]) -> re.Pattern[str]:
    alternation = "|".join(_name_pattern(variant) for variant in variants)
    return re.compile(rf"(?<![\w@/])@(?:{alternation})(?![\w-]|\.\w|\s*\()", re.IGNORECASE)


def mentions_by_name(text: str, names: Iterable[str | None]) -> bool:
    """True when ``text`` contains "@<name>" for any of ``names``; False with no names."""
    if "@" not in text:
        return False
    variants = name_variants(names)
    return bool(variants) and _pattern(variants).search(text) is not None


def strip_name_mentions(text: str, names: Iterable[str | None]) -> str:
    """Remove every "@<name>" token (and the blank after it), then trim the ends."""
    if "@" not in text:
        return text
    variants = name_variants(names)
    if not variants:
        return text
    return re.sub(rf"(?:{_pattern(variants).pattern})[ \t]?", "", text, flags=re.IGNORECASE).strip()
