"""Live interaction gates, including stale panels and Discord role removal."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.agent_setup.admins_view import AdminGrantModal
from daimon.adapters.discord.checks import is_guild_admin, is_platform_admin, refuse_if_not_admin
from daimon.core.admin_actions import DelegatedAction
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
    assert await is_guild_admin(click, action=DelegatedAction.AGENT_SETTINGS)
    assert not is_platform_admin(click)
    assert not await refuse_if_not_admin(click, action=DelegatedAction.AGENT_SETTINGS)
    # This is the same already-rendered panel/interaction object.
    if kind == "role":
        click.user.roles = []
        assert not await is_guild_admin(click, action=DelegatedAction.AGENT_SETTINGS)
        click.user.roles = [role]
    await change_grant(db_session, **key, revoke=True)
    await db_session.commit()
    assert await refuse_if_not_admin(click, action=DelegatedAction.AGENT_SETTINGS)
    click.response.send_message.assert_awaited_once()
    click.user.guild_permissions.manage_guild = True
    assert await is_guild_admin(click, action=DelegatedAction.AGENT_SETTINGS), (
        "platform authority cannot be revoked"
    )


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
    assert await is_guild_admin(click, action=DelegatedAction.AGENT_SETTINGS)
    assert await refuse_if_not_platform_admin(click)
    click.response.send_message.assert_awaited_once()


@pytest.mark.parametrize("action", list(DelegatedAction))
async def test_explicit_action_admits_delegate_without_promoting_account(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    action: DelegatedAction,
) -> None:
    from daimon.adapters.discord.checks import admin_caller
    from daimon.core.native_admins import load_live_subject
    from daimon.core.stores.accounts import get_account
    from daimon.core.stores.agent_tool_rules import set_rule
    from daimon.testing.factories import make_platform_principal

    tenant = await make_tenant(db_session, workspace_id=str(GUILD))
    principal = await make_platform_principal(
        db_session, tenant=tenant, platform="discord", external_id=str(USER)
    )
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
    click.guild.id = GUILD
    click.user.guild = click.guild
    from typing import cast

    assert not await is_guild_admin(click), "an unnamed action is platform-only"
    assert not await is_guild_admin(click, action=cast(DelegatedAction, "billing")), (
        "an unknown action cannot opt into delegated authority"
    )
    assert await is_guild_admin(click, action=action)
    subject = await load_live_subject(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        caller=admin_caller(click.user),
        action=action,
    )
    assert subject.is_admin
    # This store has its own authorization gate. It must consume the live
    # subject rather than independently consulting accounts.role.
    if action is DelegatedAction.TOOL_RULES:
        rule = await set_rule(
            db_session,
            tenant_id=tenant.id,
            agent_id="agent_test",
            agent_name="Helper",
            tool_key="example/write",
            server_url="https://example.org/mcp",
            rule="ask",
            set_by=str(USER),
            actor=subject,
        )
        assert rule.rule == "ask"
    if action is DelegatedAction.MODEL:
        from daimon.core.stores.thread_models import get_thread_model, set_thread_model

        await set_thread_model(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread",
            model="claude-sonnet-4-6",
        )
        assert (
            await get_thread_model(
                db_session, tenant_id=tenant.id, platform="discord", thread_id="thread"
            )
            == "claude-sonnet-4-6"
        )
    elif action in {DelegatedAction.ENVIRONMENTS, DelegatedAction.ROUTING}:
        from daimon.core.scope import ChannelScopeRef
        from daimon.core.stores.scoped_config_read import get_scope
        from daimon.core.stores.scoped_config_write import set_fields

        scope = ChannelScopeRef(tenant_id=tenant.id, channel_id="channel")
        await set_fields(
            db_session,
            scope=scope,
            tenant_id=tenant.id,
            agent_name="Helper" if action is DelegatedAction.ROUTING else None,
            environment_name="science" if action is DelegatedAction.ENVIRONMENTS else None,
            actor_account_id=principal.account_id,
        )
        saved = await get_scope(db_session, scope=scope)
        assert saved is not None
        assert (
            saved.agent_name if action is DelegatedAction.ROUTING else saved.environment_name
        ) == ("Helper" if action is DelegatedAction.ROUTING else "science")
    elif action is DelegatedAction.SKILLS:
        from daimon.core.channel_skills import may_set_channel_skills
        from daimon.core.stores.channel_skills import add_channel_skill

        assert may_set_channel_skills(subject, "channel")
        skill = await add_channel_skill(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="channel",
            skill_id="skill_test",
            version="1",
            name="review",
            owner_agent_name=None,
            actor_account_id=principal.account_id,
        )
        assert skill.skill_id == "skill_test"
    elif action is DelegatedAction.CHANNEL_PERMISSIONS:
        from daimon.core.channel_rules import set_channel_rule
        from daimon.core.scope import DeploymentDefault
        from daimon.core.stores.access_policy import load_access_policy

        await set_channel_rule(
            MagicMock(),
            db_session_factory,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="channel",
            writers="none",
            subject=subject,
            default=DeploymentDefault(),
            actor_account_id=principal.account_id,
        )
        assert (await load_access_policy(db_session, tenant_id=tenant.id)).channel_rules[
            "channel"
        ].writers == "none"
    account = await get_account(db_session, principal.account_id)
    assert account is not None and account.role.value == "user"
    await change_grant(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        subject_kind="user",
        subject_id=str(USER),
        actor_user_id="owner",
        is_platform_admin=True,
        revoke=True,
    )
    await db_session.commit()
    assert not await is_guild_admin(click, action=action)
    subject = await load_live_subject(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        caller=admin_caller(click.user),
        action=action,
    )
    assert not subject.is_admin
    if action is DelegatedAction.TOOL_RULES:
        with pytest.raises(PermissionError):
            await set_rule(
                db_session,
                tenant_id=tenant.id,
                agent_id="agent_test",
                agent_name="Helper",
                tool_key="example/write",
                server_url="https://example.org/mcp",
                rule="allow",
                set_by=str(USER),
                actor=subject,
            )


async def test_delegate_github_connect_button_refuses_at_adapter(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid
    from types import SimpleNamespace

    from daimon.adapters.discord import github_connect_button as button_module

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
    bind = AsyncMock()
    monkeypatch.setattr(button_module, "bind_discord_connect_click", bind)
    monkeypatch.setattr(
        button_module, "connect_intent_agent", AsyncMock(return_value=("ResearchBot", "ma-agent"))
    )
    monkeypatch.setattr(
        button_module,
        "find_agent_by_derived_uuid",
        AsyncMock(return_value=SimpleNamespace(name="ResearchBot", metadata={})),
    )
    click = interaction(db_session_factory)
    click.channel_id = 789
    click.response.defer = AsyncMock()
    click.guild.fetch_member = AsyncMock(return_value=click.user)
    assert await is_guild_admin(click, action=DelegatedAction.AGENT_SETTINGS)
    # The delegate asked for the card, so only the requester's manager check stands
    # between them and the link: a Daimon admin is neither a platform admin nor an owner.
    button = button_module.GitHubConnectButton(requester_id=str(USER), intent_id=uuid.uuid4())
    await button._reveal(click)  # pyright: ignore[reportPrivateUsage]
    bind.assert_not_awaited()
    kwargs = click.followup.send.await_args.kwargs
    assert kwargs["embed"].description == "Ask whoever manages ResearchBot to add repos."
    assert "view" not in kwargs


async def test_delegate_cannot_open_operator_token_panel(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.adapters.discord.agent_setup.routing_view import RoutingView

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
    assert await is_guild_admin(click, action=DelegatedAction.ROUTING)
    panel = MagicMock()
    panel.swap_to = AsyncMock()
    await RoutingView._on_operator_tokens(panel, click)  # pyright: ignore[reportPrivateUsage]
    click.response.send_message.assert_awaited_once()
    assert "platform admin" in click.response.send_message.call_args.kwargs["embed"].description
    panel.swap_to.assert_not_awaited()
