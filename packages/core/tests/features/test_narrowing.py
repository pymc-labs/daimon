"""A ceiling or a limit narrows below the tier above, like a guardrail: a write that
resolution would override is refused, and every feature write can be listed back."""

from __future__ import annotations

import pytest
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.features.service import FeatureActor, change_feature, change_refusal
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.stores.channel_changes import list_changes
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
        }
    )


async def _admin(_: object) -> bool:
    return True


async def test_a_channel_cannot_turn_on_a_capability_the_workspace_turned_off(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    settings = _settings()
    actor = FeatureActor(tenant.id, None, "web", None, _admin)
    place = FeaturePlace(tenant.id, "discord", "C1")
    await change_feature(
        db_session_factory, settings, actor, key="watches", tier="workspace", value="off",
        place=place,
    )  # fmt: skip
    early = await change_refusal(
        db_session_factory, settings, actor, key="watches", tier="channel", place=place,
        value="on",
    )  # fmt: skip
    assert early is not None and early.reason == "looser_than_above", "refused before a card"
    assert "can't turn it on" in str(early) and "Ask" in str(early), "names who can help"
    with pytest.raises(ChannelChangeRefused, match="can't turn it on"):
        await change_feature(
            db_session_factory, settings, actor, key="watches", tier="channel", value="on",
            place=place,
        )  # fmt: skip
    await change_feature(
        db_session_factory, settings, actor, key="watches", tier="channel", value="off",
        place=place,
    )  # fmt: skip
    found = await feature_in(db_session, settings, place, "watches")
    assert (found.value, found.tier) == ("off", "workspace"), "Off below Off is allowed"


async def test_a_channel_cannot_raise_a_limit_above_the_workspace(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    settings = _settings()
    actor = FeatureActor(tenant.id, None, "web", None, _admin)
    place = FeaturePlace(tenant.id, "discord", "C1")
    await change_feature(
        db_session_factory, settings, actor, key="history_depth", tier="workspace", value="25",
        place=place,
    )  # fmt: skip
    with pytest.raises(ChannelChangeRefused, match="only choose that or lower") as raised:
        await change_feature(
            db_session_factory, settings, actor, key="history_depth", tier="channel",
            value="100", place=place,
        )  # fmt: skip
    assert raised.value.reason == "looser_than_above"
    await change_feature(
        db_session_factory, settings, actor, key="history_depth", tier="channel", value="10",
        place=place,
    )  # fmt: skip
    found = await feature_in(db_session, settings, place, "history_depth")
    assert (found.value, found.tier) == ("10", "channel"), "a lower step is allowed"


@pytest.mark.parametrize(
    ("key", "tier", "value", "setting"),
    [
        ("watches", "channel", "off", "watches"),
        ("outbound_places", "channel", "1", "outbound_places"),
        ("participation", "thread", "on", "thread_participation"),
    ],
)
async def test_the_channel_history_lists_every_feature_write(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    key: str,
    tier: str,
    value: str,
    setting: str,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    actor = FeatureActor(tenant.id, None, "web", None, _admin)
    place = FeaturePlace(tenant.id, "discord", "C1", thread_id="T1")
    await change_feature(
        db_session_factory,
        _settings(),
        actor,
        key=key,
        tier=tier,
        value=value,
        place=place,  # pyright: ignore[reportArgumentType]
    )
    async with db_session_factory() as session:
        changes = await list_changes(session, tenant_id=tenant.id, channel_id="C1")
    assert [change.setting for change in changes] == [setting], "a non-legacy key lists back"
