"""Integration tests for the support_escalation store — real Postgres.

Covers the delivery stamp and the erasure predicates. The credit gate is in
test_support_ledger.
"""

from __future__ import annotations

from decimal import Decimal

from daimon.core._models import SupportEscalation
from daimon.core.stores import support_escalation as store
from daimon.testing.factories import make_account, make_support_escalation, make_tenant
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def _rows(session: AsyncSession) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(SupportEscalation))).scalar_one()
    )


async def test_mark_delivered_stamps_the_row(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    row_id = await make_support_escalation(db_session, tenant_id=tenant.id)

    stamped = await store.mark_delivered(db_session, escalation_id=row_id)
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
        await make_support_escalation(
            db_session, tenant_id=tenant.id, platform_user_id="asker-1", note="personal text"
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

    await make_support_escalation(
        db_session,
        tenant_id=tenant.id,
        platform_user_id="asker-1",
        message_id="m1",
        note="orphaned note",  # written before the person had an accounts row
    )
    await make_support_escalation(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform_user_id="asker-1",
        message_id="m2",
        note="attributed note",
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


async def test_list_requests_when_undelivered_then_only_pending_ones(
    db_session: AsyncSession,
) -> None:
    """A request that settled or was declined needs nobody to settle it, delivered or not."""
    tenant = await make_tenant(db_session)
    pending = await make_support_escalation(db_session, tenant_id=tenant.id, message_id="m1")
    settled = await make_support_escalation(db_session, tenant_id=tenant.id, message_id="m2")
    declined = await make_support_escalation(db_session, tenant_id=tenant.id, message_id="m3")
    for row_id, status, charge in (
        (pending, "pending", None),
        (settled, "settled", Decimal(1)),
        (declined, "declined", Decimal(0)),
    ):
        orm = await db_session.get(SupportEscalation, row_id)
        assert orm is not None
        orm.status, orm.charge = status, charge
    await db_session.flush()

    lost = await store.list_requests(db_session, tenant_id=tenant.id, undelivered=True)
    every = await store.list_requests(db_session, tenant_id=tenant.id)

    assert [r.id for r in lost] == [pending], "only the pending one is lost"
    assert {r.id for r in every} == {pending, settled, declined}, "the plain list keeps all"
