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
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, cast, get_args

import anthropic
import anyio
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
from daimon.adapters.mcp.tools._owner import (
    caller_owned_ids,
    caller_owns_agent,
    may_own,
    owned_in,
    owner_labels,
)
from daimon.adapters.mcp.tools._pin_guard import require_pin_write_access
from daimon.adapters.mcp.tools._rule_view import (
    load_caller_view,
    load_isolation_view,
    load_skill_owners,
)
from daimon.adapters.mcp.tools._words import admin_noun, space_noun
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
from daimon.core.agent_ownership import agent_name_lock, member_copies_lock, record_agent_creator
from daimon.core.constants import AGENT_MCP_CAP, AGENT_SKILL_CAP, ALLOWED_MODEL_IDS
from daimon.core.continuity.messages import ConfigurationChange, render_change_confirmation
from daimon.core.defaults.ma_index import find_agents_by_daimon_tag, list_agents_by_tenant
from daimon.core.defaults.mcp_merge import DAIMON_MCP_SERVER_NAME, get_reserved_mcp_rejection
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_SETUP_HIDDEN,
)
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.defaults.reconcile_agents import AgentNameTaken, reconcile_agent
from daimon.core.defaults.report import Action as ReconcileAction
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
from daimon.core.specs import AgentSpec, SkillRepo, merge_default_agent_toolset
from daimon.core.stores.domain import BackendAgent, TurnOriginRow
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant
from daimon.core.tool_safety import get_mcp_server_name_rejection
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mux.contracts.config import DEFAULT_BACKEND, ThreadMode
from mux.contracts.ids import ModelRef, Provider
from mux.contracts.profile import Capability
from mux.profiles import selectable_models
from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError

if TYPE_CHECKING:
    from daimon.core.confirmation import BackendSwitchReference

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
    mcp_server_names: list[str] = Field(default_factory=list)
    """Every attached server's name, kept for a caller who may not see the URLs.
    A member's own copy withholds ``mcp_servers`` but not this: whether Daimon's
    built-in connection is there is a capability, not setup detail (J6)."""
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
    owned_by_you: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    """Set by ``get_agent`` and ``list_agents``: whether the caller owns this agent
    (created it, or an admin named them), so may change it without an admin."""
    owner: str | None = Field(default=None, exclude_if=lambda value: value is None)
    """Set by ``get_agent`` and ``list_agents``: who to ask about changing this agent:
    ``"you"``, the owner's name, or ``"another member"`` when no name is known.
    Absent when the agent has no owner: then only an admin changes it."""
    # The provider it answers on: its own provider setting, else Anthropic. When
    # that setting moved it off Anthropic, ``active_model`` is that provider's model;
    # ``model`` stays its own (Claude) model, the one update_agent changes.
    provider: Provider | None = Field(default=None, exclude_if=lambda value: value is None)
    active_model: str | None = Field(default=None, exclude_if=lambda value: value is None)

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
            mcp_server_names=[s.name for s in agent.mcp_servers],
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
        how the agent was set up, which is for admins. Only Daimon's own built-in
        connection keeps its name: whether it is there is a capability (J6), and
        no other connector of anyone's agent is named to a member.
        """
        return self.model_copy(
            update={
                "created_at": None,
                "mcp_servers": None,
                "sync_warnings": None,
                "mcp_server_names": [
                    name for name in self.mcp_server_names if name == DAIMON_MCP_SERVER_NAME
                ],
            }
        )


async def _with_providers(
    runtime: McpRuntime, auth: AuthIdentity, infos: list[AgentInfo]
) -> list[AgentInfo]:
    """Each agent's own provider setting, as the next conversation with it uses it."""
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores import agent_backends
    from mux.profiles import backend_selection

    if not infos:
        return infos
    ids = {
        info.id: derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=info.id) for info in infos
    }
    async with runtime.session_factory() as session:
        settings = await agent_backends.current_many(session, auth.tenant_id, set(ids.values()))
    placed: list[AgentInfo] = []
    for info in infos:
        setting = settings.get(ids[info.id])
        backend = setting.backend if setting is not None else None
        if setting is None or backend is None or backend == DEFAULT_BACKEND:
            placed.append(info.model_copy(update={"provider": DEFAULT_BACKEND}))
            continue
        active = setting.model or backend_selection(backend).model
        placed.append(info.model_copy(update={"provider": backend, "active_model": active}))
    return placed


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
            "Anyone talking to it can make their own editable copy with fork_agent (an "
            "admin can copy it from anywhere), or create_agent a new one."
        )
    if agent.metadata.get(MA_METADATA_KEY_ACCOUNT) is None:
        return (
            f"agent '{agent.name}' is a system agent; chat tools cannot modify it. "
            "Anyone talking to it can make their own editable copy with fork_agent (an "
            "admin can copy it from anywhere), or create_agent a new one."
        )
    return None


def _setup_hidden(agent: BetaManagedAgentsAgent, auth: AuthIdentity) -> bool:
    """A member's own copy of an agent they talked to: its inherited setup stays an
    admin's to read, as on the source, though they own it."""
    return not auth.is_admin and agent.metadata.get(MA_METADATA_KEY_SETUP_HIDDEN) == "true"


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
        raise ToolError(
            f"agent '{name}' already exists in this {space_noun(auth.platform)} — pick another name"
        )


async def _free_agent_name(runtime: McpRuntime, auth: AuthIdentity, name: str) -> str:
    """`name`, or the first of `name-2`, `name-3`... that no live agent here holds."""
    rows = await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    taken = {row.metadata.get(MA_METADATA_KEY_NAME) for row in rows}
    candidate, n = name, 1
    while candidate in taken:
        n += 1
        candidate = f"{name}-{n}"
    return candidate


