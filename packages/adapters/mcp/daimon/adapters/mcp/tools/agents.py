"""Agent tools: list / get / create / update / fork / archive.

``register_agent_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures for
this group; each closure delegates to a module-private ``_*_impl`` function
that can be unit-tested without a FastMCP Context.
"""

from __future__ import annotations

import contextlib
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
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._channel_policy import require_agent_creatable, turn_origin_place
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._isolation import load_caller_isolation, load_skill_owners
from daimon.adapters.mcp.tools._pin_guard import require_pin_write_access
from daimon.adapters.mcp.tools.setup_target import (
    get_chat_origin,
    origin_channel_id,
    resolve_setup_agent,
)
from daimon.core.access_policy import DM_SCOPE_PREFIX
from daimon.core.agent_fork import copy_agent
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.agent_mcp_credentials import agent_mcp_write_lock
from daimon.core.agent_reach import record_created_for_channel
from daimon.core.constants import AGENT_MCP_CAP, AGENT_SKILL_CAP, ALLOWED_MODEL_IDS
from daimon.core.continuity.messages import ConfigurationChange, render_change_confirmation
from daimon.core.defaults.ma_index import (
    find_agents_by_daimon_tag,
    list_agents_by_tenant,
)
from daimon.core.defaults.mcp_merge import (
    DAIMON_MCP_SERVER_NAME,
    get_reserved_mcp_rejection,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_MANAGED,
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
from daimon.core.mcp_attach import (
    McpServerReplaceRefusedError,
    attach_mcp_server_to_agent,
    decide_mcp_replacement,
    replaced_server_url,
)
from daimon.core.memory_resource import archive_memory_store_for_agent
from daimon.core.routing_facts import build_unrouted_note
from daimon.core.skill_sync import SyncRepoFailure, sync_agent_skills, sync_report_failures
from daimon.core.specs import (
    AgentSpec,
    SkillRepo,
    merge_default_agent_toolset,
)
from daimon.core.stores.domain import TurnOriginRow
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
    system: str | None = None
    """Set only by ``get_agent``, and only for an admin caller on an agent chat
    tools may edit: the full system prompt (``""`` when it has none). ``None``
    means withheld, not empty — a non-admin caller, or a defaults-managed or
    system agent."""
    applies: str | None = None
    """Set only by ``update_agent`` when it changed ``model`` and/or ``system``:
    person-facing confirmation that the change reaches this conversation on
    its next message, not the one running now."""
    answering: str | None = None
    """Set only by ``create_agent`` and ``fork_agent``, and only when nothing
    routes to the new agent yet: post it verbatim, so the person learns the
    agent exists but answers nowhere and what to say to change that."""
    dropped_skills: list[str] | None = None
    """Set only by ``fork_agent``: skills left off the copy (another agent's, or the
    source's own that failed to copy; its own are otherwise copied). Tell the person."""
    copied_skills: list[str] | None = None
    """Set only by ``fork_agent``: the source's own skills, uploaded again under the
    copy's name. The copy has them even when ``skills`` does not list them yet; do
    not add them again."""

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
    rejection = _system_agent_rejection(agent)
    if rejection is not None:
        raise ToolError(rejection)


def _system_agent_rejection(agent: BetaManagedAgentsAgent) -> str | None:
    """Return why chat tools cannot modify ``agent``, or ``None`` if they can."""
    if agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        return (
            f"agent '{agent.name}' is managed by defaults; chat tools cannot modify it. "
            "An admin can make an editable copy with fork_agent; a member can create_agent "
            "a new one instead."
        )
    if agent.metadata.get(MA_METADATA_KEY_ACCOUNT) is None:
        return (
            f"agent '{agent.name}' is a system agent; chat tools cannot modify it. "
            "An admin can make an editable copy with fork_agent; a member can create_agent "
            "a new one instead."
        )
    return None


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
    origin_context_id: str | None = None,
) -> list[AgentInfo]:
    del page
    rows = await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    caller = await load_caller_isolation(
        runtime, auth, agents=rows, location_channel_id=origin_channel_id(origin)
    )
    rows = [row for row in rows if caller.sees_agent(row)]
    skill_titles, _truncated = await resolve_custom_skill_titles(
        runtime.client, agents=rows, tenant_id=auth.tenant_id
    )
    return [AgentInfo.from_ma(a, skill_titles=skill_titles) for a in rows]


