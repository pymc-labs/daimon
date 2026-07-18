"""Tests for the two-phase on_guild_join handler + welcome channel fallback.

Covers VALIDATION invariants:
- provision_idempotent: a re-join provisions exactly one Tenant + Account.
- tenant_deterministic: the provisioned tenant_id == derive_tenant_uuid(platform, workspace_id).
- provisioning_pending: join sets status 'pending', the bg seed (_seed_tenant_defaults) flips
  it ready/failed, and a terminal-failure run posts the "snag" follow-up (never "failed").

The MA reconcile is patched at the bot-module boundary (`reconcile_tenant_defaults`).
Here we assert the bot's two-phase control flow + the status flip owned by _seed_tenant_defaults.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import structlog.testing
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.report import ApplyReport
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.tenants import get_tenant_liveness, set_provision_status
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _make_runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    mcp_public_url: str | None = None,
) -> DiscordRuntime:
    from decimal import Decimal

    settings = MagicMock()
    settings.mcp.public_url = mcp_public_url
    settings.defaults_root = MagicMock()
    settings.billing.signup_credit = Decimal("0")
    return DiscordRuntime(
        settings=settings,
        anthropic=AsyncMock(),
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


def _make_bot(runtime: DiscordRuntime) -> DaimonBot:
    intents = discord.Intents.default()
    bot = DaimonBot(runtime=runtime, intents=intents)
    bot._connection.user = MagicMock(spec=discord.ClientUser)  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.id = 999  # pyright: ignore[reportPrivateUsage]
    # Stub tree command APIs so the per-guild clear+sync doesn't hit the gateway.
    # copy_global_to is stubbed only so a test can assert it is NEVER used —
    # registering commands at both global and guild scope doubles every command.
    bot.tree.copy_global_to = MagicMock()  # type: ignore[method-assign]
    bot.tree.clear_commands = MagicMock()  # type: ignore[method-assign]
    bot.tree.sync = AsyncMock()  # type: ignore[method-assign]
    return bot


def _make_guild(
    *,
    guild_id: int = 555000111,
    name: str = "Test Guild",
    system_channel_sendable: bool = True,
    text_channels_sendable: list[bool] | None = None,
) -> MagicMock:
    """Build a discord.Guild stub with a controllable channel-fallback chain."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = name

    me = MagicMock(spec=discord.Member)
    guild.me = me

    def _channel(sendable: bool) -> MagicMock:
        ch = MagicMock(spec=discord.TextChannel)
        perms = MagicMock()
        perms.send_messages = sendable
        ch.permissions_for = MagicMock(return_value=perms)
        ch.send = AsyncMock()
        return ch

    if system_channel_sendable:
        guild.system_channel = _channel(True)
    else:
        guild.system_channel = None

    sendable_flags = text_channels_sendable if text_channels_sendable is not None else [True]
    guild.text_channels = [_channel(flag) for flag in sendable_flags]

    owner = MagicMock(spec=discord.Member)
    owner.send = AsyncMock()
    guild.owner = owner
    guild.owner_id = 42
    return guild


async def _drain_bg_tasks(bot: DaimonBot) -> None:
    """Await every spawned background task to completion (test determinism)."""
    while bot._bg_tasks:  # pyright: ignore[reportPrivateUsage]
        await asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]