async def _list_agents_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    page: str | None,
    origin_context_id: str | None = None,
    mine: bool = False,
) -> list[AgentInfo]:
    del page
    rows = await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    owned = await caller_owned_ids(runtime, auth)
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    caller = await load_caller_view(
        runtime,
        auth,
        agents=rows,
        location_channel_id=origin_channel_id(origin),
        location_thread_id=origin.thread_id if origin is not None else None,
    )
    # An owner always sees their own agents, routed anywhere or nowhere.
    rows = [
        row
        for row in rows
        if (owned_in(owned, auth, row.id) or (not mine and caller.sees_agent(row)))
    ]
    skill_titles, _truncated = await resolve_custom_skill_titles(
        runtime.client, agents=rows, tenant_id=auth.tenant_id
    )
    owners = await owner_labels(runtime, auth, [a.id for a in rows])
    infos = [
        AgentInfo.from_ma(a, skill_titles=skill_titles).model_copy(
            update={"owned_by_you": owned_in(owned, auth, a.id), "owner": owners.get(a.id)}
        )
        for a in rows
    ]
    infos = await _with_providers(runtime, auth, infos)
    if auth.admin_for(DelegatedAction.AGENT_SETTINGS):
        return infos
    hidden = {row.id for row in rows if _setup_hidden(row, auth)}
    return [
        info if info.owned_by_you and info.id not in hidden else info.for_member() for info in infos
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
    # The prompt is readable only by callers who could replace it: admins and the
    # agent's owner, on an agent chat tools may edit at all. A defaults-managed or system
    # agent's prompt stays withheld even from admins — the edit path is a fork,
    # and the fork's own prompt is then readable.
    owned = await caller_owns_agent(runtime, auth, agent.id)
    sees_setup = auth.admin_for(DelegatedAction.AGENT_SETTINGS) or (
        owned and not _setup_hidden(agent, auth)
    )
    if sees_setup and _system_agent_rejection(agent) is None:
        info = info.model_copy(update={"system": agent.system or ""})
    if not sees_setup:
        info = info.for_member()
    owner = (await owner_labels(runtime, auth, [agent.id])).get(agent.id)
    (info,) = await _with_providers(runtime, auth, [info])
    return info.model_copy(update={"owned_by_you": owned, "owner": owner})


#: The built-in tools an agent's toolset can turn on or off (Managed Agents' names).
BuiltinToolName = Literal[
    "bash", "edit", "read", "write", "glob", "grep", "web_fetch", "web_search"
]
_TOOLSET_TYPE: Final[str] = "agent_toolset_20260401"


def _builtin_enabled(tools: list[Tool], tool: str) -> bool | None:
    """Whether any of the agent's toolsets turns `tool` on; None when it has none."""
    found: bool | None = None
    for entry in tools:
        if not str(entry.get("type", "")).startswith("agent_toolset"):
            continue
        raw = cast(dict[str, Any], entry)
        default = cast(dict[str, Any], raw.get("default_config") or {}).get("enabled")
        on = default is not False
        for config in raw.get("configs") or ():
            if config.get("name") == tool:
                enabled = config.get("enabled")
                on = bool(enabled) if enabled is not None else default is not False
                break
        found = on or bool(found)
    return found


def _with_builtin(tools: list[Tool], tool: str, enabled: bool) -> list[Tool]:
    """`tools` with `tool` turned on or off; any permission policy it had is kept.

    An agent with no toolset gains one holding only `tool`, never the defaults.
    """
    if _builtin_enabled(tools, tool) is None:
        only: Tool = cast(
            Tool,
            {
                "type": _TOOLSET_TYPE,
                "default_config": {"enabled": False},
                "configs": [{"name": tool, "enabled": enabled}],
            },
        )
        return [*tools, only]
    updated: list[Tool] = []
    for entry in tools:
        if not str(entry.get("type", "")).startswith("agent_toolset"):
            updated.append(entry)
            continue
        raw = dict(cast(dict[str, Any], entry))
        configs = [dict(config) for config in raw.get("configs") or ()]
        for config in configs:
            if config.get("name") == tool:
                config["enabled"] = enabled
                break
        else:
            configs.append({"name": tool, "enabled": enabled})
        raw["configs"] = configs
        updated.append(cast(Tool, raw))
    return updated


_BUILTIN_NAMES: Final[tuple[str, ...]] = get_args(BuiltinToolName)


def _denied_builtins(runtime: McpRuntime) -> frozenset[str]:
    """Built-ins whoever runs Daimon forbids: ``builtin/<name>`` in tool-safety ``denied``.

    Only that explicit form: a bare name there keeps meaning an MCP tool.
    """
    denied = runtime.settings.tool_safety.denied
    return frozenset(name for name in _BUILTIN_NAMES if f"builtin/{name}" in denied)


def refuse_forbidden_builtins(runtime: McpRuntime, before: list[Tool], after: list[Tool]) -> None:
    """Refuse a change that turns on a built-in whoever runs Daimon forbids.

    The one check create_agent, update_agent and set_agent_builtin_tool share.
    """
    turned_on = sorted(
        name
        for name in _denied_builtins(runtime)
        if _builtin_enabled(after, name) and not _builtin_enabled(before, name)
    )
    if turned_on:
        raise ToolError(
            f"Whoever runs Daimon has turned {', '.join(turned_on)} off for every agent "
            "here. Nothing changed."
        )


def _explicitly_on(tools: list[Tool], tool: str) -> bool:
    """Whether a toolset in `tools` names `tool` and turns it on (not just by default)."""
    for entry in tools:
        if str(entry.get("type", "")).startswith("agent_toolset"):
            for config in cast(dict[str, Any], entry).get("configs") or ():
                if config.get("name") == tool and config.get("enabled") is True:
                    return True
    return False


def _forbidden_builtins_off(runtime: McpRuntime, tools: list[Tool]) -> list[Tool]:
    """A new agent's default toolset with every forbidden built-in written as off."""
    denied = _denied_builtins(runtime)
    if not denied:
        return tools
    tools = merge_default_agent_toolset(tools)
    for name in sorted(denied):
        tools = _with_builtin(tools, name, False)
    return tools


async def _set_agent_builtin_tool_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
    *,
    tool: BuiltinToolName,
    enabled: bool,
    expected_ma_agent_id: str | None = None,
    origin_context_id: str | None = None,
) -> AgentInfo:
    """Turn one built-in tool on or off, with update_agent's authorization."""
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
    await reachability.require_admin_for_reachable_agent(
        runtime, auth, agent_name=name, agent=agent
    )
    current = [_ma_tool_to_param(t) for t in agent.tools]
    was = _builtin_enabled(current, tool)
    if enabled:
        if auth.anthropic_disabled:
            raise ToolError(
                "This server runs its own model, so built-in tools can't be turned on here. "
                "Nothing changed."
            )
        refuse_forbidden_builtins(runtime, current, _with_builtin(current, tool, True))
    if (was or False) == enabled:
        state = "on" if enabled else "off"
        reason = "" if was is not None else f" ({name} has no built-in tools)"
        info = await _build_agent_info(runtime.client, agent, tenant_id=auth.tenant_id)
        return info.model_copy(
            update={"applies": f"{tool} is already {state} for {name}{reason}. Nothing changed."}
        )

    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        tools = _with_builtin([_ma_tool_to_param(t) for t in fresh.tools], tool, enabled)
        return await update_agent(
            runtime.client,
            fresh.id,
            version=fresh.version,
            payload={"tools": tools},
            scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
        )

    try:
        updated = await update_agent_with_version_retry(
            runtime.client,
            agent.id,
            _apply,
            scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
        )
    except anthropic.ConflictError as exc:
        raise ToolError("the agent was modified concurrently — please retry the operation") from exc
    info = await _build_agent_info(runtime.client, updated, tenant_id=auth.tenant_id)
    if _setup_hidden(updated, auth):
        info = info.for_member()
    (info,) = await _with_providers(runtime, auth, [info])
    applies = f"{tool} is now {'on' if enabled else 'off'} for {name}, from its next message."
    if info.provider is not None and info.provider != DEFAULT_BACKEND:
        from daimon.core.turn.notices import provider_name

        applies += (
            f" It answers on {provider_name(info.provider)} now, which runs without built-in "
            "tools; this applies when it's back on Claude."
        )
    return info.model_copy(update={"applies": applies})


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


async def _move_this_thread(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow,
    agent_name: str,
    target: Provider,
    *,
    pinned_here: bool,
) -> str:
    """After a confirmed switch, move the requester's own slot of this thread too."""
    from daimon.core.switch_cards import move_this_thread

    return await move_this_thread(
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        account_id=auth.account_id,
        platform=origin.platform,
        parent_channel_id=origin.parent_channel_id,
        thread_id=origin.thread_id,
        responder_name=origin.responder_name,
        agent_name=agent_name,
        target=target,
        pinned_here=pinned_here,
    )


def _resolve_native_agent_model(model: ModelSelection) -> ModelSelection:
    """Native agent edits share the alias resolver without changing model controls."""
    choice = model_choice_id(model)
    if choice in ALLOWED_MODEL_IDS:
        return model
    from daimon.core.agent_model_names import agent_catalog, model_name, resolve_model
    from daimon.core.turn.notices import provider_name
    from mux.profiles import MANAGED_AGENTS

    choices = tuple(
        entry.model for entry in agent_catalog() if entry.model.provider == MANAGED_AGENTS.provider
    )
    try:
        resolved = resolve_model(choice, available=choices).id
    except ValueError as exc:
        # Another provider's model: say how to get there instead of a dead end.
        with contextlib.suppress(ValueError):
            other = resolve_model(choice)
            if other.provider != MANAGED_AGENTS.provider:
                raise ToolError(
                    f"{model_name(other)} is a {provider_name(other.provider)} model. Move "
                    f"an existing agent to it with request_agent_backend_switch "
                    f"(target_backend={other.provider}, model={other.id}); create_agent "
                    "makes a new agent on it directly. Nothing was changed."
                ) from exc
        if "could mean" not in str(exc):
            # Unknown (not ambiguous): the refusal lists what this deployment offers.
            _reject_unknown_model(model)
        raise ToolError(str(exc)) from exc
    return resolved if isinstance(model, str) else {**model, "id": resolved}


def _create_model(model: ModelSelection) -> tuple[ModelSelection, tuple[Provider, str] | None]:
    """create_agent's model: a Claude model as it is, or another provider's model by its
    name or id ("flash", "luna", "gemini-4-argon"), returned as the backend to start on.

    An agent's definition (prompt, tools, skills) is a Managed Agents record, so one
    on another provider is saved on the default Claude model as its source only; its
    backend revision is what every turn runs on.
    """
    from daimon.core.agent_model_names import resolve_model
    from daimon.core.constants import DEFAULT_AGENT_MODEL
    from mux.profiles import MANAGED_AGENTS

    choice = model_choice_id(model)
    if choice in ALLOWED_MODEL_IDS:
        return model, None
    try:
        other = resolve_model(choice)
    except ValueError:
        return model, None  # the Claude path names the choices
    if other.provider == MANAGED_AGENTS.provider:
        return model, None
    if not isinstance(model, str):
        raise ToolError(
            "Model options apply only to Claude models: pass this model by name. "
            "Nothing was created."
        )
    return DEFAULT_AGENT_MODEL, (other.provider, other.id)


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
    model = _resolve_native_agent_model(model)
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
            "and ensure every mcp_servers entry has a matching mcp_toolset entry. "
            "Nothing was saved."
        ) from exc


