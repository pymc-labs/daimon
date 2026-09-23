"""Agent tools: list / get / create / update / fork / archive.

``register_agent_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures for
this group; each closure delegates to a module-private ``_*_impl`` function
that can be unit-tested without a FastMCP Context.
"""

from __future__ import annotations

import datetime
import time
import uuid
from collections.abc import Mapping
from typing import Any, Final, cast

import anthropic
import httpx
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSkillParams
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_agent import Tool as MATool
from anthropic.types.beta.beta_managed_agents_model_param import BetaManagedAgentsModelParam
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import reachability
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.setup_target import resolve_setup_agent
from daimon.core import agent_lifecycle
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.constants import AGENT_MCP_CAP, AGENT_SKILL_CAP, ALLOWED_MODEL_IDS
from daimon.core.continuity.messages import ConfigurationChange, render_change_confirmation
from daimon.core.defaults.ma_index import (
    find_agents_by_daimon_tag,
    list_agents_by_tenant,
)
from daimon.core.defaults.mcp_merge import (
    get_reserved_mcp_rejection,
    merge_default_mcp_server,
    merge_default_mcp_toolset,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_ISOLATED,
    MA_METADATA_KEY_MANAGED,
    build_metadata,
)
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.defaults.skills import resolve_custom_skill_titles, resolve_skill_names
from daimon.core.defaults.spec_merge import merge_mcp_servers_with_ma, merge_skills_with_ma
from daimon.core.errors import DaimonError, DefaultsError
from daimon.core.github_app_auth import build_app_jwt, get_installation_id_for_repo
from daimon.core.github_repo_auth import InstallationLookup
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_attach import attach_mcp_server_to_agent
from daimon.core.memory_resource import archive_memory_store_for_agent
from daimon.core.routing_facts import build_unrouted_note
from daimon.core.skill_sync import SyncRepoFailure, sync_agent_skills, sync_report_failures
from daimon.core.specs import (
    AgentSpec,
    SkillRepo,
    merge_default_agent_toolset,
)
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant
from daimon.core.stores.scoped_config_write import clear_agent_references
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, SecretStr, ValidationError

log = structlog.get_logger()


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
    applies: str | None = None
    """Set only by ``update_agent`` when it changed ``model`` and/or ``system``:
    person-facing confirmation that the change reaches this conversation on
    its next message, not the one running now."""
    answering: str | None = None
    """Set only by ``create_agent`` and ``fork_agent``, and only when nothing
    routes to the new agent yet: post it verbatim, so the person learns the
    agent exists but answers nowhere and what to say to change that."""

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


async def _build_agent_info(
    client: AsyncAnthropic,
    agent: BetaManagedAgentsAgent,
    *,
    tenant_id: uuid.UUID,
    sync_warnings: list[SyncRepoFailure] | None = None,
) -> AgentInfo:
    """Map an MA agent to ``AgentInfo`` with custom skill names resolved."""
    skill_titles, _truncated = await resolve_custom_skill_titles(
        client, agents=[agent], tenant_id=tenant_id
    )
    return AgentInfo.from_ma(agent, sync_warnings=sync_warnings, skill_titles=skill_titles)


async def _with_answering_note(
    runtime: McpRuntime, auth: AuthIdentity, info: AgentInfo
) -> AgentInfo:
    """Attach the routing handoff when nothing in the tenant routes to ``info``.

    A freshly created or forked agent exists but answers nowhere, and the
    person who asked for it reliably expects to be able to talk to it by name.
    One config-cascade read decides; a reachable agent gets no note.
    """
    async with runtime.session_factory() as session:
        reachable = await is_agent_reachable_in_tenant(
            session,
            tenant_id=auth.tenant_id,
            agent_name=info.name,
            default=runtime.deployment_default,
        )
    if reachable:
        return info
    return info.model_copy(
        update={
            "answering": build_unrouted_note(
                agent_name=info.name, channel_label=None, is_admin=auth.is_admin
            )
        }
    )


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


