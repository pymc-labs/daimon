"""A private form's pin rule is decided inside the transaction that spends it.

The submit paths decided the pin rule, then consumed the form in a later
transaction: a pin committed in between was never put to it. Now
`consume_form_unless_pinned` decides it in the consume transaction itself.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_pins import (
    PIN_WRITE_REFUSAL,
    FormPinRefused,
    consume_form_unless_pinned,
    request_pin_refusal,
)
from daimon.core.credential_requests import mint_request_token
from daimon.core.stores import credential_requests as store
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import CredentialRequestRow
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
