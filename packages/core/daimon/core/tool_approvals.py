"""Daimon's own Approve card for a gated call on an OpenAI or Gemini turn.

Those providers can't hold a tool call for a person's Approve. For a tool that runs
inside the MCP process (publishing, add_skill), Daimon posts a requester-only card
for that exact call (its tool name and arguments' digest). An approval lets the
agent's identical retry run once, within a few minutes, in the same thread; the
MCP middleware spends it and the tool's own card gate (`session_card_gap`)
accepts that one call.
"""

from __future__ import annotations

import contextvars
import datetime
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final, Literal

import structlog

if TYPE_CHECKING:
    from daimon.core.posted_controls import PostedCard
    from daimon.core.switch_cards import SwitchCardPress
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

#: How long an approval waits for the agent's retry.
APPROVAL_WINDOW: Final[datetime.timedelta] = datetime.timedelta(minutes=10)

#: An approval card's buttons: ``tap:<card id>:approve`` or ``…:cancel``.
CUSTOM_ID_PATTERN: Final[str] = r"tap:(?P<card>[0-9a-f-]{36}):(?P<action>approve|cancel)"
_CUSTOM_ID = re.compile(CUSTOM_ID_PATTERN)

ApprovalAction = Literal["approve", "cancel"]

#: The gated call an OpenAI/Gemini turn is making (tool, its arguments' digest,
#: its arguments), set by the MCP middleware for the tool's card gate.
pending_call: contextvars.ContextVar[tuple[str, str, dict[str, object]] | None] = (
    contextvars.ContextVar("daimon_pending_call", default=None)
)


def args_digest(tool_name: str, arguments: Mapping[str, object]) -> str:
    """The exact call an approval covers: tool and arguments, never the turn's origin."""
    body = {key: value for key, value in arguments.items() if key != "origin_context_id"}
    return hashlib.sha256(
        json.dumps([tool_name, body], sort_keys=True, default=str).encode()
    ).hexdigest()


def card_custom_id(card_id: uuid.UUID, action: ApprovalAction) -> str:
    return f"tap:{card_id}:{action}"


def parse_custom_id(custom_id: str) -> tuple[uuid.UUID, ApprovalAction] | None:
    match = _CUSTOM_ID.fullmatch(custom_id)
    if match is None:
        return None
    try:
        return uuid.UUID(match["card"]), match["action"]  # pyright: ignore[reportReturnType]
    except ValueError:
        return None


APPROVAL_FOOTER: Final[str] = "Only {requester} can approve. Expires {expires}."
APPROVED_TEXT: Final[str] = (
    "Approved. Tell the agent to go ahead: it can run this once in the next 10 minutes."
)
CANCELLED_TEXT: Final[str] = "Cancelled. Nothing ran."
EXPIRED_TEXT: Final[str] = "This request ended before anyone clicked. Ask again to redo it."
ANSWERED_TEXT: Final[str] = "This card was already answered."


def requested_approval_card(
    card_id: uuid.UUID,
    *,
    title: str,
    facts: tuple[str, ...],
    requester_platform_user_id: str,
    expires_at: datetime.datetime,
) -> PostedCard:
    from daimon.core.posted_controls import CardButton, PostedCard

    return PostedCard(
        kind="approval",
        state="requested",
        headline=f"🔐 {title}",
        facts=facts,
        buttons=(
            CardButton(
                label="Approve", style="primary", custom_id=card_custom_id(card_id, "approve")
            ),
            CardButton(
                label="Cancel", style="primary", custom_id=card_custom_id(card_id, "cancel")
            ),
        ),
        footer=APPROVAL_FOOTER,
        requester_platform_user_id=requester_platform_user_id,
        expires_at_unix=int(expires_at.timestamp()),
    )


def answered_approval_card(text: str) -> PostedCard:
    from daimon.core.posted_controls import PostedCard

    if text == APPROVED_TEXT:
        return PostedCard(kind="approval", state="applied", headline=f"✅ {text}")
    if text == EXPIRED_TEXT:
        return PostedCard(kind="approval", state="expired", headline=f"⌛ {text}")
    return PostedCard(kind="approval", state="refused", headline=f"🛡️ {text}")


async def press_tool_approval(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    card_id: uuid.UUID,
    action: ApprovalAction,
    clicker: str,
    message_id: str,
    now: datetime.datetime,
) -> SwitchCardPress | None:
    """Answer one press. None: not the requester's, or not this card's message."""
    from daimon.core.stores import tool_approval_cards as cards
    from daimon.core.switch_cards import SwitchCardPress

    async with sessionmaker() as session:
        card = await cards.get_card(session, tenant_id=tenant_id, card_id=card_id)
    if card is None or card.requester_platform_user_id != clicker or card.message_id != message_id:
        return None
    if card.status != "pending":
        text = {"approved": APPROVED_TEXT, "cancelled": CANCELLED_TEXT}.get(
            card.status, ANSWERED_TEXT
        )
        return SwitchCardPress(text, closed=True, applied=card.status == "approved")
    async with sessionmaker.begin() as session:
        if action == "cancel":
            done = await cards.cancel(session, tenant_id=tenant_id, card_id=card_id)
            text = CANCELLED_TEXT
        else:
            done = await cards.approve(
                session, tenant_id=tenant_id, card_id=card_id, now=now, until=now + APPROVAL_WINDOW
            )
            text = APPROVED_TEXT
    if not done:
        return SwitchCardPress(
            EXPIRED_TEXT if card.expires_at <= now else ANSWERED_TEXT, closed=True
        )
    log.info("tool_approval.pressed", card_id=str(card_id), action=action, tool=card.tool_name)
    return SwitchCardPress(text, closed=True, applied=action == "approve")