async def test_on_guild_join_provisions_pending_then_seeds_ready(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _make_runtime(db_session_factory)
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=555000111)
    guild_id = str(guild.id)

    with patch(
        "daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock
    ) as mock_reconcile:
        # _seed_tenant_defaults owns the status flip; emulate a successful reconcile.
        mock_reconcile.return_value = ApplyReport()

        await bot.on_guild_join(guild)
        await _drain_bg_tasks(bot)

    # tenant_deterministic: the row keyed on the derived uuid exists.
    derived = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    tr = await get_tenant_liveness(db_session_factory, derived)
    assert tr is not None, "on_guild_join must provision a tenant row"
    assert tr.id == derived, "tenant_id must equal derive_tenant_uuid(discord, guild_id)"
    assert tr.provision_status == "ready", "background seed must flip status to ready"

    # Immediate welcome embed posted with 'setting up' copy.
    welcome_ch = guild.system_channel
    assert welcome_ch.send.await_count >= 1, "immediate welcome must be posted"
    welcome_embed = welcome_ch.send.await_args_list[0].kwargs["embed"]
    welcome_text = (welcome_embed.title or "") + (welcome_embed.description or "")
    assert "setting up" in welcome_text.lower(), "welcome embed must say 'setting up'"

    # Follow-up "Ready" embed posted after the bg task.
    followup_embed = welcome_ch.send.await_args_list[-1].kwargs["embed"]
    followup_text = (followup_embed.title or "") + (followup_embed.description or "")
    assert "ready" in followup_text.lower(), "terminal follow-up must say 'Ready'"

    # Commands sync at GLOBAL scope only; the per-guild step CLEARS guild-scoped
    # copies rather than copying globals into the guild (which would render every
    # command twice). copy_global_to must never be used.
    bot.tree.copy_global_to.assert_not_called()  # type: ignore[attr-defined]
    bot.tree.clear_commands.assert_called()  # type: ignore[attr-defined]
    clear_guild = bot.tree.clear_commands.call_args.kwargs.get("guild")  # type: ignore[attr-defined]
    assert clear_guild is not None and clear_guild.id == guild.id, (
        "per-guild clear must target the joined guild"
    )
    bot.tree.sync.assert_awaited()  # type: ignore[attr-defined]


