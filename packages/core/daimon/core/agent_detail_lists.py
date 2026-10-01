"""Fit complete detail-list entries into the space a platform reserves for them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, Literal

DetailListName = Literal["keys", "skills", "connections"]
DETAIL_LIST_COLLAPSED_COUNT: Final = 6
DETAIL_LIST_EXPANDED_CAP: Final = 60


def _format_items(items: Sequence[str], *, expanded: bool, max_chars: int) -> str:
    limit = DETAIL_LIST_EXPANDED_CAP if expanded else DETAIL_LIST_COLLAPSED_COUNT
    count = min(len(items), limit)
    while count >= 0:
        lines = list(items[:count])
        if count < len(items):
            lines.append(f"+{len(items) - count} more")
        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text
        count -= 1
    raise ValueError("Detail-list allowance must fit its omitted-count notice")


def _share_space(
    wanted: Mapping[DetailListName, int],
    minimum: Mapping[DetailListName, int],
    *,
    available: int,
) -> dict[DetailListName, int]:
    """Let short lists keep their space, then share the rest among longer lists."""
    allocated = dict(minimum)
    remaining = available - sum(allocated.values())
    pending: list[DetailListName] = [name for name in wanted if wanted[name] > allocated[name]]
    while pending and remaining > 0:
        share = max(1, remaining // len(pending))
        for name in pending:
            addition = min(share, wanted[name] - allocated[name], remaining)
            allocated[name] += addition
            remaining -= addition
        pending = [name for name in pending if wanted[name] > allocated[name]]
    return allocated


def format_detail_lists(
    items: Mapping[DetailListName, Sequence[str]],
    *,
    expanded: DetailListName | None,
    max_chars: int,
    max_list_chars: int = 3000,
) -> dict[DetailListName, str]:
    """Render preformatted rows without cutting an item or its markup.

    The caller reserves headers, notes, separators and actions before supplying
    ``max_chars``. This allowance covers only the returned list bodies, including
    their omitted-count notices. Collapsed lists take priority over expansion;
    when even those are too large they share space, preserving each notice.
    ``max_list_chars`` additionally bounds each platform text block.
    """
    desired: dict[DetailListName, str] = {
        name: _format_items(rows, expanded=name == expanded, max_chars=max_list_chars)
        for name, rows in items.items()
    }
    minimum: dict[DetailListName, int] = {
        name: min(len(text), len(f"+{len(items[name])} more")) if text else 0
        for name, text in desired.items()
    }
    if sum(minimum.values()) > max_chars:
        raise ValueError("Detail-list allowance must fit every omitted-count notice")

    collapsed: dict[DetailListName, int] = {
        name: len(text) for name, text in desired.items() if name != expanded
    }
    reserved = minimum.get(expanded, 0) if expanded is not None else 0
    allocated = _share_space(
        collapsed,
        {name: minimum[name] for name in collapsed},
        available=max_chars - reserved,
    )
    rendered = {
        name: _format_items(items[name], expanded=False, max_chars=allowance)
        for name, allowance in allocated.items()
    }
    if expanded is not None and expanded in items:
        allowance = min(max_list_chars, max_chars - sum(map(len, rendered.values())))
        rendered[expanded] = _format_items(items[expanded], expanded=True, max_chars=allowance)
    return {name: rendered[name] for name in items}