async def _start_on_backend(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow | None,
    name: str,
    backend: Provider,
    model: str,
) -> str:
    """Give a just-created agent its backend revision, through the switch's own preview
    and write (policy, enabled providers, selectable models, what it can't carry).

    No card: the agent is brand new, so nobody else's conversations change, and the
    person asked for that provider. What it won't have there is said in the answer.
    """
    from daimon.core.agent_model_names import model_name
    from daimon.core.confirmation import drop_words
    from daimon.core.turn.notices import provider_name
    from mux.contracts.ids import ModelRef

    del origin  # A new agent answers nowhere yet: its own source is the one read.
    service = channel_backend.make_backend_service(
        runtime.session_factory,
        runtime.settings.turn,
        capture_source=partial(_capture_backend_source, runtime, auth, None),
    )
    actor = channel_backend.BackendActor(
        tenant_id=auth.tenant_id,
        account_id=auth.account_id,
        subject=mcp_subject(auth, is_admin=auth.is_admin),
    )
    try:
        agent = await channel_backend.load_backend_agent(
            runtime.client, runtime.session_factory, tenant_id=auth.tenant_id, agent_name=name
        )
        # Its owner (the creator, recorded above) or an admin, as the write re-checks
        # (from the latent audit's dsh patch: a plain refusal, not a write-time one).
        await _require_agent_editor(service, agent, actor, auth)
        preview = await service.preview(agent, backend, model)
        if not preview.allowed:
            raise ToolError(f"{provider_name(backend)} isn't turned on here. Nothing was created.")
        await service.set_backend(
            agent,
            backend,
            preview.setting_revision,
            actor,
            expected_preview_digest=preview.digest,
            model=model,
            accepted_drops=preview.declared_drops,
        )
    except (channel_backend.AgentBackendRefused, channel_backend.BackendUnsupported) as exc:
        # It was saved on the default provider and hasn't moved yet (dsh).
        refusal = str(_backend_switch_refusal(exc, name, backend, auth, current=DEFAULT_BACKEND))
        raise ToolError(
            refusal.removesuffix(" Nothing changed.") + " Nothing was created."
        ) from exc
    words = f"{name} runs on {model_name(ModelRef(provider=backend, id=model))}."
    if preview.declared_drops:
        words += f" On {provider_name(backend)} it won't have {drop_words(preview.declared_drops)}."
    return words


async def _create_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    spec: AgentSpec,
    origin_context_id: str | None = None,
    *,
    backend: tuple[Provider, str] | None = None,
) -> AgentInfo:
    """Create `spec`; `backend` (from `_create_model`) starts it on another provider."""
    from daimon.core.turn.notices import provider_name

    if spec.mcp_servers and not may_own(auth):
        _require_admin(auth)
    if spec.skills:
        raise ToolError(
            "create_agent: skills must be empty. To add skills, either sync a "
            "repo via skill_repos or use sync_skills after the agent "
            "is created."
        )
    if backend is not None:
        # Before anything is created: the switch's own model and provider gates.
        if not channel_backend.backend_selection_enabled(runtime.settings.turn):
            raise ToolError(
                "Choosing a model provider is turned off here, so only Claude models "
                "are available. Nothing was created."
            )
        _require_selectable_model(*backend)
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    await require_agent_creatable(
        runtime, auth, origin=turn_origin_place(origin) if origin is not None else None
    )
    started: str | None = None
    if _denied_builtins(runtime):
        # Asked for by name: refused. Only on by default: written as off.
        asked = [
            name for name in _denied_builtins(runtime) if _explicitly_on(spec.tools or [], name)
        ]
        if asked:
            raise ToolError(
                f"Whoever runs Daimon has turned {', '.join(sorted(asked))} off for every "
                "agent here. Nothing changed."
            )
        spec = spec.model_copy(
            update={"tools": _forbidden_builtins_off(runtime, list(spec.tools or []))}
        )
        # The toolset the create sends (the base one added if missing) turns none on.
        refuse_forbidden_builtins(runtime, [], merge_default_agent_toolset(spec.tools))
    owner = may_own(auth)
    async with contextlib.AsyncExitStack() as stack:
        if owner:
            # Check and create under one lock, so a concurrent creator of the same
            # name is refused instead of owning (or rewriting) this agent.
            await stack.enter_async_context(
                agent_name_lock(runtime.session_factory, tenant_id=auth.tenant_id, name=spec.name)
            )
        await _reject_guild_name_collision(runtime, auth, spec.name)
        public_url = (
            str(runtime.settings.mcp.public_url)
            if runtime.settings.mcp.public_url is not None
            else None
        )
        try:
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
                # Never update an agent that took the name since the check above.
                create_only=True,
            )
        except AgentNameTaken as exc:
            raise ToolError(
                f"agent '{spec.name}' already exists in this {space_noun(auth.platform)} "
                "— pick another name"
            ) from exc
        if outcome.anthropic_id is None:
            raise ToolError("create_agent: reconcile returned no agent id — report this as a bug")
        created = outcome.action == ReconcileAction.CREATED
        # From here on an agent on another provider exists only as the Claude record its
        # definition is saved with: anything that stops it starting there (a refusal,
        # an error, a cancellation) removes it, so it never runs or bills on Claude.
        try:
            if owner and created:
                # The person who created it owns it: they may edit it without an admin.
                await record_agent_creator(
                    runtime.session_factory,
                    tenant_id=auth.tenant_id,
                    agent_id=derive_agent_uuid(
                        tenant_id=auth.tenant_id, ma_agent_id=outcome.anthropic_id
                    ),
                    account_id=auth.account_id,
                )
            if backend is not None and created:
                started = await _start_on_backend(
                    runtime, auth, origin, spec.name, backend[0], backend[1]
                )
        except BaseException as exc:
            if backend is None or not created:
                raise
            # Shielded: a cancellation (anyio's re-cancels every await) must not cancel
            # the removal too; it completes, then the cancellation goes on.
            removed = False
            with anyio.CancelScope(shield=True):
                removed = await _discard_created(runtime, auth, outcome.anthropic_id)
            if not removed and isinstance(exc, Exception):
                raise ToolError(
                    f"{spec.name} couldn't start on {provider_name(backend[0])}, and removing "
                    "it failed too, so it may still exist on Claude. Ask an admin to archive "
                    f"{spec.name}."
                ) from exc
            raise
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
    if backend is not None:
        info = info.model_copy(update={"applies": started})
    # Read back as get_agent and list_agents read it: ``provider`` and, off Claude,
    # the ``active_model`` it runs on; ``model`` stays its own Claude model.
    (info,) = await _with_providers(runtime, auth, [info])
    # Say whether the creator was recorded as its owner, read back as get_agent
    # reads it: recording fails closed, so a miss reads as not owned.
    owned = await caller_owns_agent(runtime, auth, ma_agent.id)
    owner = (await owner_labels(runtime, auth, [ma_agent.id])).get(ma_agent.id)
    info = info.model_copy(update={"owned_by_you": owned, "owner": owner})
    return await _with_answering_note(runtime, auth, info)


async def source_backend_for_copy(
    runtime: McpRuntime, auth: AuthIdentity, *, source_name: str, source_ma_agent_id: str
) -> tuple[Provider, str] | None:
    """The provider and model a copy of this agent must start on (sweep item 3), or None
    for an agent on Claude. Read BEFORE the copy is made, so a failed read (or provider
    choice being off) leaves nothing behind."""
    from daimon.core.stores import agent_backends
    from daimon.core.turn.notices import provider_name
    from mux.profiles import backend_selection

    async with runtime.session_factory() as session:
        revision = await agent_backends.current(
            session,
            auth.tenant_id,
            derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=source_ma_agent_id),
        )
    backend = revision.backend if revision is not None else None
    if backend is None or backend == DEFAULT_BACKEND:
        return None
    assert revision is not None
    if not channel_backend.backend_selection_enabled(runtime.settings.turn):
        raise ToolError(
            f"{source_name} runs on {provider_name(backend)}, and choosing a model provider "
            "is turned off here, so a copy of it can't start there. Nothing was created."
        )
    return backend, revision.model or backend_selection(backend).model or ""


async def start_copy_on_source_backend(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    carry: tuple[Provider, str] | None,
    copy_name: str,
    copy_ma_agent_id: str,
    archive_on_failure: bool = True,
) -> str | None:
    """Start a just-made copy on `carry` (`source_backend_for_copy`), sweep item 3.

    A copy's definition is a Managed Agents record, so without this a copy of a GPT or
    Gemini agent silently answered (and billed) on Claude. Through the switch's own
    preview and write (`_start_on_backend`). If it can't start there, the copy is
    archived (shielded, like create's) and the person is told plainly. Returns what
    to say about where it runs, or None for a copy of a Claude agent.
    """
    from daimon.core.turn.notices import provider_name

    if carry is None:
        return None
    backend, model = carry
    try:
        return await _start_on_backend(runtime, auth, None, copy_name, backend, model)
    except BaseException as exc:
        if not archive_on_failure:
            raise  # the caller archives it (a channel's copy: `_archive_unused_copy`)
        removed = False
        with anyio.CancelScope(shield=True):
            removed = await _discard_created(runtime, auth, copy_ma_agent_id)
        if not removed and isinstance(exc, Exception):
            raise ToolError(
                f"{copy_name} couldn't start on {provider_name(backend)}, and removing it "
                "failed too, so it may still exist on Claude. Ask an admin to archive "
                f"{copy_name}."
            ) from exc
        raise