async def test_seed_passes_mcp_public_url_to_reconcile(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seeded guild agents must get the daimon-mcp toolset, which reconcile only
    merges in when public_url is threaded through (mcp_merge no-ops on None)."""
    runtime = _make_runtime(db_session_factory, mcp_public_url="https://example.test/mcp")
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=555000666)

    with patch(
        "daimon.adapters.discord.bot.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        return_value=ApplyReport(),
    ) as mock_reconcile:
        await bot.on_guild_join(guild)
        await _drain_bg_tasks(bot)

    assert mock_reconcile.await_args is not None, "seed must invoke reconcile_tenant_defaults"
    assert mock_reconcile.await_args.kwargs.get("public_url") == "https://example.test/mcp", (
        "seed must pass settings.mcp.public_url so the daimon-mcp toolset is merged in"
    )


async def test_seed_passes_none_public_url_when_mcp_unconfigured(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _make_runtime(db_session_factory, mcp_public_url=None)
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=555000777)

    with patch(
        "daimon.adapters.discord.bot.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        return_value=ApplyReport(),
    ) as mock_reconcile:
        await bot.on_guild_join(guild)
        await _drain_bg_tasks(bot)

    assert mock_reconcile.await_args is not None, "seed must invoke reconcile_tenant_defaults"
    assert mock_reconcile.await_args.kwargs.get("public_url") is None, (
        "unset mcp.public_url must pass None, never the string 'None'"
    )


async def test_on_guild_join_idempotent_on_rejoin(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _make_runtime(db_session_factory)
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=555000222)
    guild_id = str(guild.id)
    derived = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

    with patch(
        "daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock
    ) as mock_reconcile:
        mock_reconcile.return_value = ApplyReport()

        await bot.on_guild_join(guild)
        await _drain_bg_tasks(bot)
        await bot.on_guild_join(guild)
        await _drain_bg_tasks(bot)

    # provision_idempotent: exactly one Tenant + one Account for the derived id.
    from daimon.core._models import Account, Tenant  # test-only ORM peek

    tenant_count = (
        await db_session.execute(
            select(func.count()).select_from(Tenant).where(Tenant.id == derived)
        )
    ).scalar_one()
    account_count = (
        await db_session.execute(
            select(func.count()).select_from(Account).where(Account.tenant_id == derived)
        )
    ).scalar_one()
    assert tenant_count == 1, "re-join must not create a second Tenant row"
    assert account_count == 1, "re-join must not create a second Account row"


async def test_on_guild_join_in_flight_guard_prevents_duplicate_seed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _make_runtime(db_session_factory)
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=555000333)

    release = asyncio.Event()
    reconcile_starts = 0

    async def _slow_reconcile(*_a: object, **_k: object) -> ApplyReport:
        nonlocal reconcile_starts
        reconcile_starts += 1
        await release.wait()
        return ApplyReport()

    with patch(
        "daimon.adapters.discord.bot.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        side_effect=_slow_reconcile,
    ):
        # Two joins back-to-back without awaiting the bg seed: the in-flight
        # set[tenant_id] guard must keep the second from starting a duplicate seed.
        await bot.on_guild_join(guild)
        await bot.on_guild_join(guild)
        await asyncio.sleep(0)  # let bg tasks reach the await
        assert reconcile_starts == 1, "in-flight guard must prevent a concurrent duplicate seed"
        release.set()
        await _drain_bg_tasks(bot)


async def test_on_guild_join_failed_posts_snag_followup(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _make_runtime(db_session_factory)
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=555000444)
    guild_id = str(guild.id)

    failing_report = ApplyReport()
    from daimon.core.defaults.report import Action, ResourceOutcome

    failing_report.add(
        ResourceOutcome(kind="skill", name="boom", action=Action.FAILED, error="5xx")
    )

    with patch(
        "daimon.adapters.discord.bot.reconcile_tenant_defaults", new_callable=AsyncMock
    ) as mock_reconcile:
        # _seed_tenant_defaults owns the status flip; return the failing report.
        mock_reconcile.return_value = failing_report

        await bot.on_guild_join(guild)
        await _drain_bg_tasks(bot)

    derived = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    tr = await get_tenant_liveness(db_session_factory, derived)
    assert tr is not None and tr.provision_status == "failed", (
        "terminal failure must set status failed"
    )

    welcome_ch = guild.system_channel
    followup_embed = welcome_ch.send.await_args_list[-1].kwargs["embed"]
    followup_text = (followup_embed.title or "") + (followup_embed.description or "")
    assert "snag" in followup_text.lower(), "failed seed must post the 'snag' follow-up"
    assert "failed" not in followup_text.lower(), "'failed' must never be shown to users"


async def test_on_guild_join_welcome_channel_fallback(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _make_runtime(db_session_factory)
    bot = _make_bot(runtime)
    # No system channel; first text channel not sendable, second sendable.
    guild = _make_guild(
        guild_id=555000555,
        system_channel_sendable=False,
        text_channels_sendable=[False, True],
    )

    with patch(
        "daimon.adapters.discord.bot.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        return_value=ApplyReport(),
    ):
        await bot.on_guild_join(guild)
        await _drain_bg_tasks(bot)

    first_ch, second_ch = guild.text_channels
    first_ch.send.assert_not_called()
    assert second_ch.send.await_count >= 1, "welcome must fall back to the first sendable channel"
    welcome_embed = second_ch.send.await_args_list[0].kwargs["embed"]
    welcome_text = (welcome_embed.title or "") + (welcome_embed.description or "")
    assert "setting up" in welcome_text.lower()


async def test_seed_pre_write_exception_flips_failed_and_posts_snag(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#131: a DaimonError raised inside reconcile must flip status to 'failed' (never wedge
    in 'pending') and post the snag embed so the operator sees it."""
    runtime = _make_runtime(db_session_factory)
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=555000888)
    guild_id = str(guild.id)

    with patch(
        "daimon.adapters.discord.bot.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        side_effect=DaimonError("seed exploded"),
    ):
        await bot.on_guild_join(guild)
        await _drain_bg_tasks(bot)

    derived = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    tr = await get_tenant_liveness(db_session_factory, derived)
    assert tr is not None, "tenant must still exist after a pre-write seed failure"
    assert tr.provision_status == "failed", (
        "pre-write DaimonError in seed must flip provision_status to 'failed', "
        "not leave tenant wedged in 'pending'"
    )

    # Snag embed posted on the failure path.
    welcome_ch = guild.system_channel
    assert welcome_ch.send.await_count >= 2, "at least welcome + snag embed expected"  # pyright: ignore[reportUnknownMemberType]
    embed_texts = [
        (c.kwargs["embed"].title or "") + (c.kwargs["embed"].description or "")
        for c in welcome_ch.send.await_args_list
        if "embed" in c.kwargs
    ]
    assert any("snag" in t.lower() for t in embed_texts), (
        "pre-write failure must post the snag embed so the operator is informed"
    )


