"""Routines to one-step workflows and back, one routine at a time, by an admin with workflows on.

`from_routine` locks the routine and, in one transaction, makes its workflow with v1
(`source: routine`) published as the routine's owner, hands the routine's cursor and link to
the schedule trigger (`sync_live`) and stops the routine claiming slots, so no slot fires
twice or is lost. `to_routine` gives the cursor back the same way and pauses the workflow.
Lead intake, handback and in-flight routines stay put; a routine on another backend moves
paused, since its workflow could not run there either.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from daimon.core.channel_backend import agent_native_provider, channel_provider
from daimon.core.errors import StoreError
from daimon.core.routine_sandbox import opted_in_by_many
from daimon.core.routines import routine_label
from daimon.core.stores import routines as routines_store
from daimon.core.stores import tenants
from daimon.core.stores import workflow_runs as runs_store
from daimon.core.stores import workflow_triggers as triggers_store
from daimon.core.stores import workflows as store
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.lead_intake import configured_routines
from daimon.core.workflows.lanes import OWNER_DM
from daimon.core.workflows.service import (
    Env,
    Forbidden,
    Invalid,
    NotFound,
    Refused,
    ensure_on,
)
from daimon.core.workflows.spec import (
    AgentNode,
    FinishNode,
    ScheduleTrigger,
    dump_spec,
    parse_spec,
    spec_hash,
)
from daimon.core.workflows.validate import validate_document
from daimon.core.workflows.views import iso
from mux.contracts.config import DEFAULT_BACKEND
from sqlalchemy.ext.asyncio import AsyncSession

STEP: Final = "run"
DONE: Final = "done"
TRIGGER: Final = "schedule"
GONE: Final = "This routine isn't here."
ADMINS: Final = "Only an admin can move routines to Workflows and back."


def routine_spec(row: RoutineRow, *, builtins: bool) -> dict[str, Any]:
    """The one-step workflow a routine becomes: its schedule, one Agent step with its message,
    and a Finish that posts where the routine posted. A DM routine runs as the owner's DM."""
    agent: dict[str, Any] = {
        "kind": "agent",
        "title": row.name or routine_label(row),
        "instructions": row.trigger_message,
        "next": DONE,
    }
    if row.destination_kind == "dm":
        agent["session"] = OWNER_DM
    if builtins:
        agent["builtins"] = "all"
    finish: dict[str, Any] = {"kind": "finish", "title": "Done"}
    to = "owner" if row.destination_kind == "dm" else row.destination_id
    if to is not None:
        finish["deliver"] = {"to": to, "message": f"${{{{ steps.{STEP}.summary }}}}"}
    spec: dict[str, Any] = {
        "schema": "daimon.workflow/v1",
        "name": (row.name or routine_label(row))[:120],
        "visibility": "private",
        "triggers": [
            {
                "id": TRIGGER,
                "kind": "schedule",
                "cron": row.cron_expr,
                "timezone": row.timezone,
                "catch_up": row.catch_up_policy,
            }
        ],
        "defaults": {"agent": row.agent_name},
        "start": STEP,
        "nodes": {STEP: agent, DONE: finish},
    }
    if row.channel_id is not None:
        spec["home"] = {"channel": row.channel_id}
    return spec


def exclusion(row: RoutineRow, *, moved: bool, lead_intake: bool) -> str | None:
    """Why this routine stays on the routine runner, in words; None when it can move."""
    if moved:
        return "This routine already runs as a workflow."
    if lead_intake:
        return "Lead intake runs this routine, so it stays a routine for now."
    if row.source_routine_id is not None or row.handback_paused_at is not None:
        return "This routine came from another system and may go back there, so it stays."
    if row.created_by_user_id is None:
        return "This routine has no owner. Create it again first."
    if row.delivery_status in ("pending", "claimed"):
        return "This routine is still posting its last result. Try again in a minute."
    if row.last_fired_at is not None and row.last_finished_at is None:
        return "This routine is running. Try again once it finishes."
    return None


def _snapshot(row: RoutineRow, now: datetime) -> dict[str, Any]:
    """What the workflow page shows as "Before migration"; a DM routine's text stays private."""
    last = None
    if row.last_fired_at is not None:
        last = {
            "at": iso(row.last_finished_at or row.last_fired_at),
            "status": "failed" if row.last_error else "succeeded",
            "text": "" if row.destination_kind == "dm" else row.last_error or row.last_result_tail,
        }
    return {"name": row.name or routine_label(row), "migrated_at": iso(now), "last": last}


