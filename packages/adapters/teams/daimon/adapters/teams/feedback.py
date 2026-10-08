"""Teams' 👍/👎 on answers, stored like Slack and Discord votes.

The last answer message (and a tool-only turn's ✅ Done) turns on Teams'
feedback loop in its custom mode (`card.rated`): a click arrives as a
`message/fetchTask` invoke, answered with daimon's own dialog. 👍 records the
vote and says thanks. 👎 records the vote and opens Slack's "What went wrong?"
form: optional reasons (`FEEDBACK_REASONS`) and optional text, at least one.
Its submit (`task/submit`) records the down-vote and the form on the
submitter's own row; the form carries the answer's id, never a row id, so a
forged one only points the submitter's own feedback at another message in
the same conversation, under the same access check. Answers posted before
the custom mode still open Teams' built-in form, which arrives as
`message/submitAction` with text only (`on_builtin`).

Who may vote: the people who could have asked the agent there
(`answer_access`), decided under the policy lock in the transaction that
writes, at the click and again on submit. A refused vote records nothing.
The principal lookup never creates one; the session is an attribution hint.

Hygiene contract (Slack's): the submitted text is somebody's unsolicited
criticism and belongs in the database row. It never enters a log record or
a message anyone else sees. The one other place it may go is the support
channel, and only for a tenant that turned that on
(`SupportSettings.feedback_to_support`): then the form says so before it is
sent, and each changed submission is posted there once, beside the person,
the agent and a link to the answer (`support.post_to_support_channel`, the
Ask a human path). Teams' built-in form said nothing of the kind, so its
text is never posted.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Literal, cast

import structlog
from daimon.adapters.teams.answer_access import (
    AnswerPlace,
    clicker,
    may_start_turn_at,
    refusal_text,
)
from daimon.adapters.teams.card_actions import (
    FAILED,
    Actor,
    card_actor,
    dialog,
    dialog_message,
    error_text,
    guarded,
    submitted_fields,
)
from daimon.adapters.teams.direct_chats import DirectChats
from daimon.adapters.teams.identity import DENIED
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.support import post_to_support_channel, routes_feedback
from daimon.core.message_feedback import FEEDBACK_REASONS, Vote, known_feedback_reasons
from daimon.core.stores.access_policy import (
    AccessPolicyUnreadable,
    load_access_policy,
    lock_access_policy,
)
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.message_feedback import (
    attach_feedback_details,
    attach_feedback_text,
    record_vote,
)
from daimon.core.stores.thread_sessions import get_latest_thread_session
from microsoft_teams.api import (
    MessageFetchTaskInvokeActivity,
    MessageSubmitActionInvokeActivity,
    TaskModuleResponse,
    TaskSubmitInvokeActivity,
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import (
    AdaptiveCard,
    CardElement,
    Choice,
    ChoiceSetInput,
    SubmitAction,
    SubmitData,
    TextBlock,
    TextInput,
)

log = structlog.get_logger()

FEEDBACK_DIALOG = "feedback_form"
TITLE = "What went wrong?"
REASONS_INPUT = "reasons"
TEXT_INPUT = "text"
MAX_FEEDBACK_CHARS = 4000  # Slack's feedback modal cap.
THANKS_VOTE = "Thanks — noted."
THANKS_TEXT = "Thanks — your feedback has been recorded."
NEEDS_ONE = "Pick a reason or tell us what went wrong."
SHARED_HINT = (
    "What you send here also goes to the support team, with a link to this answer "
    "(not its content)."
)


def feedback_text(raw: str) -> str:
    """The typed text of Teams' built-in form, sent as JSON `{"feedbackText": ...}`."""
    try:
        parsed: object = json.loads(raw)
    except ValueError:
        return raw.strip()[:MAX_FEEDBACK_CHARS]
    if isinstance(parsed, dict):
        text = cast(dict[str, object], parsed).get("feedbackText")
        return text.strip()[:MAX_FEEDBACK_CHARS] if isinstance(text, str) else ""
    return ""


def feedback_form(
    message_id: str,
    *,
    shared: bool,
    reasons: tuple[str, ...] = (),
    text: str = "",
    error: str | None = None,
) -> AdaptiveCard:
    """The "What went wrong?" form; `shared` says it goes to the support team too."""
    body: list[CardElement] = [error_text(error)] if error else []
    if shared:
        body.append(TextBlock(text=SHARED_HINT, is_subtle=True, size="Small", wrap=True))
    body.append(
        ChoiceSetInput(
            id=REASONS_INPUT,
            label="What was the problem?",
            is_multi_select=True,
            style="expanded",
            choices=[Choice(title=label, value=code) for code, label in FEEDBACK_REASONS.items()],
            value=",".join(reasons) or None,
        )
    )
    body.append(
        TextInput(
            id=TEXT_INPUT,
            label="Tell us more",
            is_multiline=True,
            max_length=MAX_FEEDBACK_CHARS,
            value=text or None,
        )
    )
    send = SubmitAction(title="Send", data=SubmitData(FEEDBACK_DIALOG, {"message": message_id}))
    return AdaptiveCard(body=body, actions=[send], fallback_text=TITLE)