async def test_seed_pre_write_exception_never_shows_failed_word(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The word 'failed' must never appear in any embed posted to guild members (#131)."""
    runtime = _make_runtime(db_session_factory)
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=555000999)

    with patch(
        "daimon.adapters.discord.bot.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        side_effect=DaimonError("seed exploded"),
    ):
        await bot.on_guild_join(guild)
        await _drain_bg_tasks(bot)

    welcome_ch = guild.system_channel
    for call in welcome_ch.send.await_args_list:  # pyright: ignore[reportUnknownMemberType]
        if "embed" in call.kwargs:
            embed = call.kwargs["embed"]
            embed_text = (embed.title or "") + (embed.description or "")
            assert "failed" not in embed_text.lower(), (
                f"'failed' must never be shown to users; embed text was: {embed_text!r}"
            )


async def test_on_guild_join_rejoin_clears_archived_at(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """on_guild_join must clear archived_at so the re-joined guild
    can reach 'ready' without a wasted first mention that triggers ensure_provisioning."""
    runtime = _make_runtime(db_session_factory)
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=555001001)
    guild_id = str(guild.id)

    # First join — provisions and seeds to ready.
    with patch(
        "daimon.adapters.discord.bot.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        return_value=ApplyReport(),
    ):
        await bot.on_guild_join(guild)
        await _drain_bg_tasks(bot)

    derived = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    # Simulate the bot being removed: stamp archived_at via the store.
    await set_provision_status(db_session_factory, tenant_id=derived, archive=True)
    tr_archived = await get_tenant_liveness(db_session_factory, derived)
    assert tr_archived is not None and tr_archived.archived_at is not None, (
        "pre-condition: archived_at must be set before rejoin"
    )

    # Re-join — on_guild_join must clear archived_at.
    with patch(
        "daimon.adapters.discord.bot.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        return_value=ApplyReport(),
    ):
        await bot.on_guild_join(guild)
        await _drain_bg_tasks(bot)

    tr = await get_tenant_liveness(db_session_factory, derived)
    assert tr is not None, "tenant row must exist after rejoin"
    assert tr.archived_at is None, (
        "#132: on_guild_join must clear archived_at so the guild is not stuck archived"
    )
    assert tr.provision_status == "ready", (
        "rejoin must eventually reach 'ready' after the seed completes"
    )


async def test_seed_unexpected_oserror_flips_failed_and_posts_snag(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#131 G1: an OSError (e.g. defaults-tree read on Fly) must flip status to 'failed'
    via the catch-all boundary, not leave the tenant wedged in 'pending'."""
    runtime = _make_runtime(db_session_factory)
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=555001002)
    guild_id = str(guild.id)

    with (
        structlog.testing.capture_logs() as logs,
        patch(
            "daimon.adapters.discord.bot.reconcile_tenant_defaults",
            new_callable=AsyncMock,
            side_effect=OSError("read failed"),
        ),
    ):
        await bot.on_guild_join(guild)
        await _drain_bg_tasks(bot)

    derived = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    tr = await get_tenant_liveness(db_session_factory, derived)
    assert tr is not None, "tenant must still exist after an unexpected seed failure"
    assert tr.provision_status == "failed", (
        "OSError in seed must flip provision_status to 'failed' via the catch-all boundary, "
        "not leave tenant wedged in 'pending'"
    )

    unexpected_events = [e for e in logs if e["event"] == "guild_seed_unexpected"]
    assert len(unexpected_events) >= 1, (
        "catch-all boundary must emit guild_seed_unexpected log so the failure is visible"
    )
    # The typed 'guild_seed_failed' path must NOT have been taken.
    typed_events = [e for e in logs if e["event"] == "guild_seed_failed"]
    assert len(typed_events) == 0, (
        "OSError must go through the catch-all path, not the typed exception branch"
    )

    # Snag embed posted; word 'failed' must never appear in guild-visible embed text.
    welcome_ch = guild.system_channel
    assert welcome_ch.send.await_count >= 2, "at least welcome + snag embed expected"  # pyright: ignore[reportUnknownMemberType]
    embed_texts = [
        (c.kwargs["embed"].title or "") + (c.kwargs["embed"].description or "")
        for c in welcome_ch.send.await_args_list
        if "embed" in c.kwargs
    ]
    assert any("snag" in t.lower() for t in embed_texts), (
        "unexpected seed failure must post the snag embed so the operator is informed"
    )
    for text in embed_texts:
        assert "failed" not in text.lower(), (
            f"'failed' must never be shown to users; embed text was: {text!r}"
        )


async def test_seed_unexpected_sqlalchemyerror_flips_failed_and_posts_snag(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#131 G1: a SQLAlchemyError must flip status to 'failed' via the catch-all boundary,
    not leave the tenant wedged in 'pending'."""
    runtime = _make_runtime(db_session_factory)
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=555001003)
    guild_id = str(guild.id)

    with (
        structlog.testing.capture_logs() as logs,
        patch(
            "daimon.adapters.discord.bot.reconcile_tenant_defaults",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("db hiccup"),
        ),
    ):
        await bot.on_guild_join(guild)
        await _drain_bg_tasks(bot)

    derived = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    tr = await get_tenant_liveness(db_session_factory, derived)
    assert tr is not None, "tenant must still exist after an unexpected SQLAlchemyError"
    assert tr.provision_status == "failed", (
        "SQLAlchemyError in seed must flip provision_status to 'failed' via the catch-all, "
        "not leave tenant wedged in 'pending'"
    )

    unexpected_events = [e for e in logs if e["event"] == "guild_seed_unexpected"]
    assert len(unexpected_events) >= 1, (
        "catch-all boundary must emit guild_seed_unexpected log for SQLAlchemyError"
    )

    welcome_ch = guild.system_channel
    assert welcome_ch.send.await_count >= 2, "at least welcome + snag embed expected"  # pyright: ignore[reportUnknownMemberType]
    embed_texts = [
        (c.kwargs["embed"].title or "") + (c.kwargs["embed"].description or "")
        for c in welcome_ch.send.await_args_list
        if "embed" in c.kwargs
    ]
    assert any("snag" in t.lower() for t in embed_texts), (
        "unexpected seed failure must post the snag embed so the operator is informed"
    )
    for text in embed_texts:
        assert "failed" not in text.lower(), (
            f"'failed' must never be shown to users; embed text was: {text!r}"
        )


async def test_seed_catchall_flip_failure_is_best_effort_and_logged(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#131 G1: if the status flip itself raises (e.g. DB outage), the exception must not
    escape _seed_tenant_defaults; both guild_seed_unexpected and guild_seed_status_flip_failed
    must be logged; the snag embed must still be posted."""
    runtime = _make_runtime(db_session_factory)
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=555001004)

    # The seed task spawned by on_guild_join doesn't run until the drain yields to
    # it, so BOTH patches must stay active across _drain_bg_tasks. on_guild_join's
    # own set_provision_status(status='pending') call also lands on the mock, so the
    # side effect fails ONLY the seed's status='failed' flip.
    async def _flip_side_effect(*_args: object, **kwargs: object) -> None:
        if kwargs.get("status") == "failed":
            raise SQLAlchemyError("db down")

    with (
        structlog.testing.capture_logs() as logs,
        patch(
            "daimon.adapters.discord.bot.reconcile_tenant_defaults",
            new_callable=AsyncMock,
            side_effect=OSError("read failed"),
        ),
        patch(
            "daimon.adapters.discord.bot.set_provision_status",
            new_callable=AsyncMock,
            side_effect=_flip_side_effect,
        ),
    ):
        await bot.on_guild_join(guild)
        # _drain_bg_tasks must complete without raising — the exception must not escape
        await _drain_bg_tasks(bot)

    unexpected_events = [e for e in logs if e["event"] == "guild_seed_unexpected"]
    assert len(unexpected_events) >= 1, (
        "catch-all boundary must log guild_seed_unexpected even when the flip itself fails"
    )
    flip_failed_events = [e for e in logs if e["event"] == "guild_seed_status_flip_failed"]
    assert len(flip_failed_events) >= 1, (
        "best-effort flip failure must log guild_seed_status_flip_failed"
    )

    # Snag embed still posted despite the flip failure.
    welcome_ch = guild.system_channel
    embed_texts = [
        (c.kwargs["embed"].title or "") + (c.kwargs["embed"].description or "")
        for c in welcome_ch.send.await_args_list  # pyright: ignore[reportUnknownMemberType]
        if "embed" in c.kwargs
    ]
    assert any("snag" in t.lower() for t in embed_texts), (
        "snag embed must still be posted even when the status flip fails"
    )


async def test_seed_typed_branch_flip_failure_is_best_effort_and_logged(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A typed seed failure (DaimonError) whose failed-flip then hits a DB error must not
    escape _seed_tenant_defaults: guild_seed_failed and guild_seed_status_flip_failed are
    logged and the snag embed is still posted (mirror of the catch-all flip-failure test)."""
    runtime = _make_runtime(db_session_factory)
    bot = _make_bot(runtime)
    guild = _make_guild(guild_id=555001005)

    async def _flip_side_effect(*_args: object, **kwargs: object) -> None:
        if kwargs.get("status") == "failed":
            raise SQLAlchemyError("db down")

    with (
        structlog.testing.capture_logs() as logs,
        patch(
            "daimon.adapters.discord.bot.reconcile_tenant_defaults",
            new_callable=AsyncMock,
            side_effect=DaimonError("seed exploded"),
        ),
        patch(
            "daimon.adapters.discord.bot.set_provision_status",
            new_callable=AsyncMock,
            side_effect=_flip_side_effect,
        ),
    ):
        await bot.on_guild_join(guild)
        # _drain_bg_tasks must complete without raising — the exception must not escape
        await _drain_bg_tasks(bot)

    typed_events = [e for e in logs if e["event"] == "guild_seed_failed"]
    assert len(typed_events) >= 1, "DaimonError must be handled by the typed branch"
    unexpected_events = [e for e in logs if e["event"] == "guild_seed_unexpected"]
    assert len(unexpected_events) == 0, (
        "typed error must stay in the typed branch, never reach the catch-all"
    )
    flip_failed_events = [e for e in logs if e["event"] == "guild_seed_status_flip_failed"]
    assert len(flip_failed_events) >= 1, (
        "typed-branch flip failure must be best-effort and log guild_seed_status_flip_failed"
    )

    # Snag embed still posted despite the flip failure.
    welcome_ch = guild.system_channel
    embed_texts = [
        (c.kwargs["embed"].title or "") + (c.kwargs["embed"].description or "")
        for c in welcome_ch.send.await_args_list  # pyright: ignore[reportUnknownMemberType]
        if "embed" in c.kwargs
    ]
    assert any("snag" in t.lower() for t in embed_texts), (
        "snag embed must still be posted even when the typed-branch flip fails"
    )


async def test_spawn_done_callback_logs_escaped_exception(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """G1: an exception that escapes a _spawn'd task must be logged immediately as
    bg_task_failed via the done-callback, not silently swallowed until GC."""
    runtime = _make_runtime(db_session_factory)
    bot = _make_bot(runtime)

    async def _raises() -> None:
        raise RuntimeError("boom")

    task = bot._spawn(_raises())  # pyright: ignore[reportPrivateUsage]

    with structlog.testing.capture_logs() as logs:
        # gather with return_exceptions so the test doesn't re-raise from the task.
        await asyncio.gather(task, return_exceptions=True)
        # yield control so the done-callback (scheduled via call_soon) fires.
        await asyncio.sleep(0)

    bg_task_events = [e for e in logs if e["event"] == "bg_task_failed"]
    assert len(bg_task_events) >= 1, (
        "_spawn done-callback must emit bg_task_failed when a task raises an exception"
    )
    assert "task_name" in bg_task_events[0], (
        "bg_task_failed log must include task_name so the failing task is identifiable"
    )
