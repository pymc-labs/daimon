"""turn_target_protected: may a turn in this channel post anything at all?"""

from __future__ import annotations

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.turn.protection import turn_target_protected
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize(
    ("policy", "channel_id", "thread_id", "category_id", "category_unresolved", "expected"),
    [
        (None, "c1", None, None, False, False),
        (TenantAccessPolicy(protected_channel_ids=("c1",)), "c1", None, None, False, True),
        (TenantAccessPolicy(protected_channel_ids=("c1",)), "c1", "t1", None, False, True),
        (TenantAccessPolicy(protected_channel_ids=("t1",)), "c1", "t1", None, False, True),
        (TenantAccessPolicy(protected_category_ids=("k1",)), "c1", None, "k1", False, True),
        (TenantAccessPolicy(protected_category_ids=("k1",)), "c1", "t1", None, True, True),
        (TenantAccessPolicy(protected_channel_ids=("c9",)), "c1", "t1", None, True, False),
    ],
    ids=[
        "no-policy",
        "channel",
        "thread-under-channel",
        "thread-itself",
        "category",
        "unresolved-category-fails-closed",
        "unresolved-category-without-category-policy",
    ],
)
async def test_turn_target_protected_follows_the_policy(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy | None,
    channel_id: str,
    thread_id: str | None,
    category_id: str | None,
    category_unresolved: bool,
    expected: bool,
) -> None:
    tenant = await make_tenant(db_session)
    if policy is not None:
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()

    protected = await turn_target_protected(
        db_session_factory,
        tenant_id=tenant.id,
        channel_id=channel_id,
        thread_id=thread_id,
        category_id=category_id,
        category_unresolved=category_unresolved,
    )

    assert protected is expected


async def test_an_unreadable_policy_counts_as_protected(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """We can't tell whether the channel is protected, so nothing is posted."""
    tenant = await make_tenant(db_session)
    await db_session.execute(
        text("INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:t, 'null')"),
        {"t": tenant.id},
    )
    await db_session.commit()

    assert await turn_target_protected(db_session_factory, tenant_id=tenant.id, channel_id="c1")
