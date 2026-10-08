"""A fixed, caller-filtered answer to who answers in this place.

The assembler only accepts names and status facts. It cannot carry credential
values into a card. The loader gathers the existing routing, detail and policy
reads; platform adapters supply live channel visibility.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import datetime

from anthropic import AsyncAnthropic
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.agent_details import AgentDetails, GitHubDeploymentFacts, load_agent_details
from daimon.core.agent_pins import agent_pin_names
from daimon.core.authz import Action, AgentRef, Place, Subject, authorize
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.permissions import (
    agent_permissions,
    channel_permissions,
    channel_rule,
    memory_writable,
)
from daimon.core.roster import load_roster
from daimon.core.rule_views import load_rule_viewer
from daimon.core.scope import (
    ChannelConfigRow,
    ChannelScopeRef,
    DeploymentDefault,
    ScopeContext,
    TenantConfigRow,
    TenantScopeRef,
)
from daimon.core.setup_conversations import get_setup_agent
from daimon.core.stores import agent_mcp_credentials, mcp_oauth_flows, scoped_config_read
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import McpOAuthGrantRow, Platform
from daimon.core.stores.identity import (
    get_discord_principal_for_account,
    get_slack_principal_for_account,
    get_teams_principal_for_account,
)
from daimon.core.stores.platform_names import get_user_names
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
    set_at: datetime | None = None
    configuration_target_name: str | None = None
    channel_rule: ChannelRule
    thread_rule: ChannelRule | None = None
    category_rule: ChannelRule | None = None
    effective_readers: str
    effective_writers: str
    agent_runs_in: tuple[str, ...] | None = None
    agent_home: str | None = None
    who_may_answer: str
    agent_can_answer_here: bool | None = None
    reads_kept_inside: bool
    memory_writable_here: bool | None = None
    publishing_needs_approval: bool | None = None
    bot_can_view: bool | None = None
    caller_can_view: bool | None = None
    bot_can_read_history: bool | None = None
    caller_can_read_history: bool | None = None
    agent_can_read_here: bool | None = None
    category_channels_bot_can_view: tuple[str, ...] = ()
    credentials: tuple[CredentialStatus, ...] = ()
    text: str


def assemble_here_card(
    *,
    channel_id: str,
    platform: Platform = "discord",
    thread_id: str | None = None,
    category_id: str | None = None,
    agent_name: str | None,
    tier: str | None,
    channel: ChannelConfigRow | None,
    tenant: TenantConfigRow | None,
    configuration_target_name: str | None,
    setup_thread: bool = False,
    set_by_label: str | None = None,
    channel_level_only: bool = False,
    thread_set_by_account_id: uuid.UUID | None = None,
    thread_set_at: datetime | None = None,
    policy: TenantAccessPolicy,
    details: AgentDetails | None,
    agent_rule_names: Collection[str | None] | None = None,
    mcp_token_urls: Collection[str] = (),
    personal_grants: Sequence[McpOAuthGrantRow] = (),
    caller_account_id: uuid.UUID | None = None,
    visible_channel_ids: Collection[str] | None = None,
    bot_can_view: bool | None = None,
    caller_can_view: bool | None = None,
    bot_can_read_history: bool | None = None,
    caller_can_read_history: bool | None = None,
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
    stored_channel_rule = channel_rule(policy, channel_id)
    # Slack keys a thread `channel:ts`; Discord and Teams by the thread's own id
    # (for Teams its `19:…;messageid=<root>` conversation).
    thread_key = (
        (f"{channel_id}:{thread_id}" if platform == "slack" else thread_id)
        if thread_id is not None
        else None
    )
    stored_thread_rule = channel_rule(policy, thread_key) if thread_key is not None else None
    stored_category_rule = policy.category_rules.get(category_id) if category_id else None
    here = channel_permissions(
        policy,
        channel_id=thread_id or channel_id,
        parent_channel_id=channel_id if thread_id is not None else None,
        category_id=category_id,
    )
    names = tuple(agent_rule_names) if agent_rule_names is not None else (agent_name,)
    permissions = agent_permissions(policy, names)
    running: set[str] | None = None
    for rule in permissions.runs_in:
        running = set(rule) if running is None else running.intersection(rule)
    runs_in: tuple[str, ...] | None = tuple(sorted(running)) if running is not None else None
    shown_runs_in = (
        tuple(cid for cid in runs_in if visible is None or cid in visible)
        if runs_in is not None
        else None
    )
    agent_ref = AgentRef.of(*names) if agent_name is not None else AgentRef.none()
    place = Place(
        channel_id=thread_id or channel_id,
        parent_channel_id=channel_id if thread_id is not None else None,
        category_id=category_id,
        setup_thread=setup_thread,
    )
    can_answer = (
        authorize(policy, subject=Subject(), action=Action.START_TURN, place=place).allowed
        and authorize(
            policy, subject=Subject(), action=Action.RUN_AGENT, agent=agent_ref, place=place
        ).allowed
        if agent_name is not None
        else None
    )
    origin_ids = {channel_id}
    if thread_id is not None:
        origin_ids.update((thread_id, f"{channel_id}:{thread_id}"))
    can_read = (
        authorize(
            policy,
            subject=Subject(),
            action=Action.READ_CHANNEL,
            agent=agent_ref,
            place=place,
            origin_channel_ids=frozenset(origin_ids),
            origin=place,
        ).allowed
        if agent_name is not None
        else None
    )
    can_write_memory = memory_writable(permissions, here) if agent_name is not None else None
    needs_approval = (
        authorize(
            policy, subject=Subject(), action=Action.PUBLISH, agent=agent_ref, origin=place
        ).reason
        == "needs_approval"
        if agent_name is not None
        else None
    )
    who_may_answer = (
        "nobody"
        if here.writers == "none"
        else "this channel's own agents"
        if here.writers == "own"
        else "the agent selected by routing, if its rule allows this place"
    )
    credentials: list[CredentialStatus] = []
    token_urls = {url.rstrip("/") for url in mcp_token_urls}
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
    routing = f"Who answers: {_short(agent_name or 'No agent')} ({tier or 'unconfigured'}"
    if configuration_target_name is not None:
        routing += f"; configuring {_short(configuration_target_name)}"
    if set_by is not None or set_at is not None:
        when = set_at.isoformat() if set_at else "unknown"
        routing += f"; set by {set_by_label or 'unknown'} at {when}"
    lines = ["**Here**", routing + ")."]
    if channel_level_only:
        lines.append(
            "Slack /here shows channel-level routing; slash commands provide no thread context."
        )
    publication = (
        "approval required"
        if needs_approval
        else "allowed without approval"
        if needs_approval is False
        else "unknown"
    )
    lines.extend(
        [
            f"Channel rule: readers {stored_channel_rule.readers}; "
            f"writers {stored_channel_rule.writers}.",
            f"Effective here: readers {here.readers}; writers {here.writers}.",
            f"Who may answer: {who_may_answer}.",
            f"Agent rule: runs_in (visible) {_runs_in_label(shown_runs_in)}; "
            f"home {permissions.home or 'none'}.",
            f"Reads kept inside: {_yes(here.readers != 'any')}; "
            f"memory writable: {_yes(can_write_memory)}.",
            f"Publishing: {publication}.",
            f"Can view here: bot {_yes(bot_can_view)}; you {_yes(caller_can_view)}.",
            f"Agent can read here: {_yes(can_read)} (Daimon rule; platform access separate).",
        ]
    )
    if bot_can_read_history is not None or caller_can_read_history is not None:
        lines.append(
            f"Can read history: bot {_yes(bot_can_read_history)}; "
            f"you {_yes(caller_can_read_history)}."
        )
    if stored_thread_rule is not None and (
        _reader_rank(stored_thread_rule.readers) > _reader_rank(stored_channel_rule.readers)
        or stored_thread_rule.writers == "none"
        and stored_channel_rule.writers != "none"
    ):
        lines.append(
            f"Thread rule: readers {stored_thread_rule.readers}; "
            f"writers {stored_thread_rule.writers}."
        )
    if (
        stored_category_rule is not None
        and stored_category_rule.writers == "none"
        and stored_channel_rule.writers != "none"
    ):
        lines.append(
            f"Category rule: readers {stored_category_rule.readers}; "
            f"writers {stored_category_rule.writers}."
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
    return HereCard(
        agent_name=agent_name,
        tier=tier,
        set_at=set_at,
        configuration_target_name=configuration_target_name,
        channel_rule=stored_channel_rule,
        thread_rule=stored_thread_rule,
        category_rule=stored_category_rule,
        effective_readers=here.readers,
        effective_writers=here.writers,
        agent_runs_in=shown_runs_in,
        agent_home=permissions.home,
        who_may_answer=who_may_answer,
        agent_can_answer_here=can_answer,
        reads_kept_inside=here.readers != "any",
        memory_writable_here=can_write_memory,
        publishing_needs_approval=needs_approval,
        bot_can_view=bot_can_view,
        caller_can_view=caller_can_view,
        bot_can_read_history=bot_can_read_history,
        caller_can_read_history=caller_can_read_history,
        agent_can_read_here=can_read,
        category_channels_bot_can_view=tuple(category_channels_bot_can_view),
        credentials=tuple(credentials),
        text="\n".join(lines)[:3900],
    )


def _yes(value: bool | None) -> str:
    return "yes" if value is True else "no" if value is False else "unknown"


def _reader_rank(value: str) -> int:
    return {"any": 0, "inside": 1, "own": 2}[value]


def _runs_in_label(channels: tuple[str, ...] | None) -> str:
    if channels is None:
        return "any channel"
    return _summary(channels) if channels else "no visible channels"


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


async def _stored_name(
    session: AsyncSession, tenant_id: uuid.UUID, platform: Platform, user_id: str
) -> str | None:
    known = await get_user_names(
        session, tenant_id=tenant_id, platform=platform, user_ids=[user_id]
    )
    return known[user_id].label if user_id in known else None


async def load_here_card(
    session: AsyncSession,
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    platform: Platform,
    channel_id: str,
    thread_id: str | None,
    category_id: str | None = None,
    default: DeploymentDefault,
    github: GitHubDeploymentFacts,
    public_mcp_url: str | None,
    is_admin: bool,
    caller_account_id: uuid.UUID | None,
    resolve_setter_display: Callable[[str], Awaitable[str | None]] | None = None,
    channel_level_only: bool = False,
    visible_channel_ids: Collection[str] | None = None,
    bot_can_view: bool | None = None,
    caller_can_view: bool | None = None,
    bot_can_read_history: bool | None = None,
    caller_can_read_history: bool | None = None,
    category_channels_bot_can_view: Sequence[tuple[str, str]] = (),
) -> HereCard:
    """Load the current routing and credential names for one caller's place."""
    policy = await load_access_policy(session, tenant_id=tenant_id)
    viewer = await load_rule_viewer(
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
        # The built-in setup responder may be outside a channel's own-agent
        # agent roster while still answering this very setup thread.
        agent_name = resolved.agent_name
    details = None
    rule_names: tuple[str | None, ...] | None = None
    mcp_urls: set[str] = set()
    grants: tuple[McpOAuthGrantRow, ...] = ()
    if roster.answering is not None:
        ma_agent = await get_setup_agent(
            anthropic, tenant_id=tenant_id, ma_agent_id=roster.answering.ma_agent_id
        )
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
            preloaded_agent=ma_agent,
        )
        # Keep aliases local to this card; get_agent's response stays unchanged.
        rule_names = agent_pin_names(ma_agent.name, ma_agent.metadata)
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
    if visible_channel_ids is not None and viewer is not None:
        visible_channel_ids = {cid for cid in visible_channel_ids if viewer.sees_place(cid)}
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
            else await get_teams_principal_for_account(session, account_id=setter_account_id)
            if platform == "teams"
            else await get_slack_principal_for_account(session, account_id=setter_account_id)
        )
        if external_id is not None:
            if resolve_setter_display is None and platform != "teams":
                set_by_label = f"<@{external_id}>"
            else:
                # Teams renders no `<@id>` mention, and its id is an Entra object id:
                # without a live lookup the setter gets the name their last message stored.
                display = (
                    await resolve_setter_display(external_id)
                    if resolve_setter_display is not None
                    else await _stored_name(session, tenant_id, platform, external_id)
                )
                if display:
                    set_by_label = _short(
                        display.replace("@", "＠")
                        .replace("<", "‹")
                        .replace(">", "›")
                        .replace("\n", " ")
                        .replace("\r", " ")
                    )
    category_labels = tuple(
        label
        for cid, label in category_channels_bot_can_view
        if viewer is None or viewer.sees_place(cid)
    )
    return assemble_here_card(
        channel_id=channel_id,
        platform=platform,
        thread_id=thread_id,
        category_id=category_id,
        agent_name=agent_name,
        tier=resolved.agent_name_tier if agent_name is not None else None,
        channel=channel_row if isinstance(channel_row, ChannelConfigRow) else None,
        tenant=tenant_row if isinstance(tenant_row, TenantConfigRow) else None,
        configuration_target_name=resolved.configuration_target_name
        if viewer is None or viewer.sees(resolved.configuration_target_name)
        else None,
        setup_thread=resolved.thread_binding_kind == "setup",
        thread_set_by_account_id=binding.creator_account_id if binding is not None else None,
        thread_set_at=binding.created_at if binding is not None else None,
        set_by_label=set_by_label,
        channel_level_only=channel_level_only,
        policy=policy,
        details=details,
        agent_rule_names=rule_names,
        mcp_token_urls=mcp_urls,
        personal_grants=grants,
        caller_account_id=caller_account_id,
        visible_channel_ids=visible_channel_ids,
        bot_can_view=bot_can_view,
        caller_can_view=caller_can_view,
        bot_can_read_history=bot_can_read_history,
        caller_can_read_history=caller_can_read_history,
        category_channels_bot_can_view=category_labels,
    )
