"""A scoped shared connection follows the store's grants, as data reads do."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DataSettings,
    McpSettings,
    Settings,
)
from daimon.core.data_audience import DataAudience
from daimon.data import pool
from daimon.data.access import person
from daimon.data.shared_scopes import holds_scope
from daimon.testing.data_db import DataDb
from pydantic import PostgresDsn, SecretStr


def _settings(url: str, data: DataSettings) -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn(url)),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("s" * 32)),
        data=data,
    )


def _audience(*people: uuid.UUID) -> DataAudience:
    return DataAudience(frozenset(people), frozenset(), False, False, False)


@pytest_asyncio.fixture
async def fresh_pools() -> AsyncIterator[None]:
    await pool.close_pools()
    yield
    await pool.close_pools()


async def test_a_scope_is_held_by_its_holders_only_and_never_without_the_store(
    data_db: DataDb, fresh_pools: None
) -> None:
    tenant, holder, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    missing = _settings(data_db.url, DataSettings(schema_name="no_store_here"))
    assert not await holds_scope(missing, tenant, _audience(holder), "sales"), "no store, no scope"
    await pool.close_pools()  # it remembers a missing store for a minute

    d = data_db.schemas.data
    async with data_db.aconnect() as conn:
        await conn.execute(
            f"insert into {d}.scopes (tenant_id, id, kind, name)"
            " values (%s, 'sales', 'internal', 'Sales')".encode(),
            (tenant,),
        )
        await conn.execute(
            f"insert into {d}.grants (tenant_id, scope, holder) values (%s, 'sales', %s)".encode(),
            (tenant, person(holder)),
        )
    settings = _settings(data_db.url, data_db.settings)
    assert await holds_scope(settings, tenant, _audience(holder), "sales"), "its holder holds it"
    assert not await holds_scope(settings, tenant, _audience(other), "sales"), "others do not"
    assert not await holds_scope(settings, tenant, _audience(holder, other), "sales"), (
        "a room holds it only when everyone in it does"
    )
