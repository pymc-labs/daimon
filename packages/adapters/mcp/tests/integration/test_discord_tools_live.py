"""Live Discord integration tests for Phase 24 MCP-05 + Phase 73 #151 repro.

Skipped unless ALL of the following are set:
  - DAIMON_DISCORD__BOT_TOKEN env var is set (real bot token)
  - DAIMON_RUN_LIVE_DISCORD=1 (opt-in flag — never gates CI)
  - DAIMON_TEST_GUILD_ID / DAIMON_TEST_CHANNEL_ID / DAIMON_TEST_THREAD_ID
    point at a real guild/channel/thread the bot token has access to

SC-1/SC-2/SC-3 are the phase-gate tests for ROADMAP Phase 24.
The #151 repro tests (read_thread + thread-scoped search) are the live
regression gates for the Discord read-tools revamp.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from unittest.mock import AsyncMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord import (
    ReadThreadResult,
    SearchResult,
    _read_channel_impl,  # pyright: ignore[reportPrivateUsage]
    _read_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _search_messages_impl,  # pyright: ignore[reportPrivateUsage]
    _send_message_impl,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    Settings,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from fastmcp.exceptions import ToolError
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TEST_GUILD_ID = os.environ.get("DAIMON_TEST_GUILD_ID", "")
_TEST_CHANNEL_ID = os.environ.get("DAIMON_TEST_CHANNEL_ID", "")
# Public thread (type 11) in the test guild — #151 repro anchor.
_TEST_THREAD_ID = os.environ.get("DAIMON_TEST_THREAD_ID", "")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.contract,
    pytest.mark.skipif(
        os.environ.get("DAIMON_RUN_LIVE_DISCORD") != "1"
        or not os.environ.get("DAIMON_DISCORD__BOT_TOKEN")
        or not _TEST_GUILD_ID
        or not _TEST_CHANNEL_ID
        or not _TEST_THREAD_ID,
        reason=(
            "live Discord opt-in: set DAIMON_RUN_LIVE_DISCORD=1, "
            "DAIMON_DISCORD__BOT_TOKEN=<real-bot-token>, and "
            "DAIMON_TEST_GUILD_ID / DAIMON_TEST_CHANNEL_ID / DAIMON_TEST_THREAD_ID"
        ),
    ),
]


def _runtime_from_env(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> McpRuntime:
    """Build a real McpRuntime from the live env. Anthropic client is a no-op
    AsyncMock — discord tools never touch it."""
    token = os.environ["DAIMON_DISCORD__BOT_TOKEN"]
    settings = Settings(
        database=DatabaseSettings(
            url=PostgresDsn(
                os.environ.get(
                    "DAIMON_DATABASE__URL",
                    "postgresql+asyncpg://daimon:daimon@localhost:5432/daimon",
                )
            ),
        ),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test-not-used")),
        discord=DiscordSettings(bot_token=SecretStr(token)),
    )
    return McpRuntime(
        session_factory=sessionmaker,
        client=AsyncMock(spec=AsyncAnthropic),
        settings=settings,
        deployment_default=DeploymentDefault(),
    )


async def _bot_user_id(token: str) -> str:
    """Resolve the bot's own Discord user_id via REST.

    The discord tools require auth.platform_user_id to identify the caller.
    For live tests, the bot acts as its own caller — fetch its identity from
    the token. Concrete uuid'd Daimon principals don't matter here; only the
    Discord-side member resolution does.
    """
    async with rest_client(token) as c:
        info = await c.application_info()
        bot_id = info.id if hasattr(info, "id") else None
        if bot_id is None:
            raise RuntimeError(
                "could not resolve bot user_id via application_info; "
                "set DAIMON_LIVE_DISCORD_BOT_USER_ID to override"
            )
        return str(bot_id)


def _make_auth(platform_user_id: str) -> AuthIdentity:
    """Mint a synthetic AuthIdentity for the bot acting as caller."""
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="discord",
        external_id=_TEST_GUILD_ID,
        platform_user_id=platform_user_id,
    )


async def test_read_channel_against_live_test_channel(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """SC-1: read_channel returns recent messages from a real channel."""
    runtime = _runtime_from_env(sessionmaker)
    token = os.environ["DAIMON_DISCORD__BOT_TOKEN"]
    override = os.environ.get("DAIMON_LIVE_DISCORD_BOT_USER_ID")
    bot_user_id = override or await _bot_user_id(token)
    auth = _make_auth(bot_user_id)

    rows = await _read_channel_impl(
        runtime,
        auth,
        channel_id=_TEST_CHANNEL_ID,
        limit=5,
    )
    assert isinstance(rows, list), "read_channel must return a list"


async def test_send_message_and_round_trip(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """SC-2: send_message posts a uniquely-tagged message; read_channel
    confirms it appears in the recent feed."""
    marker = f"daimon-test-{uuid.uuid4().hex[:8]}"
    runtime = _runtime_from_env(sessionmaker)
    token = os.environ["DAIMON_DISCORD__BOT_TOKEN"]
    override = os.environ.get("DAIMON_LIVE_DISCORD_BOT_USER_ID")
    bot_user_id = override or await _bot_user_id(token)
    auth = _make_auth(bot_user_id)

    sent = await _send_message_impl(
        runtime,
        auth,
        channel_id=_TEST_CHANNEL_ID,
        content=f"phase 24 test {marker}",
    )
    assert marker in sent.content, f"sent message must carry marker {marker}"

    recent = await _read_channel_impl(
        runtime,
        auth,
        channel_id=_TEST_CHANNEL_ID,
        limit=10,
    )
    assert any(r.id == sent.id for r in recent), (
        f"posted message {sent.id} must appear in read_channel"
    )


async def test_search_finds_recent_post(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """SC-3: search_messages returns the marker after Discord indexes it.

    Discord's guild-search endpoint has indexing lag (seconds to minutes for
    fresh posts). Retry up to 90s before failing.

    If a 202 index-building response occurs, it surfaces as a ToolError with
    the exact locked string and is treated as retryable. Deterministic 202
    coverage lives in the 73-03 unit fakes (test_discord_search.py).
    """
    marker = f"daimon-search-{uuid.uuid4().hex[:8]}"
    runtime = _runtime_from_env(sessionmaker)
    token = os.environ["DAIMON_DISCORD__BOT_TOKEN"]
    override = os.environ.get("DAIMON_LIVE_DISCORD_BOT_USER_ID")
    bot_user_id = override or await _bot_user_id(token)
    auth = _make_auth(bot_user_id)

    await _send_message_impl(
        runtime,
        auth,
        channel_id=_TEST_CHANNEL_ID,
        content=f"search test {marker}",
    )

    deadline = time.time() + 90.0
    result: SearchResult | None = None
    while time.time() < deadline:
        try:
            result = await _search_messages_impl(
                runtime,
                auth,
                content=marker,
                channel_ids=[_TEST_CHANNEL_ID],
            )
        except ToolError as exc:
            if exc.args[0] == "Search index is building. Retry in a few seconds.":
                await asyncio.sleep(5.0)
                continue
            raise
        if result.rows:
            break
        await asyncio.sleep(5.0)
    assert result is not None, "search_messages never returned a result"
    assert result.rows, (
        f"search did not find marker {marker!r} within 90s (total_results={result.total_results})"
    )
    assert any(marker in row.content for row in result.rows), (
        f"no search result row contains the marker {marker!r}"
    )


async def test_read_thread_returns_messages_from_known_thread(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """#151 repro (1/2): read_thread on the known thread returns messages.

    Thread 1514307366605029462 ("Chat with replicate-agent") is a public
    thread in the test guild. An empty result means the thread-blind bug
    has regressed.
    """
    runtime = _runtime_from_env(sessionmaker)
    token = os.environ["DAIMON_DISCORD__BOT_TOKEN"]
    override = os.environ.get("DAIMON_LIVE_DISCORD_BOT_USER_ID")
    bot_user_id = override or await _bot_user_id(token)
    auth = _make_auth(bot_user_id)

    result = await _read_thread_impl(
        runtime,
        auth,
        thread_id=_TEST_THREAD_ID,
        limit=50,
    )
    assert isinstance(result, ReadThreadResult), "read_thread must return a ReadThreadResult"
    assert result.rows, (
        "read_thread on the known thread must return messages — "
        "empty means the thread-blind bug regressed"
    )
    if len(result.rows) > 1:
        assert int(result.rows[0].id) < int(result.rows[-1].id), (
            "read_thread must return messages in oldest-first order"
        )


async def test_search_messages_finds_known_message_in_thread(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """#151 repro (2/2) + bug-1 live proof: search "CDN URLs" scoped to the
    known thread returns the matching message.

    The hit's channel_id is a thread id that fetch_channels() never returns —
    this row surviving the visibility filter is the live regression test for
    bug 1 (the deployed code dropped every such hit).
    """
    runtime = _runtime_from_env(sessionmaker)
    token = os.environ["DAIMON_DISCORD__BOT_TOKEN"]
    override = os.environ.get("DAIMON_LIVE_DISCORD_BOT_USER_ID")
    bot_user_id = override or await _bot_user_id(token)
    auth = _make_auth(bot_user_id)

    deadline = time.time() + 90.0
    result: SearchResult | None = None
    while time.time() < deadline:
        try:
            result = await _search_messages_impl(
                runtime,
                auth,
                content="CDN URLs",
                channel_ids=[_TEST_THREAD_ID],
                limit=25,
            )
        except ToolError as exc:
            if exc.args[0] == "Search index is building. Retry in a few seconds.":
                await asyncio.sleep(5.0)
                continue
            raise
        if result.rows:
            break
        await asyncio.sleep(5.0)
    assert result is not None, "search_messages never returned a result"
    assert result.total_results >= 1, (
        "search for 'CDN URLs' in the known thread must find at least 1 result"
    )
    assert result.rows, "search result rows must be non-empty when total_results >= 1"
    thread_hit = any(
        row.channel_id == _TEST_THREAD_ID and "cdn urls" in row.content.lower()
        for row in result.rows
    )
    assert thread_hit, (
        "at least one search result must be from the known thread "
        f"({_TEST_THREAD_ID}) and contain 'CDN URLs' — this proves a thread "
        "hit survives the visibility filter (bug 1 live regression)"
    )


async def test_search_denies_channel_caller_cannot_view(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Env-gated: a caller without view_channel on a restricted channel is
    denied at pre-validation with 'missing view_channel permission'.

    Requires DAIMON_LIVE_DISCORD_RESTRICTED_USER_ID and
    DAIMON_LIVE_DISCORD_RESTRICTED_CHANNEL_ID to be set. Skips with an
    explicit reason when either is unset.

    The silently-dropped-hit half of the addenda (private-thread hit dropped
    by visibility filter) is covered deterministically by the 73-03 unit
    fakes — live only asserts the explicit-denial half.
    """
    restricted_user_id = os.environ.get("DAIMON_LIVE_DISCORD_RESTRICTED_USER_ID")
    restricted_channel_id = os.environ.get("DAIMON_LIVE_DISCORD_RESTRICTED_CHANNEL_ID")
    if not restricted_user_id or not restricted_channel_id:
        pytest.skip(
            "restricted-caller ids not configured: set "
            "DAIMON_LIVE_DISCORD_RESTRICTED_USER_ID + "
            "DAIMON_LIVE_DISCORD_RESTRICTED_CHANNEL_ID"
        )

    runtime = _runtime_from_env(sessionmaker)
    auth = _make_auth(restricted_user_id)

    with pytest.raises(ToolError, match="missing view_channel permission"):
        await _search_messages_impl(
            runtime,
            auth,
            content="anything",
            channel_ids=[restricted_channel_id],
        )
