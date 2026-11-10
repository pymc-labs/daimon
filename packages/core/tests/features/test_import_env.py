"""A deprecated per-workspace env map is a workspace value that a stored row beats."""

from __future__ import annotations

from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.features.service import import_env
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import list_feature_values, set_feature_value
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession


def _settings(**maps: dict[object, bool]) -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
            **maps,
        }
    )


async def _set(db_session: AsyncSession, tenant_id: object, key: str, value: str) -> None:
    await set_feature_value(
        db_session,
        tenant_id=tenant_id,  # pyright: ignore[reportArgumentType]
        target=FeatureTarget("workspace"),
        key=key,
        value=value,
        set_by=None,
        surface="cli",
    )


async def test_a_workspace_row_beats_the_env_map(db_session: AsyncSession) -> None:
    """The env map reads as the workspace value until a row is stored; then the row wins."""
    tenant = await make_tenant(db_session)
    settings = _settings(completion_pings={tenant.id: True})
    place = FeaturePlace(tenant.id, "discord")
    before = await feature_in(db_session, settings, place, "completion_pings")
    assert (before.on, before.tier) == (True, "workspace"), "the env map is honoured"

    await _set(db_session, tenant.id, "completion_pings", "off")
    after = await feature_in(db_session, settings, place, "completion_pings")
    assert after.on is False, "a left-set variable never overrides the stored value"


async def test_import_writes_only_the_missing_rows(db_session: AsyncSession) -> None:
    """Importing fills the gaps and keeps a value stored since, however often it runs."""
    tenant = await make_tenant(db_session)
    settings = _settings(completion_pings={tenant.id: True}, table_rendering={tenant.id: False})
    await _set(db_session, tenant.id, "table_rendering", "on")
    assert await import_env(db_session, settings) == 1, "only the missing row was written"
    rows = await list_feature_values(
        db_session,
        tenant_id=tenant.id,
        keys=["completion_pings", "table_rendering"],
        tier="workspace",
    )
    assert {row.key: row.value for row in rows} == {
        "completion_pings": "on",
        "table_rendering": "on",
    }
    assert await import_env(db_session, settings) == 0, "nothing left to write"
