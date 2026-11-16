"""Unattended runs read their own time limit, and every wait that outlives one covers the longest."""

from __future__ import annotations

from datetime import timedelta

from daimon.core.continuity.wakes import WAKE_RUN_LEASE
from daimon.core.features.registry import FEATURES
from daimon.core.features.service import set_workspace_feature
from daimon.core.headless_runner import unattended_ceiling_s
from daimon.core.turn.ceiling import LONGEST_TURN_CEILING_S, TURN_CEILING_S
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_both_time_limits_offer_the_same_choices() -> None:
    chat, unattended = FEATURES["turn_time_limit"], FEATURES["unattended_time_limit"]
    assert dict(chat.values) == dict(unattended.values), "the two limits stay in step"
    assert set(chat.values) == {"15", "20", "30", "45", "90", "180"}
    assert (chat.default, unattended.default) == ("45", "45"), "the standard stays 45 minutes"
    assert (chat.tiers, unattended.tiers) == (("workspace", "agent"),) * 2


def test_a_woken_turn_lease_covers_the_longest_limit() -> None:
    assert timedelta(seconds=LONGEST_TURN_CEILING_S) < WAKE_RUN_LEASE


async def test_a_headless_run_reads_the_unattended_limit(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()

    async def ceiling() -> float:
        return await unattended_ceiling_s(
            db_session_factory,
            None,
            tenant_id=tenant.id,
            agent_id="ag_1",
            platform="discord",
            channel_id="chan-1",
            thread_id=None,
        )

    assert await ceiling() == TURN_CEILING_S, "the default stays 45 minutes"
    for key, value, expected in (
        ("turn_time_limit", "15", TURN_CEILING_S),
        ("unattended_time_limit", "180", LONGEST_TURN_CEILING_S),
        ("unattended_time_limit", "30", 30 * 60.0),
    ):
        await set_workspace_feature(
            db_session,
            None,
            tenant_id=tenant.id,
            platform="discord",
            key=key,
            value=value,
            account_id=None,
            surface="cli",
        )
        await db_session.commit()
        assert await ceiling() == expected, f"{key}={value}"


async def test_a_headless_run_without_a_workspace_keeps_45_minutes(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert (
        await unattended_ceiling_s(
            db_session_factory,
            None,
            tenant_id=None,
            agent_id="ag_1",
            platform=None,
            channel_id=None,
            thread_id=None,
        )
        == TURN_CEILING_S
    )
