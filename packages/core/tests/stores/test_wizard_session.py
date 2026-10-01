"""Integration tests for the wizard_session store — real Postgres.

Covers the read/write lifecycle, the two single-statement gates
(`update_wizard_state`, `try_claim_submit`), the bounded expiry sweep, and
the platform-user-scoped erasure helpers.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

from daimon.core.stores import wizard_session as store
from daimon.core.stores.domain import WizardSessionRow
from daimon.testing.db import build_test_engine
from daimon.testing.factories import make_account, make_tenant, make_wizard_session
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool


async def test_create_wizard_session_returns_pydantic_row_and_get_matches(
    db_session: AsyncSession,
) -> None:
    created = await make_wizard_session(db_session)

    assert isinstance(created, WizardSessionRow), "store must return Pydantic, not ORM"

    fetched = await store.get_wizard_session(db_session, short_id=created.id)

    assert fetched == created


async def test_get_wizard_session_returns_none_for_unknown_id(
    db_session: AsyncSession,
) -> None:
    fetched = await store.get_wizard_session(db_session, short_id="doesnotexist")

    assert fetched is None


async def test_update_wizard_state_on_open_unexpired_row_returns_one_and_persists(
    db_session: AsyncSession,
) -> None:
    created = await make_wizard_session(db_session, answers={}, current_step=0)
    now = created.updated_at + timedelta(seconds=5)

    rowcount = await store.update_wizard_state(
        db_session,
        short_id=created.id,
        answers={"choice": ["a"]},
        current_step=1,
        expected_updated_at=created.updated_at,
        now=now,
    )

    assert rowcount == 1
    refetched = await store.get_wizard_session(db_session, short_id=created.id)
    assert refetched is not None
    assert refetched.answers == {"choice": ["a"]}
    assert refetched.current_step == 1
    assert refetched.updated_at == now
    assert refetched.updated_at > created.updated_at


async def test_update_wizard_state_returns_zero_for_submitted_row(
    db_session: AsyncSession,
) -> None:
    created = await make_wizard_session(db_session, status="submitted")

    rowcount = await store.update_wizard_state(
        db_session,
        short_id=created.id,
        answers={"choice": ["a"]},
        current_step=1,
        expected_updated_at=created.updated_at,
        now=datetime.now(UTC),
    )

    assert rowcount == 0
    refetched = await store.get_wizard_session(db_session, short_id=created.id)
    assert refetched is not None
    assert refetched.answers == created.answers, "a no-op update must leave answers untouched"
    assert refetched.current_step == created.current_step


async def test_update_wizard_state_returns_zero_for_expired_row(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    created = await make_wizard_session(
        db_session,
        now=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )

    rowcount = await store.update_wizard_state(
        db_session,
        short_id=created.id,
        answers={"choice": ["a"]},
        current_step=1,
        expected_updated_at=created.updated_at,
        now=now,
    )

    assert rowcount == 0
    refetched = await store.get_wizard_session(db_session, short_id=created.id)
    assert refetched is not None
    assert refetched.answers == created.answers


async def test_update_wizard_state_returns_zero_for_deleted_row(
    db_session: AsyncSession,
) -> None:
    created = await make_wizard_session(db_session, requester_platform_user_id="del-me")
    tenant_id = created.tenant_id
    deleted = await store.delete_wizard_sessions_for_platform_user(
        db_session, platform_user_id="del-me", tenant_id=tenant_id
    )
    assert deleted == 1

    rowcount = await store.update_wizard_state(
        db_session,
        short_id=created.id,
        answers={"choice": ["a"]},
        current_step=1,
        expected_updated_at=created.updated_at,
        now=datetime.now(UTC),
    )

    assert rowcount == 0, "a tap on a purged row must write nothing"
    assert (await store.get_wizard_session(db_session, short_id=created.id)) is None


async def test_update_wizard_state_returns_zero_when_the_row_changed_since_it_was_read(
    db_session: AsyncSession,
) -> None:
    """Two taps computed from the same base row: the second must lose rather
    than overwrite the first's answer with state that predates it."""
    created = await make_wizard_session(db_session, answers={}, current_step=0)
    first_now = created.updated_at + timedelta(seconds=1)
    second_now = created.updated_at + timedelta(seconds=2)

    first = await store.update_wizard_state(
        db_session,
        short_id=created.id,
        answers={"choice": ["a"]},
        current_step=1,
        expected_updated_at=created.updated_at,
        now=first_now,
    )
    second = await store.update_wizard_state(
        db_session,
        short_id=created.id,
        answers={},
        current_step=1,
        expected_updated_at=created.updated_at,
        now=second_now,
    )

    assert first == 1, "the tap that read the current row must win"
    assert second == 0, "a tap computed from a superseded row must write nothing"
    refetched = await store.get_wizard_session(db_session, short_id=created.id)
    assert refetched is not None
    assert refetched.answers == {"choice": ["a"]}, (
        "the losing tap must not erase the answer the winning tap recorded"
    )
    assert refetched.updated_at == first_now


