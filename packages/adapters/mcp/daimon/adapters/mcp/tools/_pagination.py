"""Pagination primitives shared by all list_* tools.

`Page[T]` is the uniform envelope returned from every list tool.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.core.cursor import decode_cursor, encode_cursor
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict


class Page[T](BaseModel):
    """Uniform page envelope returned by every list_* tool."""

    model_config = ConfigDict(frozen=True)

    items: list[T]
    next_page: str | None


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

LIST_CAP = 25
"""Most rows a list tool returns; `next_page` says there are further rows."""


def paged[T](
    rows: list[T], after: str | None, cap: int = LIST_CAP
) -> tuple[list[T], int, str | None]:
    """One page of `rows` from the cursor `after`, the total, and the cursor of the next page."""
    start = 0
    if after:
        try:
            start = int(decode_cursor(after)[1].int)
        except ValueError:
            raise ToolError("after must be the next_page of an earlier list.") from None
    end = start + cap
    return rows[start:end], len(rows), _cursor(end) if end < len(rows) else None


def _cursor(offset: int) -> str:
    return encode_cursor((_EPOCH, uuid.UUID(int=offset)))
