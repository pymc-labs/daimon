"""Set charge and Decline on a pending support post, at parity with Slack and Discord.

Anyone acting in the configured support channel may settle (decision 4);
`settle_from_post` audits it. The requester is never messaged (decision 8):
the post and the settler's confirmation say so. The charge is an input on the
post itself, sent with Set charge (Action.Execute); the buttons carry the
request id and the post's lines, so they work after a restart. Settling edits
the post: the final status and settler in place of the pending line, no buttons.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import cast

import structlog
from daimon.adapters.teams.card_actions import (
    FAILED,
    Actor,
    card_actor,
    guarded,
    submitted_fields,
    toast,
)
from daimon.adapters.teams.identity import DENIED, canonical_uuid
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.support_plan_card import CHARGE_INPUT, closed_card
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.platform_names import get_user_names
from daimon.core.stores.support_escalation import SettleOutcome, get_request
from daimon.core.stores.support_rows import SupportEscalationRow
from daimon.core.support_plan_changes import is_support_channel, settle_from_post
from daimon.core.support_plans import (
    CHARGE_FIELD,
    PLAN_GONE,
    REQUEST_GONE,
    SETTLE_ELSEWHERE,
    SupportPlanError,
    already_settled_text,
    parse_amount,
    settle_range_text,
    settled_text,
)
from microsoft_teams.api import (
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    MessageActivityInput,
)
from microsoft_teams.apps import ActivityContext

log = structlog.get_logger(__name__)

_EDIT_TIMEOUT_S = 5.0


def _escalation_id(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(hex=str(value or ""))
    except ValueError:
        return None


def _post_lines(data: Mapping[str, object]) -> list[str]:
    """The post's lines, carried in the buttons: a bot cannot read a channel post back."""
    raw = data.get("post")
    lines = cast("list[object]", raw) if isinstance(raw, list) else []
    return [str(line) for line in lines]


class TeamsSupportSettle:
    """The Set charge and Decline buttons on a pending request's support post."""

    def __init__(self, runtime: TeamsRuntime) -> None:
        self._runtime = runtime

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        return await guarded(self._act(ctx), toast(FAILED), "teams.support.settle_failed")

    def _in_support_channel(self, activity: AdaptiveCardInvokeActivity) -> bool:
        conversation = activity.conversation
        if conversation.conversation_type != "channel":
            return False
        return is_support_channel(
            configured=self._runtime.settings.support.teams_escalation_channel_id,
            channel_id=conversation.id.split(";", 1)[0],
            parent_id=None,
        )

    async def _settler(self, tenant_id: uuid.UUID, by: str | None) -> str | None:
        """A Teams settler by the name Teams last gave: a card cannot mention by id."""
        if by is None or canonical_uuid(by) is None:
            return by
        async with self._runtime.sessionmaker() as session:
            names = await get_user_names(
                session, tenant_id=tenant_id, platform="teams", user_ids=(by,)
            )
        known = names.get(by)
        return None if known is None else known.label

    async def _act(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        activity = ctx.activity
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return toast(DENIED)
        if not self._in_support_channel(activity):
            return toast(SETTLE_ELSEWHERE)
        data = submitted_fields(activity.value.action.data)
        escalation_id = _escalation_id(data.get("request"))
        row = None
        if escalation_id is not None:
            async with self._runtime.sessionmaker() as session:
                row = await get_request(session, escalation_id=escalation_id)
        if row is None:
            return toast(REQUEST_GONE)
        if row.status != "pending":
            outcome = SettleOutcome(status="already_settled", row=row)
            return await self._finish(ctx, actor, data, before=row, outcome=outcome)
        charge = None
        if data.get("op") != "decline":
            value = data.get(CHARGE_INPUT)
            try:
                charge = parse_amount(
                    "" if value is None else str(value), name=CHARGE_FIELD.lower(), allow_zero=True
                )
            except SupportPlanError as err:
                return toast(str(err))
        return await self._settle(ctx, actor, data, row=row, charge=charge)

    async def _settle(
        self,
        ctx: ActivityContext[AdaptiveCardInvokeActivity],
        actor: Actor,
        data: Mapping[str, object],
        *,
        row: SupportEscalationRow,
        charge: Decimal | None,
    ) -> AdaptiveCardInvokeResponse:
        """Settle at `charge` (None declines) and say how it went."""
        async with self._runtime.sessionmaker() as session:
            principal = await find_platform_principal(
                session, tenant_id=actor.tenant_id, platform="teams", external_id=actor.user_id
            )
        outcome = await settle_from_post(
            self._runtime.sessionmaker,
            escalation_id=row.id,
            charge=charge,
            settled_by=actor.user_id,
            settled_by_account_id=None if principal is None else principal.account_id,
            default_allowance=self._runtime.settings.support.credits_per_user,
        )
        log.info("support.request_settled", escalation_id=str(row.id), status=outcome.status)
        return await self._finish(ctx, actor, data, before=row, outcome=outcome)

    async def _finish(
        self,
        ctx: ActivityContext[AdaptiveCardInvokeActivity],
        actor: Actor,
        data: Mapping[str, object],
        *,
        before: SupportEscalationRow,
        outcome: SettleOutcome,
    ) -> AdaptiveCardInvokeResponse:
        after = outcome.row
        if after is None:
            return toast(REQUEST_GONE)
        if outcome.status == "plan_gone":
            return toast(PLAN_GONE)
        if outcome.status in ("invalid", "over_balance"):
            return toast(
                settle_range_text(after.bounds, counted=after.counted, available=outcome.available)
            )
        settled_now = outcome.status in ("settled", "declined")
        by = ctx.activity.from_.name if settled_now else None
        by = by or await self._settler(actor.tenant_id, after.settled_by)
        if after.status != "pending":
            await self._close_post(ctx, data, before=before, after=after, by=by)
        if outcome.status == "settled":
            return toast(settled_text(charge=after.charge))
        if outcome.status == "declined":
            return toast(settled_text(charge=None))
        return toast(already_settled_text(status=after.status, charge=after.charge, by=by))

    async def _close_post(
        self,
        ctx: ActivityContext[AdaptiveCardInvokeActivity],
        data: Mapping[str, object],
        *,
        before: SupportEscalationRow,
        after: SupportEscalationRow,
        by: str | None,
    ) -> None:
        """Edit the post for everyone in the channel: no buttons. Best-effort."""
        post_id = ctx.activity.reply_to_id or after.support_post_id
        if not post_id:
            log.warning("support.post_edit_failed", escalation_id=str(after.id), error="missing")
            return
        card = closed_card(_post_lines(data), before=before, after=after, by=by)
        try:
            await asyncio.wait_for(
                ctx.send(MessageActivityInput(id=post_id).add_card(card)), _EDIT_TIMEOUT_S
            )
        except TEAMS_SEND_ERRORS as err:
            # The settle is committed; a stale post only shows old buttons, which
            # answer "Already charged" on the next click.
            log.warning(
                "support.post_edit_failed", escalation_id=str(after.id), error=type(err).__name__
            )


__all__ = ["TeamsSupportSettle"]
