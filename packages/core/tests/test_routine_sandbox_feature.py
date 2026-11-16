"""`routine_sandbox_tools` on the feature system: tiers, locks, pins, drift and history."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from daimon.core import agent_convert
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.features.registry import DEFAULT
from daimon.core.features.service import FeatureActor, change_feature
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.routine_sandbox import (
    SANDBOX_KEY,
    builtins_allowed,
    change_routine_sandbox,
    own_opt_in,
    routine_opt_in_pin,
    routine_place,
    routine_sandbox,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.channel_changes import WORKSPACE_ID, list_changes
from daimon.core.stores.domain import AgentSandboxPin, FeatureTarget, RoutineRow, SandboxPin
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.routines import create_routine, update_routine
from daimon.core.stores.sandbox_pins import list_sandbox_pins, pin_json
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PIN = SandboxPin("agent_a", 3, "env_1", datetime(2026, 10, 1, tzinfo=UTC))
NEWER = SandboxPin("agent_a", 4, "env_1", datetime(2026, 10, 2, tzinfo=UTC))
AGENT_PIN = AgentSandboxPin("agent_a", "default")
SETTINGS = Settings.model_validate(
    {
        "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
        "anthropic": AnthropicSettings(api_key=SecretStr("test")),
        "discord": DiscordSettings(bot_token=SecretStr("test")),
    }
)
Sessions = async_sessionmaker[AsyncSession]


async def _admin(_action: object) -> bool:
    return True


async def _routine(db_session: AsyncSession) -> RoutineRow:
    tenant = await make_tenant(db_session)
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="owner-1",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="nightly",
    )
    await db_session.commit()
    return row


def _actor(row: RoutineRow) -> FeatureActor:
    return FeatureActor(row.tenant_id, None, "cli", None, _admin)


async def _set(sessions: Sessions, row: RoutineRow, tier: Any, value: str, **kw: Any) -> None:
    await change_feature(
        sessions, SETTINGS, _actor(row), key=SANDBOX_KEY, tier=tier, value=value,
        place=routine_place(row, "discord"), **kw,
    )  # fmt: skip


async def _pin(sessions: Sessions, row: RoutineRow) -> object:
    found = await routine_sandbox(sessions, SETTINGS, row, platform="discord")
    return None if found is None else found.pin


async def test_a_routine_own_value_overrides_its_agent(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """The routine tier is the most specific: its Off or On beats the agent's."""
    row = await _routine(db_session)
    assert await _pin(db_session_factory, row) is None, "off by default"
    await _set(db_session_factory, row, "agent", "on", pin=AGENT_PIN)
    assert await _pin(db_session_factory, row) == AGENT_PIN, "the agent's opt-in decides"
    await _set(db_session_factory, row, "routine", "off")
    assert await _pin(db_session_factory, row) is None, "the routine's Off beats the agent's On"
    await _set(db_session_factory, row, "agent", "off")
    await _set(db_session_factory, row, "routine", "on", pin=PIN)
    assert await _pin(db_session_factory, row) == PIN, "the routine's On beats the agent's Off"
    await _set(db_session_factory, row, "routine", DEFAULT)
    assert await _pin(db_session_factory, row) is None, "cleared, the agent's Off applies"


