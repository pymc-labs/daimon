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
