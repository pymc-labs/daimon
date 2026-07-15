"""Pagination primitives shared by all list_* tools.

`Page[T]` is the uniform envelope returned from every list tool.
`DEFAULT_PAGE_SIZES` pins our page sizes independent of the MA SDK defaults.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from pydantic import BaseModel, ConfigDict


class Page[T](BaseModel):
    """Uniform page envelope returned by every list_* tool."""

    model_config = ConfigDict(frozen=True)

    items: list[T]
    next_page: str | None


DEFAULT_PAGE_SIZES: Final[Mapping[str, int]] = {
    "agents": 50,
    "environments": 50,
}
