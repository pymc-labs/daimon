"""Every read the setup panel makes, out of the views and out of the Cog.

The screens are pure renderers over `PanelState`; this module is the shell that
fills that state in. It also fixes what is paid for up front: the roster screen
buys one MA listing, one config resolution, the cascade and — inside a thread —
one binding read. A key list, a repo binding and the per-agent MA retrieve are
bought only when a reader opens Details on one agent.

Nothing here resolves a credential. `github_facts` reads two booleans off the
deployment's settings so `daimon.core.agent_details` can describe repo access
while remaining unable to touch a token.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from daimon.adapters.discord.agent_setup.scope_default import (
    list_guild_propagations,
    resolve_account_display,
)
from daimon.adapters.discord.agent_setup.state import PanelState, ThreadContext
from daimon.adapters.discord.checks import channel_admin_caller
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_details import AgentDetails, GitHubDeploymentFacts, load_agent_details
from daimon.core.agent_identity import (
    identity_enabled_for,
    is_builtin_agent,
    resolve_agent_identity,
)
from daimon.core.answering_map import AnsweringMap, load_answering_map
from daimon.core.channel_admins import load_administered_channel_ids
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.errors import UserFacingError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.roster import RosterAgent, load_roster
from daimon.core.rule_views import RuleViewer, load_rule_viewer
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.thread_agent_bindings import get_binding
from sqlalchemy.ext.asyncio import AsyncSession

import discord

# `resolve_account_display` renders a resolved Discord principal as a mention
# and anything else as "account <8 hex>". Only the first names a person the
# reader can recognise, and the shared guild stamp never resolves to one, so the
# mention prefix separates an attribution worth showing from noise.
_MENTION_PREFIX = "<@"

# The bot permissions Daimon asks for at install, Manage Webhooks included.
# Re-authorizing with this set restores webhooks without dropping anything else.
BOT_INSTALL_PERMISSIONS = 326954503232


def reauthorize_url(*, application_id: int, guild_id: int) -> str:
    """The OAuth link that re-adds the bot to one server with its full permission set."""
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={application_id}&permissions={BOT_INSTALL_PERMISSIONS}"
        f"&scope=bot+applications.commands&guild_id={guild_id}&disable_guild_select=true"
    )


def webhook_fix_url(
    runtime: DiscordRuntime, interaction: discord.Interaction, *, is_admin: bool
) -> str | None:
    """The re-authorize link for an admin whose server withholds Manage Webhooks.

    Agent identity posts through a channel webhook; without Manage Webhooks every
    answer falls back to the bot account. Only an admin can fix that, so nobody
    else is shown the link, and a server with identity off has nothing to fix.
    """
    guild = interaction.guild
    if not is_admin or guild is None:
        return None
    if not identity_enabled_for(runtime.settings, "discord", guild.id):
        return None
    permissions = guild.me.guild_permissions
    if permissions.manage_webhooks or permissions.administrator:
        return None
    return reauthorize_url(application_id=interaction.application_id, guild_id=guild.id)


def github_facts(runtime: DiscordRuntime) -> GitHubDeploymentFacts:
    """The two GitHub facts a repo's access state is derived from.

    The App counts as configured only with both halves of its identity present;
    an app id without a private key mints nothing.
    """
    github = runtime.settings.github
    return GitHubDeploymentFacts(
        has_fallback_pat=github.fallback_pat is not None,
        app_configured=github.app_id is not None and github.app_private_key is not None,
    )


def public_mcp_url(runtime: DiscordRuntime) -> str | None:
    """This deployment's own MCP server URL, so Details can leave it out."""
    if runtime.settings.mcp.public_url is None:
        return None
    return str(runtime.settings.mcp.public_url)


def _tenant_id(state: PanelState) -> uuid.UUID:
    """The tenant the open panel belongs to, from the guild it was opened in.

    The id is derived rather than carried because it is a pure function of the
    guild, and the Cog already proved the row exists before building the state.
    """
    return derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id))


