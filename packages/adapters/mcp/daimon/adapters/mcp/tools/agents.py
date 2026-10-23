"""Agent tools: list / get / create / update / fork / archive.

``register_agent_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures for
this group; each closure delegates to a module-private ``_*_impl`` function
that can be unit-tested without a FastMCP Context.
"""

from __future__ import annotations

import contextlib
import datetime
import uuid
from collections.abc import Mapping
from functools import partial
from typing import Any, Final, Literal, cast

import anthropic
import httpx
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSkillParams
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_agent import Tool as MATool
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
from daimon.adapters.mcp.tools._pin_guard import require_pin_write_access
from daimon.adapters.mcp.tools._rule_view import (
    load_caller_view,
    load_isolation_view,
    load_skill_owners,
)
from daimon.adapters.mcp.tools.agent_chat import require_outside_chat_turn
from daimon.adapters.mcp.tools.setup_target import (
    get_chat_origin,
    origin_channel_id,
    resolve_setup_agent,
)
from daimon.core import channel_backend
from daimon.core.admin_actions import DelegatedAction
from daimon.core.agent_fork import copy_agent
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.agent_identity import queue_agent_face
from daimon.core.agent_lifecycle import forget_archived_agent
from daimon.core.agent_mcp_credentials import agent_mcp_write_lock
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
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_attach import (
    McpServerReplaceRefusedError,
    attach_mcp_server_to_agent,
    decide_mcp_replacement,
    replaced_server_url,
)
from daimon.core.memory_resource import archive_memory_store_for_agent
from daimon.core.model_presets import ModelSelection, model_choice_id
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import archive_agent, retrieve_agent, update_agent
from daimon.core.routing_facts import build_unrouted_note
from daimon.core.skill_sync import SyncRepoFailure, sync_agent_skills, sync_report_failures
from daimon.core.specs import (
    AgentSpec,
    SkillRepo,
    merge_default_agent_toolset,
)
from daimon.core.stores.domain import BackendAgent, TurnOriginRow
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant
from daimon.core.tool_safety import get_mcp_server_name_rejection
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mux.contracts.ids import Provider
from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError

log = structlog.get_logger()


class AgentMcpServerInfo(BaseModel):
    name: str
    url: str


class AgentSkillInfo(BaseModel):
    # Plain str, NOT Literal["anthropic", "custom"] — upstream-controlled
    # value set; MA may ship a skill type the pinned SDK does not model.
    type: str
    skill_id: str
    name: str | None
    version: str


class AgentInfo(BaseModel):
    name: str
    id: str
    description: str | None
    model: str
    created_at: datetime.datetime | None
    """``None`` means withheld from a non-admin caller (setup detail)."""
    mcp_servers: list[AgentMcpServerInfo] | None
    """Attached server names and URLs. ``None`` means withheld from a non-admin
    caller; ``[]`` means the agent has none."""
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
            model=model_choice_id(agent.model),
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

    def for_member(self) -> AgentInfo:
        """Capabilities only: drop the setup details a non-admin must not see.

        Name, description, model, skills and the agent id stay. The id is the
        identity chat callers pass back as ``expected_ma_agent_id``; the rest
        (attached MCP servers and their URLs, creation date, sync warnings) is
        how the agent was set up, which is for admins.
        """
        return self.model_copy(
            update={"created_at": None, "mcp_servers": None, "sync_warnings": None}
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
                agent_name=info.name,
                channel_label=None,
                is_admin=auth.admin_for(DelegatedAction.AGENT_SETTINGS),
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

    The chat `update_agent` is an
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
      Discord panel hit the same trap and moved to this marker; the MCP
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


async def _may_see_setup(
    runtime: McpRuntime, auth: AuthIdentity, agent: BetaManagedAgentsAgent
) -> bool:
    """Members see capabilities; native admins see setup details."""
    return auth.admin_for(DelegatedAction.AGENT_SETTINGS)


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
    caller = await load_caller_view(
        runtime,
        auth,
        agents=rows,
        location_channel_id=origin_channel_id(origin),
        location_thread_id=origin.thread_id if origin is not None else None,
    )
    rows = [row for row in rows if caller.sees_agent(row)]
    skill_titles, _truncated = await resolve_custom_skill_titles(
        runtime.client, agents=rows, tenant_id=auth.tenant_id
    )
    infos = [AgentInfo.from_ma(a, skill_titles=skill_titles) for a in rows]
    if auth.admin_for(DelegatedAction.AGENT_SETTINGS):
        return infos
    return [
        info if await _may_see_setup(runtime, auth, row) else info.for_member()
        for row, info in zip(rows, infos, strict=True)
    ]


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
        discovery=True,
        location_channel_id=origin_channel_id(origin),
        location_thread_id=origin.thread_id if origin is not None else None,
    )
    info = await _build_agent_info(runtime.client, agent, tenant_id=auth.tenant_id)
    # The prompt is readable only by callers who could replace it: admins, and
    # tools may edit at all. A defaults-managed or system
    # agent's prompt stays withheld even from admins — the edit path is a fork,
    # and the fork's own prompt is then readable.
    sees_setup = auth.admin_for(DelegatedAction.AGENT_SETTINGS) or await _may_see_setup(
        runtime, auth, agent
    )
    if sees_setup and _system_agent_rejection(agent) is None:
        info = info.model_copy(update={"system": agent.system or ""})
    if not sees_setup:
        info = info.for_member()
    return info


