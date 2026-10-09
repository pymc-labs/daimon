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
somebody's unsolicited criticism and belongs in the database row. It never
enters a log record, an action_id or private_metadata. Log lines carry the
feedback row id and the reason codes only. The one other place it may go is
the support channel, and only for a tenant that turned that on
(`SupportSettings.feedback_to_support`): then the form says so before it is
sent, and each submission is posted there once, beside the person, the agent
and a link to the answer (`post_to_support_channel`, the Ask a human path).
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from typing import Any, Final, Literal, cast

import structlog
from daimon.adapters.slack.click_replies import (
    notice_modal,
    open_modal,
    post_ephemeral,
    update_modal,
)
from daimon.adapters.slack.gating import is_external_interactive
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.modal_limits import MAX_PLAIN_TEXT_INPUT_CHARS
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.place_access import may_start_turn_at, resolve_clicker
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.slack.support_escalation import post_to_support_channel
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.message_feedback import FEEDBACK_REASONS, Vote, known_feedback_reasons
from daimon.core.permissions import readers_limited_at
from daimon.core.stores.access_policy import (
    AccessPolicyUnreadable,
    load_access_policy,
    lock_access_policy,
)
from daimon.core.stores.message_feedback import (
    attach_feedback_details,
    record_vote,
)
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import get_latest_thread_session
from slack_sdk.web.async_client import AsyncWebClient

__all__ = [
    "FEEDBACK_DETAILS_ACTION_ID",
    "FEEDBACK_TEXT_CALLBACK_ID",
    "FEEDBACK_VOTE_DOWN",
    "FEEDBACK_VOTE_UP",
    "FeedbackTextDecision",
    "build_feedback_actions_block",
    "build_feedback_modal",
    "render_feedback_post",
    "evaluate_feedback_text_submission",
    "handle_feedback_details_click",
    "handle_feedback_vote",
    "run_feedback_text_submission",
    "vote_for_action_id",
]

log = structlog.get_logger()

_THANKS_VOTE: Final = "Thanks."
_THANKS_TEXT: Final = "Thanks for the feedback."
_NO_LONGER_AVAILABLE: Final = "This request has expired."
_NOT_ALLOWED: Final = "You can't leave feedback on this answer."
_POLICY_UNREADABLE: Final = (
    "This workspace's access policy could not be read, so your feedback wasn't recorded. "
    "Ask an admin to check it."
)
_FORM_DID_NOT_OPEN: Final = "That didn't work. Try again."
_FORM_EXPIRED: Final = "This request has expired."
_TELL_US_PROMPT: Final = "What went wrong with this answer?"
_SHARED_HINT: Final = (
    "What you send here also goes to the support team, with a link to this answer "
    "(not its content)."
)

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


def build_feedback_modal(
    *, channel_id: str, message_ts: str, thread_ts: str, shared: bool = False
) -> dict[str, Any]:
    """The "What went wrong?" form: optional reasons, optional text, one required.

    ``private_metadata`` carries the answer's place only, never identity: the
    submit takes the person from the verified payload, so a forged blob can
    only point the submitter's own feedback at another answer, under the same
    access check. ``shared`` adds the line saying the form goes to the support
    team too, for a tenant that routes it there.
    """
    place = _AnswerPlace(channel_id=channel_id, message_ts=message_ts, thread_ts=thread_ts)
    notice: list[dict[str, Any]] = (
        [{"type": "context", "elements": [{"type": "mrkdwn", "text": _SHARED_HINT}]}]
        if shared
        else []
    )
    return {
        "type": "modal",
        "callback_id": FEEDBACK_TEXT_CALLBACK_ID,
        "private_metadata": place.metadata(),
        "title": {"type": "plain_text", "text": "What went wrong?"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            *notice,
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
                    "max_length": MAX_PLAIN_TEXT_INPUT_CHARS,
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
                    "text": {"type": "plain_text", "text": "Give feedback"},
                    "value": place.metadata(),
                }
            ],
        },
    ]