@dataclass(frozen=True)
class FormDetails:
    """One submitted form. `text` is held in memory only: it must never be logged."""

    message_id: str
    text: str
    reasons: tuple[str, ...]


def form_details(data: object) -> FormDetails:
    """The submitted form; a multi-select answers with its codes comma-joined.

    The form is client-built, so codes outside the vocabulary are dropped.
    """
    fields = submitted_fields(data)
    picked = fields.get(REASONS_INPUT)
    codes = [code.strip() for code in picked.split(",")] if isinstance(picked, str) else []
    return FormDetails(
        message_id=str(fields.get("message") or ""),
        text=str(fields.get(TEXT_INPUT) or "").strip()[:MAX_FEEDBACK_CHARS],
        reasons=tuple(known_feedback_reasons(codes)),
    )


_Outcome = Literal["recorded", "refused", "unreadable"]


@dataclass(frozen=True)
class _Recorded:
    """What `_record` decided, and what a routed form's post needs to say.

    `details_changed` is False when the form matches what the row already
    held, so an identical form sent again is not posted twice.
    """

    outcome: _Outcome
    row_id: uuid.UUID | None = None
    ma_agent_id: str | None = None
    ma_session_id: str | None = None
    details_changed: bool = False


def feedback_post(
    *, user_id: str, user_name: str | None, link: str, form: FormDetails, recorded: _Recorded
) -> str:
    """The support channel's message for one routed form: who, where, the agent, the
    reasons and the text, as Ask a human spells them. Never the answer itself."""
    lines = [
        f"**\N{THUMBS DOWN SIGN} Feedback** from {user_name or 'Someone'} (Teams user {user_id})",
        link,
    ]
    if recorded.ma_agent_id or recorded.ma_session_id:
        lines.append(
            f"Agent `{recorded.ma_agent_id or 'unknown'}`, session "
            f"`{recorded.ma_session_id or 'unknown'}`"
        )
    labels = [FEEDBACK_REASONS[code] for code in form.reasons]
    lines.append(f"**Reasons:** {', '.join(labels) if labels else 'none picked'}")
    post = "\n".join(lines)
    return f"{post}\n\n{form.text}" if form.text else post


