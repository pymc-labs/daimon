"""Routines to one-step workflows and back, one routine at a time, by an admin with workflows on.

`from_routine` locks the routine and, in one transaction, writes the move `routine_translation`
makes of it: the workflow with v1 published as the routine's owner, its schedule cursor, posts,
features and intake binding; the routine row goes. `to_routine` undoes one the same way, from
the workflow as it is now. Both stay in the caller's workspace.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from daimon.core.cron import InvalidScheduleError, validated_next_slot
from daimon.core.stores import feature_values, lead_intake, tenants
from daimon.core.stores import routines as routines_store
from daimon.core.stores import workflow_deliveries as deliveries_store
from daimon.core.stores import workflow_runs as runs_store
from daimon.core.stores import workflow_triggers as triggers_store
from daimon.core.stores import workflows as store
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.workflows.routine_translation import (
    TRIGGER,
    Delivery,
    Move,
    Reason,
    Refusal,
    RoutineSnapshot,
    WorkflowSnapshot,
    apply_move,
    translate,
    untranslate,
)
from daimon.core.workflows.service import Env, Forbidden, Invalid, NotFound, Refused, ensure_on
from daimon.core.workflows.validate import Validation, validate_document
from sqlalchemy.ext.asyncio import AsyncSession

GONE: Final = "This routine isn't here."
ADMINS: Final = "Only an admin can move routines to Workflows and back."
MOVED: Final = "This routine already runs as a workflow."
REFUSALS: Final[dict[Reason, str]] = {
    "handback_paused": "This routine is paused for handback to another system, so it stays.",
    "invalid_schedule": "This routine's schedule isn't valid. Fix it first.",
    "owner_unlinked": "This routine's owner has no account. Create it again first.",
    "busy": "This routine is running or posting. Try again once it finishes.",
    "not_a_routine": "This workflow does more than a routine can: one schedule, one step.",
    "changed": "Put its agent, where it posts and its owner back as the routine had them first.",
}
BUSY_WORKFLOW: Final = "This workflow has a run or a post in progress. Try again once it finishes."


@dataclass(frozen=True)
class Plan:
    """What `from_routine` would do: the move, its spec, the grants the converting admin
    approves, and whether the workflow starts paused."""

    routine: RoutineRow
    spec: dict[str, Any]
    paused: bool
    validation: Validation
    move: Move


async def routine_snapshot(
    session: AsyncSession,
    row: RoutineRow,
    notice: dict[str, Any] | None,
    *,
    platform: str,
    now: datetime,
) -> RoutineSnapshot:
    """What `translate` reads of a locked routine: its owner's account, routine-tier features,
    intake binding and next slot."""
    owner = None
    if row.created_by_user_id is not None:
        principal = await get_or_create_platform_principal(
            session, tenant_id=row.tenant_id, platform=platform, external_id=row.created_by_user_id
        )
        owner = principal.account_id
    values = await feature_values.place_values(
        session,
        tenant_id=row.tenant_id,
        agent_id=None,
        channel_id=None,
        thread_key=None,
        routine_id=row.id,
    )
    try:
        slot = validated_next_slot(row.cron_expr, row.timezone, now)
    except InvalidScheduleError:
        slot = None
    extra = {
        "owner_account_id": owner,
        "owner_notice": notice,
        "features": tuple(sorted(v.key for v in values if v.tier == "routine")),
        "intake": await lead_intake.bound_config(
            session, row.tenant_id, key="routine_id", holder=row.id
        ),
        "next_slot": slot,
    }
    return RoutineSnapshot.of(row.model_dump(), extra)


async def plan_move(
    session: AsyncSession, tenant_id: uuid.UUID, routine_id: uuid.UUID, now: datetime
) -> tuple[RoutineRow, Move | Refusal]:
    """Lock a routine of the workspace and translate it; NotFound or Refused when it is gone."""
    found = await routines_store.get_routine_for_move(session, routine_id, tenant_id=tenant_id)
    tenant = await tenants.get_tenant(session, tenant_id)
    if found is None or tenant is None:
        if await routines_store.migrated_workflows(session, [routine_id], tenant_id=tenant_id):
            raise Refused(MOVED)
        raise NotFound(GONE)
    row, notice = found
    snap = await routine_snapshot(session, row, notice, platform=tenant.platform, now=now)
    return row, translate(snap, now=now)


async def _plan(
    session: AsyncSession, tenant_id: uuid.UUID, routine_id: uuid.UUID, now: datetime
) -> Plan:
    row, move = await plan_move(session, tenant_id, routine_id, now)
    if isinstance(move, Refusal):
        raise Refused(REFUSALS[move.reason])
    validation = validate_document(move.spec, None)
    return Plan(row, move.spec, move.outcome == "paused", validation, move)


async def apply(session: AsyncSession, tenant_id: uuid.UUID, move: Move) -> None:
    """Write a move in the session's transaction; a moved feature makes snapshots stale."""
    await session.run_sync(lambda s: apply_move(s.connection(), move))
    if any(w.table == "feature_values" for w in move.writes):
        await feature_values.bump_revision(session, tenant_id)


