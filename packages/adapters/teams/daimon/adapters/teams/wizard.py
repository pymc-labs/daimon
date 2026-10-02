"""Taps on a form the agent posted with post_wizard: redraw it, or submit and start the turn.

The MCP server posts the first screen (`daimon.core.wizard.teams_card`); every
button arrives here under `VERB`, with the card's inputs in its data. Only the
person who asked may tap, on the form's own message, while it is open and
unexpired. Each write is predicated on the row being unchanged since it was
read (`update_wizard_state`), and Submit is claimed once (`try_claim_submit`)
before the turn starts, as that person, with the answer block as its message.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from daimon.adapters.teams.card_actions import (
    FAILED,
    Actor,
    card_actor,
    guarded,
    replace_card,
    submitted_fields,
    toast,
)
from daimon.adapters.teams.identity import TeamsInbound, canonical_uuid
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.stores.domain import WizardSessionRow
from daimon.core.stores.wizard_session import (
    get_wizard_session,
    try_claim_submit,
    update_wizard_state,
)
from daimon.core.teams_threads import conversation_of
from daimon.core.wizard.answers import format_answer_block
from daimon.core.wizard.apply import apply, build_action
from daimon.core.wizard.render import to_screen
from daimon.core.wizard.spec import WizardSpec
from daimon.core.wizard.state import ActionKind, WizardAction, WizardState, WizardStatus
from daimon.core.wizard.teams_card import (
    MAX_TEXT_CHARS,
    TEXT_INPUT,
    VALUES_INPUT,
    VERB,
    wizard_card,
)
from microsoft_teams.api import AdaptiveCardInvokeActivity, AdaptiveCardInvokeResponse
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import AdaptiveCard

__all__ = ["VERB", "TeamsWizards"]

_NOT_AVAILABLE = "This form is no longer available."
_WRONG_REQUESTER = "This form was for someone else."
_ALREADY_SUBMITTED = "This form was already submitted."
_EXPIRED = "This form has expired."
_STALE = "This form changed before your tap landed."
_DRAINING = "daimon is restarting. Press Submit again in a minute."
_EMPTY = "Type your answer first."
_TOO_LONG = f"Keep your answer under {MAX_TEXT_CHARS} characters."


def _card(spec: WizardSpec, state: WizardState) -> AdaptiveCard:
    return AdaptiveCard.model_validate(wizard_card(to_screen(spec, state)))


def _refusal(
    row: WizardSessionRow | None, actor: Actor, activity: AdaptiveCardInvokeActivity
) -> str | None:
    if (
        row is None
        or row.tenant_id != actor.tenant_id
        or row.message_id != activity.reply_to_id
        or row.channel_id.split(";", 1)[0] != activity.conversation.id.split(";", 1)[0]
    ):
        return _NOT_AVAILABLE
    if row.requester_platform_user_id != actor.user_id:
        return _WRONG_REQUESTER
    if row.status == WizardStatus.SUBMITTED:
        return _ALREADY_SUBMITTED
    if row.status == WizardStatus.ABANDONED or row.expires_at <= datetime.now(UTC):
        return _EXPIRED
    return None


def _selected(state: WizardState, spec: WizardSpec, data: Mapping[str, object]) -> WizardState:
    """`state` with a multi step's checked boxes saved; the boxes submit as option indices."""
    select = str(data.get("sel") or "")
    if not select or VALUES_INPUT not in data:
        return state
    picked = [part for part in str(data[VALUES_INPUT]).split(",") if part]
    step = spec.steps[state.current_step] if state.current_step < len(spec.steps) else None
    if step is None or not all(part.isdigit() and int(part) < len(step.options) for part in picked):
        raise ValueError("selection does not match the step")
    values = [step.options[int(part)].value for part in picked]
    return apply(state, spec, build_action(select, spec=spec, values=values, text=None))