def _channel_label(state: PanelState) -> str | None:
    """`#channel` for the routing sentences, or None when the name is unknown."""
    if not state.channel_name:
        return None
    return f"#{state.channel_name}"


def _panel_location(
    interaction: discord.Interaction,
) -> tuple[str | None, str | None, str | None]:
    """The interaction's (channel id, channel name, thread id).

    Inside a thread the panel is about the parent channel — that is where a
    mention lands for everyone not in the thread — so the parent comes back as
    the channel and the thread rides alongside it.
    """
    channel = interaction.channel
    thread_id: str | None = None
    if isinstance(channel, discord.Thread):
        thread_id = str(channel.id)
        channel = channel.parent
    if not isinstance(channel, discord.abc.GuildChannel | discord.Thread):
        return None, None, thread_id
    return str(channel.id), channel.name, thread_id


async def load_roster_state(
    runtime: DiscordRuntime,
    interaction: discord.Interaction,
    *,
    tenant_id: uuid.UUID,
    is_admin: bool,
) -> PanelState:
    """Everything the roster screen renders, in one session.

    The selection starts on the thread's setup target when the panel was opened
    in a live setup conversation and that target is still on the roster, and on
    whichever agent answers here otherwise — so the primary action already
    points somewhere sensible before the reader touches anything.
    """
    assert interaction.guild_id is not None, "the setup panel is guild-only"
    channel_id, channel_name, thread_id = _panel_location(interaction)
    if channel_id is None:
        raise UserFacingError("Run `/agent-setup` in a server channel.")

    async with runtime.sessionmaker() as session:
        principal = await get_or_create_platform_principal(
            session,
            tenant_id=tenant_id,
            platform="discord",
            external_id=str(interaction.user.id),
        )
        await session.commit()
        roster = await load_roster(
            session,
            runtime.anthropic,
            tenant_id=tenant_id,
            platform="discord",
            channel_id=channel_id,
            thread_id=thread_id,
            default=runtime.deployment_default,
            viewer=await load_rule_viewer(
                session,
                runtime.anthropic,
                tenant_id=tenant_id,
                channel_id=channel_id,
                is_admin=is_admin,
                platform="discord",
                thread_id=thread_id,
                default=runtime.deployment_default,
            ),
        )
        cascade = await list_guild_propagations(session, tenant_id=tenant_id)
        managed_channels = (
            await load_administered_channel_ids(
                session,
                tenant_id=tenant_id,
                platform="discord",
                caller=channel_admin_caller(interaction.user),
            )
            if not is_admin
            else frozenset[str]()
        )
        binding = (
            await get_binding(
                session,
                tenant_id=tenant_id,
                platform="discord",
                parent_channel_id=channel_id,
                thread_id=thread_id,
            )
            if thread_id is not None
            else None
        )

    thread_context: ThreadContext | None = None
    selected = roster.answering
    if binding is not None:
        thread_context = ThreadContext(
            kind=binding.kind,
            responder_name=binding.responder_name,
            target_name=binding.configuration_target_name,
        )
        target = next(
            (row for row in roster.rows if row.name == binding.configuration_target_name), None
        )
        if target is not None:
            selected = target

    state = PanelState(
        roster=[],
        selected=None,
        account_id=principal.account_id,
        platform_principal_id=principal.id,
        guild_account_id=derive_guild_account_uuid(tenant_id),
        default_mcp_url=public_mcp_url(runtime),
        is_admin=is_admin,
        can_manage_github_agents=is_admin or bool(managed_channels),
        guild_id=interaction.guild_id,
        channel_id=int(channel_id),
        channel_name=channel_name,
        cascade_view=cascade,
        deployment_default=runtime.deployment_default,
        roster_agents=roster.rows,
        answering=roster.answering,
        selected_agent=selected,
        thread_context=thread_context,
        thread_id=thread_id,
        webhook_fix_url=webhook_fix_url(runtime, interaction, is_admin=is_admin),
    )
    state.attributions = await resolve_attributions(runtime, state=state, agents=roster.rows)
    return state