async def _discard_created(runtime: McpRuntime, auth: AuthIdentity, ma_agent_id: str) -> bool:
    """Archive an agent create_agent made in this call and couldn't finish; False when
    the archive itself failed (logged at error: the agent may still be live)."""
    try:
        await archive_agent(
            runtime.client,
            ma_agent_id,
            scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
        )
    except Exception as exc:
        log.error(
            "create_agent.discard_failed",
            agent_id=ma_agent_id,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return False
    return True


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
        model = _resolve_native_agent_model(model)
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
    if mcp_servers is not None:
        await _require_connection_editor(runtime, auth, agent)
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
                    f"Cannot attach connections: the merged set ({merged_mcp_count}) "
                    f"exceeds this organization's per-agent connection limit ({AGENT_MCP_CAP}). "
                    f"Nothing was changed on '{name}'. Attach fewer, or use "
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
        if "tools" in patch:
            # On the final tools: a skill's base toolset can turn built-ins on too.
            refuse_forbidden_builtins(
                runtime, [_ma_tool_to_param(t) for t in fresh.tools], patch["tools"]
            )
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
    if _setup_hidden(updated, auth):
        # The owner's edit answers with capabilities only: never the inherited setup.
        result = result.for_member()
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


async def _require_connection_editor(
    runtime: McpRuntime, auth: AuthIdentity, agent: BetaManagedAgentsAgent
) -> None:
    """Keep native admin exemptions and the independent ownership right separate."""
    if auth.admin_for() or await caller_owns_agent(runtime, auth, agent.id):
        return
    raise ToolError("Connections require a platform admin or this agent's owner.")


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
        owned=await caller_owns_agent(runtime, auth, agent.id),
    )
    if outcome != "allow":
        raise ToolError(
            f"'{agent.name}' already has {', '.join(repr(n) for n in replaced)} at another URL "
            f"and is shared, so repointing it needs its owner or {admin_noun(auth.platform)}, "
            "and the caller "
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
    await _require_connection_editor(runtime, auth, agent)
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
    info = await _build_agent_info(runtime.client, updated, tenant_id=auth.tenant_id)
    return info.for_member() if _setup_hidden(updated, auth) else info


async def _require_member_copy_room(runtime: McpRuntime, auth: AuthIdentity) -> None:
    """A member may own only so many copies they made by talking to an agent."""
    limit = runtime.settings.mcp.member_copy_limit
    owned = await caller_owned_ids(runtime, auth)
    agents = await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    mine = [
        agent
        for agent in agents
        if owned_in(owned, auth, agent.id)
        and agent.metadata.get(MA_METADATA_KEY_SETUP_HIDDEN) == "true"
    ]
    if len(mine) >= limit:
        raise ToolError(
            f"You already have {len(mine)} copies of your own (the most is {limit}). "
            f"Ask {admin_noun(auth.platform)} to archive one you no longer use, or to "
            "make this copy. "
            "Nothing was created."
        )


async def _runs_on_model(
    runtime: McpRuntime, auth: AuthIdentity, agent: BetaManagedAgentsAgent
) -> str:
    """The model `agent` runs on: its backend revision's, else its own Managed Agents
    model (an agent made on another provider is saved on a Claude record)."""
    from daimon.core.stores import agent_backends
    from mux.profiles import backend_selection

    async with runtime.session_factory() as session:
        revision = await agent_backends.current(
            session,
            auth.tenant_id,
            derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=agent.id),
        )
    if revision is not None and revision.backend not in (None, DEFAULT_BACKEND):
        return revision.model or backend_selection(revision.backend).model or ""
    return model_choice_id(agent.model)


async def _fork_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    source_name: str,
    new_name: str,
    expected_ma_agent_id: str | None = None,
    new_copy_if_taken: bool = False,
    origin_context_id: str | None = None,
    model: str | None = None,
) -> AgentInfo:
    from daimon.adapters.mcp.tools.task_continuity import forked_in_value
    from daimon.core.defaults.metadata import MA_METADATA_KEY_FORKED_FROM, MA_METADATA_KEY_FORKED_IN

    # A model named for the copy is checked before any permission gate: an unavailable
    # model must read as unavailable, never as "an admin has to do this" (J7).
    requested: ModelRef | None = None
    if model is not None:
        from daimon.core.agent_model_names import resolve_model

        try:
            requested = resolve_model(model)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        # A saved copy keeps its source's model. The source is
        # read as the caller sees it, so this names nothing they couldn't see.
        named = await resolve_setup_agent(
            runtime, auth, name=source_name, expected_ma_agent_id=expected_ma_agent_id
        )
        if requested.id != await _runs_on_model(runtime, auth, named):
            from daimon.core.agent_model_names import model_name

            raise ToolError(
                f"A saved copy keeps {source_name}'s model; it can't become "
                f"{model_name(requested)}. Make the copy, then request a model provider switch "
                "for the copy. Nothing was created."
            )
    # Forking another agent is an admin's call: a fork routes nowhere and has no
    # channel pin of its own, so it is free to run anywhere the source can't. Anyone
    # may copy the agent answering their own chat turn (it is theirs to own; TARGET-V2
    # J10), so only a person's own turn credential gets that far.
    if not auth.admin_for(DelegatedAction.AGENT_LIFECYCLE) and (
        auth.chat_agent_id is None or not may_own(auth)
    ):
        _require_admin(auth, action=DelegatedAction.AGENT_LIFECYCLE)
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    public_url = runtime.settings.mcp.public_url
    async with contextlib.AsyncExitStack() as stack:
        # Pick, check and create under the name's lock, so two copies at once
        # never both take the same name.
        await stack.enter_async_context(
            agent_name_lock(runtime.session_factory, tenant_id=auth.tenant_id, name=new_name)
        )
        if new_copy_if_taken:
            # "Make a copy of yourself" wants a new copy, never an earlier one.
            picked = await _free_agent_name(runtime, auth, new_name)
            if picked != new_name:
                # A creator naming `picked` outright locks that name, not ours.
                await stack.enter_async_context(
                    agent_name_lock(runtime.session_factory, tenant_id=auth.tenant_id, name=picked)
                )
                new_name = picked
        await _reject_guild_name_collision(runtime, auth, new_name)
        source = await resolve_setup_agent(
            runtime, auth, name=source_name, expected_ma_agent_id=expected_ma_agent_id
        )
        # From the turn's credential, never a model-supplied id.
        answering = (
            auth.chat_agent_id is not None
            and auth.agent_id is None
            and auth.hold is None
            and derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=source.id)
            == auth.chat_agent_id
        )
        if not auth.is_admin and not answering:
            _require_admin(auth)  # pyright: ignore[reportPrivateUsage]
        if not auth.is_admin:
            # Held through the create: two copies at once never both fit the cap.
            await stack.enter_async_context(
                member_copies_lock(
                    runtime.session_factory, tenant_id=auth.tenant_id, account_id=auth.account_id
                )
            )
            await _require_member_copy_room(runtime, auth)
        # Before the copy exists: where it must start (a failed read leaves nothing).
        carry = await source_backend_for_copy(
            runtime, auth, source_name=source_name, source_ma_agent_id=source.id
        )
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
                # Whoever made the copy owns it.
                owner=auth.account_id if may_own(auth) else None,
                # Made from a conversation: switching it from there hands it that
                # conversation in the same step (J1, `copy_awaiting_its_switch`).
                extra_metadata={
                    MA_METADATA_KEY_FORKED_FROM: source.id,
                    MA_METADATA_KEY_FORKED_IN: forked_in_value(origin),
                }
                if origin is not None
                else None,
                answering=answering,
                hold=auth.hold,
                # A member's copy keeps the source's setup an admin's to read.
                hide_setup=not auth.is_admin,
            )
        except DaimonError as exc:
            raise ToolError(f"fork_agent: {exc} Nothing was created. Do not retry.") from exc
        # A copy of an agent on GPT or Gemini starts there too, never on Claude.
        started = await start_copy_on_source_backend(
            runtime, auth, carry=carry, copy_name=new_name, copy_ma_agent_id=copy.agent.id
        )
    info = await _build_agent_info(runtime.client, copy.agent, tenant_id=auth.tenant_id)
    if started is not None:
        info = info.model_copy(update={"applies": started})
    if _setup_hidden(copy.agent, auth):
        info = info.for_member()
    # As get/list read it back: model = its own record; provider + active_model = where
    # it runs (#1479's rule).
    (info,) = await _with_providers(runtime, auth, [info])
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
    *,
    self_posted: bool = False,
) -> BackendAgent:
    from daimon.adapters.mcp.tools._session_gate import session_card_gap
    from daimon.core.authz import Place
    from daimon.core.ma_resolver import new_resolver_cache
    from daimon.core.scope import ScopeContext
    from daimon.core.turn.admission import capture_backend_source
    from daimon.core.turn.deps import build_turn_deps

    if self_posted:
        # No provider session to read the card gate from: the verified origin
        # itself must be a public conversation, as for an admitted Claude card.
        if origin is None or _private_origin(origin):
            raise channel_backend.AgentBackendRefused("source_context_unavailable")
    elif origin is not None and (
        gap := await session_card_gap(
            runtime,
            auth,
            origin,
            tool_name="request_agent_backend_switch",
            require_public_context=True,
        )
    ):
        # The gap names the cause, in the refusal log line and its copy.
        raise channel_backend.AgentBackendRefused("source_context_unavailable", detail=gap)
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
        responder_ma_agent_id=origin.responder_ma_agent_id if origin is not None else None,
    )


#: What a target provider can't carry, in plain words (closed obligation labels).
_MISSING_WORDS: Final[Mapping[str, str]] = {
    "credentials": "a saved key that one of its kept connections needs",
    "instructions": "instructions that can't be carried over unchanged",
    "source_definition": "a setting or attachment that provider can't represent",
    "source_context": "setup that couldn't be read completely",
    "source_representation": "a saved version for that provider that is out of date",
    "tools": "tools that must ask before publishing",
    "model_options": "model options that provider doesn't take",
}

