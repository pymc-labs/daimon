"""Tests for `/billing`'s principal persistence.

The command mints a platform principal (and the account row behind it) for a
first-time caller, then hands `principal.account_id` to `BillingPanelView`. The
panel later mints a checkout JWT naming that account. If the handler's session
never commits, the account rolls back while its uuid lives on in the view — the
JWT then authenticates against a row that does not exist and the checkout route
401s. Both tests read back through a FRESH session so they see only what
survived the handler's transaction.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
from daimon.adapters.discord.billing_panel.panel import BillingPanelView
from daimon.adapters.discord.commands.billing import BillingCog
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.accounts import account_exists
from daimon.core.stores.identity import find_platform_principal
from daimon.testing.factories import make_tenant
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _make_runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp.public_url = None
    return DiscordRuntime(
        settings=settings,
        anthropic=MagicMock(),  # pyright: ignore[reportArgumentType]  # /billing makes no MA call
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # command tests never run a turn
    )


def _make_interaction(runtime: DiscordRuntime, *, guild_id: int, user_id: int) -> MagicMock:
    """A registered-guild interaction from a plain (non-admin) member.

    The caller is a spec'd `discord.Member` with both permission flags off and a
    differing guild owner, so `is_guild_admin` resolves False and the handler
    takes `load_billing_snapshot`'s member branch. Principal creation is on that
    branch too — it does not depend on admin.
    """
    guild = MagicMock(spec=discord.Guild)
    guild.owner_id = user_id + 1

    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.guild_permissions.administrator = False
    user.guild_permissions.manage_guild = False

    interaction = MagicMock()
    interaction.client.runtime = runtime
    interaction.guild_id = guild_id
    interaction.guild = guild
    interaction.user = user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def test_billing_command_persists_the_platform_principal_for_a_first_time_user(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 710000001
    user_id = 100000000000000042
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(guild_id))
    async with db_session_factory() as setup_session:
        await make_tenant(setup_session, platform="discord", workspace_id=str(guild_id))
        await setup_session.commit()

    runtime = _make_runtime(db_session_factory)
    interaction = _make_interaction(runtime, guild_id=guild_id, user_id=user_id)
    cog = BillingCog(MagicMock())

    await cog.billing.callback(cog, interaction)  # pyright: ignore[reportArgumentType]

    async with db_session_factory() as fresh_session:
        principal = await find_platform_principal(
            fresh_session,
            tenant_id=tenant_id,
            platform="discord",
            external_id=str(user_id),
        )
        assert principal is not None, (
            "/billing must leave the platform principal it created behind — "
            "an uncommitted session rolls the row back while the panel keeps its uuid"
        )
        assert await account_exists(fresh_session, account_id=principal.account_id), (
            "the account the top-up JWT names must exist after the handler returns"
        )


async def test_billing_command_panel_account_id_matches_the_persisted_account(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 710000002
    user_id = 100000000000000043
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(guild_id))
    async with db_session_factory() as setup_session:
        await make_tenant(setup_session, platform="discord", workspace_id=str(guild_id))
        await setup_session.commit()

    runtime = _make_runtime(db_session_factory)
    interaction = _make_interaction(runtime, guild_id=guild_id, user_id=user_id)
    cog = BillingCog(MagicMock())

    await cog.billing.callback(cog, interaction)  # pyright: ignore[reportArgumentType]

    interaction.followup.send.assert_awaited_once()
    view = interaction.followup.send.call_args.kwargs.get("view")
    assert isinstance(view, BillingPanelView), (
        "a registered guild must render the billing panel, not an error followup"
    )

    async with db_session_factory() as fresh_session:
        principal = await find_platform_principal(
            fresh_session,
            tenant_id=tenant_id,
            platform="discord",
            external_id=str(user_id),
        )
    assert principal is not None, "the handler must have persisted a principal"
    assert view.account_id == principal.account_id, (
        "the account id the panel carries into the checkout JWT must be the "
        "persisted principal's account, not a uuid that only ever existed in memory"
    )


async def test_billing_command_answers_after_a_database_read_error(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 710000003
    async with db_session_factory() as session:
        await make_tenant(session, platform="discord", workspace_id=str(guild_id))
        await session.commit()
    interaction = _make_interaction(
        _make_runtime(db_session_factory), guild_id=guild_id, user_id=44
    )
    cog = BillingCog(MagicMock())
    with patch(
        "daimon.adapters.discord.commands.billing.load_billing_snapshot",
        new=AsyncMock(side_effect=SQLAlchemyError("database unavailable")),
    ):
        await cog.billing.callback(cog, interaction)  # pyright: ignore[reportArgumentType]
    interaction.response.defer.assert_awaited_once()
    # The deferred "thinking" reply gets an answer instead of hanging.
    interaction.followup.send.assert_awaited_once()
    assert "Something went wrong on our side." in interaction.followup.send.await_args.args[0]
