"""Slack message-feedback surface — vote buttons, the follow-up form, handlers.

Discord captures feedback through seeded reactions; Slack deliberately does
not (see the scope discussion in ``daimon.core.message_feedback``), so this
module uses buttons on the final answer message instead. A button click is a
``block_actions`` payload — it already carries team, channel, message ``ts``
and the clicking user, and its ``trigger_id`` can open the "What went wrong?"
form directly, so no new OAuth scope and no DM bridge is needed.

The answer message is shared across every viewer, which is why the two vote
buttons never change appearance after a click: a selected state on a shared
surface would broadcast one person's vote to the channel. Per-user
acknowledgement happens through an ephemeral, or the form itself.

Every 👎 click opens the form, and opens it before anything else: a
``trigger_id`` lives 3 seconds, and the vote's own reads and write are not
allowed to spend them. When Slack refuses the form anyway, the person gets an
ephemeral with a button that opens it from a fresh ``trigger_id``. The form
carries the answer's place, not a row id: its submit records the down-vote
and the details on the submitter's own row, so it can never reach anyone
else's.

Who may vote: the people who could have asked the agent there
(`daimon.adapters.slack.place_access`), decided when the vote is recorded and
again on submit. A refused vote records nothing.

Hygiene contract (mirrors the Discord feedback modal): the submitted text is
somebody's unsolicited criticism and belongs in exactly one place — the
database row. It never enters a log record, an action_id, private_metadata,
or any non-ephemeral message. Log lines carry the feedback row id and the
reason codes only.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from typing import Any, Final, Literal, cast

import structlog
from daimon.adapters.slack.gating import is_external_interactive
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.modals import notice_modal, open_modal, update_modal
from daimon.adapters.slack.place_access import check_place_access
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.message_feedback import FEEDBACK_REASONS, Vote, known_feedback_reasons
from daimon.core.stores.message_feedback import (
    attach_feedback_details,
    attach_feedback_text,
    record_vote,
)
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import get_latest_thread_session
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

__all__ = [
    "FEEDBACK_DETAILS_ACTION_ID",
    "FEEDBACK_TEXT_CALLBACK_ID",
    "FEEDBACK_VOTE_DOWN",
    "FEEDBACK_VOTE_UP",
    "FeedbackTextDecision",
    "build_feedback_actions_block",
    "build_feedback_modal",
    "evaluate_feedback_text_submission",
    "handle_feedback_details_click",
    "handle_feedback_vote",
    "run_feedback_text_submission",
    "vote_for_action_id",
]

log = structlog.get_logger()

_THANKS_VOTE: Final = "Thanks — noted."
_THANKS_TEXT: Final = "Thanks — your feedback has been recorded."
_NO_LONGER_AVAILABLE: Final = "This feedback request is no longer available."
_NOT_ALLOWED: Final = "You can't leave feedback on this answer."
_POLICY_UNREADABLE: Final = (
    "This workspace's access policy could not be read, so your feedback wasn't recorded. "
    "Ask an admin to check it."
)
_FORM_DID_NOT_OPEN: Final = "Slack didn't open the form in time. Click the button again."
_TELL_US_PROMPT: Final = "Thanks — noted. Want to tell us what went wrong?"

FEEDBACK_VOTE_UP: Final = "feedback_vote:up"
FEEDBACK_VOTE_DOWN: Final = "feedback_vote:down"
# Disjoint from `feedback_vote:` so the vote router never sees it.
FEEDBACK_DETAILS_ACTION_ID: Final = "feedback_details"
FEEDBACK_TEXT_CALLBACK_ID: Final = "feedback_text"

_TEXT_BLOCK_ID: Final = "feedback_text_block"
_TEXT_INPUT_ID: Final = "feedback_text_input"
_REASONS_BLOCK_ID: Final = "feedback_reasons_block"
_REASONS_INPUT_ID: Final = "feedback_reasons_input"


@dataclasses.dataclass(frozen=True)
class _AnswerPlace:
    """The answer a click or a form is about. ``thread_ts`` falls back to the
    answer's own ``ts`` (a top-level answer, a DM), as the turn place does."""

    channel_id: str
    message_ts: str
    thread_ts: str

    def metadata(self) -> str:
        return json.dumps(
            {
                "channel_id": self.channel_id,
                "message_ts": self.message_ts,
                "thread_ts": self.thread_ts,
            },
            separators=(",", ":"),
        )


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