def _ma_tool_to_param(tool: MATool) -> Tool:
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
    """Reject defaults-owned agents from chat mutating tools.

    Unconditional — applies to admins too, with no bypass. A chat edit never
    stamps the seeded agent's spec hash, so the reconcile pipeline's hash
    short-circuit skips the drifted agent forever the next time defaults are
    applied: the drift is permanent and `daimon defaults apply` cannot repair
    it. Forking is the edit path.

    Two markers, because either one on its own leaks:

    - `daimon_managed="true"` is the reconciler's own provenance stamp and the
      authoritative one. Keying on the absence of `daimon_account` alone did
      NOT work: the guild seed path account-stamps seeded agents, so the
      account key is present on them and this guard silently never fired. The
      Discord panel hit the same trap and moved to this marker (#160); the MCP
      tools did not follow until now. Panel forks stamp `managed=False` and
      chat/CLI creates leave it unset, so both stay editable.
    - a missing `daimon_account` still rejects, preserving cover for older
      unstamped seeded agents that predate the account stamp.
    """
    if agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        raise ToolError(
            f"agent '{agent.name}' is managed by defaults; chat tools cannot modify it. "
            "Use fork_agent to make an editable copy, then edit the named copy."
        )
    owner = agent.metadata.get(MA_METADATA_KEY_ACCOUNT)
    if owner is None:
        raise ToolError(
            f"agent '{agent.name}' is a system agent; chat tools cannot modify it. "
            "Use fork_agent to make an editable copy, then edit the named copy."
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
    skill_titles, _truncated = await resolve_custom_skill_titles(
        runtime.client, agents=rows, tenant_id=auth.tenant_id
    )
    return [AgentInfo.from_ma(a, skill_titles=skill_titles) for a in rows]


async def _get_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
    expected_ma_agent_id: str | None = None,
) -> AgentInfo:
    agent = await resolve_setup_agent(
        runtime, auth, name=name, expected_ma_agent_id=expected_ma_agent_id, require_identity=False
    )
    return await _build_agent_info(runtime.client, agent, tenant_id=auth.tenant_id)


def _reject_unknown_model(model: str) -> None:
    """Raise unless ``model`` is one this deployment meters and allows.

    The panel paths have validated free-text model input against
    ALLOWED_MODEL_IDS since UX-25-03; the chat paths never did, so a typo or a
    hallucinated id was accepted here and only surfaced later as a session that
    would not start. An unpriced id is also invisible to cost accounting —
    ``pricing.cost_of`` returns None for a model it has no rates for, so the
    turn is billed by Anthropic and recorded as free by us.
    """
    if model not in ALLOWED_MODEL_IDS:
        allowed = ", ".join(ALLOWED_MODEL_IDS)
        raise ToolError(f"Model '{model}' is not available. Choose one of: {allowed}")


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
    _reject_unknown_model(model)
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
        raise ToolError(
            "create_agent: invalid agent configuration; check the supplied fields "
            "and ensure every MCP server has a matching mcp_toolset entry. Nothing was saved."
        ) from exc