#: Why a source read failed for a reason that a retry won't fix.
_SOURCE_DETAIL_WORDS: Final[Mapping[str, str]] = {
    "identity": "this request doesn't come from the account that can change it here",
    "source": "its saved setup doesn't match the agent that's live now. Ask its owner to "
    "check it still exists",
    "environment": "its working environment is missing or archived",
    "place": "this conversation's channel couldn't be verified",
}

#: Why this conversation can't show the switch's Confirm card (a `CardGap`).
_CARD_GAP_WORDS: Final[Mapping[str, str]] = {
    "source_context_unavailable": (
        "I can't confirm a change from a direct message or from a conversation that "
        "started somewhere else. Ask in a server channel or thread where I answer."
    ),
    "no_live_session": (
        "I couldn't find my conversation in this thread, so I can't post the Confirm card. "
        "Mention me again here, or in a new thread."
    ),
    "other_agents_session": (
        "Another agent's conversation runs in this thread, so I can't post the Confirm "
        "card here. Mention me in a new thread."
    ),
    "session_not_gated": (
        "This thread's conversation started without the Confirm card for this change. "
        "Mention me in a new thread."
    ),
    "session_unreadable": (
        "I couldn't check this conversation just now, so I can't post the Confirm card. "
        "Try again in a minute."
    ),
}


def _backend_switch_refusal(
    exc: DaimonError | ValidationError,
    agent_name: str,
    target: Provider | None,
    auth: AuthIdentity,
    *,
    with_code: bool = False,
    current: Provider | None = None,
) -> ToolError:
    """The real reason a provider change was refused, and what the requester can do.

    ``with_code`` appends the stable reason for direct API and coding-tool callers.
    Never shows an exception type or an internal string.
    """
    from daimon.core.turn.notices import provider_name, switched_meanwhile_text

    refused = exc if isinstance(exc, channel_backend.AgentBackendRefused) else None
    reason = (
        refused.reason
        if refused is not None
        else "invalid_request"
        if isinstance(exc, ValidationError)
        else "backend_unsupported"
        if isinstance(exc, channel_backend.BackendUnsupported)
        else "internal"
    )
    log.warning(
        "agent_backend_switch_refused",
        agent=agent_name,
        target=target,
        reason=reason,
        error_type=type(exc).__name__,
        missing=list(getattr(refused, "missing", ())),
        detail=getattr(refused, "detail", None),
    )
    provider = provider_name(target or getattr(refused, "provider", None))
    admin = admin_noun(auth.platform)
    space = space_noun(auth.platform)
    if reason == "backend_unsupported":
        # Only the copy written for people passes through; internal labels never do.
        said = str(exc)
        text = (
            said
            if said.endswith(".") and " " in said and "_" not in said
            else f"{provider} can't run here right now. Pick another, or ask whoever runs Daimon."
        )
    elif reason == "source_unsupported":
        missing: tuple[str, ...] = getattr(refused, "missing", ())
        things = [_MISSING_WORDS[item] for item in missing if item in _MISSING_WORDS]
        text = (
            f"'{agent_name}' can't move to {provider}: it has "
            + ("; ".join(things) if things else "something that provider can't carry")
            + ". Its owner can remove that from the agent, or keep it on "
            + (provider_name(current) if current is not None else "its current provider")
            + "."
        )
    elif reason == "source_context_unavailable":
        text = _CARD_GAP_WORDS.get(
            getattr(refused, "detail", None) or reason,
            _CARD_GAP_WORDS["source_context_unavailable"],
        )
    elif reason == "source_unavailable":
        detail: str | None = getattr(refused, "detail", None)
        text = (
            f"I can't change '{agent_name}' from here: {_SOURCE_DETAIL_WORDS[detail]}."
            if detail in _SOURCE_DETAIL_WORDS
            else f"I couldn't read '{agent_name}''s current setup (the provider didn't "
            "answer in time). Try again in a minute."
        )
    else:
        text = {
            "agent_not_found": f"There's no agent named '{agent_name}' here. Ask me to list them.",
            "tenant_mismatch": (
                f"'{agent_name}' belongs to another {space} and can't be changed here."
            ),
            "owner_or_admin_required": f"Changing '{agent_name}' needs its owner or {admin}.",
            "admin_required": f"This change needs {admin}.",
            "actor_not_found": (
                "I couldn't match you to an account here. Send a message first, then retry."
            ),
            "admin_required_for_shared_threads": f"Sharing threads in every channel needs {admin}.",
            "feature_disabled": f"Choosing a model provider is turned off. Ask {admin}.",
            "backend_not_allowed": (
                f"{provider} isn't allowed in this {space}. Ask {admin} to allow it."
            ),
            "model_not_selectable": f"That model isn't available on {provider}. Pick another.",
            "profile_not_selectable": f"That model isn't available on {provider}. Pick another.",
            "selfhosted_requires_channel_configuration": (
                f"A self-hosted model is set per channel, not per agent. Ask {admin}."
            ),
            "stale_preview": f"'{agent_name}' changed since the preview. Ask me to preview again.",
            "stale_revision": switched_meanwhile_text(agent_name, current, target),
            "re_confirm": "This change needs a fresh confirmation. Ask me to preview again.",
            "drops_required": "The confirmation didn't accept what this switch drops. "
            "Ask me to preview again.",
            "drops_mismatch": "What this switch drops changed. Ask me to preview again.",
            "revision_busy": "Another change to this agent is in progress. Retry in a moment.",
            "invalid_request": "That request didn't match the preview. Ask me to preview again.",
        }.get(
            reason,
            "Something on Daimon's side refused this change. "
            "If it repeats, tell whoever runs Daimon.",
        )
    return ToolError(f"{text} Nothing changed." + (f" [{reason}]" if with_code else ""))


async def _provider_now(
    service: channel_backend.AgentBackendService, agent: BackendAgent
) -> Provider:
    """The provider the agent runs on now: its setting, else the default."""
    setting = await service.current(agent)
    return (setting.backend if setting is not None else None) or DEFAULT_BACKEND


def _resolve_agent_model(backend: Provider | None, model: str | None) -> str | None:
    from daimon.core.agent_model_names import resolve_switch_model

    try:
        return resolve_switch_model(backend, model)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _require_selectable_model(backend: Provider | None, model: str | None) -> None:
    """Name the models on offer instead of a generic refusal for an unknown one."""
    if model is None:
        return
    models = selectable_models(backend) if backend is not None else ()
    if model not in models:
        offer = f" Pick one of: {', '.join(models)}." if models else ""
        raise ToolError(f"{model} isn't available on {backend or 'the default'} here.{offer}")


async def _require_agent_editor(
    service: channel_backend.AgentBackendService,
    agent: BackendAgent,
    actor: channel_backend.BackendActor,
    auth: AuthIdentity,
) -> None:
    """The agent's owner or a current admin; the service checks again when it writes."""
    if not await service.may_edit(agent, actor):
        raise ToolError(
            f"Changing '{agent.name}' needs its owner or {admin_noun(auth.platform)}. "
            "Nothing changed. Do not retry."
        )


async def _set_agent_backend_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
    *,
    backend: Provider | None,
    expected_revision: int | None = None,
    preview_only: bool = False,
    model: str | None = None,
) -> dict[str, JsonValue]:
    model = _resolve_agent_model(backend, model)
    _require_selectable_model(backend, model)
    if auth.agent_id is not None:
        raise ToolError("An agent key cannot change the model provider.")
    try:
        require_outside_chat_turn(auth)
    except ToolError as exc:
        raise ToolError(
            "Use request_agent_backend_switch in chat so the admin can confirm the preview."
        ) from exc
    if not channel_backend.backend_selection_enabled(runtime.settings.turn):
        raise ToolError("Choosing a model provider is turned off. Nothing changed.")
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
    on_now: Provider | None = None
    try:
        agent = await channel_backend.load_backend_agent(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_name=name,
        )
        await _require_agent_editor(service, agent, actor, auth)
        on_now = await _provider_now(service, agent)
        preview = await service.preview(agent, backend, model)
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
        try:
            if backend is None:
                revision = await service.clear_backend(
                    current, local, actor, expected_preview_digest=preview.digest
                )
            else:
                revision = await service.set_backend(
                    current,
                    backend,
                    local,
                    actor,
                    expected_preview_digest=preview.digest,
                    model=model,
                )
        except channel_backend.AgentBackendRefused as exc:
            if exc.reason == "stale_revision":
                on_now = await _provider_now(service, current)
            raise
    except (channel_backend.AgentBackendRefused, channel_backend.BackendUnsupported) as exc:
        raise _backend_switch_refusal(
            exc, name, backend, auth, with_code=True, current=on_now
        ) from exc
    from daimon.core.turn.notices import conversations_follow_line

    return {
        "agent": name,
        "backend": revision.backend,
        "model": revision.model,
        "revision": revision.local,
        "preview": cast(
            dict[str, JsonValue],
            TypeAdapter(type(preview)).dump_python(preview, mode="json"),
        ),
        "applies": "New threads use this setting. "
        + conversations_follow_line(name, revision.backend, on_now)
        + " Channel overrides still apply.",
    }