def _support_channel(runtime: SlackRuntime, *, team_id: str) -> str | None:
    """The channel this workspace's submitted forms also go to, or None (the default).

    Anything but a configured string channel and a literal True reads as off,
    so a half-built settings object fails closed, as `slack_support_enabled` does.
    """
    support = runtime.settings.support
    channel = cast(object, support.slack_escalation_channel_id)
    if not isinstance(channel, str) or not channel:
        return None
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    return channel if cast(object, support.routes_feedback(tenant_id)) is True else None


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
    user_name: str = ""


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
    user: dict[str, Any] = payload.get("user") or {}
    decision = FeedbackTextDecision(
        proceed=False,
        response_payload=None,
        text="",
        reasons=(),
        channel_id=str(meta.get("channel_id") or ""),
        message_ts=message_ts,
        thread_ts=str(meta.get("thread_ts") or "") or message_ts,
        feedback_id=str(meta.get("feedback_id") or ""),
        user_name=str(user.get("username") or user.get("name") or ""),
    )
    if not raw_text.strip() and not reasons:
        return dataclasses.replace(
            decision,
            response_payload={
                "response_action": "errors",
                "errors": {_TEXT_BLOCK_ID: "Write a few words first."},
            },
        )
    if is_external_interactive(payload):
        log.info("feedback.external_submission_rejected")
        return decision
    if not (decision.channel_id and (decision.message_ts or decision.feedback_id)):
        return decision
    return dataclasses.replace(decision, proceed=True, text=raw_text, reasons=reasons)


_RecordOutcome = Literal["recorded", "missing", "refused", "unreadable"]


@dataclasses.dataclass(frozen=True)
class _Recorded:
    """What `_record` decided, and what a routed form's post needs to say.

    ``details_changed`` is False when the form matches what the row already
    held, so a resubmitted identical form is not posted twice.
    """

    outcome: _RecordOutcome
    row_id: uuid.UUID | None = None
    sealed: bool = False
    ma_agent_id: str | None = None
    ma_session_id: str | None = None
    details_changed: bool = False


