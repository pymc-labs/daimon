"""Private Slack GitHub connection card shared by adapters."""

from __future__ import annotations

from typing import Any


def connect_button_blocks(url: str, line: str) -> list[dict[str, Any]]:
    """One readable line and a URL button, with no link in fallback text."""
    return [
        {"type": "section", "text": {"type": "plain_text", "text": line}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "github_link__open",
                    "text": {"type": "plain_text", "text": "Connect GitHub"},
                    "url": url,
                }
            ],
        },
    ]
