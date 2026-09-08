"""The hub key-value store isolates platforms by collection prefix and encrypts values at rest."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, MultiFernet
from daimon.adapters.mcp.hub.storage import asyncpg_dsn, build_hub_kv_base, hub_kv_for
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.asyncio


def test_asyncpg_dsn_strips_sqlalchemy_driver() -> None:
    assert asyncpg_dsn("postgresql+asyncpg://u:p@h:5432/d") == "postgresql://u:p@h:5432/d"
    assert asyncpg_dsn("postgresql://u:p@h/d") == "postgresql://u:p@h/d"


async def test_platforms_share_the_table_but_not_keys(
    db_engine: AsyncEngine, db_session: AsyncSession, test_schema: str
) -> None:
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    dsn = asyncpg_dsn(str(db_engine.url.render_as_string(hide_password=False)))
    dsn = f"{dsn}?options=-csearch_path%3D{test_schema}"
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