class AgentBackendSwitchInfo(BaseModel):
    status: Literal["preview", "changed", "awaiting_confirmation"]
    agent_name: str
    target_backend: Provider
    model: str | None = Field(default=None, exclude_if=lambda value: value is None)
    models: tuple[str, ...] = ()
    expected_revision: int
    preview_digest: str
    unsupported_skills: tuple[str, ...]
    unsupported_mcps: tuple[str, ...]
    unsupported_capabilities: tuple[str, ...]
    shared_thread_limits: tuple[str, ...]
    cost_line: str
    summary: str
    declared_drops: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    agent_settings: str | None = Field(default=None, exclude_if=lambda value: value is None)
    # This chat's channel pins its own provider, which outranks the agent's setting here.
    channel_pin: str | None = Field(default=None, exclude_if=lambda value: value is None)


async def _post_own_switch_card(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow,
    service: channel_backend.AgentBackendService,
    reference: BackendSwitchReference,
) -> str:
    """Post the Confirm card a provider turn can't show, after the same checks as its click.

    The card goes only into the verified origin's thread and only its requester
    can press it (`daimon.core.switch_cards.press_switch_card`). Nothing changes
    here; the press re-verifies everything before applying.
    """
    from daimon.adapters.mcp.tools._channel_policy import turn_origin_place
    from daimon.core.confirmation import CardRefusal, ConfirmationPrompt, backend_switch_callbacks
    from daimon.core.stores import backend_switch_cards as cards
    from daimon.core.switch_cards import SWITCH_CARD_MAX_S, requested_switch_card

    if auth.platform != "discord" or auth.platform_user_id is None:
        raise ToolError(
            "I can't post a confirmation card from this conversation here yet. An admin can "
            "switch the agent from its Details. Nothing changed."
        )
    now = datetime.datetime.now(datetime.UTC)
    # Outside any turn, so not capped by one; never past a day, and the card says when.
    expires_at = now + datetime.timedelta(
        seconds=min(runtime.settings.tool_safety.confirmation_timeout_s, SWITCH_CARD_MAX_S)
    )
    async with runtime.session_factory() as session:
        waiting = await cards.waiting_for(
            session,
            tenant_id=auth.tenant_id,
            requester_platform_user_id=auth.platform_user_id,
            agent_name=reference.agent,
            now=now,
        )
    if waiting:
        raise ToolError(
            f"There's already a Confirm card waiting for you to switch {reference.agent}. "
            "Press it, or let it expire, before asking again. Nothing changed."
        )

    async def current_admin(_clicker: str) -> bool:
        return auth.is_admin and not auth.client

    prepare, _ = backend_switch_callbacks(
        service,
        sessionmaker=runtime.session_factory,
        anthropic=runtime.client,
        tenant_id=auth.tenant_id,
        account_id=auth.account_id,
        current_admin=current_admin,
        default_agent_name=runtime.deployment_default.agent_name,
        place_word=space_noun(auth.platform),
        answering_agent_name=origin.responder_name,
        handoff_thread=(origin.platform, origin.parent_channel_id, origin.thread_id),
    )
    prompt = ConfirmationPrompt(
        title="Change the model provider?",
        requester_platform_user_id=auth.platform_user_id,
        expires_at=expires_at,
        admin=True,
        backend_switch=reference,
    )
    try:
        prepared = await prepare(prompt)
    except CardRefusal as exc:
        said = str(exc)
        raise ToolError(
            said if said.endswith("Nothing changed.") else f"{said} Nothing changed."
        ) from exc
    except ValueError as exc:
        raise ToolError(f"{exc} Nothing changed.") from exc
    async with runtime.session_factory.begin() as session:
        row = await cards.create_card(
            session,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            platform=origin.platform,
            channel_id=origin.parent_channel_id,
            thread_id=origin.thread_id,
            setup_thread=origin.is_setup,
            requester_platform_user_id=auth.platform_user_id,
            client=auth.client,
            answering_agent_name=origin.responder_name,
            # What the card shows: re-previewed if the agent changed since (J5).
            reference=(prepared.backend_switch or prepared.configuration or reference).model_dump(
                mode="json"
            ),
            expires_at=expires_at,
        )
    card = requested_switch_card(
        row.id,
        title=prepared.title,
        facts=tuple(fact for fact in (prepared.consequence, *prepared.detail_lines) if fact),
        requester_platform_user_id=auth.platform_user_id,
        expires_at=expires_at,
    )
    from daimon.adapters.mcp.tools.discord._switch_card import post_switch_card

    message_id = await post_switch_card(
        runtime, auth, channel_id=origin.thread_id, card=card, origin=turn_origin_place(origin)
    )
    async with runtime.session_factory.begin() as session:
        await cards.mark_posted(session, card_id=row.id, message_id=message_id)
    return (
        "I posted a Confirm card in this conversation. Nothing changes until the requester "
        "presses Confirm on it. Tell them so; do not call this tool again for this switch."
    )


def _private_origin(origin: TurnOriginRow) -> bool:
    from daimon.core.session_seal import is_private_conversation

    return is_private_conversation(origin.parent_channel_id) or is_private_conversation(
        origin.thread_id
    )


