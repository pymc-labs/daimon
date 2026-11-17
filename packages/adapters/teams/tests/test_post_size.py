"""`fit_lines`: support posts stay under Teams' cap, long lines cut and marked."""

from __future__ import annotations

import json

from daimon.adapters.teams.post_size import CUT, fit_lines


def _bytes(lines: list[str]) -> int:
    return sum(len(json.dumps(line)) for line in lines)


def test_fit_when_under_the_budget_then_unchanged() -> None:
    """Short posts are left alone."""
    lines = ["Review", "Topic: billing"]
    assert fit_lines(lines) == lines, "nothing cut"


def test_fit_when_one_line_is_huge_then_only_it_is_cut() -> None:
    """The longest line is cut with the marker; the short ones stay whole."""
    lines = ["Review", "x" * 40_000, "Topic: billing"]
    fitted = fit_lines(lines)
    assert fitted[0] == "Review" and fitted[2] == "Topic: billing", "short lines kept"
    assert fitted[1].endswith(CUT), "the cut is marked"
    assert _bytes(fitted) < 24_000, "under the budget"


def test_fit_when_dollars_and_quotes_then_counts_their_escapes() -> None:
    """Escaped characters count at their escaped size."""
    fitted = fit_lines(['$"' * 20_000], budget=2_000)
    assert _bytes(fitted) + 2 * fitted[0].count("$") <= 2_000, "escapes counted"


def test_fit_when_many_lines_then_none_is_dropped() -> None:
    """Too many lines to cut one by one become one cut paragraph, never dropped silently."""
    fitted = fit_lines([f"line {n}" for n in range(5_000)], budget=4_000)
    assert len(fitted) == 1 and fitted[0].startswith("line 0"), fitted[:1]
    assert fitted[0].endswith(CUT), "marked as cut"
