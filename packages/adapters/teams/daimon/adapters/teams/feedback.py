"""Teams' 👍/👎 on answers, stored like Slack and Discord votes.

The card below an answer (`card.controls_card`, or a tool-only turn's ✅
Done) carries emoji-only 👍/👎 buttons (`card.vote_actions`): a click opens a
dialog (`task/fetch`, `on_up`/`on_down`). Answers posted before them carry
Teams' feedback loop in its custom mode, whose click arrives as a
`message/fetchTask` (`on_fetch`) and is answered the same way. 👍 records the
vote and says thanks. 👎 records the vote and opens Slack's "What went wrong?"
form: optional reasons (`FEEDBACK_REASONS`) and optional text, at least one.
Its submit (`task/submit`) records the down-vote and the form on the
submitter's own row. The answer is the invoke's `replyToId` when Teams sends
one, else the id the form carries (never a row id), so a forged one only
points the submitter's own feedback at another message in the same
conversation, under the same access check. Teams may instead
deliver the form's Send as `message/submitAction`, its inputs JSON-encoded in
`actionValue.feedback`, as Microsoft's samples for the custom mode handle it;
`on_builtin` recognises the form there and takes the same submit path,
except that this invoke is answered with an empty body, so no thanks is
shown. Answers posted before the custom mode still open Teams' built-in form,
which arrives the same way with text only.

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
Ask a human path), marked when the answer's channel is read only from inside.
Teams' built-in form said nothing of the kind, so its text is never posted.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from typing import Literal, cast

import structlog
from daimon.adapters.teams.answer_access import (
    AnswerPlace,
    clicker,
    may_start_turn_at,
    refusal_text,
    sealed_at,
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
from daimon.adapters.teams.output_delivery import Spawn
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.support import (
    SEALED_LINE,
    post_to_support_channel,
    requester,
    routes_feedback,
    support_post,
)
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
    TaskFetchInvokeActivity,
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
THANKS_VOTE = "Thanks."
THANKS_TEXT = "Thanks for the feedback."
NEEDS_ONE = "Pick a reason or write a few words."
SHARED_HINT = "Support gets your feedback and a link to the answer, not the answer itself."


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


def sent_form(raw: str, message_id: str) -> FormDetails | None:
    """Daimon's form when Teams delivered its Send as `message/submitAction`.

    None for anything else, such as the built-in form's `{"feedbackText": ...}`.
    The answer is the invoke's own `replyToId`, never a field in the payload.
    """
    try:
        parsed: object = json.loads(raw)
    except ValueError:
        return None
    fields = submitted_fields(parsed)
    marked = fields.get("action") == FEEDBACK_DIALOG
    if not marked and REASONS_INPUT not in fields and TEXT_INPUT not in fields:
        return None
    return replace(form_details(fields), message_id=message_id)


_Outcome = Literal["recorded", "refused", "unreadable"]


@dataclass(frozen=True)
class _Recorded:
    """What `_record` decided, and what a routed form's post needs to say.

    `details_changed` is False when the form matches what the row already
    held, so an identical form sent again is not posted twice.
    """

    outcome: _Outcome
    row_id: uuid.UUID | None = None
    sealed: bool = False
    ma_agent_id: str | None = None
    ma_session_id: str | None = None
    details_changed: bool = False


def feedback_post(
    *, user_id: str, user_name: str | None, link: str, form: FormDetails, recorded: _Recorded
) -> str:
    """The support channel's message for one routed form: who, where, the agent, the
    reasons and the text, as Ask a human spells them. Never the answer itself."""
    agent = ""
    if recorded.ma_agent_id or recorded.ma_session_id:
        agent = (
            f"Agent `{recorded.ma_agent_id or 'unknown'}`, session "
            f"`{recorded.ma_session_id or 'unknown'}`"
        )
    labels = [FEEDBACK_REASONS[code] for code in form.reasons]
    return support_post(
        f"**\N{THUMBS DOWN SIGN} Feedback** from {requester(user_name, user_id)}",
        link,
        agent,
        f"**Reasons:** {', '.join(labels) if labels else 'none picked'}",
        SEALED_LINE if recorded.sealed else "",
        *form.text.splitlines(),
    )


class TeamsFeedback:
    """The votes' invokes: the click, the form's submit, the built-in form."""

    def __init__(self, runtime: TeamsRuntime, direct: DirectChats | None, *, spawn: Spawn) -> None:
        self._runtime = runtime
        self._direct = direct
        # The routed post runs after the invoke is answered, which Teams wants within seconds.
        self._spawn = spawn

    async def on_up(self, ctx: ActivityContext[TaskFetchInvokeActivity]) -> TaskModuleResponse:
        return await self._guarded_vote(ctx.activity, "up")

    async def on_down(self, ctx: ActivityContext[TaskFetchInvokeActivity]) -> TaskModuleResponse:
        return await self._guarded_vote(ctx.activity, "down")

    async def on_fetch(
        self, ctx: ActivityContext[MessageFetchTaskInvokeActivity]
    ) -> TaskModuleResponse:
        """Teams' own 👍/👎, on answers posted before the buttons."""
        reaction = ctx.activity.value.data.action_value.reaction
        return await self._guarded_vote(ctx.activity, "up" if reaction == "like" else "down")

    async def _guarded_vote(
        self, activity: TaskFetchInvokeActivity | MessageFetchTaskInvokeActivity, vote: Vote
    ) -> TaskModuleResponse:
        return await guarded(
            self._vote(activity, vote), dialog_message(FAILED), "teams.feedback.failed"
        )

    async def on_submit(self, ctx: ActivityContext[TaskSubmitInvokeActivity]) -> TaskModuleResponse:
        return await guarded(
            self._submit(ctx.activity), dialog_message(FAILED), "teams.feedback.failed"
        )

    async def on_builtin(self, ctx: ActivityContext[MessageSubmitActionInvokeActivity]) -> None:
        """Daimon's form sent this way, or an older answer's built-in form: vote and text."""
        await guarded(self._builtin(ctx.activity), None, "teams.feedback.failed")

    def _shared(self, tenant_id: uuid.UUID) -> bool:
        return routes_feedback(self._runtime.settings, tenant_id)

    async def _vote(
        self, activity: TaskFetchInvokeActivity | MessageFetchTaskInvokeActivity, vote: Vote
    ) -> TaskModuleResponse:
        """Record the click's vote; 👍 is thanked, 👎 gets the form."""
        actor = await card_actor(self._runtime, activity)
        message_id = activity.reply_to_id
        if actor is None or not message_id:
            return dialog_message(DENIED)
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
        if activity.reply_to_id:
            # The answer Teams says the dialog came from beats the client-built form's copy.
            form = replace(form, message_id=activity.reply_to_id)
        if actor is None or not form.message_id:
            return dialog_message(DENIED)
        if not form.text and not form.reasons:
            shared = self._shared(actor.tenant_id)
            return dialog(TITLE, feedback_form(form.message_id, shared=shared, error=NEEDS_ONE))
        recorded = await self._take_form(
            actor, AnswerPlace.of(activity, form.message_id), form, activity.from_.name
        )
        if recorded.outcome != "recorded":
            return dialog_message(refusal_text(recorded.outcome))
        return dialog_message(THANKS_TEXT)

    async def _builtin(self, activity: MessageSubmitActionInvokeActivity) -> None:
        actor = await card_actor(self._runtime, activity)
        message_id = activity.reply_to_id
        if actor is None or not message_id:
            log.info("teams.feedback.dropped")
            return
        place = AnswerPlace.of(activity, message_id)
        action_value = activity.value.action_value
        form = sent_form(action_value.feedback, message_id)
        if form is not None:
            # Daimon's form, delivered this way: the same submit path, minus the
            # re-shown form, which an empty-bodied answer cannot carry. An empty
            # one keeps the vote its click already recorded.
            if form.text or form.reasons:
                await self._take_form(actor, place, form, activity.from_.name)
            return
        vote: Vote = "up" if action_value.reaction == "like" else "down"
        await self._record(actor, place, vote, text=feedback_text(action_value.feedback))

    async def _take_form(
        self, actor: Actor, place: AnswerPlace, form: FormDetails, user_name: str | None
    ) -> _Recorded:
        """Record a sent form as a down-vote with its details, and route it if the
        tenant shares forms with support."""
        recorded = await self._record(
            actor, place, "down", details=(form.text or None, form.reasons)
        )
        if recorded.outcome != "recorded" or recorded.row_id is None:
            return recorded
        log.info(
            "feedback.submission_recorded",
            feedback_id=str(recorded.row_id),
            reasons=list(form.reasons),
        )
        if self._shared(actor.tenant_id) and recorded.details_changed:
            teams = self._runtime.settings.teams
            post = feedback_post(
                user_id=actor.user_id,
                user_name=user_name,
                link=place.link(teams.tenant_id) if teams is not None else place.message_id,
                form=form,
                recorded=recorded,
            )
            self._spawn(self._route(post, recorded.row_id), name="teams.feedback.route")
        return recorded

    async def _route(self, post: str, feedback_id: uuid.UUID) -> None:
        """Best effort: the form is already recorded, whatever the post does."""
        delivered = await post_to_support_channel(self._runtime, self._direct, post)
        log.info("feedback.routed_to_support", feedback_id=str(feedback_id), delivered=delivered)

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
            sealed=sealed_at(policy, place),
            ma_agent_id=None if thread is None else thread.ma_agent_id,
            ma_session_id=None if thread is None else thread.ma_session_id,
            details_changed=changed,
        )
