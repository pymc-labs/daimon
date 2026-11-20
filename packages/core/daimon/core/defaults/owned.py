"""Daimon-owned agent and environment records for an install with no Anthropic key.

Agents are born on the deployment's keyed provider, at its default model, with a
first backend revision naming that provider, so a turn never falls back to
Managed Agents. With a key, every caller keeps creating Managed Agents records.
"""

from __future__ import annotations

import uuid
from typing import cast

import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.agent_create_params import Tool
from daimon.core.defaults.builtin_agent import builtin_lookup_names, resolve_builtin_agent_name
from daimon.core.defaults.metadata import (
    BUILTIN_AGENT_NAME,
    LEGACY_BUILTIN_AGENT_NAME,
    build_metadata,
    is_renamed_seeded_record,
)
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.errors import DaimonError
from daimon.core.model_presets import model_choice_id
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import agent_spec, create_owned_agent, create_owned_environment
from daimon.core.provider_keys import DEFAULT_KEY_ENV, provider_key_present
from daimon.core.specs import AgentSpec, EnvironmentSpec, dump_agent_spec
from daimon.core.stores.selfhosted_resources import SelfHostedAgents, SelfHostedEnvironments
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    ToolSafetyPolicy,
    session_tools_for_policy,
    trusted_servers_for,
)
from mux.contracts.ids import ModelRef, Provider, Scope
from mux.contracts.resources import Agent
from mux.contracts.resources import AgentSpec as RecordAgentSpec
from mux.contracts.resources import EnvironmentSpec as RecordEnvironmentSpec
from mux.errors import ProviderError
from mux.profiles import selectable_models
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


class AgentNameTaken(DaimonError):
    """A create-only reconcile found an agent of that name already in the tenant."""


class NoKeyedProvider(DaimonError):
    """No Anthropic key and no other provider key: nothing can run an agent."""


def owned_defaults_possible() -> bool:
    """Whether a provider other than Anthropic has a deployment key."""
    return any(provider_key_present(provider) for provider in DEFAULT_KEY_ENV)


def keyless_provider() -> Provider:
    """The provider a keyless install creates agents on: the first one keyed."""
    for provider in DEFAULT_KEY_ENV:
        if provider_key_present(provider):
            return provider
    raise NoKeyedProvider(
        "Set an Anthropic, OpenAI or Gemini key before creating agents on this install."
    )


def owned_scope(tenant_id: uuid.UUID, account_id: uuid.UUID | None) -> Scope:
    """The record write scope: the owning account, or the deployment operator."""
    if account_id is not None:
        return resource_scope(tenant_id=str(tenant_id), account_id=str(account_id))
    return Scope(
        tenant_id=str(tenant_id),
        account_id="operator",
        principal_id="cli",
        authorization_id="cli-deployment-operator",
    )


def owned_model(spec: AgentSpec, provider: Provider) -> ModelRef:
    """The spec's model when a keyed provider serves it, else `provider`'s default."""
    from daimon.core.agent_model_names import resolve_model

    try:
        named = resolve_model(model_choice_id(spec.model))
    except ValueError:
        named = None
    if (
        named is not None
        and named.provider in DEFAULT_KEY_ENV
        and provider_key_present(named.provider)
    ):
        return ModelRef(provider=named.provider, id=named.id)
    return ModelRef(provider=provider, id=selectable_models(provider)[0])


def owned_agent_spec(
    spec: AgentSpec, *, provider: Provider, metadata: dict[str, str]
) -> RecordAgentSpec:
    """The record form of an authoring spec, on `provider`'s default model.

    Anthropic toolset and model extensions are dropped: the provider host supplies
    its own tools. Skills attach later through the neutral skill host.
    """
    record = agent_spec({**dump_agent_spec(spec), "metadata": metadata})
    return record.model_copy(
        update={
            "model": owned_model(spec, provider),
            "tools": None,
            "skills": None,
            "extensions": None,
        }
    )


