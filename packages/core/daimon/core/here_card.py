"""A fixed, caller-filtered answer to who answers in this place.

The assembler only accepts names and status facts. It cannot carry credential
values into a card. The loader gathers the existing routing, detail and policy
reads; platform adapters supply live channel visibility.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from datetime import datetime

from anthropic import AsyncAnthropic
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_details import AgentDetails, GitHubDeploymentFacts, load_agent_details
from daimon.core.authz import Action, AgentRef, Place, Subject, authorize
from daimon.core.channel_isolation import load_isolation_viewer, routine_destination_channel
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.roster import load_roster
from daimon.core.scope import (
    ChannelConfigRow,
    ChannelScopeRef,
    DeploymentDefault,
    ScopeContext,
    TenantConfigRow,
    TenantScopeRef,
    merge,
)
from daimon.core.stores import agent_mcp_credentials, mcp_oauth_flows, scoped_config_read
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import McpOAuthGrantRow, Platform, RoutineRow
from daimon.core.stores.identity import (
    get_discord_principal_for_account,
    get_slack_principal_for_account,
)
from daimon.core.stores.routines import list_routines_for_tenant
from daimon.core.stores.thread_agent_bindings import get_binding
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession


class CredentialStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    kind: str
    configured: bool
    usable_in_session: bool | None = None


class HereCard(BaseModel):
    model_config = ConfigDict(frozen=True)
    agent_name: str | None
    tier: str | None
    set_by_account_id: uuid.UUID | None = None
    set_at: datetime | None = None
    configuration_target_name: str | None = None
    sealed: bool
    isolated: bool
    channel_pin_agent_names: tuple[str, ...] = ()
    pin_channels: tuple[str, ...] = ()
    bot_can_view: bool | None = None
    caller_can_view: bool | None = None
    agent_can_read_here: bool | None = None
    category_channels_bot_can_view: tuple[str, ...] = ()
    credentials: tuple[CredentialStatus, ...] = ()
    default_channels: tuple[str, ...] = ()
    routines: tuple[str, ...] = ()
    text: str


def assemble_here_card(
    *,
    channel_id: str,
    agent_name: str | None,
    tier: str | None,
    channel: ChannelConfigRow | None,
    tenant: TenantConfigRow | None,
    configuration_target_name: str | None,
    set_by_label: str | None = None,
    channel_level_only: bool = False,
    thread_set_by_account_id: uuid.UUID | None = None,
    thread_set_at: datetime | None = None,
    policy: TenantAccessPolicy,
    details: AgentDetails | None,
    visible_agent_names: Collection[str] | None = None,
    channels: Sequence[ChannelConfigRow] = (),
    deployment_default: DeploymentDefault | None = None,
    show_routines: bool = False,
    mcp_token_urls: Collection[str] = (),
    personal_grants: Sequence[McpOAuthGrantRow] = (),
    caller_account_id: uuid.UUID | None = None,
    routines: Sequence[RoutineRow] = (),
    visible_channel_ids: Collection[str] | None = None,
    bot_can_view: bool | None = None,
    caller_can_view: bool | None = None,
    agent_can_read_here: bool | None = None,
    category_channels_bot_can_view: Sequence[str] = (),
) -> HereCard:
    """Build the complete card from visible facts; never accept a token value."""
    source = channel if tier == "channel" else tenant if tier == "tenant" else None
    set_by = (
        thread_set_by_account_id
        if tier == "thread"
        else (source.agent_name_set_by_account_id if source is not None else None)
    )
    set_at = (
        thread_set_at
        if tier == "thread"
        else (source.agent_name_set_at if source is not None else None)
    )
    visible = set(visible_channel_ids) if visible_channel_ids is not None else None
    channel_pins = tuple(
        sorted(
            name
            for name, pinned in policy.agent_channel_pins.items()
            if channel_id in pinned and (visible_agent_names is None or name in visible_agent_names)
        )
    )
    pins = tuple(sorted(policy.agent_channel_pins.get(agent_name or "", ())))
    if visible is not None:
        pins = tuple(channel for channel in pins if channel in visible)
    credentials: list[CredentialStatus] = []
    token_urls = {url.rstrip("/") for url in mcp_token_urls}
    defaults: tuple[str, ...] = ()
    routine_labels: tuple[str, ...] = ()
    if details is not None and details.name == agent_name:
        credentials.extend(
            CredentialStatus(
                name=key.name, kind="agent key", configured=True, usable_in_session=None
            )
            for key in details.keys
        )
        for server in details.mcp_servers:
            credentials.append(
                CredentialStatus(
                    name=server.name,
                    kind="MCP server",
                    configured=True,
                    usable_in_session=None,
                )
            )
            if server.url.rstrip("/") in token_urls:
                credentials.append(
                    CredentialStatus(
                        name=server.name,
                        kind="MCP token",
                        configured=True,
                        usable_in_session=None,
                    )
                )
        if details.repo is not None:
            credentials.append(
                CredentialStatus(
                    name=f"GitHub ({details.repo.repo_url})",
                    kind=f"GitHub {details.repo.access.credential.replace('_', ' ')}",
                    configured=details.repo.access.credential != "none",
                    usable_in_session=None,
                )
            )
        for grant in personal_grants:
            if grant.account_id == caller_account_id:
                server = next(
                    (
                        item
                        for item in details.mcp_servers
                        if item.url.rstrip("/") == grant.mcp_server_url.rstrip("/")
                    ),
                    None,
                )
                if server is not None:
                    credentials.append(
                        CredentialStatus(
                            name=server.name,
                            kind="personal OAuth",
                            configured=True,
                            usable_in_session=None,
                        )
                    )
        if visible is not None and deployment_default is not None:
            defaults = tuple(
                sorted(
                    cid
                    for cid in visible
                    if cid != channel_id
                    and merge(
                        channel=next((row for row in channels if row.channel_id == cid), None),
                        tenant=tenant,
                        default=deployment_default,
                    ).agent_name
                    == agent_name
                    and authorize(
                        policy,
                        subject=Subject(),
                        action=Action.RUN_AGENT,
                        agent=AgentRef.of(agent_name),
                        place=Place(channel_id=cid),
                    ).allowed
                )
            )
        routine_labels = tuple(
            sorted(
                f"{row.cron_expr} ({row.timezone}) → {row.destination_id or 'no destination'}"
                for row in routines
                if show_routines
                if (row.agent_id == details.ma_agent_id or row.agent_name == agent_name)
                and (visible is None or (routine_destination_channel(row) in visible))
                and authorize(
                    policy,
                    subject=Subject(),
                    action=Action.READ_CHANNEL,
                    agent=AgentRef.of(agent_name),
                    place=Place(channel_id=routine_destination_channel(row)),
                    origin_channel_ids=frozenset({channel_id}),
                ).allowed
            )
        )
    lines = [
        "**Here**",
        f"Who answers: {_short(agent_name or 'No agent')} ({tier or 'unconfigured'}).",
    ]
    if set_by is not None or set_at is not None:
        when = set_at.isoformat() if set_at else "unknown"
        lines.append(f"Set by: {set_by_label or 'unknown'}; at: {when}.")
    if configuration_target_name is not None:
        lines.append(f"Configuration target: {_short(configuration_target_name)}.")
    if channel_level_only:
        lines.append(
            "Slack /here shows channel-level routing; slash commands provide no thread context."
        )
    lines.extend(
        [
            f"Channel: {'sealed' if channel_id in policy.sealed_channel_ids else 'unsealed'}, "
            f"{'isolated' if channel_id in policy.isolated_channel_ids else 'shared'}.",
            f"Can view here: bot {_yes(bot_can_view)}; you {_yes(caller_can_view)}.",
            f"Agent read policy here: {_yes(agent_can_read_here)}.",
        ]
    )
    if category_channels_bot_can_view:
        lines.append(
            "Bot view in other category channels: " + _summary(category_channels_bot_can_view) + "."
        )
    lines.append(
        "Credentials (names only): "
        + (
            _summary(
                tuple(
                    f"{item.name} [{item.kind}; "
                    f"{'configured' if item.configured else 'not configured'}]"
                    for item in credentials
                ),
                separator="; ",
            )
            if credentials
            else "none known"
        )
        + "."
    )
    lines.append("Credential session usability: unknown (live mount status is unavailable).")
    lines.append("Other defaults: " + (_summary(defaults) if defaults else "none visible") + ".")
    lines.append("Pin channels: " + (_summary(pins) if pins else "none visible") + ".")
    lines.append(
        "Agents pinned here: " + (_summary(channel_pins) if channel_pins else "none visible") + "."
    )
    lines.append(
        "Routines: "
        + (_summary(routine_labels, separator="; ") if routine_labels else "none visible")
        + "."
    )
    return HereCard(
        agent_name=agent_name,
        tier=tier,
        set_by_account_id=set_by,
        set_at=set_at,
        configuration_target_name=configuration_target_name,
        sealed=channel_id in policy.sealed_channel_ids,
        isolated=channel_id in policy.isolated_channel_ids,
        channel_pin_agent_names=channel_pins,
        pin_channels=pins,
        bot_can_view=bot_can_view,
        caller_can_view=caller_can_view,
        agent_can_read_here=agent_can_read_here,
        category_channels_bot_can_view=tuple(category_channels_bot_can_view),
        credentials=tuple(credentials),
        default_channels=defaults,
        routines=routine_labels,
        text="\n".join(lines)[:3900],
    )


def _yes(value: bool | None) -> str:
    return "yes" if value is True else "no" if value is False else "unknown"


def _short(value: str, limit: int = 120) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _summary(values: Sequence[str], *, separator: str = ", ") -> str:
    """Bound one rendered list while preserving the omitted count."""
    shown: list[str] = []
    length = 0
    for value in values:
        item = _short(value, 100)
        if len(shown) == 8 or length + len(item) + len(separator) > 320:
            break
        shown.append(item)
        length += len(item) + len(separator)
    omitted = len(values) - len(shown)
    return separator.join(shown) + (f"{separator}+{omitted} more" if omitted else "")


async def load_here_card(
    session: AsyncSession,
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    platform: Platform,
    channel_id: str,
    thread_id: str | None,
    default: DeploymentDefault,
    github: GitHubDeploymentFacts,
    public_mcp_url: str | None,
    is_admin: bool,
    caller_account_id: uuid.UUID | None,
    channel_level_only: bool = False,
    visible_channel_ids: Collection[str] | None = None,
    bot_can_view: bool | None = None,
    caller_can_view: bool | None = None,
    category_channels_bot_can_view: Sequence[tuple[str, str]] = (),
) -> HereCard:
    """Load the current routing and credential names for one caller's place."""
    policy = await load_access_policy(session, tenant_id=tenant_id)
    viewer = await load_isolation_viewer(
        session, anthropic, tenant_id=tenant_id, channel_id=channel_id, is_admin=is_admin
    )
    roster = await load_roster(
        session,
        anthropic,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
        default=default,
        viewer=viewer,
    )
    resolved = await scoped_config_read.resolve(
        session,
        context=ScopeContext(
            tenant_id=tenant_id, channel_id=channel_id, platform=platform, thread_id=thread_id
        ),
        default=default,
    )
    channel_row = await scoped_config_read.get_scope(
        session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id)
    )
    tenant_row = await scoped_config_read.get_scope(
        session, scope=TenantScopeRef(tenant_id=tenant_id)
    )
    agent_name = roster.answering.name if roster.answering is not None else None
    if resolved.thread_binding_kind == "setup" and resolved.agent_name is not None:
        # The built-in setup responder may be outside an isolated channel's
        # agent roster while still answering this very setup thread.
        agent_name = resolved.agent_name
    details = None
    mcp_urls: set[str] = set()
    grants: tuple[McpOAuthGrantRow, ...] = ()
    if roster.answering is not None:
        details = await load_agent_details(
            session,
            anthropic,
            tenant_id=tenant_id,
            ma_agent_id=roster.answering.ma_agent_id,
            platform=platform,
            channel_id=channel_id,
            thread_id=thread_id,
            deployment_default=default,
            github=github,
            public_mcp_url=public_mcp_url,
            is_admin=is_admin,
            channel_label=channel_id,
        )
        agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=details.ma_agent_id)
        mcp_urls = {
            row.mcp_server_url
            for row in await agent_mcp_credentials.list_credentials(
                session, tenant_id=tenant_id, agent_id=agent_id
            )
        }
        if caller_account_id is not None:
            grants = await mcp_oauth_flows.list_completed_grants(
                session,
                tenant_id=tenant_id,
                server_urls=(server.url for server in details.mcp_servers),
            )
    routines = await list_routines_for_tenant(session, tenant_id=tenant_id) if is_admin else []
    if visible_channel_ids is not None and viewer is not None:
        visible_channel_ids = {cid for cid in visible_channel_ids if viewer.sees_place(cid)}
    _, channels = await scoped_config_read.list_propagations_for_tenant(
        session, tenant_id=tenant_id
    )
    binding = (
        await get_binding(
            session,
            tenant_id=tenant_id,
            platform=platform,
            parent_channel_id=channel_id,
            thread_id=thread_id,
        )
        if thread_id is not None
        else None
    )
    source = (
        channel_row
        if resolved.agent_name_tier == "channel"
        else tenant_row
        if resolved.agent_name_tier == "tenant"
        else None
    )
    setter_account_id = (
        binding.creator_account_id
        if resolved.agent_name_tier == "thread" and binding is not None
        else source.agent_name_set_by_account_id
        if isinstance(source, (ChannelConfigRow, TenantConfigRow))
        else None
    )
    set_by_label = None
    if setter_account_id is not None:
        external_id = (
            await get_discord_principal_for_account(session, account_id=setter_account_id)
            if platform == "discord"
            else await get_slack_principal_for_account(session, account_id=setter_account_id)
        )
        if external_id is not None:
            set_by_label = f"<@{external_id}>"
    category_labels = tuple(
        label
        for cid, label in category_channels_bot_can_view
        if viewer is None or viewer.sees_place(cid)
    )
    can_read = (
        authorize(
            policy,
            subject=Subject(),
            action=Action.READ_CHANNEL,
            agent=AgentRef.of(agent_name),
            place=Place(channel_id=channel_id),
            origin_channel_ids=frozenset({channel_id}),
        ).allowed
        if agent_name is not None
        else None
    )
    return assemble_here_card(
        channel_id=channel_id,
        agent_name=agent_name,
        tier=resolved.agent_name_tier if agent_name is not None else None,
        channel=channel_row if isinstance(channel_row, ChannelConfigRow) else None,
        tenant=tenant_row if isinstance(tenant_row, TenantConfigRow) else None,
        configuration_target_name=resolved.configuration_target_name
        if viewer is None or viewer.sees(resolved.configuration_target_name)
        else None,
        thread_set_by_account_id=binding.creator_account_id if binding is not None else None,
        thread_set_at=binding.created_at if binding is not None else None,
        set_by_label=set_by_label,
        channel_level_only=channel_level_only,
        policy=policy,
        details=details,
        visible_agent_names={
            name for name in policy.agent_channel_pins if viewer is None or viewer.sees(name)
        },
        channels=channels,
        deployment_default=default,
        show_routines=is_admin,
        mcp_token_urls=mcp_urls,
        personal_grants=grants,
        caller_account_id=caller_account_id,
        routines=routines,
        visible_channel_ids=visible_channel_ids,
        bot_can_view=bot_can_view,
        caller_can_view=caller_can_view,
        agent_can_read_here=can_read,
        category_channels_bot_can_view=category_labels,
    )
