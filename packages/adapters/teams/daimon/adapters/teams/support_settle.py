"""Set charge and Decline on a pending support post, at parity with Slack and Discord.

Anyone acting in the channel the request was posted to may settle (decision 4);
`settle_from_post` audits it. The requester is never messaged (decision 8):
the post and the settler's confirmation say so. The charge is an input on the
post itself, sent with Set charge (Action.Execute); the buttons carry only the
request id, so they work after a restart. Settling edits
the post, rebuilt from the stored request: the final status and settler in
place of the pending line, no buttons.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from decimal import Decimal

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
from daimon.adapters.teams.support import closed_post_lines
from daimon.adapters.teams.support_menu_card import CHARGE_INPUT, closed_card
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.platform_names import get_user_names
from daimon.core.stores.support_escalation import SettleOutcome, get_request
from daimon.core.stores.support_rows import SupportEscalationRow
from daimon.core.support import (
    CHARGE_FIELD,
    LIMIT_GONE,
    REQUEST_GONE,
    SETTLE_ELSEWHERE,
    SupportError,
    already_settled_text,
    parse_amount,
    settle_range_text,
    settled_text,
)
from daimon.core.support_changes import settle_from_post
from daimon.core.support_routing import settle_channel_ok
from microsoft_teams.api import (
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    MessageActivityInput,
)
from microsoft_teams.apps import ActivityContext
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

_EDIT_TIMEOUT_S = 5.0


def _escalation_id(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(hex=str(value or ""))
    except ValueError:
        return None


class TeamsSupportSettle:
    """The Set charge and Decline buttons on a pending request's support post."""

    def __init__(self, runtime: TeamsRuntime) -> None:
        self._runtime = runtime

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        return await guarded(self._act(ctx), toast(FAILED), "teams.support.settle_failed")

    async def _posted_here(
        self, session: AsyncSession, activity: AdaptiveCardInvokeActivity, row: SupportEscalationRow
    ) -> bool:
        """Whether the click came from the channel `row` was posted to."""
        conversation = activity.conversation
        if conversation.conversation_type != "channel":
            return False
        return await settle_channel_ok(
            session,
            row=row,
            channel_id=conversation.id.split(";", 1)[0],
            parent_id=None,
            default_channel_id=self._runtime.settings.support.teams_escalation_channel_id,
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
        if actor.foreign:
            return toast(DENIED)
        data = submitted_fields(activity.value.action.data)
        escalation_id = _escalation_id(data.get("request"))
        row, here = None, False
        if escalation_id is not None:
            async with self._runtime.sessionmaker() as session:
                row = await get_request(session, escalation_id=escalation_id)
                here = row is not None and await self._posted_here(session, activity, row)
        if row is None:
            return toast(REQUEST_GONE)
        if not here:
            return toast(SETTLE_ELSEWHERE)
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
            except SupportError as err:
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
            channel_id=ctx.activity.conversation.id.split(";", 1)[0],
            parent_id=None,
            default_channel_id=self._runtime.settings.support.teams_escalation_channel_id,
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
        if outcome.status == "limit_gone":
            return toast(LIMIT_GONE)
        if outcome.status == "not_here":
            return toast(SETTLE_ELSEWHERE)
        if outcome.status in ("invalid", "over_balance"):
            return toast(
                settle_range_text(after.bounds, counted=after.counted, available=outcome.available)
            )
        settled_now = outcome.status in ("settled", "declined")
        by = ctx.activity.from_.name if settled_now else None
        by = by or await self._settler(actor.tenant_id, after.settled_by)
        if after.status != "pending":
            await self._close_post(ctx, before=before, after=after, by=by)
        if outcome.status == "settled":
            return toast(settled_text(charge=after.charge))
        if outcome.status == "declined":
            return toast(settled_text(charge=None))
        return toast(already_settled_text(status=after.status, charge=after.charge, by=by))

    async def _close_post(
        self,
        ctx: ActivityContext[AdaptiveCardInvokeActivity],
        before: SupportEscalationRow,
        after: SupportEscalationRow,
        by: str | None,
    ) -> None:
        """Edit the post for everyone in the channel: no buttons. Best-effort."""
        post_id = ctx.activity.reply_to_id or after.support_post_id
        if not post_id:
            log.warning("support.post_edit_failed", escalation_id=str(after.id), error="missing")
            return
        async with self._runtime.sessionmaker() as session:
            names = await get_user_names(
                session,
                tenant_id=after.tenant_id,
                platform="teams",
                user_ids=(after.platform_user_id,),
            )
        known = names.get(after.platform_user_id)
        lines = closed_post_lines(
            before,
            user_name=None if known is None else known.label,
            entra_tenant_id=ctx.activity.conversation.tenant_id or "",
        )
        card = closed_card(lines, before=before, after=after, by=by)
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
