"""A fixed, caller-filtered answer to who answers in this place.

The assembler only accepts names and status facts. It cannot carry credential
values into a card. The loader gathers the existing routing, detail and policy
reads; platform adapters supply live channel visibility.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import datetime
from typing import Literal

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
)
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
    set_by_label: str | None = None
    configuration_target_name: str | None = None
    channel_level_only: bool = False
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


class HereCardPresentation(BaseModel):
    """The small set of strings every /here surface renders."""

    model_config = ConfigDict(frozen=True)
    state: Literal["no_view", "no_replies", "no_agent", "blocked", "channel", "thread"]
    title: str
    colour: str
    subline: str | None = None
    reading: str | None = None
    publishing: str | None = None
    extras: tuple[str, ...] = ()


def render_here_card(card: HereCard) -> HereCardPresentation:
    """Choose the approved card copy from existing facts, in priority order."""
    name = " ".join((card.agent_name or "").split())[:100]
    if card.bot_can_view is False:
        state, title, colour, subline = (
            "no_view",
            "No channel access",
            "#ED4245",
            "Ask an admin to check Daimon's access.",
        )
    elif card.effective_writers == "none":
        state, title, colour, subline = "no_replies", "Replies disabled here", "#ED4245", None
    elif card.agent_name is None:
        state, title, colour, subline = (
            "no_agent",
            "No agent selected",
            "#95A5A6",
            "Ask an admin: /agent-setup",
        )
    elif card.agent_can_answer_here is False:
        state, title, colour, subline = ("blocked", f"{name} can't answer here", "#F0B429", None)
    elif card.tier == "thread":
        state, title, colour, subline = (
            "thread",
            f"{name} answers in this thread",
            "#2ECC71",
            None,
        )
    else:
        state, title, colour, subline = "channel", f"{name} answers here", "#2ECC71", None
    reading = {
        "any": "Any conversation",
        "inside": "Conversations here only",
        "own": "Own agents only",
    }.get(card.effective_readers)
    publishing = (
        "Approval required"
        if card.publishing_needs_approval is True
        else "No approval"
        if card.publishing_needs_approval is False
        else None
    )
    extras = (
        *(("Only own agents answer here",) if card.effective_writers == "own" else ()),
        *(("No access to earlier messages",) if card.bot_can_read_history is False else ()),
    )
    return HereCardPresentation(
        state=state,
        title=title,
        colour=colour,
        subline=subline,
        reading=reading,
        publishing=publishing,
        extras=extras,
    )


def render_here_card_text(card: HereCard) -> str:
    """Plain MCP text; the structured HereCard keeps all existing facts."""
    rendered = render_here_card(card)
    lines = [rendered.title]
    if rendered.subline is not None:
        lines.append(rendered.subline)
    fields: list[str] = []
    if rendered.reading is not None:
        fields.append(f"Reading: {rendered.reading}")
    if rendered.publishing is not None:
        fields.append(f"Publishing: {rendered.publishing}")
    if fields:
        lines.append(" · ".join(fields))
    lines.extend(rendered.extras)
    return "\n".join(lines)


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
    card = HereCard(
        agent_name=agent_name,
        tier=tier,
        set_at=set_at,
        set_by_label=(set_by_label or "unknown") if set_by is not None else None,
        configuration_target_name=configuration_target_name,
        channel_level_only=channel_level_only,
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
        text="",
    )
    return card.model_copy(update={"text": render_here_card_text(card)})


def _short(value: str, limit: int = 120) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


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
            else await get_slack_principal_for_account(session, account_id=setter_account_id)
        )
        if external_id is not None:
            if resolve_setter_display is None:
                set_by_label = f"<@{external_id}>"
            else:
                display = await resolve_setter_display(external_id)
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
