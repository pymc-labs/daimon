"""Waits against the database: approvals, timers, event waits, inbox decisions and the one
decision path."""

from __future__ import annotations

import ast
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from daimon.core._models import Account
from daimon.core.stores import workflow_publish_requests as publish_requests
from daimon.core.stores import workflow_runs, workflow_waits, workflows
from daimon.core.workflows import inbox, runs, waits
from daimon.core.workflows.access import Who
from daimon.core.workflows.service import Env
from daimon.testing.workflows import NOW, World, make_world
from sqlalchemy.ext.asyncio import AsyncSession

FINISH: dict[str, Any] = {"f": {"kind": "finish", "output": "${{ steps.ok.output }}"}}


async def _person(db_session: AsyncSession, world: World) -> uuid.UUID:
    account = Account(tenant_id=world.tenant_id)
    db_session.add(account)
    await db_session.commit()
    return account.id


def _who(world: World, account_id: uuid.UUID) -> Who:
    return Who(tenant_id=world.tenant_id, account_id=account_id)


async def _park(world: World, run_id: uuid.UUID, key: str, now: datetime = NOW) -> uuid.UUID:
    """Execute the step with the wait executors; returns the wait it opened."""
    claimed = await world.claim(run_id, key, now)
    executors = {
        "approval": waits.ApprovalExecutor(world.sm, clock=lambda: now),
        "wait": waits.WaitExecutor(clock=lambda: now),
    }
    await runs.execute(world.sm, world.settings, executors, claimed, clock=lambda: now)
    step = (await world.steps(run_id))[key]
    assert step.status == "waiting", f"{key} waits: {step}"
    async with world.sm() as s:
        return (await workflow_waits.waits_of_step(s, step.id))[-1].id


async def _decide(
    world: World,
    who: uuid.UUID,
    wait_id: uuid.UUID,
    route: str,
    **kw: Any,
) -> waits.Decided:
    answer = waits.Answer(route=route, comment=kw.pop("comment", None), values=kw.pop("values", {}))
    return await waits.decide_wait(
        world.sm, world.settings, _who(world, who), wait_id, answer, via="web", now=NOW, **kw
    )


async def test_n_of_m_counts_distinct_people_and_refuses_self_approval(
    db_session: AsyncSession,
) -> None:
    nodes = {
        "ok": {"kind": "approval", "approvers": ["editors"], "min_approvals": 2, "next": "f"},
        **FINISH,
    }
    world = await make_world(db_session, nodes, "ok")
    a, b = await _person(db_session, world), await _person(db_session, world)
    async with world.sm.begin() as s:
        for editor in (a, b):
            await workflows.set_access(
                s,
                tenant_id=world.tenant_id,
                workflow_id=world.workflow_id,
                role="editor",
                granted_by=world.owner,
                account_id=editor,
            )
    run = await world.run(triggered_by={"kind": "person", "account_id": str(world.owner)})
    wait_id = await _park(world, run.id, "ok")

    with pytest.raises(waits.NotYours, match="someone else must approve"):
        await _decide(world, world.owner, wait_id, "approve")
    assert (await _decide(world, a, wait_id, "approve", idempotency_key="k1")).status == "counted"
    again = await _decide(world, a, wait_id, "approve", idempotency_key="k2")
    assert again.status == "duplicate", "one person counts once"
    assert (await _decide(world, b, wait_id, "approve")).status == "decided"

    done = await world.get(run.id)
    output = (await world.steps(run.id))["f"].output
    assert done.status == "succeeded" and isinstance(output, dict), f"the run goes on: {done}"
    assert (output["route"], output["by"], output["via"]) == ("approve", str(b), "web"), output
    with pytest.raises(waits.WaitGone):
        await _decide(world, a, wait_id, "reject", comment="late")
    types = await world.verified(run.id)
    assert types.count("wait.opened") == 1 and types.count("wait.decided") == 1, types


async def test_reject_says_why_and_fails_without_a_reject_route(db_session: AsyncSession) -> None:
    world = await make_world(db_session, {"ok": {"kind": "approval", "next": "f"}, **FINISH}, "ok")
    starter = await _person(db_session, world)
    run = await world.run(triggered_by={"kind": "person", "account_id": str(starter)})
    wait_id = await _park(world, run.id, "ok")
    with pytest.raises(waits.NotYours):
        await _decide(world, starter, wait_id, "approve")  # only the owner is named
    with pytest.raises(waits.BadAnswer, match="Add a comment"):
        await _decide(world, world.owner, wait_id, "reject")
    await _decide(world, world.owner, wait_id, "reject", comment="Not now")
    failed = await world.get(run.id)
    assert (failed.status, failed.failure_message) == ("failed", "Rejected"), failed


async def test_text_answers_become_the_output(db_session: AsyncSession) -> None:
    nodes = {"ok": {"kind": "approval", "answer": "text", "next": "f"}, **FINISH}
    world = await make_world(db_session, nodes, "ok")
    run = await world.run()
    wait_id = await _park(world, run.id, "ok")
    with pytest.raises(waits.BadAnswer):
        await _decide(world, world.owner, wait_id, "answer", values={"text": " "})
    await _decide(world, world.owner, wait_id, "answer", values={"text": "Ship it"})
    output = (await world.steps(run.id))["f"].output
    assert isinstance(output, dict) and output["text"] == "Ship it", output
    async with world.sm() as s:
        wait = await workflow_waits.get_wait(s, wait_id)
    assert wait is not None and wait.answer_text == "Ship it", "the text stays on the wait"


