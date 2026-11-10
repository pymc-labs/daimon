"""Daimon-owned agent and environment records for an install with no Anthropic key.

Agents are born on the deployment's keyed provider, at its default model, with a
first backend revision naming that provider, so a turn never falls back to
Managed Agents. With a key, every caller keeps creating Managed Agents records.
"""

from __future__ import annotations

import uuid

import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.defaults.metadata import build_metadata
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.errors import DaimonError
from daimon.core.model_presets import model_choice_id
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import agent_spec, create_owned_agent, create_owned_environment
from daimon.core.provider_keys import DEFAULT_KEY_ENV, provider_key_present
from daimon.core.specs import AgentSpec, EnvironmentSpec, dump_agent_spec
from daimon.core.stores.selfhosted_resources import SelfHostedAgents, SelfHostedEnvironments
from mux.contracts.ids import ModelRef, Provider, Scope
from mux.contracts.resources import AgentSpec as RecordAgentSpec
from mux.contracts.resources import EnvironmentSpec as RecordEnvironmentSpec
from mux.errors import ProviderError
from mux.profiles import selectable_models
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
        existing = await SelfHostedAgents(sessions).resolve(scope, spec.name)
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
) -> BetaManagedAgentsAgent:
    """Create a Daimon record with the first backend revision naming its provider."""
    # Imported here: `channel_backend` reaches `authz`, which imports `defaults`.
    from daimon.core.channel_backend import record_born_backend

    provider = record.model.provider
    created = await create_owned_agent(None, record, scope=scope, records=sessions)
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