async def _get_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
    expected_ma_agent_id: str | None = None,
    origin_context_id: str | None = None,
) -> AgentInfo:
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    agent = await resolve_setup_agent(
        runtime,
        auth,
        name=name,
        expected_ma_agent_id=expected_ma_agent_id,
        require_identity=False,
        location_channel_id=origin_channel_id(origin),
    )
    info = await _build_agent_info(runtime.client, agent, tenant_id=auth.tenant_id)
    # The prompt is readable only by callers who could replace it on any agent:
    # admins, on agents chat tools may edit at all. A defaults-managed or system
    # agent's prompt stays withheld even from admins — the edit path is a fork,
    # and the fork's own prompt is then readable.
    if auth.is_admin and _system_agent_rejection(agent) is None:
        info = info.model_copy(update={"system": agent.system or ""})
    return info


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


async def _record_creation_channel(
    runtime: McpRuntime, auth: AuthIdentity, ma_agent_id: str, origin: TurnOriginRow | None
) -> None:
    """Make the new agent its channel admins' when one made it from their channel or its
    setup thread (`daimon.core.agent_reach.record_created_for_channel`). Never a DM."""
    if origin is None or origin.thread_id.startswith(DM_SCOPE_PREFIX):
        return
    async with runtime.session_factory.begin() as session:
        await record_created_for_channel(
            session,
            tenant_id=auth.tenant_id,
            platform=origin.platform,
            ma_agent_id=ma_agent_id,
            channel_id=origin_channel_id(origin),
            caller=reachability.channel_admin_caller(auth),
        )


async def _create_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    spec: AgentSpec,
    origin_context_id: str | None = None,
) -> AgentInfo:
    if spec.skills:
        raise ToolError(
            "create_agent: skills must be empty. To add skills, either sync a "
            "repo via skill_repos or use sync_skills after the agent "
            "is created."
        )
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    await require_agent_creatable(
        runtime, auth, origin=turn_origin_place(origin) if origin is not None else None
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
    await _record_creation_channel(runtime, auth, outcome.anthropic_id, origin)
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


def _reject_reserved_servers(
    runtime: McpRuntime, mcp_servers: list[BetaManagedAgentsURLMCPServerParams]
) -> None:
    """The same reserved-name guard `attach_mcp_server` applies, for `update_agent`.

    Its merge replaces servers by name, so without this a caller could re-point
    `daimon-mcp` at any URL. Re-sending the canonical entry unchanged (a
    get_agent round trip) is allowed; anything else under the reserved name,
    or the deployment's own URL under another name, is refused.
    """
    public_url = (
        str(runtime.settings.mcp.public_url)
        if runtime.settings.mcp.public_url is not None
        else None
    )
    for server in mcp_servers:
        name = str(server.get("name") or "")
        url = str(server.get("url") or "")
        if (
            name == DAIMON_MCP_SERVER_NAME
            and public_url is not None
            and url.rstrip("/") == public_url.rstrip("/")
        ):
            continue
        rejection = get_reserved_mcp_rejection(server_name=name, url=url, public_url=public_url)
        if rejection is not None:
            raise ToolError(f"update_agent: {rejection} Nothing was saved.")


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
    origin_context_id: str | None = None,
) -> AgentInfo:
    if model is not None:
        _reject_unknown_model(model)
    scalars: dict[str, Any] = {"model": model, "description": description, "system": system}
    list_fields = (tools, mcp_servers, skills)
    if all(v is None for v in scalars.values()) and all(v is None for v in list_fields):
        raise ToolError("update_agent: at least one field is required")
    if mcp_servers is not None:
        _reject_reserved_servers(runtime, mcp_servers)
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    agent = await resolve_setup_agent(
        runtime,
        auth,
        name=name,
        expected_ma_agent_id=expected_ma_agent_id,
        location_channel_id=origin_channel_id(origin),
    )
    _reject_system_agent(agent)
    await require_pin_write_access(runtime, auth, ma_agent=agent, origin=None)

    touched_fields = {field_name for field_name, value in scalars.items() if value is not None}
    if tools is not None:
        touched_fields.add("tools")
    if mcp_servers is not None:
        touched_fields.add("mcp_servers")
    if skills is not None:
        touched_fields.add("skills")
    if touched_fields & reachability.REACHABILITY_GATED_FIELDS:
        await reachability.require_admin_for_reachable_agent(
            runtime, auth, agent_name=name, agent=agent
        )
    mcp_replace_allowed = (
        await _require_mcp_replace_allowed(
            runtime,
            auth,
            agent,
            [(str(entry.get("name")), str(entry.get("url"))) for entry in mcp_servers],
        )
        if mcp_servers is not None
        else False
    )

    # Resolve skill names outside the closure — name resolution does not depend on
    # the agent's current state and must not be repeated on each retry attempt.
    resolved_skills: list[BetaManagedAgentsSkillParams] | None = None
    if skills is not None:
        try:
            caller = await load_caller_isolation(
                runtime, auth, location_channel_id=origin_channel_id(origin)
            )
            owners = await load_skill_owners(runtime, caller, auth.tenant_id)
            resolved_skills = await resolve_skill_names(
                runtime.client,
                skills,
                tenant_id=auth.tenant_id,
                is_skill_hidden=lambda skill_id, body: caller.hides_skill(
                    owners, skill_id=skill_id, body=body
                ),
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
            if not mcp_replace_allowed and any(
                replaced_server_url(
                    fresh, server_name=str(entry.get("name")), url=str(entry.get("url"))
                )
                for entry in mcp_servers
            ):
                # Attached under this name at another URL since the check above.
                raise ToolError(
                    f"'{name}' now has one of these server names at another URL; repointing "
                    "it needs an admin. Nothing was changed."
                )
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

    # An MCP server change is serialized with the token forms' attach-then-
    # publish for this agent; other fields need no lock.
    mcp_lock = (
        agent_mcp_write_lock(
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=agent.id),
        )
        if mcp_servers is not None
        else contextlib.nullcontext()
    )
    try:
        async with mcp_lock:
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


