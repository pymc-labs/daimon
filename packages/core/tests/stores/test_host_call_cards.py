"""Host call cards on real Postgres: each step moves a row exactly once."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from daimon.core._models import TaskContinuation
from daimon.core.host_calls import (
    APPROVED_TEXT,
    CRASHED_TEXT,
    DENIED_TEXT,
    NOT_RUN_TEXT,
    press_host_call,
)
from daimon.core.stores import host_call_cards as cards
from daimon.core.stores.domain import TenantRow
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_AGENT = uuid.uuid4()


@dataclass(frozen=True)
class Lane:
    sessions: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    tenant: TenantRow


@pytest.fixture
async def lane(db_preflight: object, db_committing_engine: AsyncEngine) -> AsyncIterator[Lane]:
    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    try:
        require_owned_sessionmaker(sessions)
    except ValueError:
        pytest.skip("Committed press races need an owned DB or isolated CI")
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
    yield Lane(sessions, tenant.id, account.id, tenant)


async def _card(
    lane: Lane,
    *,
    origin_id: uuid.UUID | None = None,
    requester: uuid.UUID | None = None,
    digest: str = "d1",
    thread_id: str = "t1",
    expires_in: timedelta = timedelta(minutes=10),
    now: datetime | None = None,
) -> cards.Created | None:
    async with lane.sessions.begin() as session:
        created = await cards.create_card(
            session,
            tenant_id=lane.tenant_id,
            origin_id=origin_id or uuid.uuid4(),
            requester_account_id=requester or lane.account_id,
            requester_platform_user_id="requester",
            platform="discord",
            channel_id="c1",
            thread_id=thread_id,
            executing_agent_id=_AGENT,
            executing_ma_agent_id="agent_x",
            agent_name="Researcher",
            tool_name="send_message",
            args_digest=digest,
            reference={"args": {"text": "hi"}, "admin": False},
            expires_at=(now or datetime.now(UTC)) + expires_in,
        )
        if created is not None:
            await cards.mark_posted(session, card_id=created.row.id, message_id="m1")
    return created


async def _press(lane: Lane, card_id: uuid.UUID, *, now: datetime, clicker: str = "requester"):
    async def admin(_: str) -> bool:
        return True

    return await press_host_call(
        sessionmaker=lane.sessions,
        enabled=True,
        tenant_id=lane.tenant_id,
        card_id=card_id,
        action="approve",
        clicker=clicker,
        message_id="m1",
        current_admin=admin,
        now=now,
    )


async def _wakes(lane: Lane, thread_id: str = "t1") -> int:
    async with lane.sessions() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(TaskContinuation)
            .where(
                TaskContinuation.tenant_id == lane.tenant_id,
                TaskContinuation.thread_id == thread_id,
                TaskContinuation.reason == "confirmed_call",
            )
        )
    return count or 0


async def _consume(
    lane: Lane, card_id: uuid.UUID, digest: str = "d1", requester: uuid.UUID | None = None
):
    async with lane.sessions.begin() as session:
        return await cards.consume(
            session,
            card_id=card_id,
            tool_name="send_message",
            args_digest=digest,
            executing_agent_id=_AGENT,
            requester_account_id=requester or lane.account_id,
            now=datetime.now(UTC),
        )


async def test_retry_of_a_pending_call_reuses_its_card_and_the_cap_holds(lane: Lane) -> None:
    """A model retrying the same call gets the same card; a flood stops at the cap."""
    origin = uuid.uuid4()
    first = await _card(lane, origin_id=origin)
    again = await _card(lane, origin_id=origin)
    assert first is not None and first.fresh, "the first ask is a fresh card"
    assert again is not None and not again.fresh, "the retry is not a second card"
    assert again.row.id == first.row.id, "the retry finds the same record"
    for n in range(cards.MAX_OPEN_PER_ORIGIN - 1):
        assert await _card(lane, origin_id=origin, digest=f"x{n}") is not None, "under the cap"
    assert await _card(lane, origin_id=origin, digest="over") is None, "the cap refuses more"


async def test_double_press_claims_once_and_others_cannot_use_the_card(lane: Lane) -> None:
    """Only the requester's first press counts; it queues exactly one wake."""
    created = await _card(lane)
    assert created is not None, "card recorded"
    now = datetime.now(UTC)
    assert await _press(lane, created.row.id, now=now, clicker="stranger") is None, (
        "a stranger's press leaves the card as it is"
    )
    presses = await asyncio.gather(*(_press(lane, created.row.id, now=now) for _ in range(3)))
    approved = [p for p in presses if p is not None and p.approved]
    assert len(approved) == 1, f"one press wins, got {presses}"
    assert await _wakes(lane) == 1, "one wake for one record"