async def move_routine(
    session: AsyncSession, tenant_id: uuid.UUID, routine_id: uuid.UUID, now: datetime
) -> Plan:
    """Translate one routine and write its move in the session's transaction; Refused or
    Invalid when it stays."""
    plan = await _plan(session, tenant_id, routine_id, now)
    if plan.validation.errors:
        raise Invalid(plan.validation.errors[0].message)
    await apply(session, tenant_id, plan.move)
    return plan


def _admin(env: Env) -> None:
    if not env.who.admin or env.who.external:
        raise Forbidden(ADMINS)


async def preview(env: Env, routine_id: uuid.UUID) -> Plan:
    """The conversion `from_routine` would make, changing nothing."""
    _admin(env)
    await ensure_on(env)
    async with env.sm() as session:
        try:
            return await _plan(session, env.who.tenant_id, routine_id, env.now)
        finally:
            await session.rollback()  # the owner's principal it may have made


async def from_routine(env: Env, routine_id: uuid.UUID) -> uuid.UUID:
    """Move one routine onto a one-step workflow; returns the workflow's id."""
    _admin(env)
    await ensure_on(env)
    async with env.sm.begin() as session:
        plan = await move_routine(session, env.who.tenant_id, routine_id, env.now)
        return plan.move.id


async def to_routine(env: Env, workflow_id: uuid.UUID) -> uuid.UUID:
    """Make a converted workflow its routine again, as the workflow is now, and delete the
    workflow; returns the routine's id. Its agent, destination and owner must be the routine's,
    with no run or post in progress."""
    _admin(env)
    await ensure_on(env)
    tenant_id = env.who.tenant_id
    async with env.sm.begin() as session:
        workflow = await store.get_workflow(
            session, tenant_id=tenant_id, workflow_id=workflow_id, lock=True
        )
        if workflow is None or workflow.converted_from is None:
            raise NotFound("This workflow did not come from a routine.")
        live = None
        if workflow.live_version is not None:
            live = await store.get_version(
                session, tenant_id=tenant_id, workflow_id=workflow.id, number=workflow.live_version
            )
        trigger = await triggers_store.find_trigger(
            session, tenant_id=tenant_id, workflow_id=workflow.id, key=TRIGGER
        )
        if live is None or trigger is None:
            raise Refused(REFUSALS["not_a_routine"])
        values = await feature_values.place_values(
            session,
            tenant_id=tenant_id,
            agent_id=None,
            channel_id=None,
            thread_key=None,
            workflow_id=workflow.id,
        )
        posts = await deliveries_store.unsent_routine_posts(session, workflow.id)
        last = await runs_store.last_ended(session, workflow_id=workflow.id)
        snap = WorkflowSnapshot(
            id=workflow.id,
            tenant_id=tenant_id,
            name=workflow.name,
            status=workflow.status,
            owner_account_id=workflow.owner_account_id,
            home_channel_id=workflow.home_channel_id,
            made_in=dict(workflow.made_in) if workflow.made_in else None,
            converted_from=dict(workflow.converted_from),
            spec=live.spec,
            published_by=live.published_by_account_id,
            next_fire_at=trigger.next_fire_at,
            last_fired_slot=trigger.last_fired_slot,
            seeded_after=trigger.seeded_after,
            deliveries=tuple(Delivery(p.id, p.kind, p.status, p.payload, p.note) for p in posts),
            features=tuple(sorted(v.key for v in values if v.tier == "workflow")),
            intake=await lead_intake.bound_config(
                session, tenant_id, key="workflow_id", holder=workflow.id
            ),
            active_runs=bool(await runs_store.count_active_runs(session, workflow_id=workflow.id)),
            last_status=last.status if last else None,
            last_error=last.failure_message if last else None,
        )
        move = untranslate(snap, now=env.now)
        if isinstance(move, Refusal):
            raise Refused(BUSY_WORKFLOW if move.reason == "busy" else REFUSALS[move.reason])
        await apply(session, tenant_id, move)
        return move.id
