"""Keeps a support post under Teams' message cap (about 28 KB): long lines are cut, each marked.

Pure. Sizes are measured as escaped JSON, an upper bound on the UTF-8 the
service counts. A line is never dropped: past the cap it is cut, with `CUT`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Final

POST_BUDGET: Final = 24_000  # leaves room for the rest of the card or activity
CUT: Final = " … (cut, too long for Teams)"
_PER_LINE: Final = 48  # a card's TextBlock wrapper, or a text post's paragraph break


def _size(line: str) -> int:
    # escape_dollars adds a backslash per `$`, two bytes once JSON-escaped.
    return len(json.dumps(line)) + 2 * line.count("$") + _PER_LINE


def _cut(line: str, cap: int) -> str:
    """The longest head of `line` that fits in `cap` with the marker."""
    lo, hi = 0, len(line)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _size(line[:mid] + CUT) <= cap:
            lo = mid
        else:
            hi = mid - 1
    return line[:lo].rstrip() + CUT


def fit_lines(lines: Sequence[str], budget: int = POST_BUDGET) -> list[str]:
    """`lines` within `budget`: the longest are cut to one shared cap, so short ones stay whole.

    So many lines that even cut ones overflow are joined into one paragraph first.
    """
    sizes = [_size(line) for line in lines]
    if sum(sizes) <= budget:
        return list(lines)
    lo, hi = 0, max(sizes)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if sum(min(size, mid) for size in sizes) <= budget:
            lo = mid
        else:
            hi = mid - 1
    if lo < _size(CUT) and len(lines) > 1:
        return fit_lines(["\n\n".join(lines)], budget)
    return [line if size <= lo else _cut(line, lo) for line, size in zip(lines, sizes, strict=True)]


__all__ = ["CUT", "POST_BUDGET", "fit_lines"]