async def _require_mcp_replace_allowed(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent: BetaManagedAgentsAgent,
    servers: list[tuple[str, str]],
) -> bool:
    """Gate repointing an existing server name at another URL (`mcp_replace`).

    Returns whether a replacement is authorized, for the fresh-agent re-check
    in the write. Refuses here when one is needed and the caller may not make
    it; the attachment rules count handoff threads and personal defaults as
    shared, which the plain reachability gate does not.
    """
    replaced = [
        name for name, url in servers if replaced_server_url(agent, server_name=name, url=url)
    ]
    if not replaced:
        return False
    outcome = await decide_mcp_replacement(
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        platform=auth.platform or "",
        agent=agent,
        caller=reachability.channel_admin_caller(auth),
        default=runtime.deployment_default,
    )
    if outcome != "allow":
        raise ToolError(
            f"'{agent.name}' already has {', '.join(repr(n) for n in replaced)} at another URL "
            "and is shared, so repointing it needs a server or workspace admin, and the caller "
            "is not one. Nothing was changed. Do not retry under another name."
        )
    return True


async def _attach_mcp_server_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    server_name: str,
    url: str,
    expected_ma_agent_id: str | None = None,
    origin_context_id: str | None = None,
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
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    agent = await resolve_setup_agent(
        runtime,
        auth,
        name=agent_name,
        expected_ma_agent_id=expected_ma_agent_id,
        location_channel_id=origin_channel_id(origin),
    )
    _reject_system_agent(agent)
    await require_pin_write_access(runtime, auth, ma_agent=agent, origin=None)
    await reachability.require_admin_for_reachable_agent(
        runtime, auth, agent_name=agent_name, agent=agent
    )

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
        # Serialized with the token forms' attach-then-publish for this agent.
        async with agent_mcp_write_lock(
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=agent.id),
        ):
            replace_allowed = await _require_mcp_replace_allowed(
                runtime, auth, agent, [(server_name, url)]
            )
            updated = await attach_mcp_server_to_agent(
                runtime.client,
                agent.id,
                server_name=server_name,
                url=url,
                replace_allowed=replace_allowed,
            )
    except McpServerReplaceRefusedError as exc:
        raise ToolError(str(exc)) from exc
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
    # Forking is an admin's call: a fork routes nowhere and has no channel
    # pin of its own, so it is free to run anywhere the source can't.
    _require_admin(auth)
    await _reject_guild_name_collision(runtime, auth, new_name)
    source = await resolve_setup_agent(
        runtime, auth, name=source_name, expected_ma_agent_id=expected_ma_agent_id
    )
    public_url = runtime.settings.mcp.public_url
    try:
        copy = await copy_agent(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            source=source,
            new_name=new_name,
            public_url=str(public_url) if public_url is not None else None,
            subject=mcp_subject(auth, is_admin=auth.is_admin),
        )
    except DaimonError as exc:
        raise ToolError(f"fork_agent: {exc} Nothing was created. Do not retry.") from exc
    info = await _build_agent_info(runtime.client, copy.agent, tenant_id=auth.tenant_id)
    if copy.dropped_skills:
        info = info.model_copy(update={"dropped_skills": list(copy.dropped_skills)})
    if copy.copied_skills:
        info = info.model_copy(update={"copied_skills": list(copy.copied_skills)})
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
    async def list_agents(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, page: str | None = None, origin_context_id: str | None = None
    ) -> list[AgentInfo]:
        """List agents in the tenant pool, including each agent's attached
        ``mcp_servers`` and ``skills``. ``page`` is reserved for future pagination.
        Pass this turn's ``origin_context_id`` so its channel counts."""
        return await _list_agents_impl(runtime, await _auth(ctx), page, origin_context_id)

    @mcp.tool
    async def get_agent(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        name: str,
        expected_ma_agent_id: str | None = None,
        origin_context_id: str | None = None,
    ) -> AgentInfo:
        """Show what an agent can access: attached MCP servers and skills.

        Use ``list_agent_keys`` for stored key names. Configuration does not prove
        the answering session's access. Returns server names/URLs and skills; custom
        skills have a display name (null if deleted), Anthropic skills have a readable id.
        For an admin on an editable agent, ``system`` is the full system prompt;
        null means withheld (non-admin caller, or Daimon/defaults-managed agent).
        Pass this turn's ``origin_context_id`` so its channel counts."""
        return await _get_agent_impl(
            runtime,
            await _auth(ctx),
            name,
            expected_ma_agent_id=expected_ma_agent_id,
            origin_context_id=origin_context_id,
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
        origin_context_id: str | None = None,
    ) -> AgentInfo:
        """Create an agent called, for example, churn-explorer. Pass fields directly —
        there is NO ``spec`` wrapper.

        Required: ``name`` and ``model``. Use ``"claude-sonnet-5-5"`` when the user
        asks for Sonnet and ``"claude-opus-5-5"`` when they ask for Opus — always
        the current generation. Only pass an older id (``claude-sonnet-5``,
        ``claude-opus-5``, …) when the user names that version themselves.
        Optional: ``description``, ``system`` (the system prompt), ``tools``,
        ``mcp_servers``, and ``skill_repos`` — GitHub repos to sync skills from,
        e.g. ``[{"url": "https://github.com/owner/repo", "branch": "main"}]``.

        Do not pass a ``skills`` field here. To add skills, either sync a repo via
        ``skill_repos`` or use ``sync_skills`` after the agent is created.

        A returned ``answering`` field says the new agent is routed nowhere yet:
        post it verbatim. Always pass this turn's ``origin_context_id``: a chat turn
        without it is refused while a channel in the workspace is confidential, and with
        it a channel admin creating the agent for their channel may set it up there.
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
        return await _create_agent_impl(runtime, await _auth(ctx), spec, origin_context_id)

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
        origin_context_id: str | None = None,
    ) -> AgentInfo:
        """Change an agent's system prompt or switch its model; add existing skills such as
        build-models. Scalar ``model``, ``description`` and ``system`` fields replace.

        Prefer current-generation ``claude-sonnet-5-5`` for Sonnet and ``claude-opus-5-5``
        for Opus unless an older version is explicitly requested. List fields
        (``tools``, ``mcp_servers``, ``skills``) are added to, never replaced; shorter
        lists remove nothing. Use ``remove_skill`` or ``detach_mcp_server`` to remove.
        Daimon requires ``fork_agent`` first; channel/workspace defaults require admin.

        Prompt, model and skill changes reach a conversation on its next message, not the
        one running now. ``skills`` accepts names such as
        ``["build-models", "compare-models"]``, resolved server-side; explicit
        ``{"type": "custom", "skill_id": "skill_..."}`` entries also work. A model or
        prompt change also returns ``applies``: post it verbatim as part of the reply.
        Pass this turn's ``origin_context_id`` so its channel counts."""
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
            origin_context_id=origin_context_id,
        )

    @mcp.tool
    async def attach_mcp_server(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        server_name: str,
        url: str,
        expected_ma_agent_id: str | None = None,
        origin_context_id: str | None = None,
    ) -> AgentInfo:
        """Add an MCP server that needs no token, such as Context7, to an agent.

        For Linear or other bearer-authenticated endpoints, use
        ``request_mcp_token`` instead;
        ``detach_mcp_server`` disconnects it. Fork Daimon with ``fork_agent`` before
        directly changing its setup. Channel or workspace defaults need admin.

        Connects the server; its tools are available from the agent's next message,
        not the one running now. Reusing a server name replaces the URL; the same
        name and URL is a no-op. Other connections are preserved.
        Pass this turn's ``origin_context_id`` so its channel counts."""
        return await _attach_mcp_server_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            server_name=server_name,
            url=url,
            expected_ma_agent_id=expected_ma_agent_id,
            origin_context_id=origin_context_id,
        )

    @mcp.tool
    async def fork_agent(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, source_name: str, new_name: str, expected_ma_agent_id: str | None = None
    ) -> AgentInfo:
        """Make a copy of Daimon or another agent that you can edit under a new name.
        Admin-only. Copies its prompt, model, skills and the MCP servers that need
        no stored token. The copy starts with no credentials: no repo binding or
        GitHub access, no API/service keys and no connector tokens; attach its own
        with ``request_agent_key`` and the repo tools. An agent an operator pinned
        to channels can't be copied.

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