async def test_two_consumers_run_it_once_and_a_mismatch_runs_nothing(lane: Lane) -> None:
    """Consume is approved→executing once, and only for the approved tool and args."""
    created = await _card(lane)
    assert created is not None, "card recorded"
    await _press(lane, created.row.id, now=datetime.now(UTC))
    assert await _consume(lane, created.row.id, digest="tampered") is None, "digest mismatch"
    taken = await asyncio.gather(_consume(lane, created.row.id), _consume(lane, created.row.id))
    assert sum(t is not None for t in taken) == 1, f"exactly one consumer runs it: {taken}"


async def test_press_after_the_provider_gave_up_still_runs_once_via_the_wake(lane: Lane) -> None:
    """The asking turn ended long ago (a 1 s provider timeout); a press at 60 s still runs."""
    asked = datetime.now(UTC) - timedelta(seconds=60)
    created = await _card(lane, now=asked)
    assert created is not None, "card recorded"
    press = await _press(lane, created.row.id, now=asked + timedelta(seconds=60))
    assert press is not None and press.approved, f"the late press approves: {press}"
    async with lane.sessions() as session:
        approved = await cards.list_approved(
            session,
            tenant_id=lane.tenant_id,
            platform="discord",
            thread_id="t1",
            executing_agent_id=_AGENT,
            requester_account_id=lane.account_id,
        )
    assert [r.id for r in approved] == [created.row.id], "the wake finds the approved call"
    assert await _consume(lane, created.row.id) is not None, "the wake's call runs it"
    assert await _consume(lane, created.row.id) is None, "a second wake can't run it again"


async def test_n_parallel_calls_are_n_cards_run_by_the_first_wake(lane: Lane) -> None:
    """Every ask is its own card; one wake turn runs them all, later wakes find none."""
    origin = uuid.uuid4()
    made = await asyncio.gather(*(_card(lane, origin_id=origin, digest=f"p{n}") for n in range(3)))
    ids = {m.row.id for m in made if m is not None}
    assert len(ids) == 3, f"three distinct cards: {made}"
    now = datetime.now(UTC)
    for card_id in ids:
        await _press(lane, card_id, now=now)
    async with lane.sessions() as session:
        approved = await cards.list_approved(
            session,
            tenant_id=lane.tenant_id,
            platform="discord",
            thread_id="t1",
            executing_agent_id=_AGENT,
            requester_account_id=lane.account_id,
        )
    assert {r.id for r in approved} == ids, "the first wake lists every approved call"


async def test_a_wake_lists_and_runs_only_its_own_persons_approved_calls(lane: Lane) -> None:
    """Another person's approved card is neither listed nor spent by this person's wake."""
    async with lane.sessions.begin() as session:
        other = await make_account(session, tenant=lane.tenant)
    mine = await _card(lane, digest="m")
    theirs = await _card(lane, digest="t", requester=other.id)
    assert mine is not None and theirs is not None, "both cards recorded"
    now = datetime.now(UTC)
    await _press(lane, mine.row.id, now=now)
    await _press(lane, theirs.row.id, now=now)
    async with lane.sessions() as session:
        approved = await cards.list_approved(
            session,
            tenant_id=lane.tenant_id,
            platform="discord",
            thread_id="t1",
            executing_agent_id=_AGENT,
            requester_account_id=lane.account_id,
        )
    assert [r.id for r in approved] == [mine.row.id], f"only my approved call: {approved}"
    assert await _consume(lane, theirs.row.id, digest="t") is None, "not spent by my wake"
    async with lane.sessions() as session:
        kept = await cards.get_card(session, tenant_id=lane.tenant_id, card_id=theirs.row.id)
    assert kept is not None and kept.status == "approved", f"theirs waits: {kept}"
    taken = await _consume(lane, theirs.row.id, digest="t", requester=other.id)
    assert taken is not None, "their own wake runs it"


async def test_sweep_expires_unanswered_and_fails_stuck_executing(lane: Lane) -> None:
    """A crashed worker's call may have run: it is failed with a check-first outcome."""
    late = await _card(lane, expires_in=timedelta(seconds=-1), thread_id="t2")
    stuck = await _card(lane, thread_id="t2", digest="s")
    assert late is not None and stuck is not None, "cards recorded"
    await _press(lane, stuck.row.id, now=datetime.now(UTC))
    assert await _consume(lane, stuck.row.id, digest="s") is not None, "taken to execute"
    later = datetime.now(UTC) + timedelta(minutes=5)
    async with lane.sessions.begin() as session:
        await cards.sweep(
            session,
            now=later,
            executing_before=later,
            crashed_outcome=CRASHED_TEXT,
            unrun_outcome=NOT_RUN_TEXT,
        )
        expired = await cards.get_card(session, tenant_id=lane.tenant_id, card_id=late.row.id)
        failed = await cards.get_card(session, tenant_id=lane.tenant_id, card_id=stuck.row.id)
    assert expired is not None and expired.status == "expired", f"unanswered expires: {expired}"
    assert failed is not None and failed.status == "failed", f"stuck fails: {failed}"
    assert failed.outcome == CRASHED_TEXT, "the outcome says check before asking again"