def build_feedback_modal(*, channel_id: str, message_ts: str, thread_ts: str) -> dict[str, Any]:
    """The "What went wrong?" form: optional reasons, optional text, one required.

    ``private_metadata`` carries the answer's place only, never identity: the
    submit takes the person from the verified payload, so a forged blob can
    only point the submitter's own feedback at another answer, under the same
    access check.
    """
    place = _AnswerPlace(channel_id=channel_id, message_ts=message_ts, thread_ts=thread_ts)
    return {
        "type": "modal",
        "callback_id": FEEDBACK_TEXT_CALLBACK_ID,
        "private_metadata": place.metadata(),
        "title": {"type": "plain_text", "text": "What went wrong?"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": _REASONS_BLOCK_ID,
                "optional": True,
                "label": {"type": "plain_text", "text": "What was the problem?"},
                "element": {
                    "type": "checkboxes",
                    "action_id": _REASONS_INPUT_ID,
                    "options": [
                        {"text": {"type": "plain_text", "text": label}, "value": code}
                        for code, label in FEEDBACK_REASONS.items()
                    ],
                },
            },
            {
                "type": "input",
                "block_id": _TEXT_BLOCK_ID,
                "optional": True,
                "label": {"type": "plain_text", "text": "Tell us more"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": _TEXT_INPUT_ID,
                    "multiline": True,
                    "max_length": 4000,
                },
            },
        ],
    }


