"""Structured error rendering for Slack adapter responses.

Maps known exception types to user-facing mrkdwn with an emoji prefix, a bold
label, and a ULID request ID suffix for cross-referencing with logs. Slack
counterpart of ``discord/errors.py``; adapters cannot share the module
because they must not import each other.
"""

from __future__ import annotations

import contextlib
from typing import Any, cast

import anthropic
from cryptography.fernet import InvalidToken
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.core.errors import DaimonError, SpecError, StoreError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError
from ulid import ULID


def generate_request_id() -> str:
    """Generate a ULID for request tracing."""
    return str(ULID())


def render_error(exc: Exception, *, request_id: str) -> str:
    """Map known exceptions to mrkdwn with emoji, label, and rid.

    Exception text is mrkdwn-escaped so a message that quotes user input can
    never turn into a live mention or link.
    """
    rid = f"`rid: {request_id}`"
    if isinstance(exc, SpecError):
        return f"⚠️ *Spec validation failed*: {escape_mrkdwn(str(exc))}\n{rid}"
    if isinstance(exc, StoreError):
        return f"⚠️ *Store error*: {escape_mrkdwn(str(exc))}\n{rid}"
    if isinstance(exc, DaimonError):
        return f"⚠️ *Error*: {escape_mrkdwn(str(exc))}\n{rid}"
    if isinstance(exc, anthropic.APIStatusError):
        return f"❌ *API Error ({exc.status_code})*: {escape_mrkdwn(exc.message)}\n{rid}"
    if isinstance(exc, anthropic.APIConnectionError):
        return (
            "\U0001f50c *Connection Error*: "
            "Could not connect to Anthropic API. Please try again.\n"
            f"{rid}"
        )
    if isinstance(exc, anthropic.APIError):
        return f"❌ *API Error*: {escape_mrkdwn(exc.message)}\n{rid}"
    if isinstance(exc, SlackApiError):
        response = cast(Any, exc.response)  # pyright: ignore[reportUnknownMemberType]  # SlackApiError.response is untyped
        code = str(response.get("error") or "unknown_error")
        return f"❌ *Slack Error*: `{escape_mrkdwn(code)}`. Please try again.\n{rid}"
    if isinstance(exc, SQLAlchemyError):
        # Never `{exc}` here: DBAPIError stringifies to the failing statement
        # plus its bound parameters, which would publish both to the channel.
        # The rid is the handle for the real detail, which stays in the logs.
        return f"❌ *Database error* ({type(exc).__name__}). Please try again.\n{rid}"
    if isinstance(exc, InvalidToken):
        # Fernet failures mean a stored bot token could not be decrypted; the
        # detail is operator-facing and belongs in the logs, not the channel.
        return f"❌ *Credential error*: the workspace token could not be read.\n{rid}"
    if isinstance(exc, ValueError):
        return f"⚠️ *Invalid input*: {escape_mrkdwn(str(exc))}\n{rid}"
    return f"❌ *Unexpected error*: {escape_mrkdwn(str(exc))}\n{rid}"


def build_error_view(exc: Exception, *, title: str, request_id: str) -> dict[str, Any]:
    """Modal that replaces a "Loading…" placeholder when the background fetch fails.

    ``title`` must match the placeholder's title so the modal does not visibly
    change identity when ``views.update`` swaps the content.
    """
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": title},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": render_error(exc, request_id=request_id)},
            }
        ],
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
                text=render_error(exc, request_id=request_id),
            )
