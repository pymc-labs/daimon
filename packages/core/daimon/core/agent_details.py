"""Everything a reader needs to know about one agent, assembled once.

A Details view asks a lot of small questions — what model is it on, who made
it, where does it answer, is its repo actually reachable, which keys exist —
and each one has a right answer that lives somewhere else. This module asks
them all in one place so every surface gets the same answers, and so the read
costs one MA retrieve rather than one per question.

`build_agent_details` is pure; `load_agent_details` is the shell around it.
Neither resolves a credential: `RepoAccess` is derived from what the binding
recorded, and `KeyEntry` carries names and attribution but never a value. The
module deliberately knows nothing about the vault, PAT lookup, or settings.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.constants import MODEL_DISPLAY_NAMES
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_MANAGED,
    account_id_from_metadata,
)
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.defaults.skills import resolve_custom_skill_titles
from daimon.core.github_repo_auth import RepoAccess, derive_repo_access
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.routing_facts import build_unrouted_note
from daimon.core.scope import (
    AnsweringPlace,
    ChannelConfigRow,
    DeploymentDefault,
    ResolvedConfig,
    ScopeContext,
    TenantConfigRow,
    answering_places,
)
from daimon.core.setup_conversations import get_setup_agent
from daimon.core.stores import agent_files, agent_repo_binding, scoped_config_read
from daimon.core.stores.domain import AgentFileRow, AgentRepoBindingRow, Platform
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession


class GitHubDeploymentFacts(BaseModel):
    """What the deployment has configured for GitHub, as two plain facts.

    The caller reads these off its own settings; this module never touches
    `Settings` or a credential, so a repo's state can be described without
    anything here being able to resolve a token.
    """

    model_config = ConfigDict(frozen=True)

    has_fallback_pat: bool
    app_configured: bool


class KeyEntry(BaseModel):
    """One key the agent has, by name and attribution only.

    There is no field for the value, and there must never be one: this model
    is rendered straight into chat surfaces.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    created_by_account_id: uuid.UUID | None = None
    last_set_by_account_id: uuid.UUID | None = None
    updated_at: datetime


class RepoBinding(BaseModel):
    """The agent's working repo and whether a clone of it would actually work."""

    model_config = ConfigDict(frozen=True)

    repo_url: str
    default_branch: str
    access: RepoAccess


class SkillEntry(BaseModel):
    """One attached skill; `title` is None when no display title resolved."""

    model_config = ConfigDict(frozen=True)

    type: str
    skill_id: str
    title: str | None = None
    version: str


class McpServerEntry(BaseModel):
    """One external MCP server attached to the agent."""

    model_config = ConfigDict(frozen=True)

    name: str
    url: str


class AgentDetails(BaseModel):
    """One agent's whole readable state.

    `answers_in` is empty exactly when nobody can reach the agent by
    mentioning the bot; `unrouted_note` then says how to fix that in the
    reader's own voice. `answers_here` is the narrower question of whether it
    is the agent answering in the place the reader is standing.
    """

    model_config = ConfigDict(frozen=True)

    ma_agent_id: str
    name: str
    purpose: str | None = None
    model_id: str
    model_display_name: str
    daimon_managed: bool
    created_by_account_id: uuid.UUID | None = None
    created_by_is_workspace: bool
    created_at: datetime
    answers_in: tuple[AnsweringPlace, ...] = ()
    answers_here: bool
    repo: RepoBinding | None = None
    skills: tuple[SkillEntry, ...] = ()
    skills_listing_truncated: bool = False
    mcp_servers: tuple[McpServerEntry, ...] = ()
    keys: tuple[KeyEntry, ...] = ()
    applies_note: str
    unrouted_note: str | None = None


