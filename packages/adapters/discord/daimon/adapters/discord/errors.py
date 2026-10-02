"""Structured error rendering for Discord adapter responses.

Maps known exception types to user-friendly markdown with emoji prefix,
bold labels, and ULID request ID suffix for cross-referencing with logs.
"""

from __future__ import annotations

import anthropic
import structlog
from daimon.core.continuity.messages import render_responder_changed_without_handoff
from daimon.core.errors import (
    DaimonError,
    SpecError,
    StoreError,
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

    Reusing it means the id on a failed turn's card finds every log line of
    that turn, not only the failure line.
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
    """Map known exceptions to structured markdown with emoji, label, and rid.

    `new_responder`/`owner`/`channel` are the contextual facts a
    `SessionAgentMismatch` render needs (who answers now, whose work this
    conversation belongs to, where). Resolving them (an async agent-name
    lookup) is the caller's job -- this function stays synchronous -- so
    every other caller omits them and gets a generic fallback phrasing.
    `offer_button` says the notice carries the Hand over button.
    """
    if isinstance(exc, SessionAgentMismatch):
        return (
            render_responder_changed_without_handoff(
                new_responder=new_responder or "the current responder",
                owner=owner or "the previous agent",
                channel=channel or "this channel",
                offer_button=offer_button,
            )
            + f"\n`rid: {request_id}`"
        )
    if isinstance(exc, SpecError):
        return f"⚠️ **Spec validation failed**: {exc}\n`rid: {request_id}`"
    if isinstance(exc, StoreError):
        return f"⚠️ **Store error**: {exc}\n`rid: {request_id}`"
    if isinstance(exc, DaimonError):
        return f"⚠️ **Error**: {exc}\n`rid: {request_id}`"
    if isinstance(exc, anthropic.APIStatusError):
        return f"❌ **API Error ({exc.status_code})**: {exc.message}\n`rid: {request_id}`"
    if isinstance(exc, anthropic.APIConnectionError):
        return (
            f"\U0001f50c **Connection Error**: "
            f"Could not connect to Anthropic API. Please try again.\n"
            f"`rid: {request_id}`"
        )
    if isinstance(exc, anthropic.APIError):
        return f"❌ **API Error**: {exc.message}\n`rid: {request_id}`"
    if isinstance(exc, discord.HTTPException):
        return f"❌ **Discord Error ({exc.status})**: {exc.text}\n`rid: {request_id}`"
    if isinstance(exc, SQLAlchemyError):
        # Never `{exc}` here: DBAPIError stringifies to the failing statement
        # plus its bound parameters, which would publish both to the channel.
        # The rid is the handle for the real detail, which stays in the logs.
        return (
            f"❌ **Database error** ({type(exc).__name__}). Please try again.\n`rid: {request_id}`"
        )
    if isinstance(exc, ValueError):
        return f"⚠️ **Invalid input**: {exc}\n`rid: {request_id}`"
    return f"❌ **Unexpected error**: {exc}\n`rid: {request_id}`"
