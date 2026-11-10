"""Two writers that read the same value and both write: the second is refused."""

from __future__ import annotations

import asyncio

from daimon.core.channel_changes import Expected
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.errors import StaleChannelValue
from daimon.core.features.service import FeatureActor, change_feature
from daimon.core.features.snapshot import FeaturePlace
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

SETTINGS = Settings.model_validate(
    {
        "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
        "anthropic": AnthropicSettings(api_key=SecretStr("test")),
        "discord": DiscordSettings(bot_token=SecretStr("test")),
    }
)


async def _admin(_: object) -> bool:
    return True


async def test_two_writers_with_the_same_expected_value_cannot_both_write(
    db_session: AsyncSession, db_committing_engine: AsyncEngine
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    place = FeaturePlace(tenant.id, "discord")
    actor = FeatureActor(tenant.id, None, "web", None, _admin)

    async def write(value: str) -> object:
        return await change_feature(
            sessions,
            SETTINGS,
            actor,
            key="thread_naming",
            tier="workspace",
            value=value,
            place=place,
            expected=Expected("default"),
        )

    results = await asyncio.gather(write("on"), write("off"), return_exceptions=True)
    refused = [r for r in results if isinstance(r, StaleChannelValue)]
    assert len(refused) == 1, results
    assert len([r for r in results if not isinstance(r, BaseException)]) == 1
