"""Tests for `/agent-setup`'s provision_status branch, ahead of the panel render.

A tenant mid-seed or whose seed failed must never be told "This server has no
agents yet" -- that copy is only true for a ready tenant with a genuinely empty
roster. `agent_setup` now reads `provision_status` and short-circuits before any
MA call for a non-ready tenant.
"""

from __future__ import annotations

import re
import uuid
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
from daimon.adapters.discord.agent_setup.roster_view import RosterView
from daimon.adapters.discord.commands.agent_setup import AgentSetupCog
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.setup_conversations import EMPTY_ROSTER_COPY, setup_target_label
from daimon.core.stores.tenants import set_provision_status
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _make_runtime(
    sessionmaker: async_sessionmaker[AsyncSession], *, anthropic_client: object
) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp.public_url = None
    settings.github.fallback_pat = None
    settings.defaults_root = MagicMock()
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic_client,  # pyright: ignore[reportArgumentType]  # real AsyncAnthropic, MockTransport-backed
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # command tests never run a turn
    )


def _make_interaction(runtime: DiscordRuntime, *, guild_id: int, channel_id: int = 2) -> MagicMock:
    interaction = MagicMock()
    interaction.client.runtime = runtime
    interaction.client.user = None
    interaction.guild_id = guild_id
    interaction.channel_id = channel_id
    # The panel resolves its own channel off the interaction so a thread reports
    # its parent; a not-ready tenant short-circuits before that ever runs.
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.name = "general"
    interaction.channel = channel
    interaction.guild = None
    interaction.user = MagicMock()  # not spec=discord.Member -> is_guild_admin() is False
    interaction.user.id = 999
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def _provision(
    db_session_factory: async_sessionmaker[AsyncSession], *, guild_id: int, status: str
) -> uuid.UUID:
    result = await provision_tenant(
        db_session_factory, platform="discord", workspace_id=str(guild_id)
    )
    await set_provision_status(db_session_factory, tenant_id=result.tenant_id, status=status)
    return result.tenant_id


def _counting_router() -> tuple[MARouter, list[str]]:
    """MARouter that records every path hit, for asserting an MA call never happened."""
    calls: list[str] = []
    router = MARouter()

    def _agents(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        calls.append(req.url.path)
        return list_response([])

    def _skills(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        calls.append(req.url.path)
        return list_response([])

    router.add("GET", r"/v1/agents", _agents)
    router.add("GET", r"/v1/skills", _skills)
    return router, calls


async def test_pending_status_replies_without_fetching_roster(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 700000001
    tenant_id = await _provision(db_session_factory, guild_id=guild_id, status="pending")
    router, calls = _counting_router()
    client = build_fake_anthropic(router.dispatch)
    runtime = _make_runtime(db_session_factory, anthropic_client=client)
    interaction = _make_interaction(runtime, guild_id=guild_id)
    cog = AgentSetupCog(MagicMock())

    await cog.agent_setup.callback(cog, interaction)  # pyright: ignore[reportArgumentType]

    interaction.edit_original_response.assert_awaited_once()
    kwargs = interaction.edit_original_response.call_args.kwargs
    content = kwargs.get("content", "")
    assert "no agents yet" not in content.lower(), (
        "a seeding tenant must never be told the server has no agents"
    )
    assert "setting up" in content.lower() or "try again" in content.lower(), (
        f"pending status must say the install is being set up; got: {content!r}"
    )
    assert "view" not in kwargs, "no panel view may be built for a pending tenant"
    assert calls == [], "a pending tenant must not trigger any MA agents-list/skills-list call"
    assert tenant_id is not None


async def test_failed_status_points_at_snag_followup(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 700000002
    await _provision(db_session_factory, guild_id=guild_id, status="failed")
    router, calls = _counting_router()
    client = build_fake_anthropic(router.dispatch)
    runtime = _make_runtime(db_session_factory, anthropic_client=client)
    interaction = _make_interaction(runtime, guild_id=guild_id)
    cog = AgentSetupCog(MagicMock())

    await cog.agent_setup.callback(cog, interaction)  # pyright: ignore[reportArgumentType]

    interaction.edit_original_response.assert_awaited_once()
    kwargs = interaction.edit_original_response.call_args.kwargs
    content = kwargs.get("content", "")
    assert "no agents yet" not in content.lower()
    assert "snag" in content.lower(), (
        f"failed status must point at the snag follow-up; got: {content!r}"
    )
    assert calls == [], "a failed tenant must not trigger any MA agents-list/skills-list call"


async def test_unknown_status_fails_closed_to_pending_style_message(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unrecognized provision_status must be treated as not-ready, never fall
    through to the panel."""
    guild_id = 700000003
    await _provision(db_session_factory, guild_id=guild_id, status="some_future_status")
    router, calls = _counting_router()
    client = build_fake_anthropic(router.dispatch)
    runtime = _make_runtime(db_session_factory, anthropic_client=client)
    interaction = _make_interaction(runtime, guild_id=guild_id)
    cog = AgentSetupCog(MagicMock())

    await cog.agent_setup.callback(cog, interaction)  # pyright: ignore[reportArgumentType]

    interaction.edit_original_response.assert_awaited_once()
    kwargs = interaction.edit_original_response.call_args.kwargs
    content = kwargs.get("content", "")
    assert "no agents yet" not in content.lower()
    assert "try again" in content.lower(), (
        f"an unrecognized status must fail closed to the pending-style message; got: {content!r}"
    )
    assert calls == [], "an unrecognized status must not trigger any MA call either"


async def test_ready_status_still_renders_panel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 700000004
    await _provision(db_session_factory, guild_id=guild_id, status="ready")
    router, calls = _counting_router()
    client = build_fake_anthropic(router.dispatch)
    runtime = _make_runtime(db_session_factory, anthropic_client=client)
    interaction = _make_interaction(runtime, guild_id=guild_id)
    cog = AgentSetupCog(MagicMock())

    await cog.agent_setup.callback(cog, interaction)  # pyright: ignore[reportArgumentType]

    interaction.edit_original_response.assert_awaited_once()
    kwargs = interaction.edit_original_response.call_args.kwargs
    view = kwargs.get("view")
    assert isinstance(view, RosterView), "a ready tenant renders the read-only roster screen"
    assert "/v1/agents" in calls, "a ready tenant's roster must be fetched from MA"
    assert kwargs["allowed_mentions"].users is False, (
        "the roster renders creator mentions; rendering it must not ping anybody"
    )


async def test_ready_status_names_the_channel_and_offers_setup(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The panel opens on the question, with the one primary action available."""
    guild_id = 700000005
    await _provision(db_session_factory, guild_id=guild_id, status="ready")
    router, _calls = _counting_router()
    client = build_fake_anthropic(router.dispatch)
    runtime = _make_runtime(db_session_factory, anthropic_client=client)
    interaction = _make_interaction(runtime, guild_id=guild_id)
    cog = AgentSetupCog(MagicMock())

    await cog.agent_setup.callback(cog, interaction)  # pyright: ignore[reportArgumentType]

    view = interaction.edit_original_response.call_args.kwargs["view"]
    text = "\n".join(
        child.content for child in view.walk_children() if isinstance(child, discord.ui.TextDisplay)
    )
    labels = [child.label for child in view.walk_children() if isinstance(child, discord.ui.Button)]
    assert "Agents in #general" in text, "the header names the channel the panel is about"
    assert EMPTY_ROSTER_COPY in text, "a seeded-but-empty roster says what to do next"
    assert setup_target_label(None) in labels, "setup honestly reports that no target is selected"
    assert "Done" in labels, "and the panel can always be dismissed"
