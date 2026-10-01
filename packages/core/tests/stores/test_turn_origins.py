"""Store lookups scope short-lived origin authority to the authenticated caller."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.stores.domain import Role
from daimon.core.stores.turn_origins import create_origin, get_active_origin
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.parametrize("mismatch", ["caller", "tenant", "platform", "expired", "unknown"])
async def test_origin_lookup_rejects_invalid_authority(
    db_session: AsyncSession, mismatch: str
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)
    origin = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="slack",
        parent_channel_id="C123",
        thread_id="123.456",
        responder_ma_agent_id="agent_daimon",
        responder_name="Daimon",
        configuration_target_ma_agent_id="agent_specialist",
        configuration_target_name="specialist",
        role=Role.USER,
        expires_at=now + timedelta(minutes=10),
        now=now,
    )
    result = await get_active_origin(
        db_session,
        origin_id=uuid.uuid4() if mismatch == "unknown" else origin.id,
        tenant_id=uuid.uuid4() if mismatch == "tenant" else tenant.id,
        account_id=uuid.uuid4() if mismatch == "caller" else account.id,
        platform="discord" if mismatch == "platform" else "slack",
        now=now + timedelta(minutes=10) if mismatch == "expired" else now,
    )
    assert result is None, f"{mismatch} must not gain origin authority"


async def test_new_origin_prunes_expired_crash_records(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    before = datetime.now(UTC) - timedelta(hours=3)
    crashed = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="discord",
        parent_channel_id="parent",
        thread_id="old-thread",
        responder_ma_agent_id="agent_daimon",
        responder_name="Daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        now=before,
        expires_at=before + timedelta(hours=2),
    )
    now = datetime.now(UTC)
    current = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="discord",
        parent_channel_id="parent",
        thread_id="new-thread",
        responder_ma_agent_id="agent_daimon",
        responder_name="Daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        now=now,
        expires_at=now + timedelta(hours=2),
    )
    assert (
        await get_active_origin(
            db_session,
            origin_id=crashed.id,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            now=before,
        )
        is None
    ), "crash records are physically removed, even when queried before their prior expiry"
    assert (
        await get_active_origin(
            db_session,
            origin_id=current.id,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            now=now,
        )
        == current
    ), "pruning does not affect current origin authority"


async def test_account_deletion_cascades_turn_origin(db_session: AsyncSession) -> None:
    from daimon.core.stores.accounts import delete_account

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)
    origin = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="discord",
        parent_channel_id="parent",
        thread_id="thread",
        responder_ma_agent_id="agent_daimon",
        responder_name="Daimon",
        configuration_target_ma_agent_id="agent_specialist",
        configuration_target_name="specialist",
        role=Role.USER,
        now=now,
        expires_at=now + timedelta(hours=2),
    )
    await delete_account(db_session, account_id=account.id)
    assert (
        await get_active_origin(
            db_session,
            origin_id=origin.id,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            now=now,
        )
        is None
    ), "account deletion removes turn origin attribution and authority through its FK cascade"