async def _create_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    spec: AgentSpec,
) -> AgentInfo:
    if spec.skills:
        raise ToolError(
            "create_agent: skills must be empty. To add skills, either sync a "
            "repo via skill_repos or use sync_skills after the agent "
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
            github_fallback_pat = (
                runtime.settings.github.fallback_pat.get_secret_value()
                if runtime.settings.github.fallback_pat is not None
                else None
            )
            github_app_id = runtime.settings.github.app_id
            github_app_private_key = runtime.settings.github.app_private_key
            async with httpx.AsyncClient() as http_client:
                installation_lookup: InstallationLookup | None = None
                if github_app_id is not None and github_app_private_key is not None:
                    # Interactive path — a live GitHub lookup is the right
                    # freshness choice here (the webhook resync injects a
                    # cheap cached read instead; see resync.py).
                    resolved_app_id: str = github_app_id
                    resolved_app_private_key: SecretStr = github_app_private_key

                    async def _live_installation_lookup(owner: str, repo: str) -> int | None:
                        jwt = build_app_jwt(
                            resolved_app_private_key.get_secret_value(),
                            resolved_app_id,
                            now=int(time.time()),
                        )
                        return await get_installation_id_for_repo(
                            http_client, jwt=jwt, owner=owner, repo=repo
                        )

                    installation_lookup = _live_installation_lookup
                report = await sync_agent_skills(
                    principal_id=auth.account_id,  # NOT auth.principal_id (no such field)
                    tenant_id=auth.tenant_id,
                    agent_name=spec.name,
                    repos=spec.skill_repos,
                    sessionmaker=runtime.session_factory,  # McpRuntime field is session_factory
                    fernet=fernet,
                    http_client=http_client,
                    anthropic_client=runtime.client,  # McpRuntime field is client
                    github_fallback_pat=github_fallback_pat,
                    app_id=github_app_id,
                    app_private_key=github_app_private_key,
                    installation_lookup=installation_lookup,
                    max_tarball_decompressed_bytes=(
                        runtime.settings.github.max_tarball_decompressed_bytes
                    ),
                )
            warnings = sync_report_failures(report) or None
    info = await _build_agent_info(
        runtime.client, ma_agent, tenant_id=auth.tenant_id, sync_warnings=warnings
    )
    return await _with_answering_note(runtime, auth, info)


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
    expected_ma_agent_id: str | None = None,
) -> AgentInfo:
    if model is not None:
        _reject_unknown_model(model)
    scalars: dict[str, Any] = {"model": model, "description": description, "system": system}
    list_fields = (tools, mcp_servers, skills)
    if all(v is None for v in scalars.values()) and all(v is None for v in list_fields):
        raise ToolError("update_agent: at least one field is required")
    agent = await resolve_setup_agent(
        runtime, auth, name=name, expected_ma_agent_id=expected_ma_agent_id
    )
    _reject_system_agent(agent)

    touched_fields = {field_name for field_name, value in scalars.items() if value is not None}
    if tools is not None:
        touched_fields.add("tools")
    if mcp_servers is not None:
        touched_fields.add("mcp_servers")
    if skills is not None:
        touched_fields.add("skills")
    if touched_fields & reachability.REACHABILITY_GATED_FIELDS:
        await reachability.require_admin_for_reachable_agent(runtime, auth, agent_name=name)

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
    # additions surface (dedicated removal tools handle removals), so union caller's values with
    # MA's current state — bug 2 of issue #56.
    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        patch: dict[str, Any] = dict(scalar_patch)
        if resolved_skills is not None:
            patch["skills"] = merge_skills_with_ma(resolved_skills, fresh)
            merged_skill_count = len(patch["skills"])
            if merged_skill_count > AGENT_SKILL_CAP:
                raise ToolError(
                    f"Cannot attach skills: the merged skill set ({merged_skill_count}) exceeds "
                    f"this organization's per-agent skill limit ({AGENT_SKILL_CAP}). No skills "
                    f"were changed on '{name}'. Attach fewer skills, "
                    "or use remove_skill before adding more."
                )
        if mcp_servers is not None:
            patch["mcp_servers"] = merge_mcp_servers_with_ma(mcp_servers, fresh)
            # merge_mcp_servers_with_ma's return type is `list | None` at the
            # signature level (None only for a None input), but `mcp_servers`
            # is guaranteed non-None in this branch, so the merged result is
            # never actually None — `or []` only satisfies the static type.
            merged_mcp_count = len(patch["mcp_servers"] or [])
            if merged_mcp_count > AGENT_MCP_CAP:
                raise ToolError(
                    f"Cannot attach MCP servers: the merged server set ({merged_mcp_count}) "
                    f"exceeds this organization's per-agent MCP-server limit ({AGENT_MCP_CAP}). "
                    f"No servers were changed on '{name}'. Attach fewer servers, or use "
                    "detach_mcp_server before adding more."
                )
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
                f"use remove_skill on '{name}' before adding more."
            ) from exc
        raise
    result = await _build_agent_info(runtime.client, updated, tenant_id=auth.tenant_id)
    applies_lines: list[str] = []
    if model is not None:
        applies_lines.append(
            render_change_confirmation(
                ConfigurationChange(target_name=name, kind="model", availability="next_message")
            )
        )
    if system is not None:
        applies_lines.append(
            render_change_confirmation(
                ConfigurationChange(
                    target_name=name, kind="instructions", availability="next_message"
                )
            )
        )
    if applies_lines:
        result = result.model_copy(update={"applies": "\n".join(applies_lines)})
    return result


async def _attach_mcp_server_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    server_name: str,
    url: str,
    expected_ma_agent_id: str | None = None,
) -> AgentInfo:
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
    agent = await resolve_setup_agent(
        runtime, auth, name=agent_name, expected_ma_agent_id=expected_ma_agent_id
    )
    _reject_system_agent(agent)
    await reachability.require_admin_for_reachable_agent(runtime, auth, agent_name=agent_name)

    existing = list(agent.mcp_servers or [])
    # No-op check on the initially-found agent (acceptable: a concurrent change
    # between this check and the update is exactly what the version-retry covers).
    for s in existing:
        if s.name == server_name and s.url == url:
            return await _build_agent_info(runtime.client, agent, tenant_id=auth.tenant_id)

    # #144-2: the spec recompute lives in core.mcp_attach so the Discord
    # credential modal performs the identical write — it must attach the server
    # it just stored a vault credential for, and cannot import this module.
    # The reserved-server guard above is not repeated there: it depends only on
    # caller inputs, so each entry point applies its own policy.
    try:
        updated = await attach_mcp_server_to_agent(
            runtime.client, agent.id, server_name=server_name, url=url
        )
    except anthropic.ConflictError as exc:
        # Residual conflict after the one retry — surface as a clean ToolError.
        raise ToolError("the agent was modified concurrently — please retry the operation") from exc
    return await _build_agent_info(runtime.client, updated, tenant_id=auth.tenant_id)


