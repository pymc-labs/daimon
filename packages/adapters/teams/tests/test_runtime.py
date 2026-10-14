"""Teams turn deps: derived from settings like the other chat adapters'."""

from __future__ import annotations

import asyncio
from secrets import randbits
from unittest.mock import MagicMock

from daimon.adapters.teams.runtime import hold_owner_key
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.turn.deps import build_turn_deps
from daimon.testing import build_fake_anthropic, make_agent_env_echo_handler
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_HOLDER = text("""
    SELECT pid FROM pg_locks
    WHERE locktype = 'advisory' AND granted AND objsubid = 1
      AND classid = CAST(:upper AS oid) AND objid = CAST(:lower AS oid)
""")


def test_turn_deps_carry_the_tool_safety_policy() -> None:
    """Without it, a Teams turn runs gated writes unconfirmed while tool safety is on."""
    settings = MagicMock()
    settings.crypto.keys = ()
    settings.mcp.public_url = None
    settings.tool_safety = ToolSafetyPolicy(enabled=True)

    deps = build_turn_deps(
        settings,
        build_fake_anthropic(make_agent_env_echo_handler()),
        MagicMock(),
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        billing_config=None,
    )

    assert deps.tool_safety is settings.tool_safety, "Teams must gate writes like Discord and Slack"


async def test_the_owner_key_is_taken_again_when_its_connection_drops(
    db_nullpool_engine: AsyncEngine,
) -> None:
    """Unheld, the key reads as an exited process and its messages get replayed elsewhere."""
    key = randbits(63)
    params = {"upper": key >> 32, "lower": key & 0xFFFFFFFF}
    async with (
        hold_owner_key(db_nullpool_engine, key, watch_interval_s=0.05),
        db_nullpool_engine.connect() as admin,
    ):
        first = await admin.scalar(_HOLDER, params)
        await admin.execute(text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": first})
        await admin.commit()
        async with asyncio.timeout(5):
            while (holder := await admin.scalar(_HOLDER, params)) in (None, first):
                await admin.commit()
                await asyncio.sleep(0.05)

    assert holder not in (None, first), "a new connection holds the key"
