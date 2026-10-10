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
from daimon.core.channel_admins import InvalidChannelAdminIds
from daimon.core.channel_budget import ChannelBudgetError
from daimon.core.continuity.handoff import HandoffRefusedInSetupThread
from daimon.core.cron import InvalidScheduleError
from daimon.core.errors import AgentNameCollision, TurnError, UserFacingError
from daimon.core.notebooks.publish import NotebookRateLimitError
from daimon.core.stores.direct_messages import DirectMessageBusy
from daimon.core.thread_handoff import ThreadHandoffRefused
from daimon.core.turn.errors import SessionAgentMismatch
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

_USAGE_LIMIT = (
    "Daimon has reached its usage limit.",
    "Ask the team running it to check the limit.",
)
_AI_BUSY = ("Daimon's AI service is busy.", "Try again in a minute.")
_AI_UNREACHABLE = ("Daimon couldn't reach its AI service.", "Try again in a minute.")
_AI_REFUSED = ("Daimon's AI service couldn't accept the request.", "Ask an admin to check it.")
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
    "This conversation's session belongs to another responder. "
    "Your existing work is preserved. Continuing with this responder currently "
    "requires a new conversation."
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
    if isinstance(exc, AgentNameCollision):
        return "This workspace already has an agent with that name. Pick a different name."
    if isinstance(exc, UserFacingError):
        return escape_mrkdwn(str(exc))
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
        return _USAGE_LIMIT
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code in {429, 529}:
            return _AI_BUSY
        if 400 <= exc.status_code < 500:
            return _AI_REFUSED
        return _AI_UNREACHABLE
    if isinstance(exc, anthropic.APIError):
        # Connection failures, timeouts and errors with no status.
        return _AI_UNREACHABLE
    if isinstance(exc, SlackApiError):
        response = cast(Any, exc.response)  # pyright: ignore[reportUnknownMemberType]  # SlackApiError.response is untyped
        if response.get("error") == "missing_scope":
            return _NO_PERMISSION
        return _PLATFORM_REFUSED
    if isinstance(exc, InvalidToken):
        # A stored bot token could not be decrypted: an operator problem.
        return _CANT_CONNECT
    return _OUR_SIDE


def _rendered(exc: Exception, request_id: str) -> tuple[str, str | None]:
    """The message and its `Ref` line (None without a request id), logging the full id."""
    if isinstance(exc, SessionAgentMismatch):
        return _SESSION_AGENT_MISMATCH, None  # its own wording, unchanged
    guidance = _guidance(exc)
    message = guidance if guidance is not None else "\n\n".join(_cause_lines(exc))
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
