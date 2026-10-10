"""Plain error rendering for Discord adapter responses.

Maps known failures to plain copy. Exception bodies and trace identifiers
stay in the logs; they are never interpolated into a chat response.
"""

from __future__ import annotations

import anthropic
import structlog
from daimon.core.continuity.messages import render_responder_changed_without_handoff
from daimon.core.errors import (
    AgentNameCollision,
    SpecError,
    StoreError,
    TurnError,
)
from daimon.core.turn.errors import SessionAgentMismatch
from sqlalchemy.exc import SQLAlchemyError
from ulid import ULID

import discord


def generate_request_id() -> str:
    """Generate a ULID for request tracing."""
    return str(ULID())


def bound_request_id() -> str:
    """The `rid` this turn's handler bound for its logs, or a fresh one.

    Reusing it ties the failure log to every earlier log line of that turn.
    """
    rid = structlog.contextvars.get_contextvars().get("rid")
    return rid if isinstance(rid, str) and rid else generate_request_id()


def render_error(
    exc: Exception,
    *,
    request_id: str,
    new_responder: str | None = None,
    owner: str | None = None,
    channel: str | None = None,
    offer_button: bool = False,
) -> str:
    """Map known exceptions to plain sentences without exposing their bodies.

    `new_responder`/`owner`/`channel` are the contextual facts a
    `SessionAgentMismatch` render needs (who answers now, whose work this
    conversation belongs to, where). Resolving them (an async agent-name
    lookup) is the caller's job -- this function stays synchronous -- so
    every other caller omits them and gets a generic fallback phrasing.
    `offer_button` says the notice carries the Hand over button.
    """
    # Keep this argument for existing callers that bind the same id in logs.
    # Exception text can contain provider JSON, credentials and internal ids.
    del request_id
    if isinstance(exc, SessionAgentMismatch):
        return render_responder_changed_without_handoff(
            new_responder=new_responder or "the current responder",
            owner=owner or "the previous agent",
            channel=channel or "this channel",
            offer_button=offer_button,
        )
    if isinstance(exc, TurnError) and isinstance(exc.cause, Exception):
        return render_error(exc.cause, request_id="")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code in {503, 529}:
            return "Claude is overloaded right now. Try again in a minute."
        if exc.status_code == 429:
            return "Too many requests right now. Try again in a minute."
        if exc.status_code in {401, 403}:
            return "Daimon couldn't connect to Claude. Ask an admin to check the connection."
        if exc.status_code == 400:
            return "Claude couldn't accept this request. Try sending it again."
        return "Claude is unavailable right now. Try again in a minute."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Daimon couldn't reach Claude. Try again in a minute."
    if isinstance(exc, anthropic.APIError):
        return "Daimon couldn't get a reply from Claude. Try again in a minute."
    if isinstance(exc, discord.HTTPException):
        if exc.code == 160004:
            return "A conversation already exists for this message. Continue in its thread."
        if exc.status == 403:
            return "Daimon doesn't have permission to post here. Ask a server admin for help."
        return "Daimon couldn't update a message in Discord. Try again in a minute."
    if isinstance(exc, (SQLAlchemyError, StoreError)):
        return "Daimon couldn't load or save this change. Try again in a minute."
    if isinstance(exc, SpecError):
        return "Daimon couldn't read this setup. Ask an admin to check it."
    if isinstance(exc, AgentNameCollision):
        return "This workspace already has an agent with that name. Pick a different name."
    if isinstance(exc, ValueError):
        return "Daimon couldn't use that input. Check it and try again."
    return "Something went wrong while handling your request. Try again in a minute."
