"""Plain error rendering for Slack adapter responses.

Maps known failures to two plain lines, what happened and what to do, and a
small `Ref`: the last six characters of the request id, logged in full with
the exception. Exception text never reaches the chat. Slack counterpart of
``discord/errors.py``; adapters cannot share the module because they must
not import each other.
"""

from __future__ import annotations

import contextlib
from typing import Any, cast

import anthropic
import structlog
from cryptography.fernet import InvalidToken
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.core.anthropic_spend import spend_limit_error
from daimon.core.channel_budget import ChannelBudgetError
from daimon.core.continuity.handoff import HandoffRefusedInSetupThread
from daimon.core.cron import InvalidScheduleError
from daimon.core.errors import AgentNameCollision, TurnError, UserFacingError
from daimon.core.notebooks.publish import NotebookRateLimitError
from daimon.core.stores.direct_messages import DirectMessageBusy
from daimon.core.thread_handoff import ThreadHandoffRefused
from daimon.core.turn.errors import SessionAgentMismatch
from daimon.core.turn.notices import RefusalNouns, resolver_miss_text, short_ref
from mux.errors import ProviderError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from ulid import ULID

log = structlog.get_logger(__name__)


def generate_request_id() -> str:
    """Generate a ULID for request tracing."""
    return str(ULID())


