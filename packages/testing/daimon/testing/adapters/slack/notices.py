"""Reading what a person sees in a Slack call body that carries a notice card."""

from __future__ import annotations

from typing import Any


def shown_text(body: dict[str, Any]) -> str:
    """What a person reads in a Slack call body: its text, else its notice card's text."""
    if body.get("text"):
        return str(body["text"])
    for attachment in body.get("attachments") or []:
        if attachment.get("fallback"):
            return str(attachment["fallback"])
    return ""
