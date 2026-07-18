"""env-gated live integration test for self-edit tools.

The ``agent_files`` round-trip is local-Postgres only; the "live" aspect of
this test is that the tool implementation runs through a real
``AsyncAnthropic`` client constructed from a real API key, exercising the
production code paths end-to-end. SC-5 says "Live integration test against
MA confirms read-modify-write round-trip on ``agent_files``" — and
``agent_files`` is local DB, not MA-side, so the assertion is over the DB
round-trip while the runtime carries a real SDK client.

A full SSE-driven, MA-agent-invokes-the-tool live test is the spike-020
vertical slice (this directory's live integration tests,
19/19 cases passing). That slice + this test together form the SC-5
evidence trail.

Skipped unless BOTH:
  - DAIMON_RUN_LIVE_MA=1 (opt-in flag)
  - DAIMON_TEST_ANTHROPIC_API_KEY=<real-key>
"""

from __future__ import annotations

import contextlib
import os
import uuid

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.self_edit import (
    _self_delete_file_impl,  # pyright: ignore[reportPrivateUsage]
    _self_read_file_impl,  # pyright: ignore[reportPrivateUsage]
    _self_write_file_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    Settings,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.contract,
    pytest.mark.skipif(
        os.environ.get("DAIMON_RUN_LIVE_MA") != "1"
        or not os.environ.get("DAIMON_TEST_ANTHROPIC_API_KEY"),
        reason=(
            "live MA opt-in: set DAIMON_RUN_LIVE_MA=1 and DAIMON_TEST_ANTHROPIC_API_KEY=<real-key>"
        ),
    ),
]


def _live_settings(api_key: str) -> Settings:
    """Minimal Settings carrying the live API key. URLs are placeholders —
    the impls under test never read them; they exist to satisfy the model."""
    return Settings(
        database=DatabaseSettings(
            url=PostgresDsn("postgresql+asyncpg://u:p@h/d"),
        ),
        anthropic=AnthropicSettings(api_key=SecretStr(api_key)),
        mcp=McpSettings(  # pyright: ignore[reportArgumentType]
            jwt_secret=SecretStr("a" * 32),
            public_url=HttpUrl("https://x/mcp"),
        ),
        discord=DiscordSettings(bot_token=SecretStr("test-bot-token")),
    )


async def test_self_edit_live_round_trip(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """SC-5: agent_files write → read → write (update) → read round-trip
    against the live test schema, with a real AsyncAnthropic in the runtime.
    Cleanup is best-effort in `finally` to keep the test idempotent."""
    api_key = os.environ["DAIMON_TEST_ANTHROPIC_API_KEY"]
    client = AsyncAnthropic(api_key=api_key)

    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="self-edit-live")
        tenant_id = tenant.id

    runtime = McpRuntime(
        session_factory=committing_sessionmaker,
        client=client,
        settings=_live_settings(api_key),
        deployment_default=DeploymentDefault(),
    )
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.USER,
        agent_id=uuid.uuid4(),
    )

    try:
        written = await _self_write_file_impl(
            runtime,
            auth,
            key="live-test",
            content="hello live MA",
        )
        assert written.content == "hello live MA", "initial write must persist content"

        read = await _self_read_file_impl(runtime, auth, key="live-test")
        assert read is not None, "just-written key must be readable"
        assert read.content == "hello live MA", "read must echo the written content"

        updated = await _self_write_file_impl(
            runtime,
            auth,
            key="live-test",
            content="updated",
        )
        assert updated.content == "updated", "update must overwrite the prior content"

        read_after = await _self_read_file_impl(runtime, auth, key="live-test")
        assert read_after is not None and read_after.content == "updated", (
            "read after update must return the new content (read-modify-write)"
        )
    finally:
        with contextlib.suppress(Exception):
            await _self_delete_file_impl(runtime, auth, key="live-test")
        with contextlib.suppress(Exception):
            await client.close()