class TeamsFeedback:
    """The feedback loop's invokes: the click, the form's submit, the built-in form."""

    def __init__(self, runtime: TeamsRuntime, direct: DirectChats | None) -> None:
        self._runtime = runtime
        self._direct = direct

    async def on_fetch(
        self, ctx: ActivityContext[MessageFetchTaskInvokeActivity]
    ) -> TaskModuleResponse:
        return await guarded(
            self._fetch(ctx.activity), dialog_message(FAILED), "teams.feedback.failed"
        )

    async def on_submit(self, ctx: ActivityContext[TaskSubmitInvokeActivity]) -> TaskModuleResponse:
        return await guarded(
            self._submit(ctx.activity), dialog_message(FAILED), "teams.feedback.failed"
        )

    async def on_builtin(self, ctx: ActivityContext[MessageSubmitActionInvokeActivity]) -> None:
        """An answer posted with Teams' built-in form: the vote and any text."""
        await guarded(self._builtin(ctx.activity), None, "teams.feedback.failed")

    def _shared(self, tenant_id: uuid.UUID) -> bool:
        return routes_feedback(self._runtime.settings, tenant_id)

    async def _fetch(self, activity: MessageFetchTaskInvokeActivity) -> TaskModuleResponse:
        """Record the click's vote; 👍 is thanked, 👎 gets the form."""
        actor = await card_actor(self._runtime, activity)
        message_id = activity.reply_to_id
        if actor is None or not message_id:
            return dialog_message(DENIED)
        vote: Vote = "up" if activity.value.data.action_value.reaction == "like" else "down"
        recorded = await self._record(actor, AnswerPlace.of(activity, message_id), vote)
        if recorded.outcome != "recorded":
            return dialog_message(refusal_text(recorded.outcome))
        if vote == "up":
            return dialog_message(THANKS_VOTE)
        return dialog(TITLE, feedback_form(message_id, shared=self._shared(actor.tenant_id)))

    async def _submit(self, activity: TaskSubmitInvokeActivity) -> TaskModuleResponse:
        """Submitting the form is a down-vote in its own right: upserted with the form."""
        actor = await card_actor(self._runtime, activity)
        form = form_details(activity.value.data)
        if actor is None or not form.message_id:
            return dialog_message(DENIED)
        shared = self._shared(actor.tenant_id)
        if not form.text and not form.reasons:
            return dialog(TITLE, feedback_form(form.message_id, shared=shared, error=NEEDS_ONE))
        place = AnswerPlace.of(activity, form.message_id)
        recorded = await self._record(
            actor, place, "down", details=(form.text or None, form.reasons)
        )
        if recorded.outcome != "recorded" or recorded.row_id is None:
            return dialog_message(refusal_text(recorded.outcome))
        log.info(
            "feedback.submission_recorded",
            feedback_id=str(recorded.row_id),
            reasons=list(form.reasons),
        )
        if shared and recorded.details_changed:
            teams = self._runtime.settings.teams
            post = feedback_post(
                user_id=actor.user_id,
                user_name=activity.from_.name,
                link=place.link(teams.tenant_id) if teams is not None else place.message_id,
                form=form,
                recorded=recorded,
            )
            # Best effort: the form is already recorded, whatever the post does.
            delivered = await post_to_support_channel(self._runtime, self._direct, post)
            log.info(
                "feedback.routed_to_support", feedback_id=str(recorded.row_id), delivered=delivered
            )
        return dialog_message(THANKS_TEXT)

    async def _builtin(self, activity: MessageSubmitActionInvokeActivity) -> None:
        actor = await card_actor(self._runtime, activity)
        message_id = activity.reply_to_id
        if actor is None or not message_id:
            log.info("teams.feedback.dropped")
            return
        vote: Vote = "up" if activity.value.action_value.reaction == "like" else "down"
        text = feedback_text(activity.value.action_value.feedback)
        await self._record(actor, AnswerPlace.of(activity, message_id), vote, text=text)

    async def _record(
        self,
        actor: Actor,
        place: AnswerPlace,
        vote: Vote,
        *,
        details: tuple[str | None, tuple[str, ...]] | None = None,
        text: str = "",
    ) -> _Recorded:
        """Decide access, then upsert the vote and the form's `details` (text, reasons)
        or the built-in form's `text`, in one transaction."""
        caller = await clicker(self._runtime, actor)
        tenant_id = actor.tenant_id
        async with self._runtime.sessionmaker.begin() as session:
            # Decided under the tenant policy lock in the transaction that writes,
            # so a protection or allowlist edit committed first refuses.
            await lock_access_policy(session, tenant_id=tenant_id)
            try:
                policy = await load_access_policy(session, tenant_id=tenant_id)
            except AccessPolicyUnreadable:
                log.info("feedback.refused", tenant_id=str(tenant_id), decision="unreadable")
                return _Recorded("unreadable")
            if not await may_start_turn_at(session, policy, actor, caller, place):
                log.info("feedback.refused", tenant_id=str(tenant_id), decision="refused")
                return _Recorded("refused")
            thread = await get_latest_thread_session(
                session, tenant_id=tenant_id, platform="teams", thread_id=place.conversation_id
            )
            principal = await find_platform_principal(
                session, tenant_id=tenant_id, platform="teams", external_id=actor.user_id
            )
            result = await record_vote(
                session,
                tenant_id=tenant_id,
                platform="teams",
                message_id=place.message_id,
                channel_id=place.channel_id,
                platform_user_id=actor.user_id,
                account_id=None if principal is None else principal.account_id,
                ma_session_id=None if thread is None else thread.ma_session_id,
                vote=vote,
            )
            row, changed = result.row, False
            if details is not None:
                changed = (row.feedback_text, tuple(row.feedback_reasons or ())) != details
                await attach_feedback_details(
                    session,
                    feedback_id=row.id,
                    platform_user_id=actor.user_id,
                    feedback_text=details[0],
                    feedback_reasons=details[1],
                )
            elif text:
                await attach_feedback_text(
                    session, feedback_id=row.id, platform_user_id=actor.user_id, feedback_text=text
                )
        log.info(
            "feedback.vote_recorded",
            message_id=place.message_id,
            vote=vote,
            is_new_vote=result.previous_vote != vote,
        )
        return _Recorded(
            "recorded",
            row_id=row.id,
            ma_agent_id=None if thread is None else thread.ma_agent_id,
            ma_session_id=None if thread is None else thread.ma_session_id,
            details_changed=changed,
        )
