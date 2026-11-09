"""`import_env` copies the deprecated per-workspace env maps once, and the marker ends their say."""

from __future__ import annotations

from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.features.registry import ENV_IMPORTED
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


async def test_the_env_map_applies_until_imported_then_the_row_does(
    db_session: AsyncSession,
) -> None:
    """Before the import the env value reads as the workspace tier; after it only the rows do."""
    tenant = await make_tenant(db_session)
    settings = _settings(completion_pings={tenant.id: True})
    place = FeaturePlace(tenant.id, "discord")
    before = await feature_in(db_session, settings, place, "completion_pings")
    assert (before.on, before.tier) == (True, "workspace"), "the env map is honoured at first"

    assert await import_env(db_session, settings) == 1, "one workspace was imported"
    rows = await list_feature_values(
        db_session, tenant_id=tenant.id, keys=["completion_pings", ENV_IMPORTED], tier="workspace"
    )
    assert {row.key: row.value for row in rows} == {"completion_pings": "on", ENV_IMPORTED: "done"}

    await set_feature_value(
        db_session,
        tenant_id=tenant.id,
        target=FeatureTarget("workspace"),
        key="completion_pings",
        value="off",
        set_by=None,
        surface="cli",
    )
    after = await feature_in(db_session, settings, place, "completion_pings")
    assert after.on is False, "a left-set variable no longer overrides the stored value"


async def test_importing_twice_keeps_what_was_changed_since(db_session: AsyncSession) -> None:
    """A second run skips an imported workspace, so a later change is never reverted."""
    tenant = await make_tenant(db_session)
    settings = _settings(table_rendering={tenant.id: False})
    await import_env(db_session, settings)
    await set_feature_value(
        db_session,
        tenant_id=tenant.id,
        target=FeatureTarget("workspace"),
        key="table_rendering",
        value="on",
        set_by=None,
        surface="cli",
    )
    assert await import_env(db_session, settings) == 0, "nothing left to import"
    place = FeaturePlace(tenant.id, "discord")
    assert (await feature_in(db_session, settings, place, "table_rendering")).on is True