def _reject_unknown_model(model: ModelSelection) -> None:
    """Raise unless ``model`` is one this deployment meters and allows.

    The panel paths have validated free-text model input against
    ALLOWED_MODEL_IDS; the chat paths never did, so a typo or a
    hallucinated id was accepted here and only surfaced later as a session that
    would not start. An unpriced id is also invisible to cost accounting —
    ``pricing.cost_of`` returns None for a model it has no rates for, so the
    turn is billed by Anthropic and recorded as free by us.
    """
    choice_id = model_choice_id(model)
    if choice_id not in ALLOWED_MODEL_IDS:
        allowed = ", ".join(ALLOWED_MODEL_IDS)
        raise ToolError(f"Model '{choice_id}' is not available. Choose one of: {allowed}")


def _build_create_spec(
    *,
    name: str,
    model: ModelSelection,
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
    for server in mcp_servers or []:
        if (rejection := get_mcp_server_name_rejection(str(server.get("name") or ""))) is not None:
            raise ToolError(f"create_agent: {rejection} Nothing was saved.")
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
        references=runtime.session_factory,
    )
    if outcome.anthropic_id is None:
        raise ToolError("create_agent: reconcile returned no agent id — report this as a bug")
    queue_agent_face(
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        agent_name=spec.name,
        metadata=None,  # managed=False above: only the default name can make it built-in
        default_agent_name=runtime.deployment_default.agent_name,
    )
    ma_agent = await retrieve_agent(
        runtime.client,
        outcome.anthropic_id,
        scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
    )
    # agents.create succeeded — always return AgentInfo even if sync fails.
    # if agents.create itself raises, let it propagate as ToolError.
    warnings: list[SyncRepoFailure] | None = None
    if spec.skill_repos:
        async with httpx.AsyncClient() as http_client:
            report = await sync_agent_skills(
                principal_id=auth.account_id,
                tenant_id=auth.tenant_id,
                agent_name=spec.name,
                repos=spec.skill_repos,
                sessionmaker=runtime.session_factory,
                fernet=runtime.fernet,
                http_client=http_client,
                anthropic_client=runtime.client,
                app_id=runtime.settings.github_app.app_id,
                app_private_key=runtime.settings.github_app.private_key,
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
        rejection = get_mcp_server_name_rejection(name) or get_reserved_mcp_rejection(
            server_name=name, url=url, public_url=public_url
        )
        if rejection is not None:
            raise ToolError(f"update_agent: {rejection} Nothing was saved.")


async def _update_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
    *,
    model: ModelSelection | None,
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
            caller = await load_isolation_view(
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

    # Version-retry closure. All agent-derived unions (skills, mcp_servers,
    # tools) are recomputed from `fresh` on every attempt so a retry after a stale-
    # version conflict picks up any concurrent mutations rather than re-applying a
    # stale merge. MA treats list fields as per-field replaces; chat tools are an
    # additions surface (dedicated removal tools handle removals), so union caller's values with
    # MA's current state.
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
        # Attaching skills to an agent that lacks agent_toolset_20260401 produces a
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
        return await update_agent(
            runtime.client,
            fresh.id,
            version=fresh.version,
            payload=patch,
            scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
        )

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
            updated = await update_agent_with_version_retry(
                runtime.client,
                agent.id,
                _apply,
                scope=resource_scope(
                    tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)
                ),
            )
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
        caller=reachability.admin_caller(auth),
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
    # Guard the reserved daimon-mcp entry before even looking at the agent.
    # Also reject any URL that points at the deployment's own public_url under a
    # different name — that would make the next reconcile append a second daimon-mcp.
    public_url = (
        str(runtime.settings.mcp.public_url)
        if runtime.settings.mcp.public_url is not None
        else None
    )
    rejection = get_mcp_server_name_rejection(server_name) or get_reserved_mcp_rejection(
        server_name=server_name, url=url, public_url=public_url
    )
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

    # The spec recompute lives in core.mcp_attach so the Discord
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
    _require_admin(auth, action=DelegatedAction.AGENT_LIFECYCLE)
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
            subject=mcp_subject(auth, is_admin=auth.admin_for(DelegatedAction.AGENT_LIFECYCLE)),
            default_agent_name=runtime.deployment_default.agent_name,
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
    _require_admin(auth, action=DelegatedAction.AGENT_LIFECYCLE)
    agent = await resolve_setup_agent(
        runtime, auth, name=name, expected_ma_agent_id=expected_ma_agent_id
    )
    _reject_system_agent(agent)
    await archive_agent(
        runtime.client,
        agent.id,
        scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
    )
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
    await forget_archived_agent(
        runtime.session_factory, tenant_id=auth.tenant_id, name=name, agent=agent
    )


async def _capture_backend_source(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow | None,
    source: BackendAgent,
) -> BackendAgent:
    from daimon.adapters.mcp.tools._session_gate import session_card_gap
    from daimon.core.authz import Place
    from daimon.core.ma_resolver import new_resolver_cache
    from daimon.core.scope import ScopeContext
    from daimon.core.turn.admission import capture_backend_source
    from daimon.core.turn.deps import build_turn_deps

    if (
        origin is not None
        and await session_card_gap(
            runtime,
            auth,
            origin,
            tool_name="request_agent_backend_switch",
            require_public_context=True,
        )
        is not None
    ):
        raise channel_backend.AgentBackendRefused("source_unavailable")
    deps = build_turn_deps(
        runtime.settings,
        runtime.client,
        runtime.session_factory,
        deployment_default=runtime.deployment_default,
        resolver_cache=new_resolver_cache(),
        billing_config=None,
    )
    return await capture_backend_source(
        deps,
        source,
        context=ScopeContext(
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            platform=auth.platform,
            channel_id=origin.parent_channel_id if origin is not None else None,
            thread_id=origin.thread_id if origin is not None else None,
        ),
        subject=mcp_subject(auth, is_admin=auth.admin_for(DelegatedAction.AGENT_SETTINGS)),
        place=turn_origin_place(origin) if origin is not None else Place(),
        is_dm=False,
        slack_turn_context_id=None,
        private_dm_id=None,
    )


async def _set_agent_backend_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
    *,
    backend: Provider | None,
    expected_revision: int | None = None,
    preview_only: bool = False,
) -> dict[str, JsonValue]:
    _require_admin(auth, action=DelegatedAction.AGENT_SETTINGS)
    if auth.agent_id is not None:
        raise ToolError("An agent key cannot change model backends.")
    try:
        require_outside_chat_turn(auth)
    except ToolError as exc:
        raise ToolError(
            "Use request_agent_backend_switch in chat so the admin can confirm the preview."
        ) from exc
    if not channel_backend.backend_selection_enabled(runtime.settings.turn):
        raise ToolError("Model backend selection is disabled on this deployment.")
    service = channel_backend.make_backend_service(
        runtime.session_factory,
        runtime.settings.turn,
        capture_source=partial(_capture_backend_source, runtime, auth, None),
    )
    actor = channel_backend.BackendActor(
        tenant_id=auth.tenant_id,
        account_id=auth.account_id,
        subject=mcp_subject(auth, is_admin=auth.admin_for(DelegatedAction.AGENT_SETTINGS)),
    )
    try:
        agent = await channel_backend.load_backend_agent(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_name=name,
        )
        preview = await service.preview(agent, backend)
        if preview_only:
            return {
                "agent": name,
                "applied": False,
                "preview": cast(
                    dict[str, JsonValue],
                    TypeAdapter(type(preview)).dump_python(preview, mode="json"),
                ),
                "applies": "No setting was changed. Existing threads and channel overrides remain.",
            }
        current = await channel_backend.load_backend_agent(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_name=name,
        )
        local = preview.setting_revision if expected_revision is None else expected_revision
        if backend is None:
            revision = await service.clear_backend(
                current, local, actor, expected_preview_digest=preview.digest
            )
        else:
            revision = await service.set_backend(
                current, backend, local, actor, expected_preview_digest=preview.digest
            )
    except channel_backend.AgentBackendRefused as exc:
        raise ToolError(str(exc)) from exc
    return {
        "agent": name,
        "backend": revision.backend,
        "revision": revision.local,
        "preview": cast(
            dict[str, JsonValue],
            TypeAdapter(type(preview)).dump_python(preview, mode="json"),
        ),
        "applies": "New threads use this setting; existing threads and channel overrides remain.",
    }


