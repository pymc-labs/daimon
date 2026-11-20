"""Neutral chat events (`ChatEvent`) an item records, and cards from the core card builders."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Final, Literal

from daimon.core.confirmation import ConfirmationPrompt
from daimon.core.evals.format import (
    CardAnswer,
    CardButton,
    CardFact,
    CardField,
    ChatEvent,
    NeutralCard,
)
from daimon.core.posted_controls.cards import PostedCard
from daimon.core.posted_controls.confirmation import build_confirmation_card
from pydantic import JsonValue

BOT: Final = "bot"
"""The `actor` of what the agent and Daimon post; people appear by participant id."""

ChatKind = Literal[
    "message", "edit", "delete", "reaction_add", "reaction_remove", "card", "card_update",
    "typing", "status", "title", "notice", "file",
]  # fmt: skip


@dataclass
class ChatLog:
    """The item's chat in order, timed from its start."""

    events: list[ChatEvent] = field(default_factory=list[ChatEvent])
    _start: float = field(default_factory=time.monotonic)

    def add(self, kind: ChatKind, actor: str = BOT, **fields: JsonValue | object) -> ChatEvent:
        event = ChatEvent.model_validate(
            {
                "seq": len(self.events) + 1,
                "t_ms": int((time.monotonic() - self._start) * 1000),
                "kind": kind,
                "actor": actor,
                **{k: v for k, v in fields.items() if v is not None},
            }
        )
        self.events.append(event)
        return event

    def message_id(self) -> str:
        return f"m{len(self.events) + 1}"


def confirmation_card(card_id: str, prompt: ConfirmationPrompt) -> NeutralCard:
    """A confirmation card as the platforms render it while it waits for a press."""
    card = build_confirmation_card(prompt, state="pending", token=card_id)
    buttons = [
        CardButton(id="approve", label="Approve", style="primary", action="approve"),
        CardButton(id="deny", label="Deny", style="danger", action="deny"),
    ]
    if prompt.tool_rules_enabled:
        buttons.append(CardButton(id="always", label="Always allow", action="always"))
    if card.detail_lines:
        buttons.append(CardButton(id="details", label="Details", action="details"))
    return NeutralCard(
        id=card_id, kind="confirmation", headline=card.headline,
        body=card.body or card.consequence, buttons=buttons, footer=card.footer,
        facts=[CardFact(label="", value=line) for line in prompt.visible_lines] or None,
    )  # fmt: skip


def posted_card(card_id: str, card: PostedCard) -> NeutralCard:
    """A posted control card (credential requests, host calls, switches) in its state. A
    credential request ("cred:<token>") is its private form: one secret, Save or Skip, and
    the token as its id, which the script's presses take."""
    kind, _, token = card_id.partition(":")
    if kind == "cred":
        return NeutralCard(
            id=token, kind="credential_request", headline=card.headline,
            fields=[CardField(id="value", label=card.headline, kind="secret")],
            buttons=[CardButton(id="save", label="Save", style="primary", action="save"),
                     CardButton(id="skip", label="Skip", action="skip")],
            state="open" if card.state == "requested" else "answered",
        )  # fmt: skip
    kind = "host_call" if card.kind in ("call", "switch") else "credential_request"
    buttons = [
        CardButton(
            id=button.custom_id or f"b{i}", label=button.label,
            style="link" if button.style == "link" else button.style,
            action="open_url" if button.style == "link" else _action(button.label),
            url=button.url,
        )
        for i, button in enumerate(card.buttons)
    ]  # fmt: skip
    return NeutralCard(
        id=card_id, kind=kind, headline=card.headline, buttons=buttons or None,
        facts=[CardFact(label="", value=fact) for fact in card.facts] or None, footer=card.footer,
        state="open" if card.state == "requested" else "answered",
    )  # fmt: skip


def _action(label: str) -> Literal["approve", "deny", "skip", "save", "custom"]:
    lowered = label.lower()
    for word, action in (("approve", "approve"), ("deny", "deny"), ("skip", "skip"),
                         ("save", "save"), ("allow", "approve"), ("cancel", "deny")):  # fmt: skip
        if word in lowered:
            return action  # pyright: ignore[reportReturnType]
    return "custom"


def answered(
    card: NeutralCard, button: CardButton | None, by: str, fields: dict[str, str] | None = None
) -> NeutralCard:
    """The card after `by` pressed `button`; no button: it expired."""
    if button is None:
        return card.model_copy(update={"state": "expired"})
    return card.model_copy(
        update={"state": "answered", "answer": CardAnswer(by=by, button=button.id, fields=fields)}
    )
