from __future__ import annotations

from daimon.adapters.mcp.tools._pagination import Page


def test_page_envelope_holds_items_and_next_page_token() -> None:
    page: Page[int] = Page(items=[1, 2, 3], next_page=None)
    assert page.items == [1, 2, 3]
    assert page.next_page is None