def _details_prompt_blocks(place: _AnswerPlace) -> list[dict[str, Any]]:
    """The ephemeral offered when the form could not open off the vote click."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": _TELL_US_PROMPT}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": FEEDBACK_DETAILS_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Tell us what went wrong"},
                    "value": place.metadata(),
                }
            ],
        },
    ]


@dataclasses.dataclass(frozen=True)
class FeedbackTextDecision:
    """Outcome of the pure pre-ack evaluation of a feedback_text submission.

    ``response_payload`` is the ack body (``response_action: errors``) when
    the submission is rejected, or None for an empty ack that closes the
    modal. ``text`` is carried in memory only — it must never be logged.
    ``feedback_id`` is set only for a form opened before the form carried the
    answer's place (a deploy can land while one is open).
    """

    proceed: bool
    response_payload: dict[str, Any] | None
    text: str
    reasons: tuple[str, ...]
    channel_id: str
    message_ts: str
    thread_ts: str
    feedback_id: str


def _metadata(raw: object) -> dict[str, Any]:
    try:
        parsed: Any = json.loads(str(raw or "") or "{}")
    except json.JSONDecodeError:
        return {}
    return cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else {}


def evaluate_feedback_text_submission(payload: dict[str, Any]) -> FeedbackTextDecision:
    """Pure (no I/O) evaluation of the feedback_text view_submission.

    Both fields are optional on their own but not together: a form with no
    reason picked and whitespace-only text is refused with a field error, so
    the person can fix it rather than lose the form. Reason codes outside the
    vocabulary are dropped. An external Slack Connect member's form closes
    and goes no further, as their click would have.
    """
    view: dict[str, Any] = payload.get("view") or {}
    meta = _metadata(view.get("private_metadata"))
    state: dict[str, Any] = view.get("state") or {}
    values: dict[str, Any] = state.get("values") or {}
    text_block: dict[str, Any] = values.get(_TEXT_BLOCK_ID) or {}
    text_element: dict[str, Any] = text_block.get(_TEXT_INPUT_ID) or {}
    raw_text = str(text_element.get("value") or "")
    reasons_block: dict[str, Any] = values.get(_REASONS_BLOCK_ID) or {}
    reasons_element: dict[str, Any] = reasons_block.get(_REASONS_INPUT_ID) or {}
    selected: list[dict[str, Any]] = reasons_element.get("selected_options") or []
    reasons = tuple(known_feedback_reasons([str(o.get("value") or "") for o in selected]))
    message_ts = str(meta.get("message_ts") or "")
    decision = FeedbackTextDecision(
        proceed=False,
        response_payload=None,
        text="",
        reasons=(),
        channel_id=str(meta.get("channel_id") or ""),
        message_ts=message_ts,
        thread_ts=str(meta.get("thread_ts") or "") or message_ts,
        feedback_id=str(meta.get("feedback_id") or ""),
    )
    if not raw_text.strip() and not reasons:
        return dataclasses.replace(
            decision,
            response_payload={
                "response_action": "errors",
                "errors": {_TEXT_BLOCK_ID: "Pick a reason or tell us what went wrong."},
            },
        )
    if is_external_interactive(payload):
        log.info("feedback.external_submission_rejected")
        return decision
    if not (decision.channel_id and (decision.message_ts or decision.feedback_id)):
        return decision
    return dataclasses.replace(decision, proceed=True, text=raw_text, reasons=reasons)


async def _ephemeral(
    client: AsyncWebClient,
    *,
    channel_id: str,
    user_id: str,
    thread_ts: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
) -> None:
    try:
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel_id, user=user_id, thread_ts=thread_ts or None, text=text, blocks=blocks
        )
    except SlackApiError as err:
        log.info(
            "feedback.ephemeral_failed",
            error=str(err.response.get("error", "slack_api_error")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
        )


_RecordOutcome = Literal["recorded", "missing", "refused", "unreadable"]


async def _record(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    place: _AnswerPlace,
    vote: Vote,
) -> tuple[_RecordOutcome, uuid.UUID | None]:
    """Decide access, then upsert the vote. Returns the outcome and the row id.

    The thread session is a best-effort attribution hint only.
    """
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    async with runtime.sessionmaker() as session:
        tenant = await get_tenant(session, tenant_id)
    if tenant is None or tenant.archived_at is not None:
        log.info("feedback.tenant_missing", tenant_id=str(tenant_id))
        return "missing", None
    access = await check_place_access(
        runtime,
        client,
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=place.channel_id,
        thread_ts=place.thread_ts,
    )
    if access.decision != "allowed":
        log.info("feedback.refused", tenant_id=str(tenant_id), decision=access.decision)
        return access.decision, None
    async with runtime.sessionmaker() as session, session.begin():
        thread_row = await get_latest_thread_session(
            session, tenant_id=tenant_id, platform="slack", thread_id=place.thread_ts
        )
        result = await record_vote(
            session,
            tenant_id=tenant_id,
            platform="slack",
            message_id=place.message_ts,
            channel_id=place.channel_id,
            platform_user_id=user_id,
            account_id=access.account_id,
            ma_session_id=thread_row.ma_session_id if thread_row is not None else None,
            vote=vote,
        )
    log.info(
        "feedback.vote_recorded",
        message_id=place.message_ts,
        vote=vote,
        is_new_vote=result.previous_vote != vote,
    )
    return "recorded", result.row.id


def _refusal_text(outcome: _RecordOutcome) -> str:
    if outcome == "refused":
        return _NOT_ALLOWED
    if outcome == "unreadable":
        return _POLICY_UNREADABLE
    return _NO_LONGER_AVAILABLE


async def handle_feedback_vote(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Record a 👍/👎 click; every 👎 opens the "What went wrong?" form first.

    The button lives only on bot-authored answer messages, so the
    bot-authorship question the Discord reaction listener has to settle does
    not arise here — the action_id itself is the classification. A double
    click upserts one row (tenant, message, voter).
    """
    team_info: dict[str, Any] = payload.get("team") or {}
    user_info: dict[str, Any] = payload.get("user") or {}
    channel_info: dict[str, Any] = payload.get("channel") or {}
    container: dict[str, Any] = payload.get("container") or {}
    message: dict[str, Any] = payload.get("message") or {}
    team_id = str(team_info.get("id") or "")
    user_id = str(user_info.get("id") or "")
    channel_id = str(channel_info.get("id") or container.get("channel_id") or "")
    message_ts = str(container.get("message_ts") or "")
    thread_ts = str(message.get("thread_ts") or container.get("thread_ts") or "") or message_ts
    trigger_id = str(payload.get("trigger_id") or "")
    actions: list[dict[str, Any]] = payload.get("actions") or []
    action_id = str(actions[0].get("action_id") or "") if actions else ""

    vote = vote_for_action_id(action_id)
    if vote is None or not (team_id and user_id and channel_id and message_ts):
        return

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    place = _AnswerPlace(channel_id=channel_id, message_ts=message_ts, thread_ts=thread_ts)

    view_id: str | None = None
    if vote == "down" and trigger_id:
        view_id = await open_modal(
            client,
            trigger_id=trigger_id,
            view=build_feedback_modal(
                channel_id=channel_id, message_ts=message_ts, thread_ts=thread_ts
            ),
        )

    outcome, _row_id = await _record(
        runtime, client, team_id=team_id, user_id=user_id, place=place, vote=vote
    )
    if outcome != "recorded":
        if outcome == "missing" and view_id is None:
            return
        text = _refusal_text(outcome)
        if view_id is not None and await update_modal(
            client, view_id=view_id, view=notice_modal(title="Feedback", text=text)
        ):
            return
        await _ephemeral(
            client, channel_id=channel_id, user_id=user_id, thread_ts=thread_ts, text=text
        )
        return

    if vote == "up":
        await _ephemeral(
            client, channel_id=channel_id, user_id=user_id, thread_ts=thread_ts, text=_THANKS_VOTE
        )
    elif view_id is None:
        await _ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            thread_ts=thread_ts,
            text=_TELL_US_PROMPT,
            blocks=_details_prompt_blocks(place),
        )