async def test_sweep_fails_an_approved_call_nobody_ran_as_not_run(lane: Lane) -> None:
    """An approval whose wake never ran says it didn't run; a fresh one is left for its wake."""
    now = datetime.now(UTC)
    stale = await _card(lane, thread_id="t3", digest="a", expires_in=timedelta(minutes=1), now=now)
    fresh = await _card(lane, thread_id="t3", digest="b", expires_in=timedelta(minutes=30), now=now)
    assert stale is not None and fresh is not None, "cards recorded"
    for created in (stale, fresh):
        assert await _press(lane, created.row.id, now=now) is not None, "approved"
    later = now + timedelta(minutes=5)
    async with lane.sessions.begin() as session:
        swept = await cards.sweep(
            session,
            now=later,
            executing_before=later,
            crashed_outcome=CRASHED_TEXT,
            unrun_outcome=NOT_RUN_TEXT,
        )
        stranded = await cards.get_card(session, tenant_id=lane.tenant_id, card_id=stale.row.id)
        waiting = await cards.get_card(session, tenant_id=lane.tenant_id, card_id=fresh.row.id)
    assert swept == 1, "only the approval past its expiry is swept"
    assert stranded is not None and stranded.status == "failed", f"stranded fails: {stranded}"
    assert stranded.outcome == NOT_RUN_TEXT, "approved-but-never-run says it did not run"
    assert waiting is not None and waiting.status == "approved", "an unexpired approval waits"


async def _press_with(
    lane: Lane,
    card_id: uuid.UUID,
    *,
    action: Literal["approve", "deny"] = "approve",
    clicker: str = "requester",
    message_id: str = "m1",
    tenant_id: uuid.UUID | None = None,
):
    async def admin(_: str) -> bool:
        return True

    return await press_host_call(
        sessionmaker=lane.sessions,
        enabled=True,
        tenant_id=tenant_id or lane.tenant_id,
        card_id=card_id,
        action=action,
        clicker=clicker,
        message_id=message_id,
        current_admin=admin,
        now=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"clicker": "stranger"},
        {"message_id": "some-other-message"},
        {"tenant_id": uuid.uuid4()},
    ],
    ids=["not_the_requester", "other_message", "other_tenant"],
)
async def test_press_is_ignored_unless_requester_message_and_tenant_match(
    lane: Lane, changes: dict[str, object]
) -> None:
    """Anyone else's press leaves the card pending and queues no wake."""
    created = await _card(lane)
    assert created is not None, "card recorded"

    pressed = await _press_with(lane, created.row.id, **changes)  # pyright: ignore[reportArgumentType]

    async with lane.sessions() as session:
        card = await cards.get_card(session, tenant_id=lane.tenant_id, card_id=created.row.id)
    assert pressed is None, f"the press is not the requester's: {pressed}"
    assert card is not None and card.status == "pending", f"the card is untouched: {card}"
    assert await _wakes(lane) == 0, "no wake for a press that did not count"


async def test_press_approve_claims_the_card_and_queues_one_wake(lane: Lane) -> None:
    """Approve moves the card to approved with its outcome and wakes the thread once."""
    created = await _card(lane)
    assert created is not None, "card recorded"

    pressed = await _press_with(lane, created.row.id)

    async with lane.sessions() as session:
        card = await cards.get_card(session, tenant_id=lane.tenant_id, card_id=created.row.id)
    assert pressed is not None and pressed.closed and pressed.approved, f"approved: {pressed}"
    assert card is not None and card.status == "approved", f"card approved: {card}"
    assert card.outcome == APPROVED_TEXT, "the card says it was approved"
    assert await _wakes(lane) == 1, "one wake runs the approved call"


async def test_press_deny_closes_the_card_and_wakes_nothing(lane: Lane) -> None:
    """Deny ends the call: nothing can run, and no turn is started for it."""
    created = await _card(lane)
    assert created is not None, "card recorded"

    pressed = await _press_with(lane, created.row.id, action="deny")

    async with lane.sessions() as session:
        card = await cards.get_card(session, tenant_id=lane.tenant_id, card_id=created.row.id)
    assert pressed is not None and pressed.closed and not pressed.approved, f"denied: {pressed}"
    assert card is not None and card.status == "denied", f"card denied: {card}"
    assert card.outcome == DENIED_TEXT, "the card says it was denied"
    assert await _wakes(lane) == 0, "a denial wakes nothing"
    assert await _consume(lane, created.row.id) is None, "a denied call cannot run"