async def test_try_claim_submit_returns_row_on_first_call_and_none_on_second(
    db_session: AsyncSession,
) -> None:
    created = await make_wizard_session(db_session)
    now = datetime.now(UTC)

    first = await store.try_claim_submit(
        db_session,
        short_id=created.id,
        answers={"choice": ["a"]},
        current_step=1,
        expected_updated_at=created.updated_at,
        now=now,
    )
    second = await store.try_claim_submit(
        db_session,
        short_id=created.id,
        answers={"choice": ["a"]},
        current_step=1,
        expected_updated_at=first.updated_at if first is not None else created.updated_at,
        now=now + timedelta(seconds=1),
    )

    assert first is not None
    assert first.status == "submitted"
    assert second is None, "a second claim of an already-submitted row must return None"


async def test_try_claim_submit_does_not_overwrite_a_newer_edit(db_session: AsyncSession) -> None:
    """A submit callback built from an old read must not replace a later edit."""
    created = await make_wizard_session(db_session, answers={"choice": ["old"]}, current_step=1)
    latest_answers = {"choice": ["new"]}
    # Even equal clock readings need a fresh optimistic-concurrency token.
    edited_at = created.updated_at
    edit_count = await store.update_wizard_state(
        db_session,
        short_id=created.id,
        answers=latest_answers,
        current_step=1,
        expected_updated_at=created.updated_at,
        now=edited_at,
    )
    assert edit_count == 1

    claimed = await store.try_claim_submit(
        db_session,
        short_id=created.id,
        answers=created.answers,
        current_step=created.current_step,
        expected_updated_at=created.updated_at,
        now=edited_at + timedelta(seconds=1),
    )

    assert claimed is None, "a submit from before an edit must lose its stale claim"
    current = await store.get_wizard_session(db_session, short_id=created.id)
    assert current is not None
    assert current.status == "open", "the stale submit must not claim the edited form"
    assert current.answers == latest_answers, "the stale submit must preserve the newer edit"
    assert current.updated_at > created.updated_at, "every edit must advance the concurrency token"