async def _on_other_backend(session: AsyncSession, row: RoutineRow, platform: str) -> bool:
    if await agent_native_provider(
        session, tenant_id=row.tenant_id, agent_id=row.agent_id, agent_name=row.agent_name
    ):
        return True
    for channel in {
        row.channel_id,
        row.destination_id if row.destination_kind == "channel" else None,
    }:
        if channel is None:
            continue
        provider = await channel_provider(
            session, tenant_id=row.tenant_id, platform=platform, channel_id=channel
        )
        if provider not in (None, DEFAULT_BACKEND):
            return True
    return False


@dataclass(frozen=True)
class Plan:
    """What `from_routine` would do: the spec, and whether the workflow starts paused."""

    routine: RoutineRow
    spec: dict[str, Any]
    paused: bool
    other_backend: bool


async def _plan(session: AsyncSession, env: Env, routine_id: uuid.UUID) -> Plan:
    tenant_id = env.who.tenant_id
    row = await routines_store.get_routine_for_move(session, routine_id, tenant_id=tenant_id)
    tenant = await tenants.get_tenant(session, tenant_id)
    if row is None or tenant is None:
        raise NotFound(GONE)
    moved = bool(await routines_store.migrated_workflows(session, [row.id]))
    lead = (tenant_id, row.id) in await configured_routines(session, {tenant_id})
    if (why := exclusion(row, moved=moved, lead_intake=lead)) is not None:
        raise Refused(why)
    builtins = (await opted_in_by_many(session, [row], tenant.platform))[row.id] is not None
    other = await _on_other_backend(session, row, tenant.platform)
    spec = routine_spec(row, builtins=builtins)
    return Plan(row, spec, paused=other or not row.enabled, other_backend=other)


def _admin(env: Env) -> None:
    if not env.who.admin or env.who.external:
        raise Forbidden(ADMINS)


async def preview(env: Env, routine_id: uuid.UUID) -> Plan:
    """The conversion `from_routine` would make, changing nothing."""
    _admin(env)
    await ensure_on(env)
    async with env.sm() as session:
        return await _plan(session, env, routine_id)


async def from_routine(env: Env, routine_id: uuid.UUID) -> uuid.UUID:
    """Move one routine onto a one-step workflow; returns the workflow's id."""
    _admin(env)
    await ensure_on(env)
    async with env.sm.begin() as session:
        plan = await _plan(session, env, routine_id)
        row = plan.routine
        tenant = await tenants.get_tenant(session, row.tenant_id)
        assert tenant is not None and row.created_by_user_id is not None, "checked by the plan"
        owner = await get_or_create_platform_principal(
            session,
            tenant_id=row.tenant_id,
            platform=tenant.platform,
            external_id=row.created_by_user_id,
        )
        validation = validate_document(plan.spec)
        if validation.errors:
            raise Invalid(validation.errors[0].message)
        spec = dump_spec(parse_spec(plan.spec))
        workflow = await store.create_workflow(
            session,
            tenant_id=row.tenant_id,
            owner_account_id=owner.account_id,
            name=str(spec["name"]),
            spec=spec,
            home_channel_id=row.channel_id,
        )
        cursor = triggers_store.Cursor(
            routine_id=row.id,
            next_fire_at=row.next_fire_at,
            last_fired_slot=row.last_fired_slot,
            seeded_after=row.seeded_after,
            snapshot=_snapshot(row, env.now),
        )
        await store.publish_version(
            session,
            tenant_id=row.tenant_id,
            workflow_id=workflow.id,
            spec=spec,
            spec_hash=spec_hash(parse_spec(spec)),
            source="routine",
            published_by=owner.account_id,
            note="Moved from a routine",
            # The routine's built-ins opt-in was an admin's; the converting admin carries it.
            approved_by=env.who.account_id if validation.grants else None,
            now=env.now,
            cursors={TRIGGER: cursor},
        )
        if plan.paused:
            await store.set_status(
                session, tenant_id=row.tenant_id, workflow_id=workflow.id, status="paused"
            )
        await routines_store.stop_for_workflow(session, row)
        return workflow.id


def _changed(new: str, old: str) -> str | None:
    return None if new == old else new


@dataclass(frozen=True)
class _Back:
    """The routine fields a one-step workflow carries back."""

    name: str
    cron: str
    timezone: str
    catch_up: Literal["skip", "run-once"]
    instructions: str
    agent: str | None
    to: str | None