async def resolve_owned_agent(
    sessions: async_sessionmaker[AsyncSession],
    scope: Scope,
    name: str,
    *,
    seeded_rename: bool = True,
) -> Agent:
    """The live Daimon record `name` answers to, the built-in name by `builtin_agent`'s rule.

    `default` reaches the seeded record not yet renamed from `daimon`, and a
    self-hosted workspace's own `daimon` while it has no `default`. `daimon`
    falls back to the managed `default` once renamed (`LEGACY_BUILTIN_AGENT_NAME`).
    `seeded_rename=False` is for a caller adopting a workspace's own agent.
    Raises `ProviderError` (`not_found`) when nothing answers.
    """
    agents = SelfHostedAgents(sessions)
    if seeded_rename and builtin_lookup_names(name):
        name = await builtin_record_name(agents, scope, name)
    try:
        return await agents.resolve(scope, name)
    except ProviderError as miss:
        if miss.category != "not_found" or not seeded_rename or name != LEGACY_BUILTIN_AGENT_NAME:
            raise
        renamed = await agents.resolve(scope, BUILTIN_AGENT_NAME)
        if not is_renamed_seeded_record(renamed.spec.name, renamed.spec.metadata):
            raise miss from None
        return renamed


async def builtin_record_name(agents: SelfHostedAgents, scope: Scope, name: str) -> str:
    """The record name a request for the built-in `name` resolves under.

    A `daimon` record counts whether managed or not: `selfhosted init` used to
    name a workspace's agent that.
    """
    live: list[str] = []
    for candidate in builtin_lookup_names(name):
        try:
            await agents.resolve(scope, candidate)
        except ProviderError as error:
            if error.category != "not_found":
                raise
        else:
            live.append(candidate)
    return resolve_builtin_agent_name(name, live, tenant_id=scope.tenant_id, surface="owned_record")


async def reconcile_owned_agent(
    sessions: async_sessionmaker[AsyncSession],
    spec: AgentSpec,
    *,
    tenant_id: uuid.UUID,
    dry_run: bool,
    account_id: uuid.UUID | None,
    managed: bool,
    create_only: bool,
) -> ResourceOutcome:
    """Create the agent as a Daimon record unless one of that name is live.

    An existing record is left as is; edits go through the record seam.
    """
    scope = owned_scope(tenant_id, account_id)
    try:
        # A create counts the built-in under either name as taken.
        existing = await resolve_owned_agent(
            sessions, scope, spec.name, seeded_rename=managed or create_only
        )
    except ProviderError as error:
        if error.category != "not_found":
            raise
    else:
        if create_only:
            raise AgentNameTaken(spec.name)
        return ResourceOutcome(
            kind="agent", name=spec.name, action=Action.SKIPPED, anthropic_id=existing.ref.id
        )
    if dry_run:
        return ResourceOutcome(kind="agent", name=spec.name, action=Action.CREATED)
    provider = keyless_provider()
    metadata = build_metadata(
        tenant_id=tenant_id,
        name=spec.name,
        account_id=account_id,
        managed=managed,
        spec_hash=None,
        isolated=spec.isolated,
    )
    record = owned_agent_spec(spec, provider=provider, metadata=metadata)
    created = await create_owned_record(sessions, record, scope=scope, tenant_id=tenant_id)
    return ResourceOutcome(
        kind="agent", name=spec.name, action=Action.CREATED, anthropic_id=created.id
    )


async def create_owned_record(
    sessions: async_sessionmaker[AsyncSession],
    record: RecordAgentSpec,
    *,
    scope: Scope,
    tenant_id: uuid.UUID,
    key: str | None = None,
) -> BetaManagedAgentsAgent:
    """Create a Daimon record with the first backend revision naming its provider.

    The same `key` replays the first create, so a re-run never makes a second record.
    """
    # Imported here: `channel_backend` reaches `authz`, which imports `defaults`.
    from daimon.core.channel_backend import record_born_backend

    provider = record.model.provider
    created = await create_owned_agent(None, record, scope=scope, records=sessions, key=key)
    async with sessions() as session, session.begin():
        await record_born_backend(
            session,
            tenant_id=tenant_id,
            record_id=created.id,
            agent_name=record.name,
            provider=provider,
            model=record.model.id,
        )
    _log.info("owned_agent.created", name=record.name, provider=provider, agent_id=created.id)
    return created


