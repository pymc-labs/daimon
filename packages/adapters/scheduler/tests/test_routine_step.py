"""A workflow's routine step fires through the routine fire core, on workflow state only."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from anthropic import AsyncAnthropic
from daimon.adapters.scheduler.main import _fire_deps  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.scheduler.workflows import RoutineStep
from daimon.core.delivery import DeliveryOutcome
from daimon.core.fire.delivery import DeliveryJob
from daimon.core.fire.run import Dispatch, fire_turn
from daimon.core.fire.store import FireResult
from daimon.core.fire.subject import Subject
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import workflow_deliveries
from daimon.core.stores.accounts import get_account
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.workflow_deliveries import DeliveryRow
from daimon.core.workflows import runs
from daimon.core.workflows.delivery import result_poster
from daimon.core.workflows.fire_store import WorkflowFireStore
from daimon.testing.factories import make_platform_principal
from daimon.testing.workflows import NOW, World, make_world
from sqlalchemy.ext.asyncio import AsyncSession

ROUTINE: dict[str, Any] = {
    "r": {
        "kind": "routine",
        "agent": "helper",
        "prompt": "Sum up the day",
        "to": {"kind": "channel", "id": "555"},
        "next": "f",
    },
    "f": {"kind": "finish"},
}
Turn = Callable[[Subject, WorkflowFireStore], Awaitable[None]]


async def _world(db_session: AsyncSession, *, owner: bool = True) -> World:
    world = await make_world(
        db_session,
        ROUTINE,
        "r",
        home="555",
        spec_fields={"concurrency": {"limit": 1, "on_conflict": "skip"}},
    )
    if owner:
        async with world.sm.begin() as s:
            tenant, account = (
                await get_tenant(s, world.tenant_id),
                await get_account(s, world.owner),
            )
            await make_platform_principal(
                s, platform="discord", external_id="u1", tenant=tenant, account=account
            )
    return world


async def _agent(_tenant: uuid.UUID, _agent: str | None) -> str:
    return "ag"


async def _step(world: World, turn: Turn) -> tuple[str, str | None, list[DeliveryRow]]:
    run = await world.run()
    claimed = await world.claim(run.id, "r")
    dispatch = Dispatch(world.sm, asyncio.Semaphore(1), 60)
    step = RoutineStep(world.sm, dispatch, _agent, turn)
    await runs.execute(world.sm, world.settings, {"routine": step}, claimed, clock=lambda: NOW)
    row = (await world.steps(run.id))["r"]
    async with world.sm.begin() as s:
        claimed = await workflow_deliveries.claim_deliveries(
            s, platform="discord", owner="d1", now=NOW, lease=timedelta(minutes=5)
        )
    posts = [p for p in claimed if p.kind == "result"]
    return row.status, row.error_message, posts


async def test_routine_step_when_it_replies_then_posts_through_the_routine_poster(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)

    async def reply(subject: Subject, store: WorkflowFireStore) -> None:
        assert (subject.created_by_user_id, subject.agent_id) == ("u1", "ag"), f"{subject}"
        await store.finish(FireResult("done", "The day in short", delivery="pending"))

    status, _, posts = await _step(world, reply)
    assert status == "succeeded" and len(posts) == 1, "the step succeeds with one result post"
    jobs: list[DeliveryJob] = []

    async def post(job: DeliveryJob) -> DeliveryOutcome:
        jobs.append(job)
        return DeliveryOutcome(status="delivered")

    async def other(row: DeliveryRow) -> DeliveryOutcome:
        raise AssertionError(f"a result never goes to the workflow poster: {row.kind}")

    outcome = await result_poster(world.sm, post, other, platform="discord")(posts[0])
    (job,) = jobs
    assert outcome.status == "delivered", f"{outcome}"
    assert (job.source, job.destination_id, job.delivery_payload, job.workflow_run_id) == (
        "workflow",
        "555",
        "The day in short",
        posts[0].run_id,
    ), f"the routine poster gets the run's job: {job}"


async def test_routine_step_when_the_turn_raises_then_fails_and_tells_the_channel(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)

    async def boom(_subject: Subject, _store: WorkflowFireStore) -> None:
        raise RuntimeError("boom")

    status, error, posts = await _step(world, boom)
    assert (status, error) == ("failed", "RuntimeError: boom"), "the fire's error fails the step"
    assert [p.note for p in posts] == ["failure_notice"], "the first failure tells its channel"


async def test_routine_step_when_the_owner_has_no_account_then_refuses(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session, owner=False)
    async with AsyncAnthropic(api_key="offline") as client:
        deps = _fire_deps(
            client=client,
            sm=world.sm,
            settings=world.settings,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
        )
        status, error, posts = await _step(world, lambda s, st: fire_turn(s, st, deps()))
    assert status == "failed" and error and "no owner" in error, f"refused: {error}"
    assert posts == [], "a refusal posts nothing"