def bound_request_id() -> str:
    """The `rid` bound in this turn's log context, or a fresh one.

    Reusing it means the id on a failed turn's card finds every log line of
    that turn, not only the failure line.
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
# What someone from another workspace (Slack Connect) is told: they cannot run
# `/agent-setup`, so they are pointed at the people who can.
_NOT_SET_UP_FOREIGN = (
    "Daimon isn't set up in this channel yet.\n\n"
    "Ask someone in the workspace that added Daimon to set it up."
)
_SETUP_OUT_OF_DATE_FOREIGN = (
    "This channel's setup is out of date.\n\n"
    "Ask someone in the workspace that added Daimon to check it."
)


def not_set_up_notice(*, foreign: bool = False) -> str:
    """The not-set-up notice; `foreign` words it for a member of another workspace."""
    return _NOT_SET_UP_FOREIGN if foreign else NOT_SET_UP_NOTICE


def setup_out_of_date_notice(
    *, foreign: bool = False, kind: str | None = None, name: str | None = None
) -> str:
    """The setup-out-of-date notice, for the same two audiences.

    A channel whose agent is gone names it: the `kind` and `name` a resolver miss carries.
    """
    if kind == "agent" and name:
        nouns = RefusalNouns.for_platform("slack", foreign=foreign)
        return resolver_miss_text(nouns, kind=kind, name=name)
    return _SETUP_OUT_OF_DATE_FOREIGN if foreign else SETUP_OUT_OF_DATE_NOTICE


_USAGE_LIMIT = (
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
_AI_MODEL_GONE = (
    "The model this agent uses isn't offered any more.",
    "Switch the agent's model, or ask its owner or an admin to.",
)
_NO_PERMISSION = (
    "Daimon doesn't have permission to do that in Slack.",
    "Ask a workspace admin to check Daimon's permissions and reinstall it.",
)
_CANT_CONNECT = (
    "Daimon can't connect to this Slack workspace.",
    "Tell the team running Daimon.",
)
_PLATFORM_REFUSED = ("Slack didn't accept that.", "Try again.")
_OUR_SIDE = (
    "Something went wrong on our side.",
    "Try again. If it keeps happening, tell an admin.",
)
_SESSION_AGENT_MISMATCH = (
    "This conversation belongs to another agent. Your work is kept. "
    "To use a different agent, start a new conversation."
)


def _guidance(exc: BaseException) -> str | None:
    """Fixed, reviewed copy for our own errors, or None for every other failure.

    `UserFacingError` is the one class whose text is shown: it is raised only
    with copy written for people. Every other branch is a fixed sentence.
    """
    if isinstance(exc, TurnError) and isinstance(exc.cause, Exception):
        return _guidance(exc.cause)
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
    return None


def _provider_lines(exc: ProviderError) -> tuple[str, str]:
    """Any provider behind the neutral driver layer (Gemini, OpenAI...), by category."""
    if exc.category in {"rate_limited", "overloaded"}:
        return _AI_BUSY
    if exc.category == "transient_network" or (exc.category == "upstream" and exc.retryable):
        return _AI_UNREACHABLE
    if exc.native_code == "413" or (exc.native_code or "").endswith("_too_large"):
        return _AI_TOO_LARGE
    if exc.category in {"auth", "permission"}:
        return _AI_KEY_REJECTED
    if exc.category == "not_found" and _names_the_model(exc):
        return _AI_MODEL_GONE
    return _AI_REFUSED


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
        return _USAGE_LIMIT
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
    if isinstance(exc, SlackApiError):
        response = cast(Any, exc.response)  # pyright: ignore[reportUnknownMemberType]  # SlackApiError.response is untyped
        if response.get("error") == "missing_scope":
            return _NO_PERMISSION
        return _PLATFORM_REFUSED
    if isinstance(exc, InvalidToken):
        # A stored bot token could not be decrypted: an operator problem.
        return _CANT_CONNECT
    return _OUR_SIDE


def _rendered(exc: Exception, request_id: str, *, mrkdwn: bool = True) -> tuple[str, str | None]:
    """The message and its `Ref` line (None without a request id), logging the full id.

    With `mrkdwn` the message is escaped for a mrkdwn field, so `UserFacingError`
    text can never turn into a live mention or link; without it the message is
    plain text for a field that escapes on its own.
    """
    if isinstance(exc, SessionAgentMismatch):
        return _SESSION_AGENT_MISMATCH, None  # its own wording, unchanged
    guidance = _guidance(exc)
    if guidance is not None:
        message = escape_mrkdwn(guidance) if mrkdwn else guidance
    else:
        message = "\n\n".join(_cause_lines(exc))
    if not request_id:
        return message, None
    ref = short_ref(request_id)
    log.warning(
        "error.rendered",
        rid=request_id,
        ref=ref,
        error_type=type(exc).__name__,
        exc_info=exc,
    )
    return message, f"Ref {ref}"


def render_error(exc: Exception, *, request_id: str) -> str:
    """Plain mrkdwn for a failure: what happened, what to do, then `Ref XXXXXX`.

    Lines are separated by a blank line. The ref is the last six characters
    of `request_id`; the full id and the exception are logged here so support
    can find the failure from it. No exception text reaches the chat. Where
    the message can carry blocks, use `render_error_payload` so the ref is
    drawn small.
    """
    message, ref = _rendered(exc, request_id)
    return message if ref is None else f"{message}\n\n_{ref}_"


def _error_blocks(message: str, ref: str | None) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": message}}
    ]
    if ref is not None:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": ref}]})
    return blocks


def render_error_text(exc: Exception, *, request_id: str) -> str:
    """`render_error` as plain text, for a field that escapes its own content.

    Same lines and the same logged ref, without mrkdwn escaping or italics.
    """
    message, ref = _rendered(exc, request_id, mrkdwn=False)
    return message if ref is None else f"{message}\n\n{ref}"


def render_error_payload(exc: Exception, *, request_id: str) -> dict[str, Any]:
    """`text` and `blocks` for a failure message: the ref in a small context block."""
    message, ref = _rendered(exc, request_id)
    return {
        "text": message if ref is None else f"{message}\n\n_{ref}_",
        "blocks": _error_blocks(message, ref),
    }


def build_error_view(exc: Exception, *, title: str, request_id: str) -> dict[str, Any]:
    """Modal that replaces a "Loading…" placeholder when the background fetch fails.

    ``title`` must match the placeholder's title so the modal does not visibly
    change identity when ``views.update`` swaps the content.
    """
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": title},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": render_error_payload(exc, request_id=request_id)["blocks"],
    }


async def surface_command_error(
    client: AsyncWebClient,
    exc: Exception,
    *,
    request_id: str,
    title: str,
    view_id: str,
    channel_id: str,
    user_id: str,
) -> None:
    """Show a slash-command failure to the invoker.

    With a ``view_id`` the open Loading… modal is replaced in place; without
    one (``views.open`` itself failed, or the command has no modal) the error
    goes out as an ephemeral. A failure of the notice itself is swallowed —
    the original exception has already been logged and captured, and there
    is nowhere further to report.
    """
    with contextlib.suppress(SlackApiError):
        if view_id:
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                view_id=view_id,
                view=build_error_view(exc, title=title, request_id=request_id),
            )
        else:
            await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                channel=channel_id,
                user=user_id,
                **render_error_payload(exc, request_id=request_id),
            )