def build_agent_details(
    *,
    agent: BetaManagedAgentsAgent,
    tenant_id: uuid.UUID,
    tenant: TenantConfigRow | None,
    channels: Sequence[ChannelConfigRow],
    default: DeploymentDefault,
    resolved_here: ResolvedConfig | None,
    binding: AgentRepoBindingRow | None,
    files: Sequence[AgentFileRow],
    skill_titles: Mapping[str, str],
    skills_truncated: bool,
    github: GitHubDeploymentFacts,
    public_mcp_url: str | None,
    is_admin: bool,
    channel_label: str | None,
) -> AgentDetails:
    """Fold one live agent plus this install's rows into `AgentDetails`.

    The deployment's own MCP server is filtered out of `mcp_servers`: every
    agent carries it, it is how the product works rather than something the
    reader connected, and listing it as an external connection invites
    removing it.

    Pure — no I/O, no clock.
    """
    places = answering_places(agent.name, tenant=tenant, channels=channels, default=default)
    created_by_account_id = account_id_from_metadata(agent.metadata.get(MA_METADATA_KEY_ACCOUNT))
    repo = (
        RepoBinding(
            repo_url=binding.repo_url,
            default_branch=binding.default_branch,
            access=derive_repo_access(
                binding,
                has_fallback_pat=github.has_fallback_pat,
                app_configured=github.app_configured,
            ),
        )
        if binding is not None
        else None
    )
    return AgentDetails(
        ma_agent_id=agent.id,
        name=agent.name,
        purpose=agent.description,
        model_id=agent.model.id,
        model_display_name=MODEL_DISPLAY_NAMES.get(agent.model.id, agent.model.id),
        daimon_managed=agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
        created_by_account_id=created_by_account_id,
        created_by_is_workspace=created_by_account_id == derive_guild_account_uuid(tenant_id),
        created_at=agent.created_at,
        answers_in=places,
        answers_here=resolved_here is not None and resolved_here.agent_name == agent.name,
        repo=repo,
        skills=tuple(
            SkillEntry(
                type=skill.type,
                skill_id=skill.skill_id,
                title=skill_titles.get(skill.skill_id),
                version=skill.version,
            )
            for skill in agent.skills
        ),
        skills_listing_truncated=skills_truncated,
        mcp_servers=tuple(
            McpServerEntry(name=server.name, url=server.url)
            for server in agent.mcp_servers
            if public_mcp_url is None or server.url != public_mcp_url
        ),
        keys=tuple(
            KeyEntry(
                name=file.key,
                created_by_account_id=file.created_by_account_id,
                last_set_by_account_id=file.last_set_by_account_id,
                updated_at=file.updated_at,
            )
            for file in files
        ),
        applies_note=f"Changes to {agent.name} apply from the next message to it.",
        unrouted_note=(
            build_unrouted_note(
                agent_name=agent.name, channel_label=channel_label, is_admin=is_admin
            )
            if not places
            else None
        ),
    )


async def load_agent_details(
    session: AsyncSession,
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    ma_agent_id: str,
    platform: Platform,
    channel_id: str | None,
    thread_id: str | None,
    deployment_default: DeploymentDefault,
    github: GitHubDeploymentFacts,
    public_mcp_url: str | None,
    is_admin: bool,
    channel_label: str | None,
) -> AgentDetails:
    """Read everything `build_agent_details` needs, then fold it.

    Exactly one MA retrieve, through `get_setup_agent`, so an agent from
    another install or an archived one is refused before any of this is read.
    The skills listing is a second call, which `resolve_custom_skill_titles`
    skips entirely unless a custom skill is attached — built-in skills need no
    title lookup.

    `channel_id` is what makes `answers_here` answerable; passing the thread
    with it lets a bound thread report its own responder rather than the
    channel's.
    """
    agent = await get_setup_agent(anthropic, tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    tenant_row, channel_rows = await scoped_config_read.list_propagations_for_tenant(
        session, tenant_id=tenant_id
    )
    resolved_here: ResolvedConfig | None = None
    if channel_id is not None:
        resolved_here = await scoped_config_read.resolve(
            session,
            context=ScopeContext(
                tenant_id=tenant_id,
                channel_id=channel_id,
                platform=platform,
                thread_id=thread_id,
            ),
            default=deployment_default,
        )
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    binding = await agent_repo_binding.get_binding(
        session, tenant_id=tenant_id, agent_id=agent_uuid
    )
    files = await agent_files.list_agent_files(session, tenant_id=tenant_id, agent_id=agent_uuid)
    skill_titles, skills_truncated = await resolve_custom_skill_titles(
        anthropic, agents=[agent], tenant_id=tenant_id
    )
    return build_agent_details(
        agent=agent,
        tenant_id=tenant_id,
        tenant=tenant_row,
        channels=channel_rows,
        default=deployment_default,
        resolved_here=resolved_here,
        binding=binding,
        files=files,
        skill_titles=skill_titles,
        skills_truncated=skills_truncated,
        github=github,
        public_mcp_url=public_mcp_url,
        is_admin=is_admin,
        channel_label=channel_label,
    )