async def _fork_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    source_name: str,
    new_name: str,
    expected_ma_agent_id: str | None = None,
) -> AgentInfo:
    await _reject_guild_name_collision(runtime, auth, new_name)
    source = await resolve_setup_agent(
        runtime, auth, name=source_name, expected_ma_agent_id=expected_ma_agent_id
    )
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

    # Every non-forked creation/edit path already runs the guidance applier
    # (`update_agent`, `reconcile_agent`); a fork bypasses both and copies raw
    # MA state directly, so normalize it here too. Skip for a source stamped
    # isolated — its session mounts no secrets, and the block would teach it
    # to look for resources it must not have.
    if source_ma.metadata.get(MA_METADATA_KEY_ISOLATED) != "true":
        fork_params["system"] = apply_credential_guidance(
            cast("str", fork_params.get("system") or "")
        )

    # Narrow the cached fernet BEFORE any partial write — the create
    # below is the first write, so this must gate ahead of it.
    fernet = runtime.fernet
    if fernet is None:
        raise ToolError(
            f"fork_agent: cannot copy '{source_name}' to '{new_name}' because this deployment "
            "is not fully configured. Tell the user an operator must finish setup; "
            "nothing was created. Do not ask the user to change deployment settings."
        )

    new_ma = await runtime.client.beta.agents.create(**fork_params)

    source_agent_uuid = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(source.id))
    fork_agent_uuid = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(new_ma.id))
    try:
        await agent_lifecycle.copy_credential_and_repo_binding(
            anthropic=runtime.client,
            sessionmaker=runtime.session_factory,
            fernet=fernet,
            oauth_scopes=tuple(runtime.settings.github.oauth_scopes),
            tenant_id=auth.tenant_id,
            source_agent_uuid=source_agent_uuid,
            fork_agent_uuid=fork_agent_uuid,
        )
    except DaimonError as exc:
        raise ToolError(str(exc)) from exc

    info = await _build_agent_info(runtime.client, new_ma, tenant_id=auth.tenant_id)
    return await _with_answering_note(runtime, auth, info)


async def _archive_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
    expected_ma_agent_id: str | None = None,
) -> None:
    _require_admin(auth)
    agent = await resolve_setup_agent(
        runtime, auth, name=name, expected_ma_agent_id=expected_ma_agent_id
    )
    _reject_system_agent(agent)
    await runtime.client.beta.agents.archive(agent.id)
    try:
        await archive_memory_store_for_agent(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(agent.id)),
        )
    except anthropic.APIError:
        # Best-effort degrade: the agent is already archived and the retry path
        # is dead (archived agents are filtered from lookup), so a transient
        # memory-store archive failure must not strand the agent in a failed
        # state — mirrors the mount-side policy in memory_resource.py.
        log.warning(
            "archive_agent.memory_store_archive_failed",
            tenant_id=str(auth.tenant_id),
            agent_name=name,
            ma_agent_id=agent.id,
        )
    # Outside the best-effort degrade on purpose: a transient memory-store
    # failure must not skip the scope clear, or every turn in the affected
    # channel — or the whole install, for the workspace row — keeps resolving
    # to an archived agent. A failure here surfaces to the tool caller.
    async with runtime.session_factory() as session, session.begin():
        await clear_agent_references(session, tenant_id=auth.tenant_id, agent_name=name)


