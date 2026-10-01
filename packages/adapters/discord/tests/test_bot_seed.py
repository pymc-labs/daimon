"""Tests for the roster-resolves-before-ready invariant in `_seed_tenant_defaults`.

A non-failing `ApplyReport` alone does not guarantee the deployment's configured
default agent actually exists on MA — `config.yaml`'s `agent_name` can drift from
every spec file under `defaults_root/agents/`. `_seed_tenant_defaults` must confirm
the named agent resolves before flipping to ready, and the posted follow-up embed
must always agree with the recorded status.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic as _anthropic
import discord
import httpx
import structlog.testing
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.defaults.report import Action, ApplyReport, ResourceOutcome
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.tenants import get_tenant_liveness, set_provision_status
from daimon.testing import ma_agent
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _make_runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    anthropic_client: object,
    agent_name: str | None = "daimon",
) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp.public_url = None
    settings.defaults_root = MagicMock()
    settings.billing.signup_credit = Decimal("0")
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic_client,  # pyright: ignore[reportArgumentType]  # real AsyncAnthropic, MockTransport-backed
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(agent_name=agent_name),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # seed tests never run a turn
    )


def _make_bot(runtime: DiscordRuntime) -> DaimonBot:
    intents = discord.Intents.default()
    return DaimonBot(runtime=runtime, intents=intents)


def _make_guild(*, guild_id: int) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = "Test Guild"

    me = MagicMock(spec=discord.Member)
    guild.me = me

    channel = MagicMock(spec=discord.TextChannel)
    perms = MagicMock()
    perms.send_messages = True
    channel.permissions_for = MagicMock(return_value=perms)
    channel.send = AsyncMock()
    guild.system_channel = channel
    guild.text_channels = [channel]

    owner = MagicMock(spec=discord.Member)
    owner.send = AsyncMock()
    guild.owner = owner
    guild.owner_id = 42
    return guild


def _agent_matching_tenant(tenant_id: uuid.UUID, *, name: str) -> dict[str, object]:
    return ma_agent(
        id="ag_1",
        name=name,
        model="claude-opus-4-7",
        tenant_id=tenant_id,
    ).model_dump(mode="json")


async def _provision(
    db_session_factory: async_sessionmaker[AsyncSession], *, workspace_id: str
) -> uuid.UUID:
    result = await provision_tenant(
        db_session_factory, platform="discord", workspace_id=workspace_id
    )
    await set_provision_status(db_session_factory, tenant_id=result.tenant_id, status="pending")
    return result.tenant_id


async def test_seed_flips_ready_when_report_succeeds_and_default_agent_resolves(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _provision(db_session_factory, workspace_id="seed-ready-1")
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([_agent_matching_tenant(tenant_id, name="daimon")]),
    )
    client = build_fake_anthropic(router.dispatch)
    runtime = _make_runtime(db_session_factory, anthropic_client=client, agent_name="daimon")
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=1)

    with patch(
        "daimon.adapters.discord.bot.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        return_value=ApplyReport(),
    ):
        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=tenant_id, guild=guild, was_ready=False
        )

    tr = await get_tenant_liveness(db_session_factory, tenant_id)
    assert tr is not None and tr.provision_status == "ready", (
        "a non-failing report with the default agent present on MA must flip to ready"
    )
    posted_embed = guild.system_channel.send.await_args.kwargs["embed"]
    assert posted_embed.title == "✅ Ready", "the ready embed must be posted when status is ready"


async def test_seed_flips_failed_when_default_agent_missing_from_ma(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _provision(db_session_factory, workspace_id="seed-missing-1")
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)
    runtime = _make_runtime(db_session_factory, anthropic_client=client, agent_name="daimon")
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=2)

    with (
        structlog.testing.capture_logs() as logs,
        patch(
            "daimon.adapters.discord.bot.reconcile_tenant_defaults",
            new_callable=AsyncMock,
            return_value=ApplyReport(),
        ),
    ):
        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=tenant_id, guild=guild, was_ready=False
        )

    tr = await get_tenant_liveness(db_session_factory, tenant_id)
    assert tr is not None and tr.provision_status == "failed", (
        "a non-failing report whose configured default agent is absent from MA "
        "must NOT flip to ready"
    )
    posted_embed = guild.system_channel.send.await_args.kwargs["embed"]
    assert posted_embed.title == "⚠️ Setup hit a snag", (
        "the snag embed (not the ready embed) must be posted when the default agent is missing"
    )
    missing_events = [e for e in logs if e["event"] == "guild_seed_default_agent_missing"]
    assert len(missing_events) >= 1, (
        "a missing default agent on an otherwise non-failing seed must log a warning "
        "an operator can search for"
    )
    assert missing_events[0]["tenant_id"] == str(tenant_id)
    assert missing_events[0]["agent_name"] == "daimon"


async def test_seed_flips_failed_when_report_fails_regardless_of_roster(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression: a failing report must still flip to failed, and the roster
    lookup must not even run (the failure is already known)."""
    tenant_id = await _provision(db_session_factory, workspace_id="seed-report-fails-1")
    list_calls = 0

    def _count_list(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        nonlocal list_calls
        list_calls += 1
        return list_response([])

    router = MARouter()
    router.add("GET", r"/v1/agents", _count_list)
    client = build_fake_anthropic(router.dispatch)
    runtime = _make_runtime(db_session_factory, anthropic_client=client, agent_name="daimon")
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=3)

    failing_report = ApplyReport()
    failing_report.add(
        ResourceOutcome(kind="skill", name="boom", action=Action.FAILED, error="5xx")
    )

    with patch(
        "daimon.adapters.discord.bot.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        return_value=failing_report,
    ):
        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=tenant_id, guild=guild, was_ready=False
        )

    tr = await get_tenant_liveness(db_session_factory, tenant_id)
    assert tr is not None and tr.provision_status == "failed", (
        "a failing report must flip to failed, unchanged from prior behavior"
    )
    posted_embed = guild.system_channel.send.await_args.kwargs["embed"]
    assert posted_embed.title == "⚠️ Setup hit a snag"
    assert list_calls == 0, (
        "a failing report already determines the outcome; the roster lookup must be skipped"
    )


