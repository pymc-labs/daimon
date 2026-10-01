"""Slack message-feedback surface — vote buttons, modal, and handlers.

Discord captures feedback through seeded reactions; Slack deliberately does
not (see the scope discussion in ``daimon.core.message_feedback``), so this
module uses buttons on the final answer message instead. A button click is a
``block_actions`` payload — it already carries team, channel, message ``ts``
and the clicking user, and its ``trigger_id`` can open the "What went wrong?"
modal directly, so no new OAuth scope and no DM bridge is needed.

The answer message is shared across every viewer, which is why the two vote
buttons never change appearance after a click: a selected state on a shared
surface would broadcast one person's vote to the channel. Per-user
acknowledgement happens through an ephemeral instead.

Hygiene contract (mirrors the Discord feedback modal): the submitted text is
somebody's unsolicited criticism and belongs in exactly one place — the
database row. It never enters a log record, an action_id, private_metadata,
or any non-ephemeral message. Log lines carry the feedback row id only.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from typing import Any, cast

import structlog
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.message_feedback import Vote, should_trigger_feedback_dm
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.message_feedback import attach_feedback_text, record_vote
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import get_latest_thread_session

__all__ = [
    "FEEDBACK_TEXT_CALLBACK_ID",
    "FEEDBACK_VOTE_DOWN",
    "FEEDBACK_VOTE_UP",
    "FeedbackTextDecision",
    "build_feedback_actions_block",
    "build_feedback_modal",
    "evaluate_feedback_text_submission",
    "handle_feedback_vote",
    "run_feedback_text_submission",
    "vote_for_action_id",
]

log = structlog.get_logger()

_THANKS_VOTE = "Thanks — noted."
_THANKS_TEXT = "Thanks — your feedback has been recorded."
_NO_LONGER_AVAILABLE = "This feedback request is no longer available."

FEEDBACK_VOTE_UP = "feedback_vote:up"
FEEDBACK_VOTE_DOWN = "feedback_vote:down"
FEEDBACK_TEXT_CALLBACK_ID = "feedback_text"

_TEXT_BLOCK_ID = "feedback_text_block"
_TEXT_INPUT_ID = "feedback_text_input"


def build_feedback_actions_block() -> dict[str, Any]:
    """The 👍/👎 actions block appended to the last chunk of a final answer.

    Both buttons are deliberately unstyled — the message is shared, so any
    per-click visual state would leak one viewer's vote to everyone else.
    """
    return {
        "type": "actions",
        "block_id": "feedback_vote",
        "elements": [
            {
                "type": "button",
                "action_id": FEEDBACK_VOTE_UP,
                "text": {"type": "plain_text", "text": "\N{THUMBS UP SIGN}"},
            },
            {
                "type": "button",
                "action_id": FEEDBACK_VOTE_DOWN,
                "text": {"type": "plain_text", "text": "\N{THUMBS DOWN SIGN}"},
            },
        ],
    }


def vote_for_action_id(action_id: str) -> Vote | None:
    """Classify a block_actions action_id as a vote, or None for anything else."""
    if action_id == FEEDBACK_VOTE_UP:
        return "up"
    if action_id == FEEDBACK_VOTE_DOWN:
        return "down"
    return None


def build_feedback_modal(*, feedback_id: str, channel_id: str) -> dict[str, Any]:
    """The "What went wrong?" modal opened on a genuinely new down-vote.

    ``private_metadata`` carries the feedback row id (the write handle for
    ``attach_feedback_text``) and the channel id (a view_submission payload
    has no channel of its own, and the post-submit ephemeral needs one).
    """
    return {
        "type": "modal",
        "callback_id": FEEDBACK_TEXT_CALLBACK_ID,
        "private_metadata": json.dumps(
            {"feedback_id": feedback_id, "channel_id": channel_id},
            separators=(",", ":"),
        ),
        "title": {"type": "plain_text", "text": "What went wrong?"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": _TEXT_BLOCK_ID,
                "label": {"type": "plain_text", "text": "What went wrong?"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": _TEXT_INPUT_ID,
                    "multiline": True,
                    "max_length": 4000,
                },
            }
        ],
    }


@dataclasses.dataclass(frozen=True)
class FeedbackTextDecision:
    """Outcome of the pure pre-ack evaluation of a feedback_text submission.

    ``response_payload`` is the ack body (``response_action: errors``) when
    the submission is rejected, or None for an empty ack that closes the
    modal. ``text`` is carried in memory only — it must never be logged.
    """

    proceed: bool
    response_payload: dict[str, Any] | None
    text: str
    feedback_id: str
    channel_id: str


def evaluate_feedback_text_submission(payload: dict[str, Any]) -> FeedbackTextDecision:
    """Pure (no I/O) evaluation of the feedback_text view_submission.

    The client already enforces the input as required, but that is not
    trusted alone: whitespace-only text is rejected server-side with a field
    error so the person can retype rather than lose the modal.
    """
    view: dict[str, Any] = payload.get("view") or {}
    meta: dict[str, Any]
    try:
        meta = json.loads(str(view.get("private_metadata") or "") or "{}")
    except json.JSONDecodeError:
        meta = {}
    state: dict[str, Any] = view.get("state") or {}
    values: dict[str, Any] = state.get("values") or {}
    block: dict[str, Any] = values.get(_TEXT_BLOCK_ID) or {}
    element: dict[str, Any] = block.get(_TEXT_INPUT_ID) or {}
    raw_text = str(element.get("value") or "")
    feedback_id = str(meta.get("feedback_id") or "")
    channel_id = str(meta.get("channel_id") or "")

    if not raw_text.strip():
        return FeedbackTextDecision(
            proceed=False,
            response_payload={
                "response_action": "errors",
                "errors": {_TEXT_BLOCK_ID: "Feedback cannot be empty — try again."},
            },
            text="",
            feedback_id=feedback_id,
            channel_id=channel_id,
        )
    return FeedbackTextDecision(
        proceed=True,
        response_payload=None,
        text=raw_text,
        feedback_id=feedback_id,
        channel_id=channel_id,
    )


async def handle_feedback_vote(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Record a 👍/👎 button click as a vote; open the modal on a new down-vote.

    The button lives only on bot-authored answer messages, so the
    bot-authorship question the Discord reaction listener has to settle does
    not arise here — the action_id itself is the classification.

    Identity handling mirrors the Discord listener: the principal lookup is
    read-only (a vote must not mint an identity record), and the thread
    session is a best-effort attribution hint only.
    """
    team_info: dict[str, Any] = payload.get("team") or {}
    user_info: dict[str, Any] = payload.get("user") or {}
    channel_info: dict[str, Any] = payload.get("channel") or {}
    container: dict[str, Any] = payload.get("container") or {}
    message: dict[str, Any] = payload.get("message") or {}
    team_id = str(team_info.get("id") or "")
    user_id = str(user_info.get("id") or "")
    channel_id = str(channel_info.get("id") or "")
    message_ts = str(container.get("message_ts") or "")
    thread_ts = str(message.get("thread_ts") or "") or message_ts
    trigger_id = str(payload.get("trigger_id") or "")
    actions: list[dict[str, Any]] = payload.get("actions") or []
    action_id = str(actions[0].get("action_id") or "") if actions else ""

    vote = vote_for_action_id(action_id)
    if vote is None or not (team_id and user_id and channel_id and message_ts):
        return

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    async with runtime.sessionmaker() as session, session.begin():
        tenant = await get_tenant(session, tenant_id)
        if tenant is None:
            log.info("feedback.tenant_missing", tenant_id=str(tenant_id))
            return

        thread_row = await get_latest_thread_session(
            session, tenant_id=tenant_id, platform="slack", thread_id=thread_ts
        )
        principal = await find_platform_principal(
            session, tenant_id=tenant_id, platform="slack", external_id=user_id
        )
        result = await record_vote(
            session,
            tenant_id=tenant_id,
            platform="slack",
            message_id=message_ts,
            channel_id=channel_id,
            platform_user_id=user_id,
            account_id=principal.account_id if principal is not None else None,
            ma_session_id=thread_row.ma_session_id if thread_row is not None else None,
            vote=vote,
        )

    previous_vote = cast("Vote | None", result.previous_vote)
    log.info(
        "feedback.vote_recorded",
        message_id=message_ts,
        vote=vote,
        is_new_vote=previous_vote != vote,
    )

    if should_trigger_feedback_dm(previous_vote=previous_vote, new_vote=vote) and trigger_id:
        await client.views_open(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=trigger_id,
            view=build_feedback_modal(feedback_id=str(result.row.id), channel_id=channel_id),
        )
        return

    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
        channel=channel_id, user=user_id, text=_THANKS_VOTE
    )


async def run_feedback_text_submission(
    runtime: SlackRuntime,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    feedback_id: str,
    text: str,
) -> None:
    """Write the modal's free text through the ownership-predicated store call.

    A ``None`` from ``attach_feedback_text`` covers both "the row was purged"
    and "this row belongs to someone else" — the ephemeral reply deliberately
    does not distinguish the two, so a submission carrying someone else's row
    id learns nothing about whether that row exists.
    """
    try:
        row_id = uuid.UUID(feedback_id)
    except ValueError:
        log.info("feedback.submission_bad_row_id")
        return

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return

    async with runtime.sessionmaker() as session, session.begin():
        updated = await attach_feedback_text(
            session, feedback_id=row_id, platform_user_id=user_id, feedback_text=text
        )

    if updated is None:
        log.info("feedback.submission_no_longer_available", feedback_id=feedback_id)
        reply = _NO_LONGER_AVAILABLE
    else:
        log.info("feedback.submission_recorded", feedback_id=feedback_id)
        reply = _THANKS_TEXT

    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
        channel=channel_id, user=user_id, text=reply
    )