def register_agent_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def list_agents(ctx: Context, page: str | None = None) -> list[AgentInfo]:  # pyright: ignore[reportUnusedFunction]
        """Find named agents in this server/workspace and their current IDs.

        Resolve a requested agent here before inspecting its keys or changing who
        answers. Several distinct agents can share the same Discord/Slack bot
        handle; the handle does not identify the responding agent. A named agent
        such as daimon can differ from the current responder.

        Returns each agent's name, id, model, attached ``mcp_servers`` and ``skills``.
        Use the returned name and id for tools that require ``expected_ma_agent_id``.
        ``page`` is reserved for future pagination."""
        return await _list_agents_impl(runtime, await _auth(ctx), page)

    @mcp.tool
    async def get_agent(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, name: str, expected_ma_agent_id: str | None = None
    ) -> AgentInfo:
        """Show a named agent's configured MCP servers and skills.

        Resolve the requested name with ``list_agents`` and pass its current id as
        ``expected_ma_agent_id``. The requested agent can differ from the responder;
        the bot's display name does not make them the same agent.

        Use ``list_agent_keys`` for that agent's stored key names. Configuration does
        not prove the answering session's access. Returns server names/URLs and skills; custom
        skills have a display name (null if deleted), Anthropic skills have a readable id."""
        return await _get_agent_impl(
            runtime,
            await _auth(ctx),
            name,
            expected_ma_agent_id=expected_ma_agent_id,
        )

    @mcp.tool
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
        """Create an agent called, for example, churn-explorer. Pass fields directly —
        there is NO ``spec`` wrapper.

        Required: ``name`` and ``model``. Use ``"claude-sonnet-5"`` when the user
        asks for Sonnet and ``"claude-opus-5"`` when they ask for Opus — always
        the current generation. Only pass an older id (``claude-sonnet-4-6``,
        ``claude-opus-4-8``, …) when the user names that version themselves.
        Optional: ``description``, ``system`` (the system prompt), ``tools``,
        ``mcp_servers``, and ``skill_repos`` — GitHub repos to sync skills from,
        e.g. ``[{"url": "https://github.com/owner/repo", "branch": "main"}]``.

        Do not pass a ``skills`` field here. To add skills, either sync a repo via
        ``skill_repos`` or use ``sync_skills`` after the agent is created.

        A returned ``answering`` field says the new agent is routed nowhere yet:
        post it verbatim.
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

    @mcp.tool
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
        expected_ma_agent_id: str | None = None,
    ) -> AgentInfo:
        """Change an agent's system prompt or switch its model; add existing skills such as
        build-models. Scalar ``model``, ``description`` and ``system`` fields replace.

        Prefer current-generation ``claude-sonnet-5`` for Sonnet and ``claude-opus-5``
        for Opus unless an older version is explicitly requested. List fields
        (``tools``, ``mcp_servers``, ``skills``) are added to, never replaced; shorter
        lists remove nothing. Use ``remove_skill`` or ``detach_mcp_server`` to remove.
        Daimon requires ``fork_agent`` first; channel/workspace defaults require admin.

        Prompt, model and skill changes reach a conversation on its next message, not the
        one running now. ``skills`` accepts names such as
        ``["build-models", "compare-models"]``, resolved server-side; explicit
        ``{"type": "custom", "skill_id": "skill_..."}`` entries also work. A model or
        prompt change also returns ``applies``: post it verbatim as part of the reply."""
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
            expected_ma_agent_id=expected_ma_agent_id,
        )

    @mcp.tool
    async def attach_mcp_server(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        server_name: str,
        url: str,
        expected_ma_agent_id: str | None = None,
    ) -> AgentInfo:
        """Add an MCP server that needs no token, such as Context7, to an agent.

        For Linear or other bearer-authenticated endpoints, use
        ``request_mcp_token`` instead;
        ``detach_mcp_server`` disconnects it. Fork Daimon with ``fork_agent`` before
        directly changing its setup. Channel or workspace defaults need admin.

        Connects the server; its tools are available from the agent's next message,
        not the one running now. Reusing a server name replaces the URL; the same
        name and URL is a no-op. Other connections are preserved."""
        return await _attach_mcp_server_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            server_name=server_name,
            url=url,
            expected_ma_agent_id=expected_ma_agent_id,
        )

    @mcp.tool
    async def fork_agent(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, source_name: str, new_name: str, expected_ma_agent_id: str | None = None
    ) -> AgentInfo:
        """Make a copy of Daimon or another agent that you can edit under a new name.
        Copies its prompt, model, skills, MCP definitions, working-repo binding and
        recorded GitHub access. Copying access does not freshly verify it.

        API/service keys are not copied; add them with ``request_agent_key``.
        Use ``update_agent`` to edit the copy. Daimon cannot be edited directly.

        Continue configuring it through Daimon with the copy named as the setup
        target. A returned ``answering`` field says the copy is routed nowhere yet:
        post it verbatim."""
        return await _fork_agent_impl(
            runtime,
            await _auth(ctx),
            source_name,
            new_name,
            expected_ma_agent_id=expected_ma_agent_id,
        )

    @mcp.tool(tags={"admin"})
    async def archive_agent(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, name: str, expected_ma_agent_id: str | None = None
    ) -> None:
        """Delete an agent, for example churn-explorer, by archiving it. Admin-only.

        This removes the agent from the tenant pool and clears its channel/workspace
        defaults so those channels fall back to the next routing tier."""
        await _archive_agent_impl(
            runtime,
            await _auth(ctx),
            name,
            expected_ma_agent_id=expected_ma_agent_id,
        )