async def test_expiry_sweep_does_not_abandon_a_pre_expiry_submit_in_flight(
    db_session: AsyncSession,
    db_schema: str,
) -> None:
    """The sweep's id snapshot must not overwrite a submit that commits first."""
    expires_at = datetime.now(UTC) + timedelta(seconds=2)
    created = await make_wizard_session(
        db_session,
        answers={"choice": ["a"]},
        expires_at=expires_at,
        now=expires_at - timedelta(hours=1),
    )
    await db_session.commit()
    engine = build_test_engine(
        os.environ["DAIMON_DATABASE__TEST_URL"], db_schema, poolclass=NullPool
    )
    independent_sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    submit_now = expires_at - timedelta(seconds=1)
    sweep_now = expires_at + timedelta(seconds=1)
    candidate_selected = asyncio.Event()
    continue_sweep = asyncio.Event()

    async def claim_and_hold() -> None:
        async with independent_sessions() as session, session.begin():
            claimed = await store.try_claim_submit(
                session,
                short_id=created.id,
                answers={"choice": ["a"]},
                current_step=1,
                expected_updated_at=created.updated_at,
                now=submit_now,
            )
            assert claimed is not None, "pre-expiry submit should win its claim"
            await candidate_selected.wait()

    async def select_then_sweep() -> int:
        async with independent_sessions() as session, session.begin():
            execute = session.execute
            first_execute = True

            async def pause_after_candidate_query(*args: object, **kwargs: object) -> object:
                nonlocal first_execute
                result = await execute(*args, **kwargs)  # type: ignore[arg-type]
                if first_execute:
                    first_execute = False
                    candidate_selected.set()
                    await continue_sweep.wait()
                return result

            session.execute = pause_after_candidate_query  # type: ignore[method-assign]
            return await store.abandon_expired_wizard_sessions(session, now=sweep_now)

    try:
        claim_task = asyncio.create_task(claim_and_hold())
        sweep_task = asyncio.create_task(select_then_sweep())
        await candidate_selected.wait()
        # The candidate SELECT has completed while the claim UPDATE is still
        # uncommitted, so PostgreSQL exposes the previous open row to the sweep.
        # Commit the claim before allowing its second UPDATE to run.
        await claim_task
        continue_sweep.set()
        flipped = await sweep_task

        assert flipped == 0, "expiry sweep must recheck status after a concurrent submit commits"
        async with independent_sessions() as session:
            refreshed = await store.get_wizard_session(session, short_id=created.id)
        assert refreshed is not None
        assert refreshed.status == "submitted", "a submitted wizard must remain terminal"
    finally:
        continue_sweep.set()
        await engine.dispose()


