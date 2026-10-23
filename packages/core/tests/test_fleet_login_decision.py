"""PR B: a run decides once, at its first spawn, which stored logins its workers get."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from daimon.core.fleet_credentials import LoginCandidate
from daimon.core.fleet_login_decision import (
    LOGIN_WAIT_S,
    apply_run_decision,
    await_run_decision,
    choose_login,
    expire_question,
    spawn_waits_for_logins,
)
from daimon.core.neutral_resources import WorkerPermissions
from daimon.core.stores import fleet
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.ids import Scope
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_fleet_orchestration import permissions

LATER = datetime.now(UTC) + timedelta(days=1)


def candidate(key: str, *, preapproved: bool = False, set_by: str | None = None) -> LoginCandidate:
    return LoginCandidate(
        key=key,
        label=key.removeprefix("env:"),
        version="v1",
        preapproved=preapproved,
        installed_by="admin",
        set_by=set_by,
    )


def with_candidates(parent: WorkerPermissions, *candidates: LoginCandidate) -> WorkerPermissions:
    return replace(
        parent,
        login_candidates=candidates,
        shared_versions=tuple((c.key, c.version) for c in candidates),
    )


# Pure: what a worker of the run gets


def test_undecided_shares_only_saved_shares_that_held_at_freeze():
    parent = with_candidates(
        permissions(), candidate("env:A", preapproved=True), candidate("env:B")
    )
    seen = apply_run_decision(parent, None)
    assert seen.shared_logins == {"env:A"}
    assert seen.shared_versions == (("env:A", "v1"),)
    assert seen.workers_run_without == ()


def test_decision_is_intersected_with_the_frozen_candidates():
    parent = with_candidates(permissions(), candidate("env:B"), candidate("env:C"))
    seen = apply_run_decision(
        parent, {"env:B": "share", "env:C": "no_answer", "env:NOT_FROZEN": "share"}
    )
    assert seen.shared_logins == {"env:B"}
    assert seen.shared_versions == (("env:B", "v1"),)
    assert seen.workers_run_without == ("C (no answer, so not shared)",)
    skipped = apply_run_decision(parent, {"env:B": "skip", "env:C": "skip"})
    assert skipped.shared_logins == frozenset()
    assert skipped.workers_run_without == (
        "B (skipped for this run)",
        "C (skipped for this run)",
    )


def test_no_candidates_leaves_the_root_unchanged():
    parent = permissions()
    assert apply_run_decision(parent, {"env:A": "share"}) is parent


def test_no_spawn_waits_while_sharing_is_not_live(monkeypatch):
    from daimon.core.turn import runtime_manifest

    parent = with_candidates(permissions(), candidate("env:B"))
    monkeypatch.setattr(runtime_manifest, "LOGIN_SHARING_LIVE", False)
    assert not spawn_waits_for_logins(parent)  # no card to answer: no dead 60 s
    monkeypatch.setattr(runtime_manifest, "LOGIN_SHARING_LIVE", True)
    assert spawn_waits_for_logins(parent)
    saved_only = with_candidates(permissions(), candidate("env:A", preapproved=True))
    assert not spawn_waits_for_logins(saved_only)  # nothing to ask


# Database: the question, the wait, the clicks


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def run_parent(
    db_engine, db_clean
) -> tuple[async_sessionmaker[AsyncSession], WorkerPermissions, uuid.UUID]:
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        other = await make_account(session, tenant=tenant)
    run = await fleet.create_run(
        sm,
        tenant_id=tenant.id,
        orchestrator_account_id=account.id,
        orchestrator_session_id="sess",
        orchestrator_binding_id="bind",
        platform="discord",
        channel_id="channel",
        thread_id="thread",
        operation_key="op",
        total_budget_usd=Decimal("1"),
        deadline_at=LATER,
        max_workers=3,
        created_by="mcp:orchestrator",
    )
    base = permissions()
    facts = replace(
        base.fleet,
        scope=Scope(
            tenant_id=str(tenant.id),
            account_id=str(account.id),
            principal_id="person",
            authorization_id="root-grant",
        ),
        account_id=account.id,
        run_id=run.id,
    )
    parent = with_candidates(
        # The run's limits carry the run's own deadline.
        replace(base, fleet=facts, limits=base.limits.model_copy(update={"deadline": LATER})),
        candidate("env:ADMIN_KEY"),
        candidate("env:MINE", set_by=str(account.id)),
    )
    return sm, parent, other.id


async def _question(sm, parent: WorkerPermissions) -> dict:
    found = await fleet.open_login_questions(sm, platform="discord")
    assert len(found) == 1 and found[0][1] == parent.fleet.run_id
    return found[0][2]


async def _click(
    sm, parent, question, *, actor=None, admin=False, row=0, choice="share", save=None
):
    return await choose_login(
        sm,
        tenant_id=uuid.UUID(parent.fleet.scope.tenant_id),
        run_id=parent.fleet.run_id,
        digest=question["digest"],
        actor_account_id=actor or parent.fleet.account_id,
        actor_platform_user_id="clicker" if actor else "person",
        is_admin=admin,
        row=row,
        choice=choice,
        now=datetime.now(UTC),
        save=save,
    )


async def _audited(sm) -> list:
    async with sm() as session:
        return list(
            (
                await session.execute(
                    text(
                        "SELECT outcome, operation, reason, account_id, platform_user_id "
                        "FROM security_audit_events WHERE tool_name = 'fleet_logins'"
                    )
                )
            ).all()
        )


async def _waiting(sm, parent, clock: Clock) -> tuple[asyncio.Task, dict]:
    """A spawn that is waiting for the answer, and the question it opened."""
    gate = asyncio.Event()

    async def sleep(_: float) -> None:
        gate.set()
        await asyncio.sleep(0.01)

    task = asyncio.create_task(await_run_decision(sm, parent, now=clock, sleep=sleep))
    await asyncio.wait_for(gate.wait(), 5)
    return task, await _question(sm, parent)


async def test_no_answer_records_no_answer_for_every_row_after_the_wait(run_parent):
    sm, parent, _ = run_parent
    clock = Clock()
    task, question = await _waiting(sm, parent, clock)
    assert question["state"] == "open" and question["requester_platform_user_id"] == "person"
    assert [row["label"] for row in question["rows"]] == ["ADMIN_KEY", "MINE"]
    assert not task.done()  # no worker before the decision or the timeout
    clock.now += timedelta(seconds=LOGIN_WAIT_S + 1)
    decision = await asyncio.wait_for(task, 5)
    assert decision == {"env:ADMIN_KEY": "no_answer", "env:MINE": "no_answer"}
    assert apply_run_decision(parent, decision).shared_logins == frozenset()
    assert await fleet.open_login_questions(sm, platform="discord") == ()


async def test_the_requesters_answers_decide_the_run_and_end_the_wait(run_parent):
    sm, parent, _ = run_parent
    task, question = await _waiting(sm, parent, Clock())
    mine = await _click(sm, parent, question, row=1, choice="share")
    assert mine.refused is None and not mine.decided  # one row still open
    rest = await _click(sm, parent, question, row=0, choice="skip")
    assert rest.decided
    decision = await asyncio.wait_for(task, 5)
    assert decision == {"env:ADMIN_KEY": "skip", "env:MINE": "share"}
    assert apply_run_decision(parent, decision).shared_logins == {"env:MINE"}
    # Decided once: a later click changes nothing.
    late = await _click(sm, parent, question, row=0, choice="share", admin=True)
    assert late.refused == "already_decided"
    assert (
        await fleet.run_login_decision(
            sm, tenant_id=uuid.UUID(parent.fleet.scope.tenant_id), run_id=parent.fleet.run_id
        )
        == decision
    )
    audited = await _audited(sm)
    assert sorted((row.outcome, row.operation) for row in audited) == [
        ("allowed", "share"),
        ("allowed", "skip"),
        ("denied", "share"),
    ]
    assert all("login=env:" in row.reason for row in audited if row.outcome == "allowed")
    denied = next(row for row in audited if row.outcome == "denied")
    assert "refused=already_decided" in denied.reason and "role=admin" in denied.reason


async def test_clicks_are_rechecked_per_row_at_click_time(run_parent):
    sm, parent, other = run_parent
    task, question = await _waiting(sm, parent, Clock())
    # A member can't share an admin-installed key, even for this run only.
    assert (await _click(sm, parent, question, row=0)).refused == "share_not_allowed"
    # Nor can "share all" slip it past the per-row check.
    assert (await _click(sm, parent, question, row="all")).refused == "share_not_allowed"
    # Only the run's requester answers; only on the card for these exact rows.
    assert (await _click(sm, parent, question, actor=other)).refused == "not_requester"
    stale = {**question, "digest": "0" * 64}
    assert (await _click(sm, parent, stale)).refused == "stale_card"
    assert (await _click(sm, parent, question, row=7)).refused == "unknown_row"
    # Every refusal is audited with its reason, as the CLICKER (not the requester).
    denied = [row for row in await _audited(sm) if row.outcome == "denied"]
    assert len(denied) == 5
    stranger = next(row for row in denied if "refused=not_requester" in row.reason)
    assert (stranger.account_id, stranger.platform_user_id) == (other, "clicker")
    assert any("refused=share_not_allowed; login=env:ADMIN_KEY" in row.reason for row in denied)
    # An admin may share it.
    assert (await _click(sm, parent, question, row=0, admin=True)).refused is None
    task.cancel()


async def test_skip_all_decides_at_once_and_saves_only_for_an_admin(run_parent):
    sm, parent, _ = run_parent
    task, question = await _waiting(sm, parent, Clock())
    saved: list[dict] = []

    async def save(choices):
        saved.append(dict(choices))

    member = await _click(sm, parent, question, row=1, choice="share_once", save=save)
    assert member.refused is None and not member.saved and saved == []
    admin = await _click(sm, parent, question, row="all", choice="skip", admin=True, save=save)
    assert admin.decided and admin.saved
    assert saved == [{"env:ADMIN_KEY": "skip"}]
    assert await asyncio.wait_for(task, 5) == {"env:ADMIN_KEY": "skip", "env:MINE": "share"}


async def test_a_failed_save_never_changes_the_runs_decision(run_parent):
    sm, parent, _ = run_parent
    task, question = await _waiting(sm, parent, Clock())

    async def save(choices):
        raise RuntimeError("config write failed")

    answer = await _click(sm, parent, question, row="all", choice="skip", admin=True, save=save)
    assert answer.decided and not answer.saved
    assert await asyncio.wait_for(task, 5) == {"env:ADMIN_KEY": "skip", "env:MINE": "skip"}


async def test_a_cancelled_spawn_stops_waiting_and_decides_nothing(run_parent):
    sm, parent, _ = run_parent
    task, question = await _waiting(sm, parent, Clock())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    tenant_id = uuid.UUID(parent.fleet.scope.tenant_id)
    assert (
        await fleet.run_login_decision(sm, tenant_id=tenant_id, run_id=parent.fleet.run_id) is None
    )
    # Not yet past its deadline: nothing to expire; a click past it is refused.
    now = datetime.now(UTC)
    assert (
        await expire_question(sm, tenant_id=tenant_id, run_id=parent.fleet.run_id, now=now) is None
    )
    late = await choose_login(
        sm,
        tenant_id=tenant_id,
        run_id=parent.fleet.run_id,
        digest=question["digest"],
        actor_account_id=parent.fleet.account_id,
        actor_platform_user_id="person",
        is_admin=True,
        row=0,
        choice="share",
        now=now + timedelta(seconds=LOGIN_WAIT_S + 1),
    )
    assert late.refused == "expired"


async def test_parallel_spawns_share_one_question(run_parent):
    sm, parent, _ = run_parent
    clock = Clock()
    first, question = await _waiting(sm, parent, clock)
    second, again = await _waiting(sm, parent, clock)
    assert again["digest"] == question["digest"]
    clock.now += timedelta(seconds=LOGIN_WAIT_S + 1)
    assert await asyncio.wait_for(first, 5) == await asyncio.wait_for(second, 5)


async def test_nothing_to_ask_returns_without_opening_a_question(run_parent):
    sm, parent, _ = run_parent
    saved_only = with_candidates(parent, candidate("env:A", preapproved=True))
    assert await await_run_decision(sm, saved_only) is None
    assert await fleet.open_login_questions(sm, platform="discord") == ()


async def test_the_question_never_outlives_the_run_deadline(run_parent):
    sm, parent, _ = run_parent
    soon = datetime.now(UTC) + timedelta(seconds=10)
    short = replace(parent, limits=parent.limits.model_copy(update={"deadline": soon}))
    task, question = await _waiting(sm, short, Clock())
    assert datetime.fromisoformat(question["expires_at"]) == soon
    task.cancel()


async def test_a_run_that_ends_during_the_wait_stops_it(run_parent):
    sm, parent, _ = run_parent
    task, _ = await _waiting(sm, parent, Clock())
    tenant_id = uuid.UUID(parent.fleet.scope.tenant_id)
    run, _ = await fleet.run_status(sm, tenant_id=tenant_id, run_id=parent.fleet.run_id)
    await fleet.set_run_status(
        sm, tenant_id=tenant_id, run_id=run.id, expected_version=run.version, status="cancelled"
    )
    assert await asyncio.wait_for(task, 5) is None
    assert await fleet.run_login_decision(sm, tenant_id=tenant_id, run_id=run.id) is None


async def test_truly_concurrent_clicks_decide_exactly_once(run_parent):
    sm, parent, _ = run_parent
    task, question = await _waiting(sm, parent, Clock())
    # Two "skip all" clicks race on separate connections: one decides, one is refused.
    first, second = await asyncio.gather(
        _click(sm, parent, question, row="all", choice="skip"),
        _click(sm, parent, question, row="all", choice="skip"),
    )
    assert sorted([first.decided, second.decided]) == [False, True]
    assert {first.refused, second.refused} == {None, "already_decided"}
    assert await asyncio.wait_for(task, 5) == {"env:ADMIN_KEY": "skip", "env:MINE": "skip"}


async def test_concurrent_clicks_on_different_rows_both_land(run_parent):
    sm, parent, _ = run_parent
    task, question = await _waiting(sm, parent, Clock())
    answers = await asyncio.gather(
        _click(sm, parent, question, row=0, choice="skip"),
        _click(sm, parent, question, row=1, choice="share"),
    )
    assert [a.refused for a in answers] == [None, None]
    assert sum(a.decided for a in answers) == 1
    assert await asyncio.wait_for(task, 5) == {"env:ADMIN_KEY": "skip", "env:MINE": "share"}


def test_login_candidates_survive_the_frozen_root_json_round_trip():
    from daimon.core.turn.runtime_manifest import _FrozenRoot  # pyright: ignore[reportPrivateUsage]
    from pydantic import TypeAdapter

    field = TypeAdapter(_FrozenRoot.model_fields["permissions"].annotation)
    parent = with_candidates(
        permissions(), candidate("env:A", preapproved=True), candidate("env:B", set_by="acct")
    )
    stored = json.loads(field.dump_json(parent))
    restored = field.validate_json(json.dumps(stored))
    assert restored.login_candidates == parent.login_candidates
    assert restored == parent
