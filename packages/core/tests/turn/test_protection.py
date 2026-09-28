"""protection_state: may the agent post anything where this turn would answer?"""

from __future__ import annotations

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.turn.protection import CategoryLookup, ProtectionState, protection_state
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _category(
    category_id: str | None, *, unresolved: bool = False, calls: list[str] | None = None
) -> CategoryLookup:
    async def _lookup() -> tuple[str | None, bool]:
        if calls is not None:
            calls.append("lookup")
        return category_id, unresolved

    return _lookup


@pytest.mark.parametrize(
    ("policy", "channel_id", "thread_id", "category", "expected"),
    [
        (None, "c1", None, None, ProtectionState.UNPROTECTED),
        (
            TenantAccessPolicy(protected_channel_ids=("c1",)),
            "c1",
            None,
            None,
            ProtectionState.PROTECTED,
        ),
        (
            TenantAccessPolicy(protected_channel_ids=("c1",)),
            "c1",
            "t1",
            None,
            ProtectionState.PROTECTED,
        ),
        (
            TenantAccessPolicy(protected_channel_ids=("t1",)),
            "c1",
            "t1",
            None,
            ProtectionState.PROTECTED,
        ),
        (
            TenantAccessPolicy(protected_category_ids=("k1",)),
            "c1",
            None,
            ("k1", False),
            ProtectionState.PROTECTED,
        ),
        (
            TenantAccessPolicy(protected_category_ids=("k1",)),
            "c1",
            None,
            ("k2", False),
            ProtectionState.UNPROTECTED,
        ),
        (
            TenantAccessPolicy(protected_category_ids=("k1",)),
            "c1",
            "t1",
            (None, True),
            ProtectionState.PROTECTED,
        ),
    ],
    ids=[
        "no-policy",
        "channel",
        "thread-under-channel",
        "thread-itself",
        "category",
        "other-category",
        "unresolved-category-fails-closed",
    ],
)
async def test_protection_state_follows_the_policy(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy | None,
    channel_id: str,
    thread_id: str | None,
    category: tuple[str | None, bool] | None,
    expected: ProtectionState,
) -> None:
    tenant = await make_tenant(db_session)
    if policy is not None:
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()

    state = await protection_state(
        db_session_factory,
        tenant_id=tenant.id,
        channel_id=channel_id,
        thread_id=thread_id,
        resolve_category=(
            _category(category[0], unresolved=category[1]) if category is not None else None
        ),
    )

    assert state is expected
    assert state.may_post is (expected is ProtectionState.UNPROTECTED)


@pytest.mark.parametrize(
    "policy",
    [
        TenantAccessPolicy(protected_channel_ids=("x",)),
        TenantAccessPolicy(protected_channel_ids=("c1",), protected_category_ids=("k1",)),
    ],
    ids=["no-category-policy", "channel-already-protected"],
)
async def test_the_category_lookup_runs_only_when_it_can_change_the_answer(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
) -> None:
    tenant = await make_tenant(db_session)
    await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()
    calls: list[str] = []

    await protection_state(
        db_session_factory,
        tenant_id=tenant.id,
        channel_id="c1",
        resolve_category=_category("k1", calls=calls),
    )

    assert calls == [], "no lookup (and no platform call) when the category can't matter"


async def test_an_unreadable_policy_is_unknown(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.execute(
        text("INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:t, 'null')"),
        {"t": tenant.id},
    )
    await db_session.commit()

    state = await protection_state(db_session_factory, tenant_id=tenant.id, channel_id="c1")

    assert state is ProtectionState.UNKNOWN and not state.may_post


async def test_a_failed_policy_read_is_unknown(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()

    async def _fail(*_args: object, **_kwargs: object) -> object:
        raise OperationalError("SELECT", {}, Exception("pool exhausted"))

    monkeypatch.setattr("daimon.core.turn.protection.load_access_policy", _fail)

    state = await protection_state(db_session_factory, tenant_id=tenant.id, channel_id="c1")

    assert state is ProtectionState.UNKNOWN


@pytest.mark.parametrize("error", [OSError("reset"), TimeoutError(), RuntimeError("bug")])
async def test_a_failed_category_lookup_is_unknown(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    error: Exception,
) -> None:
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session, tenant_id=tenant.id, policy=TenantAccessPolicy(protected_category_ids=("k1",))
    )
    await db_session.commit()

    async def _broken() -> tuple[str | None, bool]:
        raise error

    state = await protection_state(
        db_session_factory, tenant_id=tenant.id, channel_id="c1", resolve_category=_broken
    )

    assert state is ProtectionState.UNKNOWN
