from __future__ import annotations

from daimon.adapters.mcp.tools._pagination import Page, capped


def test_page_envelope_holds_items_and_next_page_token() -> None:
    page: Page[int] = Page(items=[1, 2, 3], next_page=None)
    assert page.items == [1, 2, 3]
    assert page.next_page is None


def test_capped_when_over_the_cap_then_first_rows_total_and_more() -> None:
    """A list longer than the cap is cut, with its full size and a `more` flag."""
    shown, total, more = capped(list(range(30)))
    assert (len(shown), total, more) == (25, 30, True), "25 rows of 30, flagged as cut"
    shown, total, more = capped(list(range(25)))
    assert (len(shown), total, more) == (25, 25, False), "exactly the cap is not cut"