async def _channel_pin_line(runtime: McpRuntime, origin: TurnOriginRow) -> str | None:
    """Say so when this chat's channel pins a provider: the agent's switch won't apply there."""
    from daimon.core.switch_cards import channel_pin_line

    return await channel_pin_line(
        runtime.session_factory,
        tenant_id=origin.tenant_id,
        platform=origin.platform,
        channel_id=origin.parent_channel_id,
    )


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
    model: str | None = None,
    thread_mode: ThreadMode | None = None,
    required_capabilities: tuple[Capability, ...] | None = None,
    for_everyone: bool = False,
    unsaved_work: Literal["copy", "leave"] | None = None,
    handoff_summary: str | None = None,
) -> AgentBackendSwitchInfo:
    model = _resolve_agent_model(target_backend, model)
    _require_selectable_model(target_backend, model)
    if auth.agent_id is not None or auth.platform_user_id is None:
        raise ToolError("Request this change from your active chat conversation.")
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    if origin is None:
        raise ToolError("Request this change from your active chat conversation.")

    from daimon.adapters.mcp.tools._session_gate import session_card_gap
    from daimon.adapters.mcp.tools.task_continuity import (
        _hand_off_task_impl,  # pyright: ignore[reportPrivateUsage]
        copy_awaiting_its_switch,
    )
    from daimon.core.channel_backend import BackendActor, load_backend_agent, make_backend_service
    from daimon.core.confirmation import (
        BackendSwitchReference,
        reference_settings,
        settings_line,
        stated_settings,
        switch_preview_digest,
    )
    from pydantic import ValidationError

    # A provider turn with no card of its own (OpenAI, Gemini): Daimon posts the
    # card itself. Decided by the verified token's surface, never by the model.
    self_card = auth.no_confirmation_surface
    on_now: Provider | None = None
    # A new copy of this conversation's responder, made here: the confirmed switch
    # hands it this conversation too.
    awaiting: str | None = None
    try:
        service = make_backend_service(
            runtime.session_factory,
            runtime.settings.turn,
            capture_source=partial(
                _capture_backend_source, runtime, auth, origin, self_posted=self_card
            ),
        )
        if not service.enabled:
            raise ToolError("Choosing a model provider is turned off. Nothing changed.")
        if self_card:
            if _private_origin(origin):
                raise ToolError(
                    "I can't post a confirmation card in a private conversation. Ask in a "
                    "channel or thread where I answer. Nothing changed."
                )
        elif (
            await session_card_gap(runtime, auth, origin, tool_name="request_agent_backend_switch")
            is not None
        ):
            raise ToolError(
                "This conversation cannot verify the confirmation card. Nothing changed."
            )
        agent = await load_backend_agent(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_name=agent_name,
        )
        if not for_everyone and await channel_backend.answers_for_everyone(
            runtime.session_factory,
            agent,
            default_agent_name=runtime.deployment_default.agent_name,
        ):
            from daimon.core.turn.notices import default_agent_switch_text

            raise ToolError(
                default_agent_switch_text(
                    agent.name,
                    target_backend,
                    place=space_noun(auth.platform),
                    me=origin.responder_name == agent.name,
                )
                + " Nothing changed."
            )
        await _require_agent_editor(
            service,
            agent,
            BackendActor(
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                subject=mcp_subject(auth, is_admin=auth.admin_for(DelegatedAction.AGENT_SETTINGS)),
            ),
            auth,
        )
        on_now = await _provider_now(service, agent)
        if agent.source is not None and await copy_awaiting_its_switch(
            runtime,
            auth,
            origin,
            ma_agent_id=agent.source.ref.id,
            metadata=agent.source.spec.metadata,
        ):
            awaiting = agent.source.ref.id
            # Every hand-off check, before a card is posted: its uncommitted-work
            # question is one the person answers here, never on the card.
            await _hand_off_task_impl(
                runtime,
                auth,
                origin_context_id=str(origin.id),
                agent_id=awaiting,
                unsaved_work=unsaved_work,
                summary=handoff_summary,
                switch="check",
            )
        if thread_mode == "shared" and not (
            auth.admin_for(DelegatedAction.AGENT_SETTINGS) and auth.agent_id is None
        ):
            # The service refuses it too; say so before a card is posted.
            raise ToolError(
                f"Sharing threads applies in every channel, so it needs "
                f"{admin_noun(auth.platform)}. Nothing changed."
            )
        settings = stated_settings(thread_mode, required_capabilities)
        preview = await service.preview(agent, target_backend, model, settings)
        if not preview.allowed:
            raise ToolError("That model provider isn't turned on here. Nothing changed.")
        status: Literal["preview", "changed", "awaiting_confirmation"] = "preview"
        summary = (
            "Tell the person in plain words the compatibility, what won't be available "
            "and the cost line, then call request_agent_backend_switch again in this same "
            "reply with its expected_revision and preview_digest. Do not ask the person to "
            "reply yes or to confirm in chat: the second call posts the switch card, and "
            "they Confirm on it. Their request is already the go-ahead. "
            "Nothing has changed yet."
        )
        reference: BackendSwitchReference | None = None
        # The person's hand-off answers, bound into the digest the card showed.
        bound = unsaved_work is not None or handoff_summary is not None
        if preview_digest is not None:
            reference = BackendSwitchReference.model_validate(
                {
                    "agent": agent_name,
                    "target_backend": target_backend,
                    "model": model,
                    "expected_revision": expected_revision,
                    "preview_digest": preview_digest,
                    "accepted_drops": accepted_drops,
                    "thread_mode": thread_mode,
                    "required_capabilities": required_capabilities,
                    "unsaved_work": unsaved_work,
                    "handoff_summary": handoff_summary,
                    "for_everyone": for_everyone,
                }
            )
            current_digest = switch_preview_digest(preview.digest, unsaved_work, handoff_summary)
            if (
                not self_card
                and awaiting is None
                and not bound
                and (
                    current_digest != reference.preview_digest
                    or preview.setting_revision != reference.expected_revision
                )
            ):
                # The agent changed after the preview; its card re-previewed and showed
                # the current one (J5). Apply that, only if nothing changed since. A
                # hand-off is never re-previewed: its answers are bound to the preview.
                from daimon.core.stores.backend_switch_cards import consume_refresh

                async with runtime.session_factory.begin() as session:
                    refreshed = await consume_refresh(
                        session,
                        tenant_id=auth.tenant_id,
                        account_id=auth.account_id,
                        agent_name=agent.name,
                        stale_digest=reference.preview_digest,
                        fresh_digest=current_digest,
                        fresh_revision=preview.setting_revision,
                        now=datetime.datetime.now(datetime.UTC),
                    )
                if refreshed:
                    reference = reference.model_copy(
                        update={
                            "expected_revision": preview.setting_revision,
                            "preview_digest": current_digest,
                            "accepted_drops": tuple(preview.declared_drops) or None,
                        }
                    )
            if (
                bound
                and switch_preview_digest(preview.digest, unsaved_work, handoff_summary)
                != reference.preview_digest
            ):
                # The answer is bound into the digest the card showed: a changed or
                # invented one is a different request. (Omitting a bound answer fails
                # the switch's own digest check.)
                raise channel_backend.AgentBackendRefused("stale_preview")
        if reference is not None and self_card:
            if awaiting is not None or bound:
                # The hand-off this switch carries runs at the write, which a
                # self-posted card's press doesn't do.
                raise ToolError(
                    "This switch also hands the conversation over, which I can't confirm "
                    "from this conversation yet. Ask from a Claude conversation. Nothing changed."
                )
            status = "awaiting_confirmation"
            summary = await _post_own_switch_card(runtime, auth, origin, service, reference)
        elif reference is not None:
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
            from daimon.core.turn.notices import conversations_follow_line, provider_name

            before = await service.current(agent)
            if awaiting is not None:
                # Checked again at the write edge, so a refusal changes nothing.
                await _hand_off_task_impl(
                    runtime,
                    auth,
                    origin_context_id=str(origin.id),
                    agent_id=awaiting,
                    unsaved_work=unsaved_work,
                    summary=handoff_summary,
                    switch="check",
                )
            try:
                await service.set_backend(
                    agent,
                    target_backend,
                    reference.expected_revision,
                    actor,
                    # A bound digest was checked above; this one is the switch's own.
                    expected_preview_digest=preview.digest if bound else reference.preview_digest,
                    model=reference.model,
                    settings=reference_settings(reference),
                    **(
                        {"accepted_drops": reference.accepted_drops}
                        if reference.accepted_drops is not None
                        else {}
                    ),
                )
            except channel_backend.AgentBackendRefused as exc:
                if exc.reason == "stale_revision":
                    # Another switch landed first: name where the agent is now.
                    on_now = await _provider_now(service, agent)
                raise
            if awaiting is not None:
                # J1: the switch and the hand-off it promises, in one step. The model's
                # own order was a coin toss: a copy handed this conversation before
                # its switch posted the card again instead of answering.
                try:
                    await _hand_off_task_impl(
                        runtime,
                        auth,
                        origin_context_id=str(origin.id),
                        agent_id=awaiting,
                        unsaved_work=unsaved_work,
                        summary=handoff_summary,
                        switch="commit",
                    )
                except Exception as exc:
                    # The switch landed: say so, whatever stopped the hand-off.
                    log.warning(
                        "backend_switch.handoff_failed",
                        agent=agent_name,
                        error_type=type(exc).__name__,
                        exc_info=True,
                    )
                    why = (
                        str(exc).removesuffix(" Nothing was changed.")
                        if isinstance(exc, ToolError)
                        else "something went wrong on our side."
                    )
                    raise ToolError(
                        f"{agent_name} now uses {provider_name(target_backend)}, but this "
                        f"conversation couldn't be handed to it: {why} Ask {agent_name} "
                        "in a new conversation."
                    ) from exc
            status = "changed"
            summary = (
                f"Model provider changed. New conversations with {agent_name} use "
                f"{provider_name(target_backend)}. "
                + conversations_follow_line(
                    agent_name,
                    target_backend,
                    (before.backend if before else None) or DEFAULT_BACKEND,
                )
                + " "
                + await _move_this_thread(
                    runtime,
                    auth,
                    origin,
                    agent_name,
                    target_backend,
                    pinned_here=await _channel_pin_line(runtime, origin) is not None,
                )
            )
        channel_pin = await _channel_pin_line(runtime, origin)
        if channel_pin is not None:
            summary = f"{summary} {channel_pin}"
        return AgentBackendSwitchInfo(
            status=status,
            agent_name=agent.name,
            target_backend=preview.target_backend,
            model=preview.target_model,
            models=selectable_models(preview.target_backend),
            expected_revision=preview.setting_revision,
            preview_digest=switch_preview_digest(preview.digest, unsaved_work, handoff_summary),
            unsupported_skills=preview.unsupported_skills,
            unsupported_mcps=preview.unsupported_mcps,
            unsupported_capabilities=preview.unsupported_capabilities,
            shared_thread_limits=preview.shared_thread_limits,
            cost_line=preview.cost_line,
            summary=summary,
            declared_drops=preview.declared_drops,
            agent_settings=settings_line(preview.target_settings)
            if preview.target_settings
            else None,
            channel_pin=channel_pin,
        )
    except (DaimonError, ValidationError) as exc:
        raise _backend_switch_refusal(
            exc, agent_name, target_backend, auth, current=on_now
        ) from exc