def _inbound(
    row: WizardSessionRow,
    text: str,
    activity: AdaptiveCardInvokeActivity,
    actor: Actor,
    *,
    entra_tenant_id: str,
    service_url: str | None,
) -> TeamsInbound:
    conversation = conversation_of(row.channel_id)
    personal = activity.conversation.conversation_type == "personal"
    team = activity.channel_data.team if activity.channel_data is not None else None
    return TeamsInbound(
        kind="dm" if personal else "channel",
        entra_tenant_id=entra_tenant_id,
        user_id=actor.user_id,
        conversation_id=conversation,
        channel_id=conversation if personal else conversation.split(";", 1)[0],
        activity_id=activity.id,
        text=text,
        service_url=service_url,
        bot_name=activity.recipient.name,
        team_id=team.id if team is not None else None,
        team_group_id=canonical_uuid(team.aad_group_id) if team is not None else None,
        user_name=activity.from_.name,
        timestamp=datetime.now(UTC).isoformat(),
    )


class TeamsWizards:
    """Wizard taps; `start_turn` runs a submitted form's turn in the background."""

    def __init__(
        self,
        runtime: TeamsRuntime,
        *,
        start_turn: Callable[[TeamsInbound], None],
        draining: Callable[[], bool],
    ) -> None:
        self._runtime = runtime
        self._start_turn = start_turn
        self._draining = draining

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        return await guarded(self._on_action(ctx), toast(FAILED), "teams.wizard.failed")

    async def _on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        activity = ctx.activity
        data = submitted_fields(activity.value.action.data)
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return toast(_NOT_AVAILABLE)
        async with self._runtime.sessionmaker() as session:
            row = await get_wizard_session(session, short_id=str(data.get("wz") or ""))
        refusal = _refusal(row, actor, activity)
        if refusal is not None or row is None:
            return toast(refusal or _NOT_AVAILABLE)
        spec = WizardSpec.model_validate(row.spec)
        state = WizardState(
            short_id=row.id,
            current_step=row.current_step,
            answers=row.answers,
            status=WizardStatus(row.status),
        )
        op = str(data.get("op") or "")
        text = str(data.get(TEXT_INPUT) or "").strip()
        if op.endswith("_custom") and not text:
            return toast(_EMPTY)
        if op.endswith("_custom") and len(text) > MAX_TEXT_CHARS:
            return toast(_TOO_LONG)
        try:
            state = _selected(state, spec, data)
            if op == "submit":
                return await self._submit(ctx, row, spec, state, actor)
            state = apply(state, spec, build_action(op, spec=spec, values=[], text=text or None))
        except ValueError:
            return toast(_STALE)
        async with self._runtime.sessionmaker.begin() as session:
            written = await update_wizard_state(
                session,
                short_id=row.id,
                answers=state.answers,
                current_step=state.current_step,
                expected_updated_at=row.updated_at,
                now=datetime.now(UTC),
            )
        return replace_card(_card(spec, state)) if written else toast(_STALE)

    async def _submit(
        self,
        ctx: ActivityContext[AdaptiveCardInvokeActivity],
        row: WizardSessionRow,
        spec: WizardSpec,
        state: WizardState,
        actor: Actor,
    ) -> AdaptiveCardInvokeResponse:
        submitted = apply(state, spec, WizardAction(kind=ActionKind.SUBMIT))
        if submitted.status is not WizardStatus.SUBMITTED:
            return replace_card(_card(spec, submitted))
        if self._draining():
            # Before the claim, which is one-shot: the form stays usable.
            return toast(_DRAINING)
        teams = self._runtime.settings.teams
        assert teams is not None, "card_actor admitted the click"
        inbound = _inbound(
            row,
            format_answer_block(spec, submitted),
            ctx.activity,
            actor,
            entra_tenant_id=teams.tenant_id,
            service_url=ctx.conversation_ref.service_url,
        )
        async with self._runtime.sessionmaker.begin() as session:
            claimed = await try_claim_submit(
                session,
                short_id=row.id,
                answers=submitted.answers,
                current_step=submitted.current_step,
                expected_updated_at=row.updated_at,
                now=datetime.now(UTC),
            )
        if claimed is None:
            return toast(_STALE)
        # Nothing that can fail sits between the claim and the turn it paid for.
        self._start_turn(inbound)
        return replace_card(_card(spec, submitted))
