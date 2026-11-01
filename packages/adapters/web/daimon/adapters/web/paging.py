"""Cursor paging for the list routes.

A list stays a plain JSON array, so a client written before paging keeps working: it
reads the first page. When more rows follow, the response carries `X-Next-Cursor`; send
it back as `cursor=` for the next page.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Annotated

from daimon.core.cursor import MAX_PAGE, Cursor, decode_cursor, encode_cursor
from fastapi import HTTPException, Query, Response

NEXT_HEADER = "X-Next-Cursor"


@dataclass(frozen=True)
class PageRequest:
    """What a client asked of a list: how many rows, and after which."""

    limit: int
    cursor: Cursor | None


@cache
def page_of(default: int) -> Callable[..., PageRequest]:
    """Dependency reading `limit` (at most `MAX_PAGE`) and `cursor`; `default` rows if no limit."""

    def read(
        limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE)] = None,
        cursor: Annotated[str | None, Query(max_length=200)] = None,
    ) -> PageRequest:
        try:
            after = None if cursor is None else decode_cursor(cursor)
        except ValueError:
            raise HTTPException(status_code=422, detail="That page doesn't exist.") from None
        return PageRequest(limit=limit or default, cursor=after)

    return read


def cut[T](
    rows: Sequence[T], page: PageRequest, response: Response, key: Callable[[T], Cursor]
) -> list[T]:
    """The page's rows out of `rows` read with one extra, and the header when more follow."""
    kept = list(rows[: page.limit])
    if len(rows) > page.limit:
        response.headers[NEXT_HEADER] = encode_cursor(key(kept[-1]))
    return kept
