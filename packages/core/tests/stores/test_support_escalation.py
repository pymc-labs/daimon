"""Integration tests for the support_escalation store — real Postgres.

Covers the credit gate, the fact that the rows ARE the ledger (no counter to
drift), per-user-within-tenant scoping, and the delivery/erasure columns.
"""

from __future__ import annotations

from daimon.core._models import SupportEscalation
from daimon.core.stores import support_escalation as store
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def _rows(session: AsyncSession) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(SupportEscalation))).scalar_one()
    )


async def test_escalation_is_recorded_undelivered_so_a_failed_dm_cannot_drop_it(
    db_session: AsyncSession,
) -> None:
    # The row must exist before delivery is attempted. This is the whole
    # reason delivered_at is nullable rather than the write happening after
    # a successful DM.
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)

    row = await store.record_escalation(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="discord",
        platform_user_id="asker-1",
        channel_id="chan-1",
        message_id="msg-1",
        ma_session_id="sess_a",
        note="the forecast looks wrong",
        allowance=3,
    )

    assert row is not None
    assert row.delivered_at is None, "the row must be durable before any DM is attempted"
    assert row.note == "the forecast looks wrong"


async def test_credit_gate_refuses_once_the_allowance_is_spent(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)

    for i in range(2):
        assert (
            await store.record_escalation(
                db_session,
                tenant_id=tenant.id,
                account_id=None,
                platform="discord",
                platform_user_id="asker-1",
                channel_id="chan-1",
                message_id=f"msg-{i}",
                ma_session_id=None,
                note=f"help {i}",
                allowance=2,
            )
            is not None
        )

    refused = await store.record_escalation(
        db_session,
        tenant_id=tenant.id,
        account_id=None,
        platform="discord",
        platform_user_id="asker-1",
        channel_id="chan-1",
        message_id="msg-2",
        ma_session_id=None,
        note="one too many",
        allowance=2,
    )

    assert refused is None, (
        "running out returns None; it is the designed end of a trial, not an error"
    )
    assert await _rows(db_session) == 2, "a refused escalation must write nothing"


async def test_asking_twice_about_the_same_message_is_allowed_and_spends_two_credits(
    db_session: AsyncSession,
) -> None:
    # Deliberately no uniqueness on (tenant, message, user): a second question
    # about the same answer is legitimate and is its own request.
    tenant = await make_tenant(db_session)
    for _ in range(2):
        assert (
            await store.record_escalation(
                db_session,
                tenant_id=tenant.id,
                account_id=None,
                platform="discord",
                platform_user_id="asker-1",
                channel_id="chan-1",
                message_id="same-msg",
                ma_session_id=None,
                note="still stuck",
                allowance=3,
            )
            is not None
        )
    assert (
        await store.count_escalations_for_user(
            db_session, tenant_id=tenant.id, platform_user_id="asker-1"
        )
        == 2
    )


async def test_credits_are_per_user_and_per_tenant(db_session: AsyncSession) -> None:
    # The same person in two installs has two separate allowances, and one
    # person exhausting theirs must not block anybody else.
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)

    for tenant in (tenant_a, tenant_b):
        assert (
            await store.record_escalation(
                db_session,
                tenant_id=tenant.id,
                account_id=None,
                platform="discord",
                platform_user_id="asker-1",
                channel_id="c",
                message_id="m",
                ma_session_id=None,
                note="n",
                allowance=1,
            )
            is not None
        )

    assert (
        await store.count_escalations_for_user(
            db_session, tenant_id=tenant_a.id, platform_user_id="asker-1"
        )
        == 1
    )
    assert (
        await store.count_escalations_for_user(
            db_session, tenant_id=tenant_a.id, platform_user_id="asker-2"
        )
        == 0
    ), "one person's spend must not count against another's allowance"


async def test_mark_delivered_stamps_the_row(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    row = await store.record_escalation(
        db_session,
        tenant_id=tenant.id,
        account_id=None,
        platform="discord",
        platform_user_id="asker-1",
        channel_id="c",
        message_id="m",
        ma_session_id=None,
        note="n",
        allowance=1,
    )
    assert row is not None

    stamped = await store.mark_delivered(db_session, escalation_id=row.id)
    assert stamped is not None and stamped.delivered_at is not None


async def test_delete_for_platform_user_is_tenant_scoped_and_idempotent(
    db_session: AsyncSession,
) -> None:
    # The note is unsolicited personal text, so an erasure that missed these
    # rows would leave it behind. Tenant-scoped because platform_user_id is
    # not globally unique.
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)
    for tenant in (tenant_a, tenant_b):
        await store.record_escalation(
            db_session,
            tenant_id=tenant.id,
            account_id=None,
            platform="discord",
            platform_user_id="asker-1",
            channel_id="c",
            message_id="m",
            ma_session_id=None,
            note="personal text",
            allowance=1,
        )

    deleted = await store.delete_support_escalations_for_platform_user(
        db_session, tenant_id=tenant_a.id, platform_user_id="asker-1"
    )
    assert deleted == 1
    assert await _rows(db_session) == 1, "the other install's row must survive"

    again = await store.delete_support_escalations_for_platform_user(
        db_session, tenant_id=tenant_a.id, platform_user_id="asker-1"
    )
    assert again == 0, "delete must be idempotent"


async def test_account_scoped_delete_reaches_rows_written_before_the_account_existed(
    db_session: AsyncSession,
) -> None:
    # The account_id = NULL gap this predicate pair exists to close: somebody
    # can ask for help without ever having taken a turn, so an account-keyed
    # delete alone would leave the request and its note behind.
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)

    await store.record_escalation(
        db_session,
        tenant_id=tenant.id,
        account_id=None,  # written before the person had an accounts row
        platform="discord",
        platform_user_id="asker-1",
        channel_id="c",
        message_id="m1",
        ma_session_id=None,
        note="orphaned note",
        allowance=5,
    )
    await store.record_escalation(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="discord",
        platform_user_id="asker-1",
        channel_id="c",
        message_id="m2",
        ma_session_id=None,
        note="attributed note",
        allowance=5,
    )

    keys = [(tenant.id, "asker-1")]
    counted = await store.count_support_escalations_for_account(
        db_session, account_id=account.id, platform_user_keys=keys
    )
    deleted = await store.delete_support_escalations_for_account(
        db_session, account_id=account.id, platform_user_keys=keys
    )

    assert counted == 2, "the count must see the null-account row too"
    assert deleted == counted, (
        "count and delete must build the SAME predicate, or the /privacy preview "
        "lies about what erasure removes"
    )
    assert await _rows(db_session) == 0
