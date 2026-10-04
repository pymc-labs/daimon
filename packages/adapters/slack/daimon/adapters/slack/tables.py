"""Slack native table blocks, bounded by the shared parser's cell/row limits."""

from __future__ import annotations

from typing import Any

from daimon.adapters.slack.mrkdwn import (
    escape_markdown_block,
    linkify_emphasized_urls,
    normalize_slack_url_links,
    prose_mentions,
    seal_markdown_block,
)
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
        return escape_markdown_block(table.raw, preserve_mentions=False), await table_block(table)

    def chunks(markdown: str) -> list[str]:
        # Each message is parsed on its own, and a split can leave code lines
        # outside their fence, so every chunk is sealed as sent, keeping live
        # only the mentions that were already prose.
        mentions = prose_mentions(markdown)
        return [
            seal_markdown_block(chunk, mentions=mentions)
            for chunk in split_for_slack_safe(markdown)
        ]

    def escape_text(prose: str) -> str:
        # Linkify before escaping: it reads the raw markdown, and escaping adds
        # no URLs or asterisks. Table cells are raw_text and keep their URLs as is.
        return escape_markdown_block(
            linkify_emphasized_urls(normalize_slack_url_links(prose)),
            preserve_mentions=preserve_mentions,
        )

    output: list[tuple[str, dict[str, Any]]] = []
    parts = await render_tables(text, hook=hook if enabled else None)
    if all(isinstance(part, str) for part in parts):
        return [(chunk, {"type": "markdown", "text": chunk}) for chunk in chunks(escape_text(text))]
    for part in parts:
        if isinstance(part, str):
            if not part.strip():
                continue
            for chunk in chunks(escape_text(part)):
                output.append((chunk, {"type": "markdown", "text": chunk}))
        else:
            # Each table gets its own message, keeping the 10,000-cell-character
            # budget per message and preserving order with surrounding prose.
            output.append(part)
    return output
