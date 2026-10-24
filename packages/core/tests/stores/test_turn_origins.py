"""Store lookups scope short-lived origin authority to the authenticated caller."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.stores.domain import Role
from daimon.core.stores.turn_origins import (
    create_origin,
    get_active_origin,
    list_active_origins,
)
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
        sender_foreign=False,
        shared_channel=False,
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
        sender_foreign=False,
        shared_channel=False,
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
        sender_foreign=False,
        shared_channel=False,
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
        sender_foreign=False,
        shared_channel=False,
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


async def test_running_origins_are_listed_per_caller(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)

    async def origin(account_id: uuid.UUID, thread: str, *, ttl: int) -> None:
        await create_origin(
            db_session,
            tenant_id=tenant.id,
            account_id=account_id,
            platform="teams",
            parent_channel_id="19:c",
            thread_id=thread,
            responder_ma_agent_id="agent_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            sender_foreign=False,
            shared_channel=False,
            expires_at=now + timedelta(minutes=ttl),
            now=now - timedelta(minutes=1),
        )

    await origin(account.id, "t-1", ttl=10)
    await origin(other.id, "t-2", ttl=10)
    running = await list_active_origins(
        db_session, tenant_id=tenant.id, account_id=account.id, platform="teams", now=now
    )
    assert [row.thread_id for row in running] == ["t-1"], "only this caller's turns"


@pytest.mark.parametrize("foreign", [False, True])
async def test_an_origin_remembers_whether_its_sender_is_from_another_organisation(
    db_session: AsyncSession, foreign: bool
) -> None:
    tenant = await make_tenant(db_session, platform="teams")
    account = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)
    created = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="teams",
        parent_channel_id="19:abc@thread.tacv2",
        thread_id="19:abc@thread.tacv2;messageid=1",
        responder_ma_agent_id="agent_daimon",
        responder_name="Daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        shared_channel=False,
        expires_at=now + timedelta(minutes=10),
        now=now,
        sender_foreign=foreign,
    )
    read = await get_active_origin(
        db_session,
        origin_id=created.id,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="teams",
        now=now,
    )

    assert created.sender_foreign is foreign
    assert read is not None and read.sender_foreign is foreign


@pytest.mark.parametrize("shared", [False, True])
async def test_an_origin_records_whether_its_channel_is_shared(
    db_session: AsyncSession, shared: bool
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)
    origin = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="slack",
        parent_channel_id="C_SHARED",
        thread_id="123.456",
        responder_ma_agent_id="agent_daimon",
        responder_name="Daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=shared,
        expires_at=now + timedelta(minutes=10),
        now=now,
    )
    read = await get_active_origin(
        db_session,
        origin_id=origin.id,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="slack",
        now=now,
    )
    assert origin.shared_channel is shared
    assert read is not None and read.shared_channel is shared
