"""Near-duplicate cases: groups by overlap of normalized word shingles of what was asked.

Cases from busy threads repeat the same request. `groups` links cases whose requests overlap by
`threshold` (Jaccard, single link); `thin` keeps the first `keep` of each group in time order.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from daimon.core.evals.format import Case, JsonObject, sha256_hex

SHINGLE: Final = 3
THRESHOLD: Final = 0.8
_WORD = re.compile(r"\w+")


def shingles(text: str) -> frozenset[str]:
    """Word 3-grams of `text`, lowercase, punctuation and spacing ignored; short text is one."""
    words = _WORD.findall(text.lower())
    if len(words) <= SHINGLE:
        return frozenset({" ".join(words)}) if words else frozenset[str]()
    return frozenset(" ".join(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1))


def _asked(case: Case) -> frozenset[str]:
    return shingles("\n".join(m.text or "" for m in case.turn.messages))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def groups(cases: Sequence[JsonObject], threshold: float = THRESHOLD) -> list[list[str]]:
    """Ids of cases that are near-duplicates, one list per group of two or more, oldest first."""
    models = sorted((Case.model_validate(c) for c in cases), key=lambda c: (c.clock.now, c.id))
    sets = [_asked(c) for c in models]
    parent = list(range(len(models)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(models)):
        for j in range(i):
            if root(i) != root(j) and _jaccard(sets[i], sets[j]) >= threshold:
                parent[root(i)] = root(j)
    by_root: dict[int, list[str]] = {}
    for i, case in enumerate(models):
        by_root.setdefault(root(i), []).append(case.id)
    return [ids for ids in by_root.values() if len(ids) > 1]


def group_id(ids: Sequence[str]) -> str:
    """A stable name for a group: from its oldest member."""
    return "dup-" + sha256_hex(ids[0].encode())[:10]


def thin(found: Sequence[Sequence[str]], keep: int) -> list[str]:
    """The ids to drop so each group keeps `keep` cases (its oldest)."""
    return [i for ids in found for i in ids[keep:]]
