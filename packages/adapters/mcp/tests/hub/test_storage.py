"""The hub key-value store isolates platforms by collection prefix and encrypts values at rest."""

from __future__ import annotations

from cryptography.fernet import Fernet, MultiFernet
from daimon.adapters.mcp.hub.storage import (
    HUB_KV_POOL_MAX,
    asyncpg_dsn,
    build_hub_kv_base,
    hub_kv_for,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


def test_asyncpg_dsn_strips_sqlalchemy_driver() -> None:
    assert asyncpg_dsn("postgresql+asyncpg://u:p@h:5432/d") == "postgresql://u:p@h:5432/d"
    assert asyncpg_dsn("postgresql://u:p@h/d") == "postgresql://u:p@h/d"


async def test_platforms_share_the_table_but_not_keys(
    db_engine: AsyncEngine, db_session: AsyncSession, db_schema: str
) -> None:
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    dsn = asyncpg_dsn(str(db_engine.url.render_as_string(hide_password=False)))
    dsn = f"{dsn}?options=-csearch_path%3D{db_schema}"
    store, base = build_hub_kv_base(database_url=dsn, fernet=fernet)
    slack = hub_kv_for(base, platform="slack")
    discord = hub_kv_for(base, platform="discord")

    async with store:
        await slack.put("k", {"v": "slack"}, collection="mcp-oauth-proxy-clients")
        await discord.put("k", {"v": "discord"}, collection="mcp-oauth-proxy-clients")

        assert await slack.get("k", collection="mcp-oauth-proxy-clients") == {"v": "slack"}
        assert await discord.get("k", collection="mcp-oauth-proxy-clients") == {"v": "discord"}

    rows = (
        await db_session.execute(
            text("SELECT collection, value::text FROM hub_oauth_kv ORDER BY collection")
        )
    ).all()
    assert [r[0] for r in rows] == [
        "discord__mcp-oauth-proxy-clients",
        "slack__mcp-oauth-proxy-clients",
    ], f"got {rows!r}"
    assert all("slack" not in r[1] and "discord" not in r[1] for r in rows), (
        f"values must be encrypted at rest, got {rows!r}"
    )


async def test_store_pool_stays_small(db_engine: AsyncEngine, db_schema: str) -> None:
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    dsn = asyncpg_dsn(str(db_engine.url.render_as_string(hide_password=False)))
    dsn = f"{dsn}?options=-csearch_path%3D{db_schema}"
    store, _ = build_hub_kv_base(database_url=dsn, fernet=fernet)

    async with store:
        pool = store._initialized_pool  # pyright: ignore[reportPrivateUsage]
        assert pool.get_min_size() == 1
        assert pool.get_max_size() == HUB_KV_POOL_MAX
        assert pool.get_size() == 1, "only one connection is opened at startup"
