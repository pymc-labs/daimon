"""Plain error rendering for Discord adapter responses.

Maps known failures to plain copy. Exception bodies stay in the logs; they
are never interpolated into a chat response. A failure shows a short `Ref`,
the last six characters of the request id, and the full id is logged with it.
"""

from __future__ import annotations

import anthropic
import structlog
from daimon.core.anthropic_spend import spend_limit_error
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
from daimon.core.platform_words import words_for
from daimon.core.stores.direct_messages import DirectMessageBusy
from daimon.core.thread_handoff import ThreadHandoffRefused
from daimon.core.turn.errors import SessionAgentMismatch
from daimon.core.turn.notices import RefusalNouns, resolver_miss_text, short_ref
from mux.errors import ProviderError
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


def setup_out_of_date_notice(*, kind: str, name: str | None) -> str:
    """What a channel whose agent or environment is gone is told; a gone agent is named."""
    return resolver_miss_text(RefusalNouns.for_platform("discord"), kind=kind, name=name)


USAGE_LIMIT_LINES = (
    "Daimon has reached its usage limit.",
    "Ask the team running it to check the limit.",
)

_AI_BUSY = ("Daimon's AI service is busy.", "Try again in a minute.")
_AI_UNREACHABLE = ("Daimon couldn't reach its AI service.", "Try again in a minute.")
_AI_REFUSED = (
    "Daimon's AI service couldn't accept the request.",
    "Try again, or start a new thread. If it keeps happening, tell the team running Daimon.",
)
_AI_TOO_LARGE = (
    "That was too much for Daimon's AI service to take in at once.",
    "Shorten it, or start a new thread.",
)
_AI_KEY_REJECTED = (
    "The AI provider rejected Daimon's request.",
    "The team running Daimon needs to check its key for this provider.",
)
_AI_CREDENTIAL_GUARD = (
    "Daimon stopped this request to keep a credential safe.",
    "Tell the team running Daimon.",
)
_AI_CREDENTIAL_MISSING = (
    "A credential Daimon needs isn't available right now.",
    "Tell the team running Daimon.",
)
_AI_MODEL_GONE = (
    "The model this agent uses isn't offered any more.",
    "Switch the agent's model, or ask its owner or an admin to.",
)
_PLATFORM_REFUSED = ("Discord didn't accept that.", "Try again.")
_OUR_SIDE = (
    "Something went wrong on our side.",
    "Try again. If it keeps happening, tell an admin.",
)


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
        return (
            f"This {words_for('discord').space} already has an agent with that name. "
            "Pick a different name."
        )
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
    return None


def _provider_lines(exc: ProviderError) -> tuple[str, str]:
    """Any provider behind the neutral driver layer (Gemini, OpenAI...), by category."""
    if exc.category in {"rate_limited", "overloaded"}:
        return _AI_BUSY
    if exc.category == "transient_network" or (exc.category == "upstream" and exc.retryable):
        return _AI_UNREACHABLE
    codes = {exc.native_code, exc.native_error_code}
    if codes & _REQUEST_TOO_LARGE:
        return _AI_TOO_LARGE
    if _guards_a_credential(exc.native_code):
        return _AI_CREDENTIAL_GUARD
    if (exc.native_code or "").endswith("_secret_unavailable"):
        return _AI_CREDENTIAL_MISSING
    if exc.category in {"auth", "permission"} and codes & _PROVIDER_REJECTED:
        return _AI_KEY_REJECTED
    if exc.category == "not_found" and _names_the_model(exc):
        return _AI_MODEL_GONE
    return _AI_REFUSED


# What a provider says when the request itself was too big: the HTTP status (OpenAI and
# self-hosted keep it bare, Gemini as `http_413`) or Anthropic's error type. A workspace
# snapshot or an artifact over its limit is not the person's message, so it is not here.
_REQUEST_TOO_LARGE = frozenset({"413", "http_413", "request_too_large"})
# A key or permission the provider itself refused, in the same three spellings.
_PROVIDER_REJECTED = frozenset(
    {"401", "403", "http_401", "http_403", "authentication_error", "permission_error"}
)


def _guards_a_credential(code: str | None) -> bool:
    """Whether Daimon's own driver stopped the call before it reached the provider,
    because a credential would have been echoed or used outside its binding."""
    code = code or ""
    return code.endswith(("_echo", "_secret_outside_binding")) or code == "gcs_downscoping_failed"


def _names_the_model(exc: ProviderError) -> bool:
    """Whether a not-found is about the model: other ids (a past reply, a file) miss too."""
    return exc.native_param == "model" or any(
        "model" in (field or "").lower() for field in (exc.native_error_code, exc.native_message)
    )


def _cause_lines(exc: BaseException) -> tuple[str, str]:
    """What happened and what to do, by cause. Never the exception's text."""
    if isinstance(exc, TurnError) and isinstance(exc.cause, Exception):
        return _cause_lines(exc.cause)
    if spend_limit_error(exc) is not None:
        return USAGE_LIMIT_LINES
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code in {429, 529}:
            return _AI_BUSY
        if exc.status_code == 413:
            return _AI_TOO_LARGE
        if exc.status_code in {401, 403}:
            return _AI_KEY_REJECTED
        if 400 <= exc.status_code < 500:
            # A 404 here is a missing session or agent as often as a model, so it
            # keeps the general wording.
            return _AI_REFUSED
        return _AI_UNREACHABLE
    if isinstance(exc, anthropic.APIError):
        # Connection failures, timeouts and errors with no status.
        return _AI_UNREACHABLE
    if isinstance(exc, ProviderError):
        return _provider_lines(exc)
    if isinstance(exc, discord.HTTPException):
        return _PLATFORM_REFUSED
    return _OUR_SIDE


WAKE_REFUSED_LINES = (
    "The fleet finished, but I couldn't pick this conversation back up to report on it. "
    "That's a problem on our side.",
    "Mention me to continue.",
)


def render_wake_refused(*, request_id: str) -> str:
    """A fleet completion wake that was refused before its turn ran, with its `Ref`."""
    return "\n\n".join((*WAKE_REFUSED_LINES, f"-# Ref {short_ref(request_id)}"))


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
    Fixed guidance for our own errors keeps its one line, and the ref.

    `new_responder`/`owner`/`channel` are the contextual facts a
    `SessionAgentMismatch` render needs (who answers now, whose work this
    conversation belongs to, where). Resolving them (an async agent-name
    lookup) is the caller's job -- this function stays synchronous -- so
    every other caller omits them and gets a generic fallback phrasing.
    `offer_button` says the notice carries the Hand over button.
    """
    if isinstance(exc, SessionAgentMismatch):
        return render_responder_changed_without_handoff(
            new_responder=new_responder or "the current agent",
            owner=owner or "the previous agent",
            channel=channel or "this channel",
            offer_button=offer_button,
        )
    lines = list(error_lines(exc))
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