async def reconcile_owned_environment(
    sessions: async_sessionmaker[AsyncSession],
    spec: EnvironmentSpec,
    *,
    tenant_id: uuid.UUID,
    dry_run: bool,
    account_id: uuid.UUID | None,
) -> ResourceOutcome:
    """Create a hosted Daimon environment record unless one of that name is live."""
    scope = owned_scope(tenant_id, account_id)
    try:
        existing = await SelfHostedEnvironments(sessions).resolve(scope, spec.name)
    except ProviderError as error:
        if error.category != "not_found":
            raise
    else:
        return ResourceOutcome(
            kind="environment",
            name=spec.name,
            action=Action.SKIPPED,
            anthropic_id=existing.ref.id,
        )
    if dry_run:
        return ResourceOutcome(kind="environment", name=spec.name, action=Action.CREATED)
    metadata = build_metadata(tenant_id=tenant_id, name=spec.name, account_id=None, managed=True)
    record = RecordEnvironmentSpec(
        name=spec.name, description=spec.description, execution="hosted", metadata=metadata
    )
    created = await create_owned_environment(None, record, scope=scope, records=sessions)
    return ResourceOutcome(
        kind="environment", name=spec.name, action=Action.CREATED, anthropic_id=created.id
    )


def host_tools_spec(
    spec: RecordAgentSpec,
    public_url: str,
    *,
    tool_safety: ToolSafetyPolicy,
    asks_before_publishing: bool = False,
) -> RecordAgentSpec:
    """A record as its provider host runs it, as a Claude default agent switched there.

    Its own toolsets when it has any (its facts, gated as its session runs them), else
    the seeded default agent's: the base agent toolset, plus Daimon's MCP toolset when
    it lists Daimon's server, as Managed Agents stores them (every tool enabled, always
    allowed). Daimon's server is this deployment's; its connectors stay, so a switch
    names or relays each as for a Managed Agents agent. Built-ins whoever runs Daimon
    forbids are off and tool safety applies, so a record never widens its template.
    """
    from daimon.core.agent_editing import denied_builtins
    from daimon.core.features.model_input import builtin_enabled, with_builtin
    from mux.contracts.extensions import ExtensionConfig
    from mux.contracts.resources import MCPConnection
    from mux.drivers.anthropic import AGENT_TOOLS_NAMESPACE

    connectors = tuple(s for s in spec.mcp_servers or () if s.name != DAIMON_SERVER_NAME)
    listed = len(connectors) < len(spec.mcp_servers or ())
    daimon = (MCPConnection(name=DAIMON_SERVER_NAME, url=public_url),) if listed else ()
    servers = (*daimon, *connectors)
    own = [
        cast(Tool, tool)
        for extension in spec.extensions or ()
        if extension.namespace == AGENT_TOOLS_NAMESPACE
        for tool in cast(list[JsonValue], extension.value.get("tools") or [])
    ]
    # A toolset of a server it doesn't list is not callable on Managed Agents either.
    names = {server.name for server in servers}
    tools = [
        tool
        for tool in own or _template_tools(public_url, listed=listed)
        if tool.get("type") != "mcp_toolset" or tool.get("mcp_server_name") in names
    ]
    for name in sorted(denied_builtins(tool_safety.denied)):
        if builtin_enabled(tools, name):
            tools = with_builtin(tools, name, False)
    gated = session_tools_for_policy(
        tool_safety,
        tools,
        trusted_servers=trusted_servers_for(public_url, {s.name: s.url for s in servers}),
        asks_before_publishing=asks_before_publishing,
    )
    toolset = ExtensionConfig(
        namespace=AGENT_TOOLS_NAMESPACE,
        version=1,
        value={"tools": cast(list[JsonValue], tools if gated is None else gated)},
    )
    others = tuple(e for e in spec.extensions or () if e.namespace != AGENT_TOOLS_NAMESPACE)
    return spec.model_copy(
        update={"tools": None, "mcp_servers": servers, "extensions": (*others, toolset)}
    )


def _template_tools(public_url: str, *, listed: bool) -> list[Tool]:
    """The seeded default agent's toolsets as Managed Agents stores them: all on, allowed."""
    from daimon.core.defaults.mcp_merge import merge_default_mcp_toolset
    from daimon.core.specs import merge_default_agent_toolset

    tools = merge_default_agent_toolset(None)
    if listed:
        tools = merge_default_mcp_toolset(tools, public_url) or tools
    stored: dict[str, JsonValue] = {"enabled": True, "permission_policy": {"type": "always_allow"}}
    readback: list[Tool] = []
    for tool in tools:
        entry: dict[str, JsonValue] = {**cast(dict[str, JsonValue], tool), "default_config": stored}
        if "configs" in tool:
            configs = cast(list[dict[str, JsonValue]], tool["configs"])
            entry["configs"] = [{**config, **stored} for config in configs]
        readback.append(cast(Tool, entry))
    return readback
