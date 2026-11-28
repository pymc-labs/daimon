"""The run lists and the inbox filter by viewer in SQL and page with a cursor."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from daimon.core._models import Account, WorkflowRun, WorkflowWait
from daimon.core.stores import workflow_reads as reads
from daimon.core.stores import workflow_waits
from daimon.core.stores import workflows as workflows_store
from daimon.core.workflows import inbox, runs, service, waits
from daimon.core.workflows.access import Who
from daimon.core.workflows.service import Env
from daimon.testing.workflows import NOW, World, make_world
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from .test_waits import FINISH

OK: dict[str, Any] = {"ok": {"kind": "approval", "next": "f"}, **FINISH}


async def _person(db_session: AsyncSession, world: World) -> uuid.UUID:
    account = Account(tenant_id=world.tenant_id)
    db_session.add(account)
    await db_session.commit()
    return account.id


def _env(world: World, account_id: uuid.UUID) -> Env:
    return Env(world.sm, world.settings, Who(world.tenant_id, account_id), NOW)


async def _ask(world: World) -> uuid.UUID:
    """A new run parked on its approval; the wait it opened."""
    run = await world.run()
    claimed = await world.claim(run.id, "ok")
    executors = {"approval": waits.ApprovalExecutor(world.sm, clock=lambda: NOW)}
    await runs.execute(world.sm, world.settings, executors, claimed, clock=lambda: NOW)
    step = (await world.steps(run.id))["ok"]
    async with world.sm() as s:
        return (await workflow_waits.waits_of_step(s, step.id))[-1].id


async def _share_as_runner(world: World, account_id: uuid.UUID) -> None:
    async with world.sm.begin() as s:
        await workflows_store.update_settings(
            s, tenant_id=world.tenant_id, workflow_id=world.workflow_id, visibility="shared"
        )
        await workflows_store.set_access(
            s,
            tenant_id=world.tenant_id,
            workflow_id=world.workflow_id,
            role="runner",
            granted_by=world.owner,
            account_id=account_id,
        )


async def test_runs_page_when_newer_runs_are_others_then_a_runner_pages_to_their_own(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, {"f": {"kind": "finish"}}, "f")
    runner = await _person(db_session, world)
    await _share_as_runner(world, runner)
    mine = {"kind": "person", "account_id": str(runner)}
    # Oldest first: two of the runner's, then three of the owner's.
    made = [await world.run(triggered_by=mine) for _ in range(2)]
    made += [await world.run() for _ in range(3)]
    async with world.sm.begin() as s:
        for i, run in enumerate(made):
            await s.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run.id)
                .values(created_at=NOW + timedelta(minutes=i))
            )
    numbers = [r.number for r in made]

    first = await service.runs_page(_env(world, runner), limit=1)
    assert [r.number for r in first.rows] == [numbers[1]], "the newest of their own runs"
    assert first.next is not None, "another follows"
    second = await service.runs_page(_env(world, runner), limit=1, before=first.next)
    assert [r.number for r in second.rows] == [numbers[0]], "then the older one"
    assert second.next is None, "and that is all of them"

    seen: list[int] = []
    page = await service.runs_page(_env(world, world.owner), limit=2)
    while True:
        seen += [r.number for r in page.rows]
        if page.next is None:
            break
        page = await service.runs_page(_env(world, world.owner), limit=2, before=page.next)
    assert seen == numbers[::-1], "the owner reads every run once, newest first"


async def test_board_runs_when_newer_runs_ended_then_open_runs_stay_on_the_board(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, {"f": {"kind": "finish"}}, "f")
    made = [await world.run() for _ in range(4)]
    async with world.sm.begin() as s:
        for i, run in enumerate(made):
            await s.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run.id)
                .values(
                    created_at=NOW + timedelta(minutes=i),
                    status="running" if i == 0 else "succeeded",
                )
            )
    scope = reads.RunScope(world.owner, frozenset({world.workflow_id}))
    async with world.sm() as s:
        shown = await reads.board_runs(
            s, tenant_id=world.tenant_id, workflow_id=world.workflow_id, scope=scope, limit=2
        )
    assert [r.number for r in shown] == [made[3].number, made[2].number, made[0].number], (
        "the open run and the two newest ended ones"
    )


async def test_inbox_page_when_newer_asks_are_others_then_the_callers_page_to_their_own(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, OK, "ok")
    stranger = await _person(db_session, world)
    asks = [await _ask(world) for _ in range(4)]
    async with world.sm.begin() as s:
        for i, wait_id in enumerate(asks):
            values: dict[str, Any] = {"opened_at": NOW + timedelta(minutes=i)}
            if i >= 2:
                values["addressees"] = [str(stranger)]
            await s.execute(update(WorkflowWait).where(WorkflowWait.id == wait_id).values(**values))

    owner = _env(world, world.owner)
    first = await inbox.items_page(owner, limit=1)
    assert [i.id for i in first.rows] == [str(asks[1])], "the newest ask that is theirs"
    assert first.next is not None, "another follows"
    second = await inbox.items_page(owner, limit=1, before=first.next)
    assert [i.id for i in second.rows] == [str(asks[0])], "then the older one"
    assert second.next is None, "and that is all of them"
    theirs = await inbox.items_page(_env(world, stranger))
    assert [i.id for i in theirs.rows] == [str(asks[3]), str(asks[2])], "the stranger's own"
    attention = await inbox.attention(owner)
    assert [a.kind for a in attention] == ["approval", "approval"], "attention lists only theirs"
    assert await inbox.open_count(owner) == 2, "the badge counts their open asks, not a page"
    assert await inbox.open_count(_env(world, stranger)) == 2, "and each person's own"