async def test_concurrent_try_claim_submit_yields_exactly_one_winner(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two Submit taps racing the same row: one wins, one gets None.

    A second winner would mean a second billed turn spawned from the same
    form. `try_claim_submit`'s single predicated UPDATE is the entire gate —
    asserting the race empirically is the difference between trusting the
    SQL shape and knowing it holds.
    """
    created = await make_wizard_session(db_session)
    now = datetime.now(UTC)

    async def claim() -> WizardSessionRow | None:
        async with db_session_factory() as session:
            row = await store.try_claim_submit(
                session,
                short_id=created.id,
                answers={"choice": ["a"]},
                current_step=1,
                expected_updated_at=created.updated_at,
                now=now,
            )
            await session.commit()
            return row

    first, second = await asyncio.gather(claim(), claim())

    winners = [row for row in (first, second) if row is not None]
    assert len(winners) == 1, (
        "exactly one racing claim may submit a wizard session; a second claim "
        f"winning would mean a second billed turn; got {len(winners)} winners"
    )


async def test_abandon_expired_wizard_sessions_flips_only_open_past_expiry_rows(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    expired_open = await make_wizard_session(
        db_session, now=now - timedelta(hours=2), expires_at=now - timedelta(hours=1)
    )
    still_open = await make_wizard_session(db_session, now=now, expires_at=now + timedelta(hours=1))
    expired_submitted = await make_wizard_session(
        db_session,
        now=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
        status="submitted",
    )

    flipped = await store.abandon_expired_wizard_sessions(db_session, now=now)

    assert flipped == 1
    after_expired_open = await store.get_wizard_session(db_session, short_id=expired_open.id)
    assert after_expired_open is not None
    assert after_expired_open.status == "abandoned"
    after_still_open = await store.get_wizard_session(db_session, short_id=still_open.id)
    assert after_still_open is not None
    assert after_still_open.status == "open", "an unexpired row must not be abandoned"
    after_expired_submitted = await store.get_wizard_session(
        db_session, short_id=expired_submitted.id
    )
    assert after_expired_submitted is not None
    assert after_expired_submitted.status == "submitted", "a submitted row must not be touched"


async def test_abandon_expired_wizard_sessions_honours_limit(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    for _ in range(3):
        await make_wizard_session(
            db_session, now=now - timedelta(hours=2), expires_at=now - timedelta(hours=1)
        )

    flipped = await store.abandon_expired_wizard_sessions(db_session, now=now, limit=2)

    assert flipped == 2


async def test_delete_wizard_sessions_for_platform_user_deletes_regardless_of_status(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    open_row = await make_wizard_session(
        db_session, tenant=tenant, account=account, requester_platform_user_id="erase-me"
    )
    submitted_row = await make_wizard_session(
        db_session,
        tenant=tenant,
        account=account,
        requester_platform_user_id="erase-me",
        status="submitted",
    )
    abandoned_row = await make_wizard_session(
        db_session,
        tenant=tenant,
        account=account,
        requester_platform_user_id="erase-me",
        status="abandoned",
    )

    deleted = await store.delete_wizard_sessions_for_platform_user(
        db_session, platform_user_id="erase-me", tenant_id=tenant.id
    )

    assert deleted == 3, "delete must remove open, submitted, AND abandoned rows"
    assert (await store.get_wizard_session(db_session, short_id=open_row.id)) is None
    assert (await store.get_wizard_session(db_session, short_id=submitted_row.id)) is None
    assert (await store.get_wizard_session(db_session, short_id=abandoned_row.id)) is None


async def test_delete_wizard_sessions_for_platform_user_is_idempotent(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_wizard_session(
        db_session, tenant=tenant, account=account, requester_platform_user_id="idem-me"
    )

    first = await store.delete_wizard_sessions_for_platform_user(
        db_session, platform_user_id="idem-me", tenant_id=tenant.id
    )
    second = await store.delete_wizard_sessions_for_platform_user(
        db_session, platform_user_id="idem-me", tenant_id=tenant.id
    )

    assert first == 1
    assert second == 0, "a re-run delete of an already-erased user must be a no-op"


async def test_delete_wizard_sessions_for_platform_user_does_not_touch_other_tenant(
    db_session: AsyncSession,
) -> None:
    tenant_a = await make_tenant(db_session, workspace_id="guild-a")
    tenant_b = await make_tenant(db_session, workspace_id="guild-b")
    account_a = await make_account(db_session, tenant=tenant_a)
    account_b = await make_account(db_session, tenant=tenant_b)
    row_a = await make_wizard_session(
        db_session,
        tenant=tenant_a,
        account=account_a,
        requester_platform_user_id="shared-platform-id",
    )
    row_b = await make_wizard_session(
        db_session,
        tenant=tenant_b,
        account=account_b,
        requester_platform_user_id="shared-platform-id",
    )

    deleted = await store.delete_wizard_sessions_for_platform_user(
        db_session, platform_user_id="shared-platform-id", tenant_id=tenant_a.id
    )

    assert deleted == 1
    assert (await store.get_wizard_session(db_session, short_id=row_a.id)) is None
    survivor = await store.get_wizard_session(db_session, short_id=row_b.id)
    assert survivor is not None, (
        "another tenant's row sharing the same platform user id must survive"
    )


async def test_count_wizard_sessions_for_platform_user_matches_delete_rowcount(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    for _ in range(3):
        await make_wizard_session(
            db_session, tenant=tenant, account=account, requester_platform_user_id="count-me"
        )

    count_before = await store.count_wizard_sessions_for_platform_user(
        db_session, platform_user_id="count-me", tenant_id=tenant.id
    )
    deleted = await store.delete_wizard_sessions_for_platform_user(
        db_session, platform_user_id="count-me", tenant_id=tenant.id
    )

    assert count_before == deleted == 3