async def handle_feedback_details_click(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Open the form from the fallback ephemeral's "Tell us what went wrong" button.

    The button's value is the answer's place; nothing is decided or written
    here — the submit does both, for the submitter only.
    """
    team_info: dict[str, Any] = payload.get("team") or {}
    user_info: dict[str, Any] = payload.get("user") or {}
    team_id = str(team_info.get("id") or "")
    user_id = str(user_info.get("id") or "")
    trigger_id = str(payload.get("trigger_id") or "")
    actions: list[dict[str, Any]] = payload.get("actions") or []
    meta = _metadata(actions[0].get("value") if actions else None)
    channel_id = str(meta.get("channel_id") or "")
    message_ts = str(meta.get("message_ts") or "")
    thread_ts = str(meta.get("thread_ts") or "") or message_ts
    if not (team_id and user_id and trigger_id and channel_id and message_ts):
        return
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    view_id = await open_modal(
        client,
        trigger_id=trigger_id,
        view=build_feedback_modal(
            channel_id=channel_id, message_ts=message_ts, thread_ts=thread_ts
        ),
    )
    if view_id is None:
        await _ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            thread_ts=thread_ts,
            text=_FORM_DID_NOT_OPEN,
        )


async def run_feedback_text_submission(
    runtime: SlackRuntime,
    *,
    team_id: str,
    user_id: str,
    decision: FeedbackTextDecision,
) -> None:
    """Record the down-vote and the form's details on the submitter's own row.

    Access is decided again here. Submitting the form is a down-vote in its
    own right, so the vote is upserted even when the click-time write never
    happened. A form opened before it carried the answer's place still holds
    a row id; that one attaches by id, gated on the submitter's own user id.
    The text never enters a log line; the reason codes may.
    """
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    d = decision

    async def reply(text: str) -> None:
        await _ephemeral(
            client, channel_id=d.channel_id, user_id=user_id, thread_ts=d.thread_ts, text=text
        )

    if not d.message_ts:
        try:
            legacy_id = uuid.UUID(d.feedback_id)
        except ValueError:
            log.info("feedback.submission_bad_row_id")
            return
        async with runtime.sessionmaker() as session, session.begin():
            legacy = await attach_feedback_text(
                session, feedback_id=legacy_id, platform_user_id=user_id, feedback_text=d.text
            )
        await reply(_THANKS_TEXT if legacy is not None else _NO_LONGER_AVAILABLE)
        return

    place = _AnswerPlace(channel_id=d.channel_id, message_ts=d.message_ts, thread_ts=d.thread_ts)
    outcome, row_id = await _record(
        runtime, client, team_id=team_id, user_id=user_id, place=place, vote="down"
    )
    if outcome != "recorded" or row_id is None:
        await reply(_refusal_text(outcome))
        return
    async with runtime.sessionmaker() as session, session.begin():
        updated = await attach_feedback_details(
            session,
            feedback_id=row_id,
            platform_user_id=user_id,
            feedback_text=d.text if d.text.strip() else None,
            feedback_reasons=d.reasons,
        )
    if updated is None:
        log.info("feedback.submission_no_longer_available", feedback_id=str(row_id))
        await reply(_NO_LONGER_AVAILABLE)
        return
    log.info("feedback.submission_recorded", feedback_id=str(row_id), reasons=list(d.reasons))
    await reply(_THANKS_TEXT)
