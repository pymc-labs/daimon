"""`routine_result_header`: which tier decides whether a routine's result keeps its header."""

from __future__ import annotations

import pytest
from daimon.core.features.registry import FEATURES
from daimon.core.routine_header import routine_header_shown
from daimon.core.stores.domain import FeatureTarget, RoutineRow
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.routines import create_routine
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KEY = "routine_result_header"
Sessions = async_sessionmaker[AsyncSession]


async def _routine(db_session: AsyncSession, **kw: object) -> RoutineRow:
    tenant = await make_tenant(db_session)
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="123",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="nightly",
        destination_kind="channel",
        destination_id="C1",
        channel_id="C1",
        **kw,  # pyright: ignore[reportArgumentType]
    )
    await db_session.commit()
    return row


async def _set(sessions: Sessions, row: RoutineRow, target: FeatureTarget, value: str) -> None:
    async with sessions.begin() as session:
        await set_feature_value(
            session, tenant_id=row.tenant_id, target=target, key=KEY, value=value,
            set_by=None, surface="cli",
        )  # fmt: skip


def test_the_header_is_on_unless_set_and_takes_the_five_tiers() -> None:
    spec = FEATURES[KEY]
    assert spec.default == "on", "nothing changes until someone turns it off"
    assert spec.tiers == ("deployment", "workspace", "agent", "channel", "routine")


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_each_tier_can_turn_the_header_off_and_the_nearest_decides(
    db_session: AsyncSession, db_session_factory: Sessions, platform: str
) -> None:
    """Workspace, agent and channel Off hide it; a routine's own On brings it back."""
    row = await _routine(db_session)
    assert await routine_header_shown(db_session_factory, row, platform), "on by default"
    targets = {
        "workspace": FeatureTarget("workspace"),
        "agent": FeatureTarget("agent", agent_id=row.agent_id),
        "channel": FeatureTarget("channel", channel_id="C1"),
    }
    for name, target in targets.items():
        await _set(db_session_factory, row, target, "off")
        assert not await routine_header_shown(db_session_factory, row, platform), name
        await _set(db_session_factory, row, target, "on")
        assert await routine_header_shown(db_session_factory, row, platform), name
    await _set(db_session_factory, row, targets["agent"], "off")
    own = FeatureTarget("routine", routine_id=row.id)
    await _set(db_session_factory, row, own, "on")
    assert await routine_header_shown(db_session_factory, row, platform), (
        "the routine's own beats its agent's"
    )
    await _set(db_session_factory, row, own, "off")
    other = await _routine(db_session)
    assert not await routine_header_shown(db_session_factory, row, platform)
    assert await routine_header_shown(db_session_factory, other, platform), (
        "another routine is unaffected"
    )


async def test_a_dm_routine_follows_its_agent_and_workspace(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    tenant = await make_tenant(db_session)
    row = await create_routine(
        db_session, tenant_id=tenant.id, created_by_user_id="123", agent_id="agent_a",
        agent_name="daimon", cron_expr="0 9 * * *", timezone_="UTC", trigger_message="nightly",
        destination_kind="dm", destination_id="123",
    )  # fmt: skip
    await db_session.commit()
    await _set(db_session_factory, row, FeatureTarget("agent", agent_id="agent_a"), "off")
    assert not await routine_header_shown(db_session_factory, row, "discord")