async def load_details_for(
    runtime: DiscordRuntime, *, state: PanelState, agent: RosterAgent
) -> AgentDetails:
    """Read one agent's whole readable state, as of right now.

    Details is read fresh on every open rather than cached on the panel: the
    reader is being shown claims about keys, a repo and routing, and a claim
    that was true when the panel opened is not the same as a claim that is true.
    """
    async with runtime.sessionmaker() as session:
        details = await load_agent_details(
            session,
            runtime.anthropic,
            tenant_id=_tenant_id(state),
            ma_agent_id=agent.ma_agent_id,
            platform="discord",
            channel_id=str(state.channel_id),
            thread_id=state.thread_id,
            deployment_default=state.deployment_default,
            github=github_facts(runtime),
            public_mcp_url=public_mcp_url(runtime),
            is_admin=state.is_admin,
            channel_label=_channel_label(state),
        )
        state.avatar_urls[agent.name] = None
        if identity_enabled_for(
            runtime.settings, "discord", state.guild_id
        ) and not is_builtin_agent(
            name=details.name,
            metadata={MA_METADATA_KEY_MANAGED: "true"} if details.daimon_managed else None,
            default_agent_name=runtime.deployment_default.agent_name,
        ):
            identity = await resolve_agent_identity(
                session,
                tenant_id=_tenant_id(state),
                agent_name=agent.name,
                is_builtin=False,
                public_base_url=runtime.settings.mcp.app_root_url,
                enabled=True,
                background_sessionmaker=runtime.sessionmaker,
            )
            state.avatar_urls[agent.name] = identity.avatar_url
        return details


async def load_answering_map_for(runtime: DiscordRuntime, *, state: PanelState) -> AnsweringMap:
    """Read every tier of this install's routing, plus its setup conversations.

    A non-admin sees only their side of every isolated channel's line.
    """
    async with runtime.sessionmaker() as session:
        return await load_answering_map(
            session,
            tenant_id=_tenant_id(state),
            platform="discord",
            default=state.deployment_default,
            viewer=await panel_viewer(session, runtime, state=state),
        )


async def panel_viewer(
    session: AsyncSession, runtime: DiscordRuntime, *, state: PanelState
) -> RuleViewer | None:
    """What this panel's reader sees of isolated channels; None for admins."""
    return await load_rule_viewer(
        session,
        runtime.anthropic,
        tenant_id=_tenant_id(state),
        channel_id=str(state.channel_id) if state.channel_id else None,
        is_admin=state.is_admin,
        platform="discord",
        thread_id=str(state.thread_id) if state.thread_id else None,
        default=runtime.deployment_default,
    )


async def resolve_attributions(
    runtime: DiscordRuntime, *, state: PanelState, agents: Sequence[RosterAgent]
) -> dict[str, str]:
    """Map ma_agent_id to a Discord mention, for the creators that resolve to one.

    An agent stamped with the shared guild account was created on behalf of the
    server rather than by a person, and inventing a name for it would be worse
    than saying nothing — so it is skipped, as is any account with no Discord
    principal behind it. Each distinct account is looked up once, however many
    agents it made.
    """
    by_account: dict[uuid.UUID, list[str]] = {}
    for agent in agents:
        account_id = agent.created_by_account_id
        if account_id is None or account_id == state.guild_account_id:
            continue
        by_account.setdefault(account_id, []).append(agent.ma_agent_id)
    if not by_account:
        return {}
    resolved: dict[str, str] = {}
    async with runtime.sessionmaker() as session:
        for account_id, ma_agent_ids in by_account.items():
            display = await resolve_account_display(session, account_id=account_id)
            if not display.startswith(_MENTION_PREFIX):
                continue
            for ma_agent_id in ma_agent_ids:
                resolved[ma_agent_id] = display
    return resolved