async def test_seed_skips_roster_check_when_deployment_default_has_no_agent_name(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No configured default agent name means no roster assertion is possible —
    behave exactly as before (flip on the report alone), and log that the check
    was skipped."""
    tenant_id = await _provision(db_session_factory, workspace_id="seed-no-agent-name-1")
    router = MARouter()  # no routes registered — any call would raise
    client = build_fake_anthropic(router.dispatch)
    runtime = _make_runtime(db_session_factory, anthropic_client=client, agent_name=None)
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=4)

    with (
        structlog.testing.capture_logs() as logs,
        patch(
            "daimon.adapters.discord.bot.reconcile_tenant_defaults",
            new_callable=AsyncMock,
            return_value=ApplyReport(),
        ),
    ):
        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=tenant_id, guild=guild, was_ready=False
        )

    tr = await get_tenant_liveness(db_session_factory, tenant_id)
    assert tr is not None and tr.provision_status == "ready", (
        "with no configured default agent name, a non-failing report alone must flip to ready"
    )
    skipped_events = [e for e in logs if e["event"] == "guild_seed_roster_check_skipped"]
    assert len(skipped_events) >= 1, (
        "skipping the roster check because no default agent name is configured must be logged"
    )


async def test_seed_keeps_ready_tenant_ready_when_reconcile_report_fails(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant that was already `ready` before a boot-sweep reconcile must not be
    demoted to `failed` by a transient FAILED resource outcome -- only the reason
    is recorded so an operator can still see it happened."""
    tenant_id = await _provision(db_session_factory, workspace_id="seed-ready-transient-1")
    await set_provision_status(db_session_factory, tenant_id=tenant_id, status="ready")
    router = MARouter()  # no routes registered — roster check must not even run
    client = build_fake_anthropic(router.dispatch)
    runtime = _make_runtime(db_session_factory, anthropic_client=client, agent_name="daimon")
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=6)

    failing_report = ApplyReport()
    failing_report.add(
        ResourceOutcome(kind="skill", name="boom", action=Action.FAILED, error="429 rate limited")
    )

    with (
        structlog.testing.capture_logs() as logs,
        patch(
            "daimon.adapters.discord.bot.reconcile_tenant_defaults",
            new_callable=AsyncMock,
            return_value=failing_report,
        ),
    ):
        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=tenant_id, guild=guild, was_ready=True
        )

    tr = await get_tenant_liveness(db_session_factory, tenant_id)
    assert tr is not None and tr.provision_status == "ready", (
        "a previously-ready tenant must stay ready on a transient reconcile failure -- "
        "demoting it would take a working guild's turns offline"
    )
    assert tr.last_reconcile_error is not None and "boom" in tr.last_reconcile_error, (
        "the failure reason must still be persisted even though status is unchanged"
    )
    guild.system_channel.send.assert_not_awaited()
    warn_events = [e for e in logs if e["event"] == "guild_reconcile_failed_ready_tenant"]
    assert len(warn_events) >= 1, "a swallowed failure on an already-ready tenant must be logged"
    assert warn_events[0]["tenant_id"] == str(tenant_id)


async def test_seed_keeps_ready_tenant_ready_when_reconcile_raises_api_error(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The exception-path status flip (`_flip_failed_best_effort`) must honor the
    same was_ready guard as the FAILED-outcome branch: a previously-ready tenant
    whose reconcile call itself raises must stay ready, reason recorded."""
    tenant_id = await _provision(db_session_factory, workspace_id="seed-ready-transient-2")
    await set_provision_status(db_session_factory, tenant_id=tenant_id, status="ready")
    runtime = _make_runtime(db_session_factory, anthropic_client=MagicMock(), agent_name="daimon")
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=7)

    with patch(
        "daimon.adapters.discord.bot.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        side_effect=_anthropic.APIConnectionError(request=MagicMock()),
    ):
        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=tenant_id, guild=guild, was_ready=True
        )

    tr = await get_tenant_liveness(db_session_factory, tenant_id)
    assert tr is not None and tr.provision_status == "ready", (
        "a previously-ready tenant must stay ready even when the reconcile call "
        "itself raises -- the exception-path flip must honor was_ready too"
    )
    assert tr.last_reconcile_error is not None, "the failure reason must still be persisted"


async def test_seed_ma_error_during_roster_check_flips_failed_via_existing_boundary(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An anthropic.APIError raised by the roster lookup must land in the
    pre-existing typed except clause — no new handler — and still flip to
    failed with the snag embed posted."""
    tenant_id = await _provision(db_session_factory, workspace_id="seed-ma-error-1")
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: httpx.Response(
            503,
            json={"type": "error", "error": {"type": "service_unavailable", "message": "down"}},
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    runtime = _make_runtime(db_session_factory, anthropic_client=client, agent_name="daimon")
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=5)

    with patch(
        "daimon.adapters.discord.bot.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        return_value=ApplyReport(),
    ):
        await bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=tenant_id, guild=guild, was_ready=False
        )

    tr = await get_tenant_liveness(db_session_factory, tenant_id)
    assert tr is not None and tr.provision_status == "failed", (
        "an API error from the roster lookup must flip status to failed via the "
        "existing typed except clause"
    )
    posted_embed = guild.system_channel.send.await_args.kwargs["embed"]
    assert posted_embed.title == "⚠️ Setup hit a snag"
