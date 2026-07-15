"""Tests for gating decorators: require_registered_guild, require_admin."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.discord.checks import (
    require_admin,
    require_registered_guild,
    resolve_tenant_for_interaction,
)
from daimon.core._models import Account, PlatformPrincipal, Tenant
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _make_interaction(
    *,
    guild_id: int | None = 123456,
    sessionmaker: AsyncMock | async_sessionmaker[AsyncSession] | None = None,
    user_id: int = 999,
) -> MagicMock:
    """Build a mock Interaction with the minimum attributes the decorators touch."""
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.response.send_message = AsyncMock()
    interaction.user.id = user_id

    runtime = MagicMock()

    if sessionmaker is not None:
        runtime.sessionmaker = sessionmaker
    else:
        # Default mock sessionmaker that yields an AsyncMock session
        session = AsyncMock()
        sm = AsyncMock()
        sm.__aenter__ = AsyncMock(return_value=session)
        sm.__aexit__ = AsyncMock(return_value=False)
        runtime.sessionmaker.return_value = sm

    interaction.client.runtime = runtime
    return interaction


# ---------------------------------------------------------------------------
# require_registered_guild
# ---------------------------------------------------------------------------


class TestRequireRegisteredGuild:
    async def test_rejects_no_guild_id(self) -> None:
        """DM context (guild_id=None) should be rejected with ephemeral error."""
        wrapped = AsyncMock()

        @require_registered_guild
        async def handler(self: object, interaction: object) -> None:  # type: ignore[override]
            await wrapped(self, interaction)  # type: ignore[arg-type]

        interaction = _make_interaction(guild_id=None)
        await handler(MagicMock(), interaction)

        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.call_args
        assert "only available in a server" in call_kwargs.args[0], (
            "should mention server requirement"
        )
        assert call_kwargs.kwargs.get("ephemeral") is True, "error should be ephemeral"
        wrapped.assert_not_awaited()

    async def test_rejects_unregistered_guild(self) -> None:
        """Guild without a tenants row should be rejected."""
        wrapped = AsyncMock()

        @require_registered_guild
        async def handler(self: object, interaction: object) -> None:  # type: ignore[override]
            await wrapped(self, interaction)  # type: ignore[arg-type]

        session = AsyncMock()
        sm = AsyncMock()
        sm.__aenter__ = AsyncMock(return_value=session)
        sm.__aexit__ = AsyncMock(return_value=False)

        interaction = _make_interaction()
        interaction.client.runtime.sessionmaker.return_value = sm

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "daimon.adapters.discord.checks.resolve_tenant_for_interaction",
                AsyncMock(return_value=None),
            )
            await handler(MagicMock(), interaction)

        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.call_args
        assert "not registered" in call_kwargs.args[0], "should mention registration"
        assert call_kwargs.kwargs.get("ephemeral") is True
        wrapped.assert_not_awaited()

    async def test_passes_registered_guild(self) -> None:
        """Registered guild should call through to the wrapped function."""
        wrapped = AsyncMock()

        @require_registered_guild
        async def handler(self: object, interaction: object) -> None:  # type: ignore[override]
            await wrapped(self, interaction)  # type: ignore[arg-type]

        session = AsyncMock()
        sm = AsyncMock()
        sm.__aenter__ = AsyncMock(return_value=session)
        sm.__aexit__ = AsyncMock(return_value=False)

        interaction = _make_interaction()
        interaction.client.runtime.sessionmaker.return_value = sm

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "daimon.adapters.discord.checks.resolve_tenant_for_interaction",
                AsyncMock(return_value=uuid.uuid4()),
            )
            self_mock = MagicMock()
            await handler(self_mock, interaction)

        interaction.response.send_message.assert_not_awaited()
        wrapped.assert_awaited_once()


# ---------------------------------------------------------------------------
# require_admin
# ---------------------------------------------------------------------------


class TestRequireAdmin:
    async def test_rejects_user_role(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Discord user with role='user' is rejected with ephemeral error."""
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id="123456")
        tenant = Tenant(id=tenant_id, platform="discord", external_id="123456")
        db_session.add(tenant)
        await db_session.flush()

        account = Account(tenant_id=tenant.id, role="user")
        db_session.add(account)
        await db_session.flush()

        principal = PlatformPrincipal(
            tenant_id=tenant.id,
            platform="discord",
            external_id="111",
            account_id=account.id,
        )
        db_session.add(principal)
        await db_session.flush()

        wrapped = AsyncMock()

        @require_admin
        async def handler(self: object, interaction: object) -> None:  # type: ignore[override]
            await wrapped(self, interaction)  # type: ignore[arg-type]

        interaction = _make_interaction(
            sessionmaker=db_session_factory,
            user_id=111,
        )

        await handler(MagicMock(), interaction)

        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.call_args
        assert "admin" in call_kwargs.args[0].lower(), "rejection message should mention admin"
        assert call_kwargs.kwargs.get("ephemeral") is True, "rejection should be ephemeral"
        wrapped.assert_not_awaited()

    async def test_passes_admin_role(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Discord user with role='admin' passes through to handler."""
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id="123456")
        tenant = Tenant(id=tenant_id, platform="discord", external_id="123456")
        db_session.add(tenant)
        await db_session.flush()

        account = Account(tenant_id=tenant.id, role="admin")
        db_session.add(account)
        await db_session.flush()

        principal = PlatformPrincipal(
            tenant_id=tenant.id,
            platform="discord",
            external_id="222",
            account_id=account.id,
        )
        db_session.add(principal)
        await db_session.flush()

        wrapped = AsyncMock()

        @require_admin
        async def handler(self: object, interaction: object) -> None:  # type: ignore[override]
            await wrapped(self, interaction)  # type: ignore[arg-type]

        interaction = _make_interaction(
            sessionmaker=db_session_factory,
            user_id=222,
        )

        await handler(MagicMock(), interaction)

        interaction.response.send_message.assert_not_awaited()
        wrapped.assert_awaited_once()

    async def test_auto_creates_user_account_and_rejects(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """First-time Discord user gets auto-created account (role='user') and is rejected."""
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id="123456")
        tenant = Tenant(id=tenant_id, platform="discord", external_id="123456")
        db_session.add(tenant)
        # No account or principal seeded for external_id="333"
        await db_session.flush()

        wrapped = AsyncMock()

        @require_admin
        async def handler(self: object, interaction: object) -> None:  # type: ignore[override]
            await wrapped(self, interaction)  # type: ignore[arg-type]

        interaction = _make_interaction(
            sessionmaker=db_session_factory,
            user_id=333,
        )

        await handler(MagicMock(), interaction)

        # Should be rejected since auto-created account has role='user'
        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.call_args
        assert call_kwargs.kwargs.get("ephemeral") is True, "rejection should be ephemeral"
        wrapped.assert_not_awaited()

        # Verify account was auto-created with role='user' in the DB
        from daimon.core._models import Account as Acc
        from daimon.core._models import PlatformPrincipal as PP
        from sqlalchemy import select

        pp_row = (
            await db_session.execute(
                select(PP).where(
                    PP.tenant_id == tenant.id,
                    PP.platform == "discord",
                    PP.external_id == "333",
                )
            )
        ).scalar_one()
        created_account = await db_session.get(Acc, pp_row.account_id)
        assert created_account is not None, "account should have been created for first-time user"
        assert created_account.role == "user", "auto-created account should have role='user'"


# ---------------------------------------------------------------------------
# resolve_tenant_for_interaction (per_message_tenant invariant)
# ---------------------------------------------------------------------------


class TestResolveTenantForInteraction:
    async def test_resolve_tenant_for_interaction_returns_provisioned_tenant(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Per-interaction resolution returns the provisioned guild's tenant_id."""
        result = await provision_tenant(db_session_factory, platform="discord", workspace_id="123")

        interaction = _make_interaction(guild_id=123, sessionmaker=db_session_factory)
        bot = interaction.client

        resolved = await resolve_tenant_for_interaction(bot, interaction)
        assert resolved == result.tenant_id, "should resolve the provisioned tenant_id"

    async def test_resolve_tenant_for_interaction_returns_none_unprovisioned(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An unprovisioned guild resolves to None."""
        interaction = _make_interaction(guild_id=99999, sessionmaker=db_session_factory)
        resolved = await resolve_tenant_for_interaction(interaction.client, interaction)
        assert resolved is None, "unprovisioned guild should resolve to None"

    async def test_resolve_tenant_for_interaction_returns_none_no_guild(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A DM context (guild_id None) resolves to None without a DB hit."""
        interaction = _make_interaction(guild_id=None, sessionmaker=db_session_factory)
        resolved = await resolve_tenant_for_interaction(interaction.client, interaction)
        assert resolved is None, "guild_id None should resolve to None"
