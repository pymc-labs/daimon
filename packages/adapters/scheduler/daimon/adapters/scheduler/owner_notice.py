"""Tell a routine's owner by direct message that a run failed: the agent writes it.

The routine's agent gets a fresh, unattended DM turn of the owner with fixed facts only
(`routine_owner_notice`). When that turn cannot run or say anything (no credits, provider
down, the agent closed to the owner), the owner gets the plain error card instead. Only on
Discord, and only where routines may DM the owner, as for the lapsed-role card.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Final

import httpx
import structlog
from daimon.core.channel_audience import requester_standing
from daimon.core.config import Settings, env_admits
from daimon.core.direct_messages import run_dm_routine
from daimon.core.ops_events import OpsEvent, report_ops_event
from daimon.core.routine_delivery import routine_agent_name, routine_links
from daimon.core.routine_owner_notice import (
    owner_notice_card,
    owner_notice_facts,
    owner_notice_prompt,
)
from daimon.core.scheduler import OwnerNotifier
from daimon.core.stores.direct_messages import dm_enabled
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import routine_platform
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.termination import TerminationReason
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: The notice turn is short; past this the plain card goes instead.
_TURN_TIMEOUT_S: Final = 300.0


def build_owner_notifier(
    *,
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    deps: TurnDeps,
    send_card: Callable[[str, str, str], Awaitable[None]],
    send_text: Callable[[str, str], Awaitable[None]],
) -> OwnerNotifier:
    """The scheduler's `notify_owner`: `send_card(user, title, body)`, `send_text(user, text)`."""

    async def notify(row: RoutineRow, reason: TerminationReason, channel_told: bool) -> None:
        owner = row.created_by_user_id
        async with sm() as s:
            if (
                owner is None
                or await routine_platform(s, row.id) != "discord"
                or not env_admits(settings.direct_message_policies, row.tenant_id, owner)
                or not await dm_enabled(s, tenant_id=row.tenant_id)
            ):
                return
        turn_error: str | None = None
        try:
            links = await routine_links(sm, settings, row, "discord")
            facts = owner_notice_facts(
                row,
                reason,
                agent_name=await routine_agent_name(sm, row),
                channel_told=channel_told,
                now=datetime.now(UTC),
                link=links.routine(row.id) if links is not None else None,
            )
            if reason in _NO_TURN:
                turn_error = "refused"  # a turn would be refused, or spend what is out
            else:
                text = await _agent_text(row, owner, facts)
                # Once sending starts, a failure is not retried as a card: no second DM.
                await send_text(owner, text)
                log.info("routine.owner_notified", routine_id=str(row.id), written_by="agent")
                return
        except _SendFailed as exc:
            return _report_failed(row, exc.__cause__ or exc)
        except Exception as exc:
            # Boundary: any reason the agent could not write it falls back to the card.
            if "facts" not in locals():
                return _report_failed(row, exc)
            turn_error = type(exc).__name__
        try:
            await send_card(owner, *owner_notice_card(facts))
        except (httpx.HTTPError, ValueError) as exc:
            return _report_failed(row, exc)
        log.info(
            "routine.owner_notified",
            routine_id=str(row.id),
            written_by="plain",
            turn_error_class=turn_error,
        )

    async def _agent_text(row: RoutineRow, owner: str, facts: OwnerNoticeFacts) -> str:
        async with asyncio.timeout(_TURN_TIMEOUT_S):
            async with sm() as s:
                standing = await requester_standing(
                    s,
                    tenant_id=row.tenant_id,
                    user_id=owner,
                    is_dm=True,
                    resolver=deps.membership_resolver,
                )
            reply = await run_dm_routine(
                deps,
                requester_org=standing,
                tenant_id=row.tenant_id,
                owner_user_id=owner,
                agent_name=row.agent_name,
                prompt=owner_notice_prompt(facts),
                source=None,
                budget_channel_id=None,
                origin="routine_notice",
            )
        if not (text := reply.text.strip()):
            raise ValueError("the notice turn wrote nothing")
        return text

    def _report_failed(row: RoutineRow, exc: BaseException) -> None:
        report_ops_event(
            sm,
            OpsEvent(
                "routine.owner_notice_failed",
                row.tenant_id,
                reason=type(exc).__name__,
                routine_id=row.id,
                agent_id=row.agent_id,
                platform="discord",
            ),
        )

    return notify
