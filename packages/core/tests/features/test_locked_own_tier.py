"""A value an admin locked at a tier stays: whoever may not lock cannot overwrite or clear it,
on chat and on the web alike."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.admin_actions import DelegatedAction
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    Settings,
)
from daimon.core.features.registry import DEFAULT
from daimon.core.features.service import FeatureActor, change_feature, change_refusal
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.stores.domain import ChangeSurface
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KEY = "completion_pings"


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
        }
    )


def _actor(
    tenant_id: uuid.UUID, surface: ChangeSurface, *, admin: bool, owner: bool
) -> FeatureActor:
    async def admin_for(_: DelegatedAction | None) -> bool:
        return admin

    async def owns_agent(_: str) -> bool:
        return owner

    return FeatureActor(tenant_id, None, surface, None, admin_for, owns_agent=owns_agent)


@pytest.mark.parametrize("surface", ["chat", "web"])
@pytest.mark.parametrize("value", ["on", DEFAULT])
async def test_an_owner_cannot_overwrite_or_clear_an_admins_lock_at_the_agent_tier(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    surface: ChangeSurface,
    value: str,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    settings = _settings()
    place = FeaturePlace(tenant.id, "discord", "C1", agent_id="agent_1")
    admin = _actor(tenant.id, surface, admin=True, owner=False)
    owner = _actor(tenant.id, surface, admin=False, owner=True)
    await change_feature(
        db_session_factory, settings, admin, key=KEY, tier="agent", value="off", place=place,
        locked=True,
    )  # fmt: skip
    with pytest.raises(ChannelChangeRefused, match="locked") as raised:
        await change_feature(
            db_session_factory, settings, owner, key=KEY, tier="agent", value=value, place=place
        )
    assert raised.value.reason == "locked_here", "the refusal names the lock"
    found = await feature_in(db_session, settings, place, KEY)
    assert (found.value, found.tier) == ("off", "agent"), "the locked value is untouched"
    await change_feature(
        db_session_factory, settings, admin, key=KEY, tier="agent", value="on", place=place
    )
    found = await feature_in(db_session, settings, place, KEY)
    assert found.value == "on", "an admin still changes a lock"


@pytest.mark.parametrize("surface", ["chat", "web"])
async def test_an_owner_still_changes_an_unlocked_agent_value(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    surface: ChangeSurface,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    settings = _settings()
    place = FeaturePlace(tenant.id, "discord", "C1", agent_id="agent_1")
    owner = _actor(tenant.id, surface, admin=False, owner=True)
    await change_feature(
        db_session_factory, settings, owner, key=KEY, tier="agent", value="off", place=place
    )
    await change_feature(
        db_session_factory, settings, owner, key=KEY, tier="agent", value="on", place=place
    )
    found = await feature_in(db_session, settings, place, KEY)
    assert (found.value, found.tier) == ("on", "agent"), "no lock, no refusal"


async def test_the_early_refusal_names_a_lock_at_the_own_tier(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    settings = _settings()
    place = FeaturePlace(tenant.id, "discord", "C1", agent_id="agent_1")
    admin = _actor(tenant.id, "web", admin=True, owner=False)
    owner = _actor(tenant.id, "web", admin=False, owner=True)
    open_to_owner = await change_refusal(
        db_session_factory, settings, owner, key=KEY, tier="agent", place=place, value="on"
    )
    assert open_to_owner is None, "an unlocked value shows its control"
    await change_feature(
        db_session_factory, settings, admin, key=KEY, tier="agent", value="off", place=place,
        locked=True,
    )  # fmt: skip
    early = await change_refusal(
        db_session_factory, settings, owner, key=KEY, tier="agent", place=place, value="on"
    )
    assert early is not None and early.reason == "locked_here", "refused before a card"
    by_admin = await change_refusal(
        db_session_factory, settings, admin, key=KEY, tier="agent", place=place, value="on"
    )
    assert by_admin is None, "an admin may still change a lock"