def register_agent_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    if channel_backend.backend_selection_enabled(runtime.settings.turn):

        @mcp.tool
        async def set_agent_backend(  # pyright: ignore[reportUnusedFunction]
            ctx: Context,
            name: str,
            backend: Provider | None,
            expected_revision: int | None = None,
            preview_only: bool = False,
            model: str | None = None,
        ) -> dict[str, JsonValue]:
            """Set an enabled model provider, or clear the agent setting with backend=null.

            The agent's owner or an admin, for direct coding-tool or API requests. Chat
            turns use request_agent_backend_switch so the requester confirms the preview.
            The returned preview names compatibility limits and list prices.
            Each person's own conversation with the agent moves to the new provider on
            their next message; shared threads stay on the old one; channel settings
            still apply.
            expected_revision refuses a setting changed since it was read.
            preview_only returns the compatibility and cost preview without applying it.
            model picks one of the provider's selectable models (Gemini: gemini-3.8-flash or
            gemini-4-argon); omit it for the provider's default.
            """
            return await _set_agent_backend_impl(
                runtime,
                await _auth(ctx),
                name,
                backend=backend,
                expected_revision=expected_revision,
                preview_only=preview_only,
                model=model,
            )

    @mcp.tool
    async def list_agents(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        page: str | None = None,
        origin_context_id: str | None = None,
        mine: bool = False,
    ) -> list[AgentInfo]:
        """List agents visible here, including their attached ``mcp_servers`` and ``skills``.

        Each agent says ``owned_by_you``: the caller owns it and may change it without
        an admin, and ``owner``: who to ask about changing it (absent: admins only).
        ``mine=true`` lists only the caller's own agents ("which agents do I
        own?"), wherever they answer, including ones that answer nowhere yet.
        ``mcp_servers`` and ``created_at`` are null for a caller who is neither an
        admin nor its owner: setup details are for them, capabilities (name,
        description, model, skills) are not. Members see the channel/thread
        responders, agents explicitly allowed to run here, and their own agents;
        other unrouted agents and other channels' agents are hidden.
        Always pass this turn's ``origin_context_id`` to verify its location: without
        it a member sees only their own agents. Admins retain their existing
        visibility. ``page`` is reserved for pagination.
        """
        return await _list_agents_impl(
            runtime, await _auth(ctx), page, origin_context_id, mine=mine
        )

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
        the current generation. For the cheapest Claude, use ``"claude-haiku-5-5"``:
        Haiku is the cheapest current generation, never ``claude-haiku-4-5``. Only
        pass an older id (``claude-sonnet-5``, ``claude-opus-5``, …) when the user
        names that version themselves.
        Any other provider's model works the same way, by name: ``"flash"``,
        ``"argon"``, ``"luna"``, ``"astra"`` (or its id). The agent is created on that
        provider directly: never create it on Claude first, and nothing else is needed
        afterwards. The result's ``applies`` says where it runs and what it won't have
        there: post it.
        Optional: ``description``, ``system`` (the system prompt), ``tools``,
        ``mcp_servers``, and ``skill_repos`` — GitHub repos to sync skills from,
        e.g. ``[{"url": "https://github.com/owner/repo", "branch": "main"}]``.

        Do not pass a ``skills`` field here. To add skills, either sync a repo via
        ``skill_repos`` or use ``sync_skills`` after the agent is created.

        A returned ``answering`` field says the new agent is routed nowhere yet:
        post it verbatim. Always pass this turn's ``origin_context_id``: a chat turn
        without it is refused while a channel in the workspace is confidential, and with
        """
        source_model, backend = _create_model(model)
        spec = _build_create_spec(
            name=name,
            model=source_model,
            description=description,
            system=system,
            tools=tools,
            mcp_servers=mcp_servers,
            skill_repos=skill_repos,
        )
        return await _create_agent_impl(
            runtime, await _auth(ctx), spec, origin_context_id, backend=backend
        )

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

        Prefer current-generation ``claude-sonnet-5-5`` for Sonnet,
        ``claude-opus-5-5`` for Opus and ``claude-haiku-5-5`` for the cheapest
        Claude unless an older version is explicitly requested. List fields
        (``tools``, ``mcp_servers``, ``skills``) are added to, never replaced; shorter
        lists remove nothing. Use ``remove_skill`` or ``detach_mcp_server`` to remove.
        Daimon requires ``fork_agent`` first. Needs the agent's owner (whoever created it
        in chat, or whom an admin named) or an admin.

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
    async def set_agent_builtin_tool(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        name: str,
        tool: BuiltinToolName,
        enabled: bool,
        expected_ma_agent_id: str | None = None,
        origin_context_id: str | None = None,
    ) -> AgentInfo:
        """Turn one of an agent's built-in tools on or off, e.g. "turn off its web search".

        ``tool`` is the built-in's name (web_search, web_fetch, bash, read, write,
        edit, glob, grep). Needs the agent's owner or an admin, like update_agent.
        Whoever runs Daimon can forbid one for every agent by listing
        ``builtin/<name>`` in the tool-safety ``denied`` setting; turning it on is
        then refused here, in update_agent and in create_agent.
        Returns ``applies``: post it as part of the reply. Pass this turn's
        ``origin_context_id`` so its channel counts."""
        return await _set_agent_builtin_tool_impl(
            runtime,
            await _auth(ctx),
            name,
            tool=tool,
            enabled=enabled,
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
        directly changing its setup. Needs the agent's owner or an admin.

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
        ctx: Context,
        source_name: str,
        new_name: str,
        expected_ma_agent_id: str | None = None,
        new_copy_if_taken: bool = False,
        origin_context_id: str | None = None,
        model: str | None = None,
    ) -> AgentInfo:
        """Make a copy of Daimon or another agent that you can edit under a new name.
        Anyone may copy you (the agent answering them), and owns that copy; copying
        any other agent is admin-only. Only for a saved, named copy someone asked
        to keep. The copy keeps its source's tool rules (copied once, at the fork:
        later changes to the source's rules don't follow).
        Copies its prompt, model, skills and the MCP servers that need
        no stored token. The copy starts with no credentials: no repo binding or
        GitHub access, no API/service keys and no connector tokens; attach its own
        with ``request_agent_key`` and the repo tools. An agent an operator pinned
        to channels can't be copied.

        Use ``update_agent`` to edit the copy. Daimon cannot be edited directly.

        Continue configuring it through Daimon with the copy named as the setup
        target. A returned ``answering`` field says the copy is routed nowhere yet:
        post it verbatim.

        ``new_copy_if_taken``: when ``new_name`` is taken, copy under the first free
        ``new_name-2``, ``new_name-3``... instead of refusing; the result's ``name``
        is the copy's: name the copy by it, not ``new_name``. Pass true for "make a
        copy of yourself" asks, where the person named no copy; when the person
        names the copy, leave this false.

        If the person names a model for the copy ("fork yourself onto
        gpt-3.5-turbo"), pass it as ``model``. A saved copy keeps its source's
        model: an unavailable model is refused as unavailable, and a different
        available one is refused; make the copy and then request a backend switch
        for it. Pass this active turn's ``origin_context_id``."""
        return await _fork_agent_impl(
            runtime,
            await _auth(ctx),
            source_name,
            new_name,
            expected_ma_agent_id=expected_ma_agent_id,
            new_copy_if_taken=new_copy_if_taken,
            origin_context_id=origin_context_id,
            model=model,
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
        from daimon.adapters.mcp.tools.provider_config import register_provider_config_tools

        register_provider_config_tools(mcp, runtime)

        @mcp.tool
        async def request_agent_backend_switch(  # pyright: ignore[reportUnusedFunction]
            ctx: Context,
            agent_name: str,
            target_backend: Provider,
            expected_revision: int | None = None,
            preview_digest: str | None = None,
            origin_context_id: str | None = None,
            accepted_drops: tuple[str, ...] | None = None,
            model: str | None = None,
            thread_mode: ThreadMode | None = None,
            required_capabilities: tuple[Capability, ...] | None = None,
            for_everyone: bool = False,
            unsaved_work: Literal["copy", "leave"] | None = None,
            handoff_summary: Annotated[str | None, Field(max_length=2000)] = None,
        ) -> AgentBackendSwitchInfo:
            """Preview a requested agent backend switch, then Confirm in this chat.

            Use when an admin or the agent's owner asks to switch it to Anthropic, OpenAI
            or Gemini. A member can make a copy they own and switch that copy.
            If the named model isn't available, say that with the allowed list.
            optionally naming a model naturally: "switch Researcher to argon" or
            "switch Researcher to luna". For an Anthropic model change such as
            "set Writer to opus 5.5", use update_agent with that natural model name.
            ``model`` accepts natural names or one of
            the returned ``models``; omit it for the provider's default. A provider
            alone ("switch to GPT", "to Gemini") means its default: omit ``model`` and
            preview; never ask which model. Never ask the person for a model id.
            Ambiguous model names return the choices.
            The switch applies to
            new conversations wherever the agent answers, and to this conversation
            from the next message when the switched agent answers here (the result's
            summary says which). The server's default or built-in agent switches only
            when the person says it's for everyone (for_everyone=true, on both calls);
            otherwise offer the copy below. For "make a copy of yourself on Gemini":
            copy the agent with fork_agent and new_copy_if_taken=true (always a NEW
            copy, never an earlier one; use the name it returns from here on), then
            switch the copy here. Don't hand_off_task first: the confirmed switch hands
            this conversation to the copy and moves it to Gemini from the next message.
            Pass ``handoff_summary`` on both calls: a short summary of this conversation
            (what was asked, decided and still open; no secrets, keys or tool output),
            which the copy starts from there, since the history itself can't move.
            If that preview asks the uncommitted-work question, pass the person's
            answer as ``unsaved_work`` on both calls.
            First call without preview_digest: tell the person in plain words the
            compatibility, shared-thread limits, what won't be available and the cost
            line, then call again in this same turn with the same model and that preview's
            expected_revision, preview_digest and exact declared_drops as accepted_drops.
            Never ask the person to reply "yes", to confirm in chat, or for a second
            go-ahead: the request that began this turn is the go-ahead, and the second
            call posts the card, where they Confirm. On an OpenAI or Gemini turn
            that call posts the card itself and returns awaiting_confirmation: tell the
            person to press Confirm, and don't call again. Never confirm, approve or accept it
            on their behalf, and never say you did. A stale preview or expired card
            changes nothing; preview again. Make any other change to the agent (its tools,
            skills or settings) before previewing a switch: a change after the preview
            makes it stale. Pass this active turn's origin_context_id.
            The same tool states the agent's own settings: ``thread_mode`` ("shared"
            to share threads where the channel allows it, admins only; or "per_caller") and
            ``required_capabilities`` (a channel that can't give one refuses the turn).
            Pass both together; an omitted one returns to the default. To change only
            settings, pass the agent's current target_backend and model.
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
                model=model,
                thread_mode=thread_mode,
                required_capabilities=required_capabilities,
                for_everyone=for_everyone,
                unsaved_work=unsaved_work,
                handoff_summary=handoff_summary,
            )
