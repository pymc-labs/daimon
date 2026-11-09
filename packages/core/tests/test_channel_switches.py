"""Thread participation and progress plans set through one recorded service."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.authz import Subject
from daimon.core.channel_changes import ChannelActor, ChannelChangeRefused, Expected
from daimon.core.channel_switches import WORKSPACE_ID, SwitchChange, SwitchTarget, set_switch
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.errors import StaleChannelValue
from daimon.core.scope_cascade import CascadeMode, CascadeScope
from daimon.core.stores.channel_changes import list_changes
from daimon.core.stores.switch_modes import list_switch_modes
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ON, OFF, DISABLED = CascadeMode.ON, CascadeMode.OFF, CascadeMode.DISABLED
SETTINGS = Settings.model_validate(
    {
        "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
        "anthropic": AnthropicSettings(api_key=SecretStr("test")),
        "discord": DiscordSettings(bot_token=SecretStr("test")),
    }
)


def _actor(tenant_id: uuid.UUID, *, admin: bool = True) -> ChannelActor:
    return ChannelActor(
        tenant_id=tenant_id, account_id=None, subject=Subject(is_admin=admin), surface="web"
    )


async def test_refusals_come_before_any_write(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    cases: list[tuple[str, ChannelActor, str, CascadeMode, SwitchTarget, CascadeMode | None]] = [
        ("wrong_platform", _actor(tenant.id), "slack", ON, SwitchTarget.channel("c1"), ON),
        (
            "deployment_disabled",
            _actor(tenant.id),
            "discord",
            DISABLED,
            SwitchTarget.channel("c1"),
            ON,
        ),
        (
            "admin_required",
            _actor(tenant.id, admin=False),
            "discord",
            ON,
            SwitchTarget.channel("c1"),
            ON,
        ),
        (
            "thread_disabled",
            _actor(tenant.id),
            "discord",
            ON,
            SwitchTarget.thread("t1", "c1"),
            DISABLED,
        ),
    ]
    for reason, actor, platform, deployment, target, mode in cases:
        with pytest.raises(ChannelChangeRefused) as refused:
            await set_switch(
                db_session_factory,
                actor,
                "participation",
                platform=platform,
                deployment=deployment,
                target=target,
                mode=mode,
                settings=SETTINGS,
            )
        assert refused.value.reason == reason, f"{reason} is refused as such"
    assert await list_changes(db_session, tenant_id=tenant.id, channel_id="c1") == []


async def test_a_member_sets_a_thread_and_it_lists_under_its_channel(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    change = await set_switch(
        db_session_factory,
        _actor(tenant.id, admin=False),
        "progress_plans",
        platform="slack",
        deployment=OFF,
        target=SwitchTarget.thread("c1:171.1", "c1"),
        mode=ON,
        settings=SETTINGS,
    )
    assert (change.before, change.after, change.effective.tier) == (None, ON, "thread")
    threads = await list_switch_modes(
        db_session,
        "progress_plans",
        tenant_id=tenant.id,
        platform="slack",
        scope=CascadeScope.THREAD,
        parent_id="c1",
    )
    assert threads == {"c1:171.1": ON}, "the thread row knows its channel"
    [record] = await list_changes(db_session, tenant_id=tenant.id, channel_id="c1")
    assert (record.setting, record.after) == (
        "thread_progress_plans",
        {"thread": "c1:171.1", "mode": "on"},
    )


async def test_channel_tier_is_compared_and_a_disabled_workspace_wins(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    actor = _actor(tenant.id)

    async def put(
        target: SwitchTarget, mode: CascadeMode | None, expected: CascadeMode | None
    ) -> SwitchChange:
        return await set_switch(
            db_session_factory,
            actor,
            "participation",
            platform="discord",
            deployment=OFF,
            target=target,
            mode=mode,
            settings=SETTINGS,
            expected=Expected(expected),
        )

    await put(SwitchTarget.channel("c1"), ON, None)
    with pytest.raises(StaleChannelValue):
        await put(SwitchTarget.channel("c1"), OFF, None)
    await put(SwitchTarget.workspace(), DISABLED, None)
    with pytest.raises(ChannelChangeRefused) as above:
        await put(SwitchTarget.channel("c1"), ON, ON)
    assert above.value.reason == "disabled_above"
    cleared = await put(SwitchTarget.channel("c1"), None, ON)
    assert cleared.effective.mode is DISABLED, "the workspace's disabled is final below it"

    channel = await list_changes(db_session, tenant_id=tenant.id, channel_id="c1")
    workspace = await list_changes(db_session, tenant_id=tenant.id, channel_id=WORKSPACE_ID)
    assert [(c.before, c.after) for c in channel] == [("on", None), (None, "on")]
    assert [(c.setting, c.after) for c in workspace] == [("participation", "disabled")]
