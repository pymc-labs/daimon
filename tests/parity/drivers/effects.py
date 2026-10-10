"""Chronological platform API effects captured at the fake transport boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EffectKind = Literal["send", "edit", "delete", "react", "upload", "ack"]


@dataclass(frozen=True)
class DeliveryEffect:
    kind: EffectKind
    message_id: str | None = None
    text: str | None = None
    summary: str | None = None
    feedback: bool = False
    files: tuple[str, ...] = ()
    reply_to: str | None = None
    sender: str = "bot"
    success: bool = True
    error: str | None = None


def cost_line(payload: object) -> str | None:
    """Find the rendered cost line inside a Discord embed, Slack blocks or Teams card."""
    if isinstance(payload, str):
        return payload if "$" in payload and "used" in payload else None
    if isinstance(payload, dict):
        for value in payload.values():
            if found := cost_line(value):
                return found
    if isinstance(payload, list):
        for value in payload:
            if found := cost_line(value):
                return found
    return None
