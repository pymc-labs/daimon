"""Plain error rendering for Discord adapter responses.

Maps known failures to plain copy. Exception bodies stay in the logs; they
are never interpolated into a chat response. A failure shows a short `Ref`,
the last six characters of the request id, and the full id is logged with it.
"""

from __future__ import annotations

import anthropic
import structlog
from daimon.core.anthropic_spend import spend_limit_error
from daimon.core.channel_admins import InvalidChannelAdminIds
from daimon.core.channel_budget import ChannelBudgetError
from daimon.core.continuity.handoff import HandoffRefusedInSetupThread
from daimon.core.continuity.messages import render_responder_changed_without_handoff
from daimon.core.cron import InvalidScheduleError
from daimon.core.errors import (
    AgentNameCollision,
    TurnError,
    UserFacingError,
)
from daimon.core.notebooks.publish import NotebookRateLimitError
from daimon.core.stores.direct_messages import DirectMessageBusy
from daimon.core.thread_handoff import ThreadHandoffRefused
from daimon.core.turn.errors import SessionAgentMismatch
from ulid import ULID

import discord

log = structlog.get_logger(__name__)


def generate_request_id() -> str:
    """Generate a ULID for request tracing."""
    return str(ULID())


def bound_request_id() -> str:
    """The `rid` this turn's handler bound for its logs, or a fresh one.

    Reusing it ties the failure log to every earlier log line of that turn.
    """
    rid = structlog.contextvars.get_contextvars().get("rid")
    return rid if isinstance(rid, str) and rid else generate_request_id()


# "Daimon can't answer here": the same words on every platform; only the
# command name differs (Teams says `setup`).
NOT_SET_UP_NOTICE = (
    "Daimon isn't set up in this channel yet.\n\nAsk an admin to run `/agent-setup`."
)
SETUP_OUT_OF_DATE_NOTICE = (
    "This channel's setup is out of date.\n\nAsk an admin to check `/agent-setup`."
)
USAGE_LIMIT_LINES = (
    "Daimon has reached its usage limit.",
    "Ask the team running it to check the limit.",
)

_AI_BUSY = ("Daimon's AI service is busy.", "Try again in a minute.")
_AI_UNREACHABLE = ("Daimon couldn't reach its AI service.", "Try again in a minute.")
_AI_REFUSED = ("Daimon's AI service couldn't accept the request.", "Ask an admin to check it.")
_PLATFORM_REFUSED = ("Discord didn't accept that.", "Try again.")
_OUR_SIDE = (
    "Something went wrong on our side.",
    "Try again. If it keeps happening, tell an admin.",
)


def short_ref(request_id: str) -> str:
    """The last six characters of a request id: the `Ref` a person can quote."""
    return request_id[-6:].upper()


def _guidance(exc: BaseException) -> str | None:
    """Fixed, reviewed copy for our own errors, or None for every other failure.

    `UserFacingError` is the one class whose text is shown: it is raised only
    with copy written for people. Every other branch is a fixed sentence.
    """
    if isinstance(exc, TurnError) and isinstance(exc.cause, Exception):
        return _guidance(exc.cause)
    if isinstance(exc, discord.HTTPException):
        if exc.code == 160004:
            return "A conversation already exists for this message. Continue in its thread."
        if exc.status == 403:
            return "Daimon doesn't have permission to post here. Ask a server admin for help."
        return None
    if isinstance(exc, AgentNameCollision):
        return "This workspace already has an agent with that name. Pick a different name."
    if isinstance(exc, UserFacingError):
        return str(exc)
    if isinstance(exc, DirectMessageBusy):
        return (
            "A reply is still running. Wait for it to finish before starting another conversation."
        )
    if isinstance(exc, HandoffRefusedInSetupThread):
        return (
            "This setup conversation can't change agents. Start a new thread to use another agent."
        )
    if isinstance(exc, ThreadHandoffRefused):
        return (
            "This conversation can't change agents. "
            "Ask an admin to check the agent and channel settings."
        )
    if isinstance(exc, ChannelBudgetError):
        return "That spending budget isn't valid. Check its amount and time window, then try again."
    if isinstance(exc, NotebookRateLimitError):
        return "The notebook publishing limit has been reached. Try again later."
    if isinstance(exc, InvalidScheduleError):
        return "That schedule isn't valid. Check its cron expression and timezone, then try again."
    if isinstance(exc, InvalidChannelAdminIds):
        return "That admin selection isn't valid. Check the selected people and try again."
    return None


def _cause_lines(exc: BaseException) -> tuple[str, str]:
    """What happened and what to do, by cause. Never the exception's text."""
    if isinstance(exc, TurnError) and isinstance(exc.cause, Exception):
        return _cause_lines(exc.cause)
    if spend_limit_error(exc) is not None:
        return USAGE_LIMIT_LINES
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code in {429, 529}:
            return _AI_BUSY
        if 400 <= exc.status_code < 500:
            return _AI_REFUSED
        return _AI_UNREACHABLE
    if isinstance(exc, anthropic.APIError):
        # Connection failures, timeouts and errors with no status.
        return _AI_UNREACHABLE
    if isinstance(exc, discord.HTTPException):
        return _PLATFORM_REFUSED
    return _OUR_SIDE


def error_lines(exc: BaseException) -> tuple[str, ...]:
    """The plain lines for `exc` without the `Ref` line, for surfaces that draw their own."""
    guidance = _guidance(exc)
    return (guidance,) if guidance is not None else _cause_lines(exc)


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

    A failure reads as two lines, what happened and what to do, then a small
    `Ref` line: the last six characters of `request_id`. The full id and the
    exception are logged here, so support can find the failure from the ref.
    Fixed guidance for our own errors carries no ref.

    `new_responder`/`owner`/`channel` are the contextual facts a
    `SessionAgentMismatch` render needs (who answers now, whose work this
    conversation belongs to, where). Resolving them (an async agent-name
    lookup) is the caller's job -- this function stays synchronous -- so
    every other caller omits them and gets a generic fallback phrasing.
    `offer_button` says the notice carries the Hand over button.
    """
    if isinstance(exc, SessionAgentMismatch):
        return render_responder_changed_without_handoff(
            new_responder=new_responder or "the current responder",
            owner=owner or "the previous agent",
            channel=channel or "this channel",
            offer_button=offer_button,
        )
    guidance = _guidance(exc)
    if guidance is not None:
        return guidance
    lines = list(_cause_lines(exc))
    if request_id:
        log.warning(
            "error.rendered",
            rid=request_id,
            ref=short_ref(request_id),
            error_type=type(exc).__name__,
            exc_info=exc,
        )
        lines.append(f"-# Ref {short_ref(request_id)}")
    return "\n\n".join(lines)
