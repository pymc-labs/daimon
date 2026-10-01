"""A private form's pin rule is decided inside the transaction that spends it.

The submit paths decided the pin rule, then consumed the form in a later
transaction: a pin committed in between was never put to it. Now
`consume_form_unless_pinned` decides it in the consume transaction itself.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from daimon.core._models import CredentialRequest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_pins import (
    PIN_WRITE_REFUSAL,
    FormPinRefused,
    consume_form_unless_pinned,
    request_pin_refusal,
)
from daimon.core.credential_requests import mint_request_token
from daimon.core.stores import credential_requests as store
from daimon.core.stores.access_policy import lock_access_policy, set_access_policy
from daimon.core.stores.agent_files import lock_agent_keys, put_agent_file_if_unchanged
from daimon.core.stores.domain import CredentialRequestRow
from daimon.testing import ma_agent
from daimon.testing.db import build_test_engine
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

_AGENT = ma_agent(id="ag_acme", name="acme")


async def _seed(db_session: AsyncSession) -> CredentialRequestRow:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await store.create_credential_request(
        db_session,
        token=mint_request_token(),
        kind="env",
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        account_id=account.id,
        target="NOTES_TOKEN",
        mcp_server_url=None,
        requester_platform_user_id="U1",
        channel_id="C_HERE",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_acme",
        target_name="acme",
        requested_work=None,
        platform="slack",
        parent_channel_id="C_HERE",
        origin_thread_id="1700000000.000001",
    )
    await db_session.commit()
    return row


async def _pin(factory: async_sessionmaker[AsyncSession], row: CredentialRequestRow) -> None:
    async with factory.begin() as session:
        await lock_access_policy(session, tenant_id=row.tenant_id)
        await set_access_policy(
            session,
            tenant_id=row.tenant_id,
            policy=TenantAccessPolicy(agent_channel_pins={"acme": ("C_ELSEWHERE",)}),
        )


async def test_pin_after_the_early_check_refuses_inside_the_consume(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _seed(db_session)
    async with db_session_factory() as session:
        # The early check, before any confirmation: still allowed.
        assert await request_pin_refusal(session, row=row, agent=_AGENT) is None
    await _pin(db_session_factory, row)

    with pytest.raises(FormPinRefused) as refused:
        async with db_session_factory.begin() as session:
            await consume_form_unless_pinned(session, row=row, agent=_AGENT, now=datetime.now(UTC))
    assert refused.value.refusal == PIN_WRITE_REFUSAL
    async with db_session_factory() as session:
        peeked = await store.peek_credential_request(session, token=row.token)
    assert peeked is not None and peeked.used_at is None, "a refused form stays unspent"


async def test_allowed_form_is_consumed_once(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _seed(db_session)
    now = datetime.now(UTC)
    async with db_session_factory.begin() as session:
        consumed = await consume_form_unless_pinned(session, row=row, agent=_AGENT, now=now)
    assert consumed is not None and consumed.used_at is not None
    async with db_session_factory.begin() as session:
        again = await consume_form_unless_pinned(session, row=row, agent=_AGENT, now=now)
    assert again is None


async def test_vanished_agent_refuses_under_a_pin(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _seed(db_session)
    await _pin(db_session_factory, row)
    with pytest.raises(FormPinRefused):
        async with db_session_factory.begin() as session:
            await consume_form_unless_pinned(session, row=row, agent=None, now=datetime.now(UTC))


# Concurrent connections: a pin edit and a consume serialize on the tenant's
# policy lock, with or without a stored policy row.


@pytest_asyncio.fixture
async def race_engine(db_engine: AsyncEngine, db_schema: str) -> AsyncIterator[AsyncEngine]:
    engine = build_test_engine(
        db_engine.url.render_as_string(hide_password=False), db_schema, poolclass=NullPool
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_policy(
    factory: async_sessionmaker[AsyncSession], row: CredentialRequestRow, *, policy_row: bool
) -> None:
    if not policy_row:
        return
    async with factory.begin() as session:
        await lock_access_policy(session, tenant_id=row.tenant_id)
        await set_access_policy(
            session,
            tenant_id=row.tenant_id,
            policy=TenantAccessPolicy(agent_channel_pins={"other": ("C_OTHER",)}),
        )


async def _pid(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def _until_lock_wait(engine: AsyncEngine, pid: int) -> None:
    async with engine.connect() as probe:
        for _ in range(200):
            waiting = (
                await probe.execute(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": pid},
                )
            ).scalar_one_or_none()
            if waiting == "Lock":
                return
            await probe.rollback()
            await asyncio.sleep(0.025)
    raise AssertionError(f"backend {pid} never waited on a lock")


@pytest.mark.parametrize("policy_row", [False, True], ids=["open-default", "stored-row"])
async def test_pin_waits_for_a_pending_consume_then_applies_after(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    race_engine: AsyncEngine,
    policy_row: bool,
) -> None:
    row = await _seed(db_session)
    await _seed_policy(db_session_factory, row, policy_row=policy_row)
    factory = async_sessionmaker(race_engine, expire_on_commit=False)
    form_spent_when_pin_locked: list[bool] = []
    consumer_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    pinner_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def consume() -> CredentialRequestRow | None:
        async with factory.begin() as session:
            consumer_pid.set_result(await _pid(session))
            return await consume_form_unless_pinned(
                session, row=row, agent=_AGENT, now=datetime.now(UTC)
            )

    async def pin() -> None:
        async with factory.begin() as session:
            pinner_pid.set_result(await _pid(session))
            await lock_access_policy(session, tenant_id=row.tenant_id)
            # Commit order, read from the database once the lock is held: the
            # consume that held it first has already committed.
            peeked = await store.peek_credential_request(session, token=row.token)
            form_spent_when_pin_locked.append(peeked is not None and peeked.used_at is not None)
            await set_access_policy(
                session,
                tenant_id=row.tenant_id,
                policy=TenantAccessPolicy(agent_channel_pins={"acme": ("C_ELSEWHERE",)}),
            )

    async with factory() as holder, holder.begin():
        # Another submit holds the form row: this consume decides, then its
        # single-use UPDATE waits.
        await holder.execute(
            select(CredentialRequest.token)
            .where(CredentialRequest.token == row.token)
            .with_for_update()
        )
        consuming = asyncio.create_task(consume())
        await _until_lock_wait(race_engine, await consumer_pid)
        pinning = asyncio.create_task(pin())
        # The pin can't commit while the consume that already decided is pending.
        await _until_lock_wait(race_engine, await pinner_pid)
        await holder.rollback()
    consumed = await asyncio.wait_for(consuming, 10)
    await asyncio.wait_for(pinning, 10)

    assert consumed is not None and consumed.used_at is not None
    assert form_spent_when_pin_locked == [True]
    async with factory() as session:
        assert await request_pin_refusal(session, row=row, agent=_AGENT) == PIN_WRITE_REFUSAL


@pytest.mark.parametrize("policy_row", [False, True], ids=["open-default", "stored-row"])
async def test_pin_holding_the_lock_first_is_read_and_refuses(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    race_engine: AsyncEngine,
    policy_row: bool,
) -> None:
    row = await _seed(db_session)
    await _seed_policy(db_session_factory, row, policy_row=policy_row)
    factory = async_sessionmaker(race_engine, expire_on_commit=False)
    consumer_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def consume() -> CredentialRequestRow | None:
        async with factory.begin() as session:
            consumer_pid.set_result(await _pid(session))
            return await consume_form_unless_pinned(
                session, row=row, agent=_AGENT, now=datetime.now(UTC)
            )

    async with factory() as pinner, pinner.begin():
        await lock_access_policy(pinner, tenant_id=row.tenant_id)
        await set_access_policy(
            pinner,
            tenant_id=row.tenant_id,
            policy=TenantAccessPolicy(agent_channel_pins={"acme": ("C_ELSEWHERE",)}),
        )
        consuming = asyncio.create_task(consume())
        # The consume waits for the policy edit instead of reading the old policy.
        await _until_lock_wait(race_engine, await consumer_pid)
    with pytest.raises(FormPinRefused):
        await asyncio.wait_for(consuming, 10)
    async with factory() as session:
        peeked = await store.peek_credential_request(session, token=row.token)
    assert peeked is not None and peeked.used_at is None, "a refused form stays unspent"


async def test_consumes_of_different_forms_both_land(
    db_session: AsyncSession, race_engine: AsyncEngine
) -> None:
    first = await _seed(db_session)
    factory = async_sessionmaker(race_engine, expire_on_commit=False)
    second = await store.create_credential_request(
        db_session,
        token=mint_request_token(),
        kind="env",
        tenant_id=first.tenant_id,
        agent_id=first.agent_id,
        account_id=first.account_id,
        target="OTHER_TOKEN",
        mcp_server_url=None,
        requester_platform_user_id="U1",
        channel_id="C_HERE",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_acme",
        target_name="acme",
        requested_work=None,
        platform="slack",
        parent_channel_id="C_HERE",
        origin_thread_id="1700000000.000002",
    )
    await db_session.commit()

    async def consume(row: CredentialRequestRow) -> CredentialRequestRow | None:
        async with factory.begin() as session:
            return await consume_form_unless_pinned(
                session, row=row, agent=_AGENT, now=datetime.now(UTC)
            )

    results = await asyncio.wait_for(asyncio.gather(consume(first), consume(second)), 10)
    assert all(r is not None and r.used_at is not None for r in results)


async def test_consume_does_not_deadlock_with_a_key_writer(
    db_session: AsyncSession, race_engine: AsyncEngine
) -> None:
    # An env consume takes the policy lock, then the agent's key lock; an agent
    # writing its own file takes the key lock, then inserts a row keyed to the
    # tenant. The policy lock must not block that insert's foreign-key check.
    row = await _seed(db_session)
    factory = async_sessionmaker(race_engine, expire_on_commit=False)
    consumer_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    writer_locked = asyncio.Event()
    writer_go = asyncio.Event()

    async def self_write() -> None:
        async with factory.begin() as session:
            await lock_agent_keys(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
            writer_locked.set()
            await writer_go.wait()
            await put_agent_file_if_unchanged(
                session,
                tenant_id=row.tenant_id,
                agent_id=row.agent_id,
                key="OTHER_TOKEN",
                content="x",
                set_by_account_id=row.account_id,
                expected_updated_at=None,
            )

    async def env_consume() -> CredentialRequestRow | None:
        async with factory.begin() as session:
            consumer_pid.set_result(await _pid(session))
            consumed = await consume_form_unless_pinned(
                session, row=row, agent=_AGENT, now=datetime.now(UTC)
            )
            await lock_agent_keys(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
            return consumed

    writing = asyncio.create_task(self_write())
    await writer_locked.wait()
    consuming = asyncio.create_task(env_consume())
    await _until_lock_wait(race_engine, await consumer_pid)
    writer_go.set()
    _, consumed = await asyncio.wait_for(asyncio.gather(writing, consuming), 15)
    assert consumed is not None and consumed.used_at is not None
