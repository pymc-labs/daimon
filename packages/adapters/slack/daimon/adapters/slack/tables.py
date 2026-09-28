"""Slack native table blocks, bounded by the shared parser's cell/row limits."""

from __future__ import annotations

from typing import Any

from daimon.adapters.slack.mrkdwn import escape_mrkdwn, escape_mrkdwn_preserving_mentions
from daimon.adapters.slack.split import split_for_slack_safe
from daimon.core.tables import MarkdownTable, render_tables


async def table_block(table: MarkdownTable) -> dict[str, Any]:
    return {
        "type": "table",
        "rows": [
            [{"type": "raw_text", "text": cell} for cell in row]
            for row in (table.headers, *table.rows)
        ],
        "column_settings": [{"align": align, "is_wrapped": True} for align in table.alignments],
    }


async def render_slack_tables(
    text: str, *, enabled: bool = False, preserve_mentions: bool = True
) -> list[tuple[str, dict[str, Any]]]:
    async def hook(table: MarkdownTable) -> tuple[str, dict[str, Any]]:
        return escape_mrkdwn(table.raw), await table_block(table)

    escape_text = escape_mrkdwn_preserving_mentions if preserve_mentions else escape_mrkdwn
    output: list[tuple[str, dict[str, Any]]] = []
    parts = await render_tables(text, hook=hook if enabled else None)
    if all(isinstance(part, str) for part in parts):
        return [
            (chunk, {"type": "markdown", "text": chunk})
            for chunk in split_for_slack_safe(escape_text(text))
        ]
    for part in parts:
        if isinstance(part, str):
            if not part.strip():
                continue
            for chunk in split_for_slack_safe(escape_text(part)):
                output.append((chunk, {"type": "markdown", "text": chunk}))
        else:
            # Each table gets its own message, keeping the 10,000-cell-character
            # budget per message and preserving order with surrounding prose.
            output.append(part)
    return output
