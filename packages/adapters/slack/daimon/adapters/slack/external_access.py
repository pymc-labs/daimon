"""What a member of another workspace (Slack Connect) may click or submit.

An external member is a plain, held user: they mention the bot, stop their own
turn, answer the approval card of their own turn and rate an answer to their own
turn. Every other control is for the workspace that added Daimon, so a click or a
form submission outside this allowlist is refused with one line saying so. The
allowlist names action ids and callback ids; a new control is closed to external
members until it is added here. Requester binding (the click is on their own
turn) is checked by each allowed handler, not here.
"""

from __future__ import annotations

import contextlib
from typing import Any, Final

import structlog
from daimon.adapters.slack.click_replies import CLICK_REPLY_ERRORS, notice_modal
from daimon.adapters.slack.feedback import (
    FEEDBACK_DETAILS_ACTION_ID,
    FEEDBACK_TEXT_CALLBACK_ID,
)
from daimon.core.posted_controls.confirmation import CONFIRMATION_CUSTOM_ID_PREFIX
from slack_sdk.web.async_client import AsyncWebClient

__all__ = [
    "external_action_allowed",
    "external_submission_allowed",
    "host_only_notice",
    "refuse_external_click",
    "refusal_view",
    "TRY_AGAIN_NOTICE",
]

log = structlog.get_logger()

#: Used when the host workspace's name is not known yet.
_FALLBACK_HOST: Final = "the workspace that added Daimon"


def host_only_notice(workspace_name: str | None) -> str:
    """The one line an external member gets for a control that is not theirs."""
    return f"That's only for people at {workspace_name or _FALLBACK_HOST}."


def external_action_allowed(action_id: str) -> bool:
    """Whether an external clicker may reach this control's handler at all."""
    return (
        action_id == "cancel_turn"
        or action_id.startswith(CONFIRMATION_CUSTOM_ID_PREFIX)
        or action_id.startswith("feedback_vote:")
        or action_id == FEEDBACK_DETAILS_ACTION_ID
    )


def external_submission_allowed(callback_id: str) -> bool:
    """Whether an external submitter may reach this modal's handler at all."""
    return callback_id == FEEDBACK_TEXT_CALLBACK_ID


#: Said when the membership check could not finish in time; nothing ran, trying again works.
TRY_AGAIN_NOTICE: Final = "I couldn't verify you just now. Try again."


def refusal_view(workspace_name: str | None, *, text: str | None = None) -> dict[str, Any]:
    """A `view_submission` ack payload that replaces the form with the refusal."""
    return {
        "response_action": "update",
        "view": notice_modal(title="Daimon", text=text or host_only_notice(workspace_name)),
    }


async def refuse_external_click(
    client: AsyncWebClient,
    payload: dict[str, Any],
    *,
    workspace_name: str | None,
    text: str | None = None,
) -> None:
    """Tell an external clicker the control is not theirs: an ephemeral, else a thread reply.

    The thread reply names the person, since it is visible to the channel; it
    is the fallback for a Slack that refuses the ephemeral to an external member.
    """
    container: dict[str, Any] = payload.get("container") or {}
    channel_info: dict[str, Any] = payload.get("channel") or {}
    message: dict[str, Any] = payload.get("message") or {}
    user: dict[str, Any] = payload.get("user") or {}
    channel = str(channel_info.get("id") or container.get("channel_id") or "")
    user_id = str(user.get("id") or "")
    thread_ts = str(message.get("thread_ts") or container.get("message_ts") or "") or None
    if not (channel and user_id):
        return
    text = text or host_only_notice(workspace_name)
    try:
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel, user=user_id, thread_ts=message.get("thread_ts"), text=text
        )
        return
    except CLICK_REPLY_ERRORS as err:
        log.info("slack.external_refusal.ephemeral_failed", error=type(err).__name__)
    with contextlib.suppress(*CLICK_REPLY_ERRORS):
        await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel, thread_ts=thread_ts, text=f"<@{user_id}> {text}"
        )
