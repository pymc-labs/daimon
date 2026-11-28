"""An agent step signs its workflow as a routine at the same place signs the routine: the same
room, the run-as person as requester, and a private step their DM with room `dm`."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import uuid
from typing import Any

from daimon.core.audience_facts import RoomFacts
from daimon.core.lead_intake_execution import lane_grant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_identity import RelayBinding, routine_destination_key, workflow_revision
from daimon.core.stores import identity, workflow_runs
from daimon.core.stores.routines import create_routine
from daimon.core.workflows.place import bound_step
from daimon.testing.workflows import World, make_world
from sqlalchemy.ext.asyncio import AsyncSession

from .test_identity_relay import OWNER, SERVER, _Person, _token, _tool_call
from .test_identity_relay_room import _facts, _signed, _sql

DM_SERVER = SERVER.model_copy(
    update={"routine_claims": True, "dm_routine_claims": True, "requester_meta": True}
)
WS = "discord:guild-1"


async def _running(db_session: AsyncSession, step: dict[str, Any]) -> tuple[World, uuid.UUID, str]:
    """A live run whose one agent step is running, its run-as person linked on Discord; and
    the lane it runs in."""
    world = await make_world(
        db_session,
        {"a": {"kind": "agent", "agent": "helper", "instructions": "go", **step, "next": "f"},
         "f": {"kind": "finish"}},
        "a", home="555", workspace_id="guild-1",
    )  # fmt: skip
    run = await world.run()
    async with world.sm.begin() as s:
        await identity.create_platform_principal(
            s, tenant_id=world.tenant_id, platform="discord", external_id=OWNER,
            account_id=world.owner,
        )  # fmt: skip
        await workflow_runs.save_steps(
            s, tenant_id=world.tenant_id, run_id=run.id,
            steps={("a", 1): {"status": "running"}},
        )  # fmt: skip
        await workflow_runs.update_run(s, run.id, status="running")
    lane = (await world.steps(run.id))["a"].lane
    assert lane is not None
    return world, run.id, lane


def _person(world: World) -> _Person:
    agent = derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id="agent_a")
    return _Person(world.tenant_id, world.owner, agent)


async def test_a_step_at_a_channel_signs_its_workflow_as_the_routine_there_would(
    db_session: AsyncSession,
) -> None:
    world, run_id, lane = await _running(db_session, {"at": "home"})
    sm, person = world.sm, _person(world)
    async with sm.begin() as s:
        row = await create_routine(
            s, tenant_id=world.tenant_id, created_by_user_id=OWNER, agent_id="agent_a",
            agent_name="helper", cron_expr="0 9 * * *", timezone_="UTC", trigger_message="go",
            destination_kind="channel", destination_id="555",
        )  # fmt: skip
    await _facts(sm, person, {"555": RoomFacts(contained=False, narrow=False)})
    body = _tool_call(requester_platform_user_id="666", requester_kind="person")
    fire = uuid.uuid4()
    await _sql(
        sm,
        "INSERT INTO lead_intake_executions (tenant_id, id, routine_id, expires_at)"
        " VALUES (:tenant, :id, :routine, now() + interval '5 minutes')",
        tenant=world.tenant_id, id=fire, routine=row.id,
    )  # fmt: skip
    routine = RelayBinding(
        "helper", routine_id=row.id, owner=OWNER, destination=routine_destination_key(row),
        execution=fire,
    )  # fmt: skip
    claims, stamp = await _signed(sm, await _token(sm, person, routine), DM_SERVER, body)
    step = RelayBinding(
        "helper", workflow_run_id=run_id, execution=lane_grant(run_id, lane), owner=OWNER
    )
    as_step = await _signed(sm, await _token(sm, person, step), DM_SERVER, body)
    async with sm() as s:
        bound = await bound_step(
            s,
            tenant_id=world.tenant_id,
            run_id=run_id,
            execution_id=lane_grant(run_id, lane),
            agent_name="helper",
        )
    assert bound is not None
    named = {
        "workflow": str(world.workflow_id),
        "workflow_rev": workflow_revision(world.tenant_id, world.workflow_id, 1, workspace=WS),
        "workflow_name": bound.workflow.name,
    }
    unnamed = {k: v for k, v in claims.items() if k not in ("routine", "rev", "routine_name")}
    assert claims["room"] == "team" and claims["routine"] == str(row.id), claims
    assert claims["owner"] == f"discord:{OWNER}", "the run-as person, as a routine's owner"
    assert as_step == ({**unnamed, **named}, stamp), "the routine's room and stamp, its workflow"


async def test_a_private_step_signs_its_run_as_persons_dm(db_session: AsyncSession) -> None:
    world, run_id, lane = await _running(db_session, {"memory": "personal"})
    sm, person = world.sm, _person(world)
    body = _tool_call(requester_platform_user_id="666", requester_kind="person")
    grant = lane_grant(run_id, lane)
    step = RelayBinding(
        "helper", workflow_run_id=run_id, execution=grant, owner=OWNER, dm_channel="777"
    )
    claims, _ = await _signed(sm, await _token(sm, person, step), DM_SERVER, body)
    assert (claims["channel"], claims["room"], claims["sub"]) == ("777", "dm", f"discord:{OWNER}")
    assert claims["workflow"] == str(world.workflow_id) and "routine" not in claims, claims
    other = RelayBinding(
        "helper", workflow_run_id=run_id, execution=grant, owner="999", dm_channel="777"
    )
    unbound, _ = await _signed(sm, await _token(sm, person, other), DM_SERVER, body)
    assert "workflow" not in unbound and "room" not in unbound, "another person signs nothing"


async def test_each_running_step_and_item_signs_its_own_lane(db_session: AsyncSession) -> None:
    """A step beside a For each item running: each lane's credential is bound to the step or
    item running in it (`lane_grant`), and a lane with nothing running signs nothing."""
    world = await make_world(
        db_session,
        {"a": {"kind": "agent", "agent": "helper", "instructions": "go", "at": "home", "next": "f"},
         "e": {"kind": "for_each", "items": "${{ [1] }}",
               "do": {"kind": "agent", "agent": "helper", "instructions": "go"}, "next": "f"},
         "f": {"kind": "finish"}},
        "a", home="555", workspace_id="guild-1",
    )  # fmt: skip
    run = await world.run()
    step_lane = (await world.steps(run.id))["a"].lane
    assert step_lane is not None
    item_lane = "main:helper@e[0]"
    async with world.sm.begin() as s:
        await identity.create_platform_principal(
            s, tenant_id=world.tenant_id, platform="discord", external_id=OWNER,
            account_id=world.owner,
        )  # fmt: skip
        running: dict[tuple[str, int], Any] = {
            ("a", 1): {"status": "running"},
            ("e", 1): {"status": "running"},
        }
        await workflow_runs.save_steps(s, tenant_id=world.tenant_id, run_id=run.id, steps=running)
        parent = next(x for x in await workflow_runs.list_steps(s, run.id) if x.node_id == "e")
        await workflow_runs.save_items(
            s, parent=parent, items={0: {"status": "running", "lane": item_lane}}
        )
        await workflow_runs.update_run(s, run.id, status="running")

    async def bound(lane: str) -> str | None | bool:
        async with world.sm() as s:
            got = await bound_step(
                s, tenant_id=world.tenant_id, run_id=run.id, execution_id=lane_grant(run.id, lane)
            )
        return False if got is None else got.at

    assert await bound(step_lane) == "home", "the step's own lane binds the step"
    assert await bound(item_lane) is None, "the item's lane binds the private item"
    assert await bound("other:helper") is False, "a lane with nothing running binds nothing"
    body = _tool_call(requester_platform_user_id="666", requester_kind="person")

    async def signed(lane: str) -> tuple[object, object]:
        step = RelayBinding(
            "helper", workflow_run_id=run.id, execution=lane_grant(run.id, lane), owner=OWNER
        )
        claims, _ = await _signed(
            world.sm, await _token(world.sm, _person(world), step), DM_SERVER, body
        )
        return claims.get("workflow"), claims.get("workflow_rev")

    rev = workflow_revision(world.tenant_id, world.workflow_id, 1, workspace=WS)
    flow = str(world.workflow_id)
    assert [await signed(lane) for lane in (step_lane, "other:helper")] == [
        (flow, rev), (None, None),
    ], "the workflow's revision, not the step's"  # fmt: skip
    await _sql(world.sm, "UPDATE workflow_runs SET mode = 'test' WHERE id = :id", id=run.id)
    assert await signed(step_lane) == (flow, None), "no approval covers a test run"
