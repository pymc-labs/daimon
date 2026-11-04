"""Pagination primitives shared by all list_* tools.

`Page[T]` is the uniform envelope returned from every list tool.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Page[T](BaseModel):
    """Uniform page envelope returned by every list_* tool."""

    model_config = ConfigDict(frozen=True)

    items: list[T]
    next_page: str | None


LIST_CAP = 25
"""Most rows a list tool returns; `more` says there are further rows."""


def capped[T](rows: list[T], cap: int = LIST_CAP) -> tuple[list[T], int, bool]:
    """The first `cap` rows, the total, and whether rows were left out."""
    return rows[:cap], len(rows), len(rows) > cap
