"""Agent tools: list / get / create / update / fork / archive.

``register_agent_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures for
this group; each closure delegates to a module-private ``_*_impl`` function
that can be unit-tested without a FastMCP Context.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final, cast

import anthropic
import httpx
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSkillParams
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_model_param import BetaManagedAgentsModelParam
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
    find_agents_by_daimon_tag,
    list_agents_by_tenant,
    list_skills_lenient,
)
from daimon.core.defaults.mcp_merge import (
    get_reserved_mcp_rejection,
    merge_default_mcp_server,
    merge_default_mcp_toolset,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    build_metadata,
    strip_tenant_prefix,
)
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.defaults.skills import resolve_skill_names
from daimon.core.defaults.spec_merge import merge_mcp_servers_with_ma, merge_skills_with_ma
from daimon.core.errors import DefaultsError
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.skill_sync import SyncRepoFailure, sync_agent_skills, sync_report_failures
from daimon.core.specs import (
    AgentSpec,
    SkillRepo,
    merge_default_agent_toolset,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ValidationError


class AgentMcpServerInfo(BaseModel):
    name: str
    url: str


class AgentSkillInfo(BaseModel):
    # Plain str, NOT Literal["anthropic", "custom"] — upstream-controlled
    # value set; MA may ship a skill type the pinned SDK does not model
    # (#214 class).
    type: str
    skill_id: str
    name: str | None
    version: str


class AgentInfo(BaseModel):
    name: str
    id: str
    description: str | None
    model: str
    created_at: datetime.datetime
    mcp_servers: list[AgentMcpServerInfo]
    skills: list[AgentSkillInfo]
    sync_warnings: list[SyncRepoFailure] | None = None

    @classmethod
    def from_ma(
        cls,
        agent: BetaManagedAgentsAgent,
        *,
        sync_warnings: list[SyncRepoFailure] | None = None,
        skill_titles: Mapping[str, str] | None = None,
    ) -> AgentInfo:
        titles = skill_titles or {}
        return cls(
            name=agent.name,
            id=agent.id,
            description=agent.description,
            model=agent.model.id,
            created_at=agent.created_at,
            mcp_servers=[AgentMcpServerInfo(name=s.name, url=s.url) for s in agent.mcp_servers],
            skills=[
                AgentSkillInfo(
                    type=sk.type,
                    skill_id=sk.skill_id,
                    name=titles.get(sk.skill_id),
                    version=sk.version,
                )
                for sk in agent.skills
            ],
            sync_warnings=sync_warnings,
        )


async def _resolve_custom_skill_titles(
    client: AsyncAnthropic,
    agents: Sequence[BetaManagedAgentsAgent],
    *,
    tenant_id: uuid.UUID,
) -> dict[str, str]:
    """MA skill id → bare display name for own-namespace custom skills referenced by ``agents``.

    Agent responses carry opaque custom skill ids (``skill_...``); the
    human-readable display titles only live on the skills list. One LIST call,
    skipped entirely when no agent references a custom skill.

    Only skills whose display_title strips to a non-None bare name for the caller's
    tenant_id are included — foreign-tenant and legacy titles are excluded from the
    map, so downstream display falls back to the skill_id (the existing map-miss path).
    """
    if not any(sk.type == "custom" for agent in agents for sk in agent.skills):
        return {}
    rows, _truncated = await list_skills_lenient(client)
    result: dict[str, str] = {}
    for sk in rows:
        if sk.display_title is None:
            continue
        bare = strip_tenant_prefix(tenant_id=tenant_id, display_title=sk.display_title)
        if bare is not None:
            result[sk.id] = bare
    return result


async def _build_agent_info(
    client: AsyncAnthropic,
    agent: BetaManagedAgentsAgent,
    *,
    tenant_id: uuid.UUID,
    sync_warnings: list[SyncRepoFailure] | None = None,
) -> AgentInfo:
    """Map an MA agent to ``AgentInfo`` with custom skill names resolved."""
    skill_titles = await _resolve_custom_skill_titles(client, [agent], tenant_id=tenant_id)
    return AgentInfo.from_ma(agent, sync_warnings=sync_warnings, skill_titles=skill_titles)


_CREATE_FIELDS: Final = frozenset(
    {
        "name",
        "model",
        "description",
        "system",
        "tools",
        "mcp_servers",
        "metadata",
        # "skills" excluded — create_agent rejects non-empty skills until the
        # skills tool group ships; attach skills via update_agent instead.
    }
)

# Fork copies the source's attached skills (panel _FORK_COPY_FIELDS parity).
# The create_agent skills restriction (above) applies only to create_agent's
# flat params, not to cloning an existing agent's state.
_FORK_COPY_FIELDS: Final = _CREATE_FIELDS | {"skills"}


_DEFAULT_MCP_TOOLSET_CONFIG: Final[dict[str, Any]] = {
    "permission_policy": {"type": "always_allow"},
}


def _ma_tool_to_param(tool: Any) -> Tool:
    """Dump an MA response Tool to a Params dict suitable for the SDK update body."""
    return cast(Tool, tool.model_dump(mode="json", exclude_none=True))


def _union_tools(spec_tools: list[Tool], ma_agent: BetaManagedAgentsAgent) -> list[Tool]:
    """Union caller's tools with MA's existing tools.

    Caller wins on collision; MA-only entries are appended in MA order. Keying:

    * `mcp_toolset`     — by `mcp_server_name`
    * `agent_toolset_20260401` — singleton (MA allows only one)
    * `custom`         — by `name`

    Caller-only fix for issue #56 bug 2: the chat `update_agent` is an
    additions surface (panel handles removals), so a per-field replace would
    drop everything the user didn't explicitly resend. Mirror the panel,
    which goes through `reconcile_agent`'s merge helpers.
    """
    spec_mcp_names: set[str] = set()
    spec_custom_names: set[str] = set()
    spec_has_agent_toolset = False
    for tool in spec_tools:
        ttype = tool.get("type")
        if ttype == "mcp_toolset":
            name = tool.get("mcp_server_name")
            if isinstance(name, str):
                spec_mcp_names.add(name)
        elif ttype == "agent_toolset_20260401":
            spec_has_agent_toolset = True
        elif ttype == "custom":
            name = tool.get("name")
            if isinstance(name, str):
                spec_custom_names.add(name)

    extras: list[Tool] = []
    for entry in ma_agent.tools:
        if entry.type == "mcp_toolset":
            if entry.mcp_server_name in spec_mcp_names:
                continue
        elif entry.type == "agent_toolset_20260401":
            if spec_has_agent_toolset:
                continue
        elif entry.type == "custom" and entry.name in spec_custom_names:
            continue
        extras.append(_ma_tool_to_param(entry))
    return list(spec_tools) + extras


def _reject_system_agent(agent: BetaManagedAgentsAgent) -> None:
    """Reject system agents (no daimon_account stamp) from chat mutating tools.

    Authorization boundary: _require_admin (already called first on every
    mutating impl) + tenant-scoped lookup. Any stamped agent in the tenant is
    admin-mutable. Unstamped agents are the read-only seeded/system class —
    they lack a daimon_account key — and must be forked before chat tools can
    edit them.
    """
    owner = agent.metadata.get(MA_METADATA_KEY_ACCOUNT)
    if owner is None:
        raise ToolError(
            f"agent '{agent.name}' is a system agent; chat tools cannot modify it. "
            "Use /agent-setup to fork it first, then edit the fork."
        )


async def _reject_guild_name_collision(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
) -> None:
    """Raise ToolError if any non-archived agent with this name already exists in the tenant.

    Tenant-scoped name uniqueness matches the resolver's (daimon_tenant, daimon_name)
    identity model exactly (ma_index keys on tenant+name only); legacy personal-stamped
    agents now also block. Any non-empty match raises regardless of owner.
    """
    matches = await find_agents_by_daimon_tag(runtime.client, tenant_id=auth.tenant_id, name=name)
    if matches:
        raise ToolError(f"agent '{name}' already exists in this server — pick another name")


async def _list_agents_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    page: str | None,
) -> list[AgentInfo]:
    del page
    rows = await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    skill_titles = await _resolve_custom_skill_titles(
        runtime.client, rows, tenant_id=auth.tenant_id
    )
    return [AgentInfo.from_ma(a, skill_titles=skill_titles) for a in rows]


async def _get_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
) -> AgentInfo:
    agent = await find_agent_by_daimon_tag(runtime.client, tenant_id=auth.tenant_id, name=name)
    if agent is None:
        raise ToolError(f"agent '{name}' not found")
    return await _build_agent_info(runtime.client, agent, tenant_id=auth.tenant_id)


def _build_create_spec(
    *,
    name: str,
    model: BetaManagedAgentsModelParam,
    description: str | None,
    system: str | None,
    tools: list[Tool] | None,
    mcp_servers: list[BetaManagedAgentsURLMCPServerParams] | None,
    skill_repos: list[SkillRepo] | None,
) -> AgentSpec:
    """Assemble an ``AgentSpec`` from ``create_agent``'s flat parameters.

    ``create_agent`` takes the same flat parameters as ``update_agent`` rather
    than a single nested ``spec`` object: the two tools disagreeing on shape was
    the top cause of failed chat agent-creation (callers passed ``name``/``model``
    at the top level and hit a ``spec``-missing validation error). A pydantic
    ``ValidationError`` here (e.g. ``mcp_servers`` without a matching
    ``mcp_toolset``) is reshaped into a readable ``ToolError`` rather than leaking
    raw validator output.
    """
    try:
        return AgentSpec(
            name=name,
            model=model,
            description=description,
            system=system,
            tools=tools,
            mcp_servers=mcp_servers,
            skill_repos=skill_repos or [],
        )
    except ValidationError as exc:
        raise ToolError(f"create_agent: invalid agent configuration — {exc}") from exc


async def _create_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    spec: AgentSpec,
) -> AgentInfo:
    _require_admin(auth)
    if spec.skills:
        raise ToolError(
            "create_agent: skills must be empty. To add skills, either sync a "
            "repo via skill_repos or use the skills_* tools after the agent "
            "is created."
        )
    await _reject_guild_name_collision(runtime, auth, spec.name)
    public_url = (
        str(runtime.settings.mcp.public_url)
        if runtime.settings.mcp.public_url is not None
        else None
    )
    outcome = await reconcile_agent(
        runtime.client,
        spec,
        tenant_id=auth.tenant_id,
        dry_run=False,
        account_id=derive_guild_account_uuid(auth.tenant_id),
        public_url=public_url,
        # New agents created from chat are user-owned, NOT seeded resources —
        # managed=True would stamp daimon_managed=true and make them
        # sweep-eligible, so the next defaults apply (every boot/deploy)
        # archives them because they aren't in the seeded spec list.
        managed=False,
    )
    if outcome.anthropic_id is None:
        raise ToolError("create_agent: reconcile returned no agent id — report this as a bug")
    ma_agent = await runtime.client.beta.agents.retrieve(outcome.anthropic_id)
    # agents.create succeeded — always return AgentInfo even if sync fails.
    # if agents.create itself raises, let it propagate as ToolError.
    warnings: list[SyncRepoFailure] | None = None
    if spec.skill_repos:
        fernet = runtime.fernet
        if fernet is None:
            # No crypto keys configured — cannot decrypt PAT; surface as warnings.
            warnings = [
                SyncRepoFailure(
                    repo_url=r.url,
                    reason="no crypto keys configured",
                    phase="fetch",
                )
                for r in spec.skill_repos
            ]
        else:
            async with httpx.AsyncClient() as http_client:
                report = await sync_agent_skills(
                    principal_id=auth.account_id,  # NOT auth.principal_id (no such field)
                    tenant_id=auth.tenant_id,
                    agent_name=spec.name,
                    repos=spec.skill_repos,
                    sessionmaker=runtime.session_factory,  # McpRuntime field is session_factory
                    fernet=fernet,
                    http_client=http_client,
                    anthropic_client=runtime.client,  # McpRuntime field is client
                )
            warnings = sync_report_failures(report) or None
    return await _build_agent_info(
        runtime.client, ma_agent, tenant_id=auth.tenant_id, sync_warnings=warnings
    )


async def _update_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
    *,
    model: BetaManagedAgentsModelParam | None,
    description: str | None,
    system: str | None,
    tools: list[Tool] | None,
    mcp_servers: list[BetaManagedAgentsURLMCPServerParams] | None,
    skills: list[str | BetaManagedAgentsSkillParams] | None,
) -> AgentInfo:
    _require_admin(auth)
    scalars: dict[str, Any] = {"model": model, "description": description, "system": system}
    list_fields = (tools, mcp_servers, skills)
    if all(v is None for v in scalars.values()) and all(v is None for v in list_fields):
        raise ToolError("update_agent: at least one field is required")
    agent = await find_agent_by_daimon_tag(runtime.client, tenant_id=auth.tenant_id, name=name)
    if agent is None:
        raise ToolError(f"agent '{name}' not found")
    _reject_system_agent(agent)

    # Resolve skill names outside the closure — name resolution does not depend on
    # the agent's current state and must not be repeated on each retry attempt.
    resolved_skills: list[BetaManagedAgentsSkillParams] | None = None
    if skills is not None:
        try:
            resolved_skills = await resolve_skill_names(
                runtime.client, skills, tenant_id=auth.tenant_id
            )
        except DefaultsError as exc:
            raise ToolError(str(exc)) from exc

    # Build scalar patch outside the closure (scalars are caller-supplied, not
    # derived from the agent's current state).
    scalar_patch: dict[str, Any] = {k: v for k, v in scalars.items() if v is not None}
    if "system" in scalar_patch:
        scalar_patch["system"] = apply_credential_guidance(scalar_patch["system"])

    # #144-2: version-retry closure. All agent-derived unions (skills, mcp_servers,
    # tools) are recomputed from `fresh` on every attempt so a retry after a stale-
    # version conflict picks up any concurrent mutations rather than re-applying a
    # stale merge. MA treats list fields as per-field replaces; chat tools are an
    # additions surface (the panel handles removals), so union caller's values with
    # MA's current state — bug 2 of issue #56.
    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        patch: dict[str, Any] = dict(scalar_patch)
        if resolved_skills is not None:
            patch["skills"] = merge_skills_with_ma(resolved_skills, fresh)
        if mcp_servers is not None:
            patch["mcp_servers"] = merge_mcp_servers_with_ma(mcp_servers, fresh)
        if tools is not None:
            patch["tools"] = _union_tools(tools, fresh)
        # #141: attaching skills to an agent that lacks agent_toolset_20260401 produces a
        # skills-unusable hole — MA rejects session creation ("skills require the read tool").
        # If this update includes skills, ensure the effective tools list carries the base toolset.
        if "skills" in patch:
            effective_tools: list[Tool] = patch.get("tools") or [
                _ma_tool_to_param(t) for t in fresh.tools
            ]
            has_base_toolset = any(
                entry.get("type") == "agent_toolset_20260401" for entry in effective_tools
            )
            if not has_base_toolset:
                patch["tools"] = merge_default_agent_toolset(effective_tools)
        return await runtime.client.beta.agents.update(fresh.id, version=fresh.version, **patch)

    try:
        updated = await update_agent_with_version_retry(runtime.client, agent.id, _apply)
    except anthropic.ConflictError as exc:
        # Residual conflict after the one retry — surface as a clean ToolError.
        raise ToolError("the agent was modified concurrently — please retry the operation") from exc
    except anthropic.BadRequestError as exc:
        # MA caps skills-per-agent org-wide. When the merged skill set blows
        # the cap, MA 400s and (before this) the raw pydantic/SDK error leaked
        # to chat, prompting the model to silently drop skills. Surface a clear,
        # actionable message instead — and leave the agent untouched.
        if resolved_skills is not None and "exceeds maximum" in str(exc).lower():
            raise ToolError(
                "Cannot attach skills: the merged skill set exceeds this organization's "
                "per-agent skill limit. No skills were changed. Attach fewer skills, or "
                "remove existing ones via the /agent-setup panel before adding more. "
                f"(Managed Agents reported: {exc})"
            ) from exc
        raise
    return await _build_agent_info(runtime.client, updated, tenant_id=auth.tenant_id)


async def _attach_mcp_server_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    server_name: str,
    url: str,
) -> AgentInfo:
    _require_admin(auth)
    # #142: guard the reserved daimon-mcp entry before even looking at the agent.
    # Also reject any URL that points at the deployment's own public_url under a
    # different name — that would make the next reconcile append a second daimon-mcp.
    public_url = (
        str(runtime.settings.mcp.public_url)
        if runtime.settings.mcp.public_url is not None
        else None
    )
    rejection = get_reserved_mcp_rejection(server_name=server_name, url=url, public_url=public_url)
    if rejection is not None:
        raise ToolError(rejection)
    agent = await find_agent_by_daimon_tag(
        runtime.client, tenant_id=auth.tenant_id, name=agent_name
    )
    if agent is None:
        raise ToolError(f"agent '{agent_name}' not found")
    _reject_system_agent(agent)

    existing = list(agent.mcp_servers or [])
    # No-op check on the initially-found agent (acceptable: a concurrent change
    # between this check and the update is exactly what the version-retry covers).
    for s in existing:
        if s.name == server_name and s.url == url:
            return await _build_agent_info(runtime.client, agent, tenant_id=auth.tenant_id)

    # #144-2: version-retry closure. new_mcp_servers and new_tools are recomputed
    # from `fresh` on each retry attempt. The reserved-server guard above is not
    # repeated here — it depends only on caller inputs.
    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        # Replace any entry with the same server_name (last-write-wins); keep the rest.
        fresh_existing = list(fresh.mcp_servers or [])
        new_mcp_servers: list[BetaManagedAgentsURLMCPServerParams] = [
            {"name": s.name, "type": "url", "url": s.url}
            for s in fresh_existing
            if s.name != server_name
        ]
        new_mcp_servers.append({"name": server_name, "type": "url", "url": url})

        # MA rejects an agent whose mcp_servers names are not each referenced by
        # a matching mcp_toolset entry in tools. Mirror the panel's
        # apply_mcp_modal (state.py): append the missing toolset entry atomically
        # — bug 3 of issue #56. Preserve every existing tool (caller win on
        # mcp_toolset same-name collision is implicit since we keep the existing
        # entry below).
        new_tools: list[Tool] = [_ma_tool_to_param(t) for t in fresh.tools]
        if not any(
            t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == server_name
            for t in new_tools
        ):
            new_tools.append(
                cast(
                    Tool,
                    {
                        "type": "mcp_toolset",
                        "mcp_server_name": server_name,
                        "default_config": _DEFAULT_MCP_TOOLSET_CONFIG,
                    },
                )
            )
        return await runtime.client.beta.agents.update(
            fresh.id,
            version=fresh.version,
            mcp_servers=new_mcp_servers,
            tools=new_tools,
        )

    try:
        updated = await update_agent_with_version_retry(runtime.client, agent.id, _apply)
    except anthropic.ConflictError as exc:
        # Residual conflict after the one retry — surface as a clean ToolError.
        raise ToolError("the agent was modified concurrently — please retry the operation") from exc
    return await _build_agent_info(runtime.client, updated, tenant_id=auth.tenant_id)


async def _fork_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    source_name: str,
    new_name: str,
) -> AgentInfo:
    _require_admin(auth)
    await _reject_guild_name_collision(runtime, auth, new_name)
    source = await find_agent_by_daimon_tag(
        runtime.client, tenant_id=auth.tenant_id, name=source_name
    )
    if source is None:
        raise ToolError(f"agent '{source_name}' not found")
    source_ma = await runtime.client.beta.agents.retrieve(source.id)
    params = source_ma.model_dump(mode="json")
    fork_params = {k: params[k] for k in _FORK_COPY_FIELDS if k in params}
    fork_params["name"] = new_name
    fork_params["metadata"] = build_metadata(
        tenant_id=auth.tenant_id,
        name=new_name,
        account_id=derive_guild_account_uuid(auth.tenant_id),
    )
    public_url = (
        str(runtime.settings.mcp.public_url)
        if runtime.settings.mcp.public_url is not None
        else None
    )
    fork_params["mcp_servers"] = merge_default_mcp_server(
        cast("list[BetaManagedAgentsURLMCPServerParams] | None", fork_params.get("mcp_servers")),
        public_url,
    )
    fork_params["tools"] = merge_default_mcp_toolset(
        cast("list[Tool] | None", fork_params.get("tools")),
        public_url,
    )
    # Fork copies raw MA state and bypasses dump_agent_spec — guarantee the
    # base toolset here so forking a legacy pre-guarantee agent doesn't
    # propagate the skills-unusable hole.
    fork_params["tools"] = merge_default_agent_toolset(
        cast("list[Tool] | None", fork_params.get("tools"))
    )
    new_ma = await runtime.client.beta.agents.create(**fork_params)
    return await _build_agent_info(runtime.client, new_ma, tenant_id=auth.tenant_id)


async def _archive_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
) -> None:
    _require_admin(auth)
    agent = await find_agent_by_daimon_tag(runtime.client, tenant_id=auth.tenant_id, name=name)
    if agent is None:
        raise ToolError(f"agent '{name}' not found")
    _reject_system_agent(agent)
    await runtime.client.beta.agents.archive(agent.id)


def register_agent_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def list_agents(ctx: Context, page: str | None = None) -> list[AgentInfo]:  # pyright: ignore[reportUnusedFunction]
        """List agents in the tenant pool, including each agent's attached
        ``mcp_servers`` and ``skills``. ``page`` is reserved for future pagination."""
        return await _list_agents_impl(runtime, await _auth(ctx), page)

    @mcp.tool
    async def get_agent(ctx: Context, name: str) -> AgentInfo:  # pyright: ignore[reportUnusedFunction]
        """Look up an agent by name.

        Returns the agent's attached ``mcp_servers`` (name + url) and
        ``skills``. Custom skill entries include ``name`` — the resolved
        display title (``null`` if the underlying skill was deleted);
        anthropic skill entries have a readable ``skill_id`` and no name.
        """
        return await _get_agent_impl(runtime, await _auth(ctx), name)

    @mcp.tool(tags={"admin"})
    async def create_agent(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        name: str,
        model: BetaManagedAgentsModelParam,
        *,
        description: str | None = None,
        system: str | None = None,
        tools: list[Tool] | None = None,
        mcp_servers: list[BetaManagedAgentsURLMCPServerParams] | None = None,
        skill_repos: list[SkillRepo] | None = None,
    ) -> AgentInfo:
        """Create a new agent. Pass fields directly — there is NO ``spec`` wrapper.

        Required: ``name`` and ``model`` (e.g. ``"claude-sonnet-4-6"``).
        Optional: ``description``, ``system`` (the system prompt), ``tools``,
        ``mcp_servers``, and ``skill_repos`` — GitHub repos to sync skills from,
        e.g. ``[{"url": "https://github.com/owner/repo", "branch": "main"}]``.

        Do not pass a ``skills`` field here. To add skills, either sync a repo via
        ``skill_repos`` or use the ``skills_*`` tools after the agent is created.
        """
        spec = _build_create_spec(
            name=name,
            model=model,
            description=description,
            system=system,
            tools=tools,
            mcp_servers=mcp_servers,
            skill_repos=skill_repos,
        )
        return await _create_agent_impl(runtime, await _auth(ctx), spec)

    @mcp.tool(tags={"admin"})
    async def update_agent(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        name: str,
        *,
        model: BetaManagedAgentsModelParam | None = None,
        description: str | None = None,
        system: str | None = None,
        tools: list[Tool] | None = None,
        mcp_servers: list[BetaManagedAgentsURLMCPServerParams] | None = None,
        skills: list[str | BetaManagedAgentsSkillParams] | None = None,
    ) -> AgentInfo:
        """Patch-update an agent.

        Scalar fields (``model``, ``description``, ``system``) replace.
        List fields (``tools``, ``mcp_servers``, ``skills``) UNION with the
        agent's current state — caller's entries win on collision. To remove
        an existing entry, use the ``/agent-setup`` panel.

        ``skills`` accepts skill NAMES, e.g.
        ``skills=["build-models", "compare-models"]`` — names are resolved to MA
        skill ids server-side. The explicit dict form
        ``{"type": "custom", "skill_id": "skill_..."}`` still works.
        """
        return await _update_agent_impl(
            runtime,
            await _auth(ctx),
            name,
            model=model,
            description=description,
            system=system,
            tools=tools,
            mcp_servers=mcp_servers,
            skills=skills,
        )

    @mcp.tool(tags={"admin"})
    async def attach_mcp_server(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        server_name: str,
        url: str,
    ) -> AgentInfo:
        """Attach a no-auth MCP server to an agent.

        Use this ONLY for MCP servers that do not require authentication.
        The tool patches the agent's ``mcp_servers`` with
        ``{name: server_name, type: "url", url: url}`` AND appends a
        matching ``mcp_toolset`` entry to ``tools`` (required by MA, which
        rejects an agent whose ``mcp_servers`` entries aren't each
        referenced by a ``mcp_toolset``). Existing entries are preserved.
        If a server with the same ``server_name`` is already attached, the
        new ``url`` replaces it (last-write-wins) and the existing
        ``mcp_toolset`` is reused (no duplicate). If both ``server_name``
        and ``url`` already match, this is a no-op.

        For MCP servers that REQUIRE an auth token, do NOT collect the
        token in chat — direct the user to ``/agent-setup`` -> MCPs modal.
        Tokens sent in chat end up in Discord channel history and MA's
        tenant-wide session event log; the modal flow is the only
        supported path for auth-required servers.
        """
        return await _attach_mcp_server_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            server_name=server_name,
            url=url,
        )

    @mcp.tool(tags={"admin"})
    async def fork_agent(ctx: Context, source_name: str, new_name: str) -> AgentInfo:  # pyright: ignore[reportUnusedFunction]
        """Clone an agent within the tenant pool under a new name."""
        return await _fork_agent_impl(runtime, await _auth(ctx), source_name, new_name)

    @mcp.tool(tags={"admin"})
    async def archive_agent(ctx: Context, name: str) -> None:  # pyright: ignore[reportUnusedFunction]
        """Archive the MA agent and delete from the tenant pool."""
        await _archive_agent_impl(runtime, await _auth(ctx), name)