async def test_a_workspace_lock_wins_over_a_routine_on(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """An admin's lock above is final, whatever the routine sets."""
    row = await _routine(db_session)
    await _set(db_session_factory, row, "routine", "on", pin=PIN)
    await _set(db_session_factory, row, "workspace", "off", locked=True)
    assert await _pin(db_session_factory, row) is None, "the lock wins"
    with pytest.raises(ChannelChangeRefused):
        await _set(db_session_factory, row, "routine", "on", pin=NEWER)
    await _set(db_session_factory, row, "workspace", DEFAULT)
    assert await _pin(db_session_factory, row) == PIN, "unlocked, the routine's On applies"


async def test_unattended_capabilities_off_wins_over_an_opt_in(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """The guardrail caps an opted-in routine: its built-ins stay off."""
    row = await _routine(db_session)
    await _set(db_session_factory, row, "routine", "on", pin=PIN)
    args = (db_session_factory, SETTINGS, row.tenant_id, "discord", row.agent_id)
    assert await builtins_allowed(*args), "the guardrail is on by default"
    await change_feature(
        db_session_factory, SETTINGS, _actor(row), key="unattended_capabilities",
        tier="workspace", value="off", place=routine_place(row, "discord"),
    )  # fmt: skip
    assert not await builtins_allowed(*args), "the guardrail off wins"


async def test_an_on_needs_a_pin_at_the_agent_or_routine_tier(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """An On covers one agent or routine with what it pins; elsewhere it is refused."""
    row = await _routine(db_session)
    with pytest.raises(ChannelChangeRefused, match="one agent or one routine"):
        await _set(db_session_factory, row, "routine", "on")
    with pytest.raises(ChannelChangeRefused, match="one agent or one routine"):
        await _set(db_session_factory, row, "workspace", "on", pin=PIN)
    async with db_session_factory() as session:
        assert await list_sandbox_pins(session, tenant_id=row.tenant_id) == [], "nothing written"


async def test_an_on_without_its_pin_covers_nothing(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """A feature row whose pin is missing gives no built-ins."""
    row = await _routine(db_session)
    async with db_session_factory.begin() as session:
        await set_feature_value(
            session, tenant_id=row.tenant_id, key=SANDBOX_KEY, value="on", set_by=None,
            target=FeatureTarget("routine", routine_id=str(row.id)), surface="cli",
        )  # fmt: skip
    assert await _pin(db_session_factory, row) is None, "no pin, no built-ins"


async def test_each_change_is_recorded_with_its_pin(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """The settings history keeps what each On covered."""
    row = await _routine(db_session)
    await _set(db_session_factory, row, "routine", "on", pin=PIN)
    await _set(db_session_factory, row, "routine", "off")
    async with db_session_factory() as session:
        changes = await list_changes(session, tenant_id=row.tenant_id, channel_id=WORKSPACE_ID)
        pins = await list_sandbox_pins(session, tenant_id=row.tenant_id)
    on = {"routine": str(row.id), "value": "on", "pin": {
        "agent_id": "agent_a", "agent_version": 3, "environment_id": "env_1",
        "environment_updated_at": PIN.environment_updated_at.isoformat(),
    }}  # fmt: skip
    assert [(c.setting, c.before, c.after, c.surface) for c in reversed(changes)] == [
        (SANDBOX_KEY, None, on, "cli"),
        (SANDBOX_KEY, on, {"routine": str(row.id), "value": "off"}, "cli"),
    ], "each change and its pin"
    assert pins == [], "an Off drops the pin"


async def test_a_routine_drift_clears_only_the_opt_in_it_ran_under(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """A mismatched run clears the routine's On, unless an admin re-approved meanwhile."""
    row = await _routine(db_session)
    await _set(db_session_factory, row, "routine", "on", pin=PIN)
    stale = await routine_sandbox(db_session_factory, SETTINGS, row, platform="discord")
    await _set(db_session_factory, row, "routine", "on", pin=NEWER)
    assert stale is not None
    await stale.on_drift()
    assert await _pin(db_session_factory, row) == NEWER, "a fresh opt-in is kept"
    fresh = await routine_sandbox(db_session_factory, SETTINGS, row, platform="discord")
    assert fresh is not None
    await fresh.on_drift()
    assert await _pin(db_session_factory, row) is None, "the drifted opt-in is cleared"
    async with db_session_factory() as session:
        [last, *_] = await list_changes(session, tenant_id=row.tenant_id, channel_id=WORKSPACE_ID)
    on = {"routine": str(row.id), "value": "on", "pin": pin_json(NEWER)}
    assert (last.before, last.after, last.surface) == (on, None, "system"), (
        "recorded as Daimon's own change, with what the On covered"
    )


async def test_an_agent_drift_keeps_the_agent_opt_in(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """An agent's opt-in may still match its other routines, so a drift keeps it."""
    row = await _routine(db_session)
    await _set(db_session_factory, row, "agent", "on", pin=AGENT_PIN)
    found = await routine_sandbox(db_session_factory, SETTINGS, row, platform="discord")
    assert found is not None
    await found.on_drift()
    assert await _pin(db_session_factory, row) == AGENT_PIN, "kept"


async def test_a_material_edit_clears_the_routine_on_and_a_schedule_edit_keeps_it(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """Changing what was approved clears the routine's On; the schedule does not."""
    row = await _routine(db_session)
    await _set(db_session_factory, row, "routine", "on", pin=PIN)
    place = routine_place(row, "discord")
    async with db_session_factory.begin() as session:
        await update_routine(
            session, row.id, tenant_id=row.tenant_id, cron_expr="0 10 * * *", edited_by="owner-1"
        )
    assert await own_opt_in(db_session_factory, place, "routine") == ("on", PIN), "kept"
    async with db_session_factory.begin() as session:
        await update_routine(
            session, row.id, tenant_id=row.tenant_id, trigger_message="new", edited_by="owner-1"
        )
    assert await own_opt_in(db_session_factory, place, "routine") == (None, None), "cleared"
    async with db_session_factory() as session:
        [last, *_] = await list_changes(session, tenant_id=row.tenant_id, channel_id=WORKSPACE_ID)
    assert (last.after, last.surface) == (None, "system"), "recorded as Daimon's own change"


async def test_a_provider_move_clears_the_opt_ins_naming_the_old_agent(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """Moving an agent to another id clears its On and its routines' On; others stay."""
    row = await _routine(db_session)
    other = FeaturePlace(row.tenant_id, "discord", agent_id="agent_b")
    other_pin = AgentSandboxPin("agent_b", "default")
    await _set(db_session_factory, row, "agent", "on", pin=AGENT_PIN)
    await _set(db_session_factory, row, "routine", "on", pin=PIN)
    await change_feature(
        db_session_factory, SETTINGS, _actor(row), key=SANDBOX_KEY, tier="agent", value="on",
        place=other, pin=other_pin,
    )  # fmt: skip
    await agent_convert._move_rows(  # pyright: ignore[reportPrivateUsage]
        db_session_factory, tenant_id=row.tenant_id, name="daimon", old_id="agent_a", new_id="dm_x"
    )
    moved = FeaturePlace(row.tenant_id, "discord", agent_id="dm_x", routine_id=str(row.id))
    assert await own_opt_in(db_session_factory, moved, "agent") == (None, None), "not moved"
    assert await own_opt_in(db_session_factory, moved, "routine") == (None, None), "cleared"
    assert await own_opt_in(db_session_factory, other, "agent") == ("on", other_pin), "kept"
    async with db_session_factory() as session:
        pins = await list_sandbox_pins(session, tenant_id=row.tenant_id)
        changes = await list_changes(session, tenant_id=row.tenant_id, channel_id=WORKSPACE_ID)
    assert [entry.pin for entry in pins] == [other_pin], "no pin is left on the old id"
    assert [c.surface for c in changes[:2]] == ["system", "system"], "both clears recorded"


async def test_a_dm_routine_takes_no_on_of_its_own(db_session: AsyncSession) -> None:
    """A DM routine runs as its owner's private conversation: only its agent opts it in."""
    row = (await _routine(db_session)).model_copy(update={"destination_kind": "dm"})
    no_client: Any = None
    with pytest.raises(ChannelChangeRefused, match="DM routines"):
        await routine_opt_in_pin(
            no_client, async_sessionmaker(), row, value="on", default=DeploymentDefault()
        )
    assert (
        await routine_opt_in_pin(
            no_client, async_sessionmaker(), row, value="off", default=DeploymentDefault()
        )
        is None
    ), "an Off needs no pin"


async def test_setting_the_same_value_and_pin_again_writes_nothing(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """change_routine_sandbox is idempotent on the routine's own value and pin."""
    row = await _routine(db_session)
    write: dict[str, Any] = {"platform": "discord", "value": "on", "pin": PIN}
    first = await change_routine_sandbox(db_session_factory, SETTINGS, _actor(row), row, **write)
    again = await change_routine_sandbox(db_session_factory, SETTINGS, _actor(row), row, **write)
    assert first is not None and again is None, "the second write is a no-op"