class AgentBackendSwitchInfo(BaseModel):
    status: Literal["preview", "changed"]
    agent_name: str
    target_backend: Provider
    expected_revision: int
    preview_digest: str
    unsupported_skills: tuple[str, ...]
    unsupported_mcps: tuple[str, ...]
    unsupported_capabilities: tuple[str, ...]
    shared_thread_limits: tuple[str, ...]
    cost_line: str
    summary: str
    declared_drops: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)


async def _request_agent_backend_switch_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    target_backend: Provider,
    expected_revision: int | None = None,
    preview_digest: str | None = None,
    origin_context_id: str | None = None,
    accepted_drops: tuple[str, ...] | None = None,
) -> AgentBackendSwitchInfo:
    _require_admin(auth, action=DelegatedAction.AGENT_SETTINGS)
    if auth.agent_id is not None or auth.platform_user_id is None:
        raise ToolError("Request this change from an admin's active chat conversation.")
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    if origin is None:
        raise ToolError("Request this change from an admin's active chat conversation.")

    from daimon.adapters.mcp.tools._session_gate import session_card_gap
    from daimon.core.channel_backend import (
        BackendActor,
        load_backend_agent,
        make_backend_service,
    )
    from daimon.core.confirmation import BackendSwitchReference
    from pydantic import ValidationError

    try:
        service = make_backend_service(
            runtime.session_factory,
            runtime.settings.turn,
            capture_source=partial(_capture_backend_source, runtime, auth, origin),
        )
        if not service.enabled:
            raise ToolError("Model backend selection is disabled. Nothing changed.")
        gap = await session_card_gap(
            runtime, auth, origin, tool_name="request_agent_backend_switch"
        )
        if gap is not None:
            raise ToolError(
                "This conversation cannot verify the confirmation card. Nothing changed."
            )
        agent = await load_backend_agent(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_name=agent_name,
        )
        preview = await service.preview(agent, target_backend)
        if not preview.allowed:
            raise ToolError(
                "That model backend is not enabled for this workspace. Nothing changed."
            )
        status: Literal["preview", "changed"] = "preview"
        summary = (
            "Show this compatibility and cost preview. If they want to proceed, call "
            "request_agent_backend_switch again with its expected_revision and preview_digest; "
            "the requesting admin must Confirm on the card. Nothing has changed yet."
        )
        if preview_digest is not None:
            reference = BackendSwitchReference.model_validate(
                {
                    "agent": agent_name,
                    "target_backend": target_backend,
                    "expected_revision": expected_revision,
                    "preview_digest": preview_digest,
                    "accepted_drops": accepted_drops,
                }
            )
            # The card check awaited provider I/O; reread configuration at the write edge.
            agent = await load_backend_agent(
                runtime.client,
                runtime.session_factory,
                tenant_id=auth.tenant_id,
                agent_name=agent_name,
            )
            actor = BackendActor(
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                subject=mcp_subject(auth, is_admin=auth.admin_for(DelegatedAction.AGENT_SETTINGS)),
            )
            await service.set_backend(
                agent,
                target_backend,
                reference.expected_revision,
                actor,
                expected_preview_digest=reference.preview_digest,
                **(
                    {"accepted_drops": reference.accepted_drops}
                    if reference.accepted_drops is not None
                    else {}
                ),
            )
            status = "changed"
            summary = (
                "Model backend changed. Existing threads keep their backend; "
                "channel overrides still take priority in new threads."
            )
        return AgentBackendSwitchInfo(
            status=status,
            agent_name=agent.name,
            target_backend=preview.target_backend,
            expected_revision=preview.setting_revision,
            preview_digest=preview.digest,
            unsupported_skills=preview.unsupported_skills,
            unsupported_mcps=preview.unsupported_mcps,
            unsupported_capabilities=preview.unsupported_capabilities,
            shared_thread_limits=preview.shared_thread_limits,
            cost_line=preview.cost_line,
            summary=summary,
            declared_drops=preview.declared_drops,
        )
    except (DaimonError, ValidationError) as exc:
        raise ToolError(
            "The preview is unavailable or changed. Preview again; nothing changed."
        ) from exc