async def test_timers_arrive_and_approvals_time_out_to_their_route(
    db_session: AsyncSession,
) -> None:
    nodes = {
        "pause": {"kind": "wait", "for": "10m", "next": "ok"},
        "ok": {
            "kind": "approval",
            "timeout": {"after": "1h", "route": "late"},
            "routes": {"approve": "f", "late": "f"},
        },
        **FINISH,
    }
    world = await make_world(db_session, nodes, "pause")
    run = await world.run()
    await _park(world, run.id, "pause")
    later = NOW + timedelta(minutes=10)
    async with world.sm() as s:
        due = await workflow_runs.due_waits(s, kind="timer", now=later, limit=10)
    assert len(due) == 1, "the timer is due"
    await waits.on_due(world.sm, world.settings, due[0], now=later)
    assert (await world.steps(run.id))["pause"].status == "succeeded", "the timer arrived"

    await _park(world, run.id, "ok", later)
    expired = later + timedelta(hours=1)
    async with world.sm() as s:
        due = await workflow_runs.due_waits(s, kind="approval", now=expired, limit=10)
    await waits.on_due(world.sm, world.settings, due[0], now=expired)
    done = await world.get(run.id)
    assert done.status == "succeeded" and "wait.timed_out" in await world.verified(run.id), done


async def test_an_event_wait_takes_the_first_matching_buffered_event(
    db_session: AsyncSession,
) -> None:
    nodes = {
        "watch": {
            "kind": "wait",
            "event": {"source": "trigger:hook", "match": "${{ event.status == 'done' }}"},
            "next": "f",
        },
        "f": {"kind": "finish", "output": "${{ steps.watch.output }}"},
    }
    world = await make_world(db_session, nodes, "watch")
    run = await world.run()
    await _park(world, run.id, "watch")
    source = f"trigger:{world.workflow_id}:hook"
    async with world.sm.begin() as s:
        for status in ("busy", "done"):
            await runs.offer_event(s, world.tenant_id, source, None, {"status": status}, NOW)
    assert await waits.match_events(world.sm, world.settings, now=NOW) == 1
    assert (await world.steps(run.id))["f"].output == {"status": "done"}, "the match arrived"


async def test_the_inbox_lists_and_decides_for_the_addressee(db_session: AsyncSession) -> None:
    world = await make_world(db_session, {"ok": {"kind": "approval", "next": "f"}, **FINISH}, "ok")
    stranger = await _person(db_session, world)
    run = await world.run()
    wait_id = await _park(world, run.id, "ok")

    def env(account_id: uuid.UUID) -> Env:
        return Env(world.sm, world.settings, _who(world, account_id), NOW)

    assert await inbox.list_items(env(stranger)) == [], "others never see it"
    items = await inbox.list_items(env(world.owner))
    assert [(i.id, i.kind, i.decision) for i in items] == [(str(wait_id), "approval", None)]
    detail = await inbox.item(env(world.owner), wait_id)
    assert [(r.name, r.comment) for r in detail.routes] == [("reject", True), ("approve", False)]
    attention = await inbox.attention(env(world.owner))
    assert [a.kind for a in attention] == ["approval"], attention

    decided = await inbox.decide(env(world.owner), wait_id, inbox.Decide(route="approve"))
    assert decided.decision is not None and decided.decision.label == "Approved", decided
    with pytest.raises(inbox.service.Refused, match="Already decided"):
        await inbox.decide(env(world.owner), wait_id, inbox.Decide(route="approve"))


async def test_an_admin_approves_a_publish_request_from_the_inbox(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, {"ok": {"kind": "approval", "next": "f"}, **FINISH}, "ok")
    admin = await _person(db_session, world)
    async with world.sm.begin() as s:
        spec = await workflows.get_version(
            s, tenant_id=world.tenant_id, workflow_id=world.workflow_id, number=1
        )
        assert spec is not None
        request = await publish_requests.open_request(
            s,
            tenant_id=world.tenant_id,
            workflow_id=world.workflow_id,
            revision=1,
            spec=spec.spec,
            note="Ship",
            grants=[{"kind": "unattended_write", "node": "ok", "detail": "writes"}],
            requested_by=world.owner,
        )
    env = Env(world.sm, world.settings, Who(world.tenant_id, admin, admin=True), NOW)
    items = await inbox.list_items(env)
    assert [(i.kind, i.title) for i in items] == [("publish", "Publish v2 of T")], items
    with pytest.raises(inbox.service.Invalid, match="Add a comment"):
        await inbox.decide(env, request.id, inbox.Decide(route="reject"))
    decided = await inbox.decide(env, request.id, inbox.Decide(route="approve"))
    assert decided.decision is not None and decided.decision.route == "approve", decided
    async with world.sm() as s:
        versions = await workflows.list_versions(
            s, tenant_id=world.tenant_id, workflow_id=world.workflow_id
        )
    assert (versions[0].number, versions[0].published_by_account_id) == (2, world.owner)


# Every surface that decides a wait calls `waits.decide_wait`; step tools and agents never do.
DECIDERS = {"daimon/core/workflows/inbox.py"}
NEVER = ("step_tools.py", "agent_step.py", "workflow_step.py")


def test_only_decision_surfaces_call_decide_wait() -> None:
    root = Path(__file__).resolve().parents[4] / "packages"
    callers: set[str] = set()
    for path in root.rglob("*.py"):
        if "tests" in path.parts or path.name == "waits.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute | ast.Name) and "decide_wait" in (
                node.attr if isinstance(node, ast.Attribute) else node.id
            ):
                callers.add(path.as_posix())
    found = {c for c in callers if any(c.endswith(d) for d in DECIDERS)}
    assert found == {c for c in callers}, f"only listed surfaces decide waits: {callers}"
    assert not [c for c in callers if c.endswith(NEVER)], "agents never decide"
    assert found, "the inbox decides through it"