def routine_fields(spec: dict[str, Any]) -> _Back | str:
    """The routine a live spec still is, or why it is more than a routine now."""
    parsed = parse_spec(spec)
    schedules = [t for t in parsed.triggers or [] if isinstance(t, ScheduleTrigger)]
    others = [t for t in parsed.triggers or [] if t.kind not in ("schedule", "manual")]
    agents = [n for n in parsed.nodes.values() if isinstance(n, AgentNode)]
    finishes = [n for n in parsed.nodes.values() if isinstance(n, FinishNode)]
    if len(schedules) != 1 or others or len(agents) != 1 or len(parsed.nodes) != 2 or not finishes:
        return "This workflow does more than a routine can: one schedule, one step, one finish."
    trigger, agent, finish = schedules[0], agents[0], finishes[0]
    return _Back(
        name=parsed.name,
        cron=trigger.cron,
        timezone=trigger.timezone or "UTC",
        catch_up=trigger.catch_up or "skip",
        instructions=agent.instructions,
        agent=agent.agent or (parsed.defaults.agent if parsed.defaults else None),
        to=finish.deliver.to if finish.deliver else None,
    )


async def to_routine(env: Env, workflow_id: uuid.UUID) -> uuid.UUID:
    """Give a moved routine its schedule back and pause its workflow; returns the routine's id.

    Its schedule, name and instructions come back as the workflow has them. A different agent
    or destination, a version published by someone else, or a run in progress is refused."""
    _admin(env)
    await ensure_on(env)
    tenant_id = env.who.tenant_id
    async with env.sm.begin() as session:
        workflow = await store.get_workflow(
            session, tenant_id=tenant_id, workflow_id=workflow_id, lock=True
        )
        trigger = await triggers_store.get_routine_trigger(
            session, tenant_id=tenant_id, workflow_id=workflow_id
        )
        if workflow is None or trigger is None or trigger.from_routine_id is None:
            raise NotFound("This workflow did not come from a routine.")
        row = await routines_store.get_routine_for_move(
            session, trigger.from_routine_id, tenant_id=tenant_id
        )
        live = None
        if workflow.live_version is not None:
            live = await store.get_version(
                session, tenant_id=tenant_id, workflow_id=workflow.id, number=workflow.live_version
            )
        if row is None or live is None:
            raise NotFound(GONE)
        if await runs_store.count_active_runs(session, workflow_id=workflow.id):
            raise Refused("This workflow has runs in progress. Cancel them first.")
        back = routine_fields(live.spec)
        if isinstance(back, str):
            raise Refused(back)
        tenant = await tenants.get_tenant(session, tenant_id)
        assert tenant is not None and row.created_by_user_id is not None, "a moved routine"
        owner = await get_or_create_platform_principal(
            session,
            tenant_id=tenant_id,
            platform=tenant.platform,
            external_id=row.created_by_user_id,
        )
        was = routine_spec(row, builtins=False)
        if live.published_by_account_id != owner.account_id:
            raise Refused("Someone other than the routine's owner published this version.")
        if back.agent != row.agent_name or back.to != was["nodes"][DONE].get("deliver", {}).get(
            "to"
        ):
            raise Refused("Put the agent and where it posts back as the routine had them first.")
        await triggers_store.release_to_routine(session, trigger)
        await routines_store.resume_from_workflow(
            session,
            row,
            enabled=workflow.status == "active",
            next_fire_at=trigger.next_fire_at,
            last_fired_slot=trigger.last_fired_slot,
            seeded_after=trigger.seeded_after,
        )
        try:
            # A changed message clears the routine's built-ins opt-in, as any routine edit does.
            await routines_store.update_routine(
                session,
                row.id,
                tenant_id=tenant_id,
                name=_changed(back.name, row.name or routine_label(row)),
                cron_expr=_changed(back.cron, row.cron_expr),
                timezone_=_changed(back.timezone, row.timezone),
                catch_up_policy=None if back.catch_up == row.catch_up_policy else back.catch_up,
                trigger_message=_changed(back.instructions, row.trigger_message),
                edited_by=row.created_by_user_id,
            )
        except StoreError as err:
            raise Refused(str(err)) from err
        await store.set_status(
            session, tenant_id=tenant_id, workflow_id=workflow.id, status="paused"
        )
        return row.id
