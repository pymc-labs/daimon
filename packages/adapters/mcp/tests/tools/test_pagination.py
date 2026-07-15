from __future__ import annotations

from daimon.adapters.mcp.tools._pagination import (
    DEFAULT_PAGE_SIZES,
    Page,
)


def test_default_page_sizes_pinned() -> None:
    assert DEFAULT_PAGE_SIZES == {
        "agents": 50,
        "environments": 50,
    }


def test_page_envelope_holds_items_and_next_page_token() -> None:
    page: Page[int] = Page(items=[1, 2, 3], next_page=None)
    assert page.items == [1, 2, 3]
    assert page.next_page is None
