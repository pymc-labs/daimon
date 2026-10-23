"""Live interaction gates, including stale panels and Discord role removal."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.agent_setup.admins_view import AdminGrantModal
from daimon.adapters.discord.checks import is_guild_admin, is_platform_admin, refuse_if_not_admin
from daimon.core.stores.daimon_admins import change_grant, list_admins
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD = 123456789012345678
USER = 223456789012345678
ROLE = 323456789012345678


def interaction(sm: async_sessionmaker[AsyncSession]) -> MagicMock:
    value = MagicMock()
    value.guild_id = GUILD
    value.guild.owner_id = GUILD + 1
    value.user = MagicMock(spec=discord.Member)
    value.user.id = USER
    value.user.guild_permissions.administrator = False
    value.user.guild_permissions.manage_guild = False
    value.user.roles = []
    value.client.runtime.sessionmaker = sm
    value.response.send_message = AsyncMock()
    value.followup.send = AsyncMock()
    value.response.is_done.return_value = False
    return value


@pytest.mark.parametrize("kind", ["user", "role"])
async def test_granted_gate_rechecks_revocation_and_role_removal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    kind: str,
) -> None:
    tenant = await make_tenant(db_session, workspace_id=str(GUILD))
    key = dict(
        tenant_id=tenant.id,
        platform="discord",
        subject_kind=kind,
        subject_id=str(USER if kind == "user" else ROLE),
        actor_user_id="owner",
        is_platform_admin=True,
    )
    await change_grant(db_session, **key)
    await db_session.commit()
    click = interaction(db_session_factory)
    role = MagicMock(spec=discord.Role)
    role.id = ROLE
    role.is_default.return_value = False
    click.user.roles = [role]
    assert await is_guild_admin(click)
    assert not is_platform_admin(click)
    assert not await refuse_if_not_admin(click)
    # This is the same already-rendered panel/interaction object.
    if kind == "role":
        click.user.roles = []
        assert not await is_guild_admin(click)
        click.user.roles = [role]
    await change_grant(db_session, **key, revoke=True)
    await db_session.commit()
    assert await refuse_if_not_admin(click)
    click.response.send_message.assert_awaited_once()
    click.user.guild_permissions.manage_guild = True
    assert await is_guild_admin(click), "platform authority cannot be revoked"


async def test_delegated_admin_cannot_submit_grant_modal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, workspace_id=str(GUILD))
    await change_grant(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        subject_kind="user",
        subject_id=str(USER),
        actor_user_id="owner",
        is_platform_admin=True,
    )
    await db_session.commit()
    click = interaction(db_session_factory)
    view = MagicMock(allowed_user_id=USER)
    view.state.guild_id = GUILD
    view.runtime.sessionmaker = db_session_factory
    modal = AdminGrantModal(view)
    await modal.on_submit(click)
    click.response.send_message.assert_awaited_once()
    assert "Only platform admins" in click.response.send_message.call_args.args[0]
    assert len(await list_admins(db_session, tenant_id=tenant.id)) == 1


async def test_granted_admin_is_refused_at_billing_gate(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.adapters.discord.checks import refuse_if_not_platform_admin

    tenant = await make_tenant(db_session, workspace_id=str(GUILD))
    await change_grant(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        subject_kind="user",
        subject_id=str(USER),
        actor_user_id="owner",
        is_platform_admin=True,
    )
    await db_session.commit()
    click = interaction(db_session_factory)
    assert await is_guild_admin(click)
    assert await refuse_if_not_platform_admin(click)
    click.response.send_message.assert_awaited_once()