async def _record(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    place: _AnswerPlace,
    vote: Vote,
    details: tuple[str | None, tuple[str, ...]] | None = None,
) -> _Recorded:
    """Decide access, then upsert the vote, and the form's ``details`` (text,
    reasons) when given, all in one transaction.

    The thread session is a best-effort attribution hint only.
    """
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    async with runtime.sessionmaker() as session:
        tenant = await get_tenant(session, tenant_id)
    if tenant is None or tenant.archived_at is not None:
        log.info("feedback.tenant_missing", tenant_id=str(tenant_id))
        return _Recorded("missing")
    subject, account_id = await resolve_clicker(
        runtime, client, tenant_id=tenant_id, user_id=user_id
    )
    async with runtime.sessionmaker() as session, session.begin():
        # Decided under the tenant policy lock in the transaction that writes,
        # so a protection or allowlist edit committed first refuses, and one
        # arriving later waits for this vote.
        await lock_access_policy(session, tenant_id=tenant_id)
        try:
            policy = await load_access_policy(session, tenant_id=tenant_id)
        except AccessPolicyUnreadable:
            log.info("feedback.refused", tenant_id=str(tenant_id), decision="unreadable")
            return _Recorded("unreadable")
        if not may_start_turn_at(
            policy, subject, channel_id=place.channel_id, thread_ts=place.thread_ts
        ):
            log.info("feedback.refused", tenant_id=str(tenant_id), decision="refused")
            return _Recorded("refused")
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
            account_id=account_id,
            ma_session_id=thread_row.ma_session_id if thread_row is not None else None,
            vote=vote,
        )
        details_changed = False
        if details is not None:
            details_changed = (
                result.row.feedback_text,
                tuple(result.row.feedback_reasons or ()),
            ) != (
                details[0],
                details[1],
            )
            await attach_feedback_details(
                session,
                feedback_id=result.row.id,
                platform_user_id=user_id,
                feedback_text=details[0],
                feedback_reasons=details[1],
            )
    log.info(
        "feedback.vote_recorded",
        message_id=place.message_ts,
        vote=vote,
        is_new_vote=result.previous_vote != vote,
    )
    return _Recorded(
        "recorded",
        row_id=result.row.id,
        sealed=readers_limited_at(policy, channel_id=place.channel_id, thread_id=place.thread_ts),
        ma_agent_id=thread_row.ma_agent_id if thread_row is not None else None,
        ma_session_id=thread_row.ma_session_id if thread_row is not None else None,
        details_changed=details_changed,
    )


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
                channel_id=channel_id,
                message_ts=message_ts,
                thread_ts=thread_ts,
                shared=_support_channel(runtime, team_id=team_id) is not None,
            ),
        )

    outcome = (
        await _record(runtime, client, team_id=team_id, user_id=user_id, place=place, vote=vote)
    ).outcome
    if outcome != "recorded":
        if outcome == "missing" and view_id is None:
            return
        text = _refusal_text(outcome)
        if view_id is not None and await update_modal(
            client, view_id=view_id, view=notice_modal(title="Feedback", text=text)
        ):
            return
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            text=text,
        )
        return

    if vote == "up":
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            text=_THANKS_VOTE,
        )
    elif view_id is None:
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
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
            channel_id=channel_id,
            message_ts=message_ts,
            thread_ts=thread_ts,
            shared=_support_channel(runtime, team_id=team_id) is not None,
        ),
    )
    if view_id is None:
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
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
        await post_ephemeral(
            client,
            channel_id=d.channel_id,
            user_id=user_id,
            thread_ts=d.thread_ts,
            message_ts=d.message_ts,
            text=text,
        )

    if not d.message_ts:
        # A form opened before forms carried the answer's place holds only a
        # row id, which says nothing about where the answer is, so access
        # cannot be decided again. Its vote was recorded at the click.
        log.info("feedback.submission_legacy_form")
        await reply(_FORM_EXPIRED)
        return

    place = _AnswerPlace(channel_id=d.channel_id, message_ts=d.message_ts, thread_ts=d.thread_ts)
    recorded = await _record(
        runtime,
        client,
        team_id=team_id,
        user_id=user_id,
        place=place,
        vote="down",
        details=(d.text if d.text.strip() else None, d.reasons),
    )
    if recorded.outcome != "recorded" or recorded.row_id is None:
        await reply(_refusal_text(recorded.outcome))
        return
    log.info(
        "feedback.submission_recorded",
        feedback_id=str(recorded.row_id),
        reasons=list(d.reasons),
    )
    dest_channel = _support_channel(runtime, team_id=team_id)
    if dest_channel is not None and recorded.details_changed:
        # Best-effort: the form is already recorded, whatever the post does.
        delivered = await post_to_support_channel(
            runtime,
            source_client=client,
            source_team_id=team_id,
            channel_id=d.channel_id,
            message_ts=d.message_ts,
            dest_channel=dest_channel,
            render=lambda link: render_feedback_post(
                decision=d, team_id=team_id, user_id=user_id, recorded=recorded, link=link
            ),
        )
        log.info(
            "feedback.routed_to_support", feedback_id=str(recorded.row_id), delivered=delivered
        )
    await reply(_THANKS_TEXT)


def render_feedback_post(
    *,
    decision: FeedbackTextDecision,
    team_id: str,
    user_id: str,
    recorded: _Recorded,
    link: str | None,
) -> str:
    """The support channel's message for one routed form.

    Who and where are spelled out as Ask a human spells them
    (`render_escalation_post`): the channel may sit in another workspace.
    The answer's content is not included, only the link to it.
    """
    d = decision
    who = f"<@{user_id}>"
    if d.user_name:
        who += f" ({escape_mrkdwn(d.user_name)}, {user_id} in {team_id})"
    else:
        who += f" ({user_id} in {team_id})"
    lines = [
        f"*\N{THUMBS DOWN SIGN} Feedback* from {who}",
        link if link is not None else f"message {d.message_ts} in channel {d.channel_id}",
    ]
    if recorded.ma_agent_id or recorded.ma_session_id:
        lines.append(
            f"Agent `{recorded.ma_agent_id or 'unknown'}`, session "
            f"`{recorded.ma_session_id or 'unknown'}`"
        )
    labels = [FEEDBACK_REASONS[code] for code in d.reasons if code in FEEDBACK_REASONS]
    lines.append(f"*Reasons:* {', '.join(labels) if labels else 'none picked'}")
    if recorded.sealed:
        lines.append(
            "_From a channel read only from inside: answer there, the conversation stays in it._"
        )
    text = "\n".join(lines)
    if d.text.strip():
        text += "\n\n" + escape_mrkdwn(d.text)
    return text
