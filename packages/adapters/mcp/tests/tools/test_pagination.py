from __future__ import annotations

import pytest
from daimon.adapters.mcp.tools._pagination import Page, paged
from fastmcp.exceptions import ToolError


def test_page_envelope_holds_items_and_next_page_token() -> None:
    page: Page[int] = Page(items=[1, 2, 3], next_page=None)
    assert page.items == [1, 2, 3]
    assert page.next_page is None


def test_paged_walks_a_long_list_to_the_end_by_cursor() -> None:
    """A list longer than the cap is cut at the cap, and each cursor continues where it stopped."""
    rows = list(range(60))
    first, total, cursor = paged(rows, None)
    assert (first, total) == (rows[:25], 60), "the first 25 of 60"
    assert cursor is not None, "more rows remain, so there is a cursor"
    second, _, cursor = paged(rows, cursor)
    assert second == rows[25:50], "the cursor continues after the last row shown"
    assert cursor is not None, "ten rows still remain"
    last, _, cursor = paged(rows, cursor)
    assert (last, cursor) == (rows[50:], None), "the last page has no cursor"


def test_paged_when_exactly_the_cap_then_no_cursor() -> None:
    shown, total, cursor = paged(list(range(25)), None)
    assert (len(shown), total, cursor) == (25, 25, None), "exactly the cap is not cut"


def test_paged_when_the_cursor_is_not_one_then_tool_error() -> None:
    with pytest.raises(ToolError, match="next_page"):
        paged([1], "garbage")
