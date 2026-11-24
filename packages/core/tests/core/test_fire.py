"""The fire guard over a `FireStore`: it needs no routine row, only the store."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator

from daimon.core.fire.run import Dispatch, fire
from daimon.core.fire.store import FireStatus
from daimon.core.fire.subject import Subject
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.turn.termination import TerminationReason
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _subject(tenant_id: uuid.UUID) -> Subject:
    return Subject(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        created_by_user_id="owner_1",
        agent_id="agent_1",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone="UTC",
        trigger_message="m",
    )


class RecordingStore:
    """A `FireStore` that records every call instead of writing a row."""

    def __init__(self) -> None:
        self.granted: list[float] = []
        self.failures: list[tuple[str, TerminationReason | None]] = []
        self.told: list[str | None] = []

    @contextlib.asynccontextmanager
    async def _grant(self, lifetime_s: float) -> AsyncIterator[None]:
        self.granted.append(lifetime_s)
        yield

    def execution(self, lifetime_s: float) -> contextlib.AbstractAsyncContextManager[None]:
        return self._grant(lifetime_s)

    async def fail(
        self, session: AsyncSession, *, error: str, reason: TerminationReason | None
    ) -> bool:
        self.failures.append((error, reason))
        return False

    def tell_owner(self, reason: str | None, *, channel_told: bool) -> None:
        self.told.append(reason)


async def _fire(
    sm: async_sessionmaker[AsyncSession],
    subject: Subject,
    store: RecordingStore,
    turn_error: Exception | None = None,
    *,
    intake_bound: bool = False,
) -> tuple[FireStatus, list[FireStatus], list[str]]:
    ran: list[str] = []
    settled: list[FireStatus] = []

    async def turn() -> None:
        ran.append("turn")
        if turn_error is not None:
            raise turn_error

    async def settle(status: FireStatus) -> None:
        settled.append(status)

    status = await fire(
        subject,
        store,
        turn,
        Dispatch(sm, asyncio.Semaphore(1), 10, capture=lambda _: None),
        routines_gate=True,
        intake_bound=intake_bound,
        settle=settle,
    )
    return status, settled, ran


async def test_a_fire_runs_with_no_routine_row(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A subject no routine row backs runs its turn under the store's grant."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    store = RecordingStore()
    subject = _subject(tenant.id)
    status, settled, ran = await _fire(db_session_factory, subject, store)
    assert (status, settled, ran) == ("ok", ["ok"], ["turn"]), "the turn runs once and settles ok"
    assert store.granted == [10], "the turn runs under the store's grant, bound to the timeout"
    assert store.failures == [], "a fire that succeeded records no failure"


async def test_a_failed_turn_is_recorded_through_the_store(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The error is recorded and the owner told through the store, never a routine row."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    store = RecordingStore()
    subject = _subject(tenant.id)
    status, settled, _ = await _fire(db_session_factory, subject, store, RuntimeError("boom"))
    assert (status, settled) == ("failed", ["failed"]), "a raised turn fails the fire"
    assert [error for error, _ in store.failures] == ["RuntimeError: boom"], store.failures
    assert store.told == [store.failures[0][1]], "the owner is told why it failed"


async def test_lead_intake_off_skips_a_bound_fire(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A fire bound as lead intake does not run while `lead_intake` is off, and tells nobody."""
    tenant = await make_tenant(db_session)
    await set_feature_value(
        db_session,
        tenant_id=tenant.id,
        target=FeatureTarget("workspace"),
        key="lead_intake",
        value="off",
        set_by=None,
        surface="chat",
    )
    await db_session.commit()
    store = RecordingStore()
    subject = _subject(tenant.id)
    status, settled, ran = await _fire(db_session_factory, subject, store, intake_bound=True)
    assert (status, settled, ran) == ("skipped", ["skipped"], []), "the turn never starts"
    assert store.failures == [("lead_intake_turned_off", None)], store.failures
    assert store.told == [None], "a benign skip carries no reason, so nobody is told"