def register_agent_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    if channel_backend.backend_selection_enabled(runtime.settings.turn):

        @mcp.tool(tags={"admin"})
        async def set_agent_backend(  # pyright: ignore[reportUnusedFunction]
            ctx: Context,
            name: str,
            backend: Provider | None,
            expected_revision: int | None = None,
            preview_only: bool = False,
        ) -> dict[str, JsonValue]:
            """Set an enabled model backend, or clear the agent setting with backend=null.

            Admin only, for direct coding-tool or API requests. Chat turns use
            request_agent_backend_switch so the admin confirms the preview.
            The returned preview names compatibility limits and list prices.
            Existing threads keep their backend; channel overrides still apply.
            expected_revision refuses a setting changed since it was read.
            preview_only returns the compatibility and cost preview without applying it.
            """
            return await _set_agent_backend_impl(
                runtime,
                await _auth(ctx),
                name,
                backend=backend,
                expected_revision=expected_revision,
                preview_only=preview_only,
            )

    @mcp.tool
    async def list_agents(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, page: str | None = None, origin_context_id: str | None = None
    ) -> list[AgentInfo]:
        """List agents visible here, including their attached ``mcp_servers`` and ``skills``.

        ``mcp_servers`` and ``created_at`` are null for a caller who is neither an
        admin: setup details are
        for admins, capabilities (name, description, model, skills) are not. Members
        see the channel/thread responders and agents explicitly allowed to run
        here; unrouted agents and other channels' agents are hidden.
        Pass this turn's ``origin_context_id`` to verify its location. Without
        a verified location a member cannot list unrestricted agents. Admins
        retain their existing visibility. ``page`` is reserved for pagination.
        """
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
        Non-admin callers get null ``mcp_servers`` and ``created_at``.
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
        model: ModelSelection,
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
        model: ModelSelection | None = None,
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

    # The shared factory owns the enablement decision and validation.
    from daimon.core.channel_backend import backend_selection_enabled

    if backend_selection_enabled(runtime.settings.turn):

        @mcp.tool(tags={"admin"})
        async def request_agent_backend_switch(  # pyright: ignore[reportUnusedFunction]
            ctx: Context,
            agent_name: str,
            target_backend: Provider,
            expected_revision: int | None = None,
            preview_digest: str | None = None,
            origin_context_id: str | None = None,
            accepted_drops: tuple[str, ...] | None = None,
        ) -> AgentBackendSwitchInfo:
            """Preview a requested agent backend switch, then Confirm in this chat.

            Use when an admin asks to switch an agent to Anthropic, OpenAI or Gemini.
            First call without preview_digest and show the returned compatibility,
            shared-thread limits and cost line. Call again with that preview's
            expected_revision, preview_digest and exact declared_drops as accepted_drops:
            the requesting admin must Confirm
            on the card before anything is changed. A stale preview or expired card
            changes nothing; preview again. Pass this active turn's origin_context_id.
            """
            return await _request_agent_backend_switch_impl(
                runtime,
                await _auth(ctx),
                agent_name=agent_name,
                target_backend=target_backend,
                expected_revision=expected_revision,
                preview_digest=preview_digest,
                origin_context_id=origin_context_id,
                accepted_drops=accepted_drops,
            )
