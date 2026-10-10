"""Block Kit layout for private GitHub status cards."""

from __future__ import annotations

from typing import Any

from daimon.adapters.slack.mrkdwn import escape_mrkdwn_preserving_mentions


def github_card_blocks(
    text: str, *, buttons: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    title, _, detail = text.partition("\n")
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{escape_mrkdwn_preserving_mentions(title)}*"},
        }
    ]
    if detail:
        blocks.append(
            {
                "type": "section",
                "fields": [{"type": "mrkdwn", "text": escape_mrkdwn_preserving_mentions(detail)}],
            }
        )
    if buttons:
        blocks.extend(({"type": "divider"}, {"type": "actions", "elements": buttons}))
    return blocks
