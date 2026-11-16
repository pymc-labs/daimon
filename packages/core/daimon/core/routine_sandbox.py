"""What an admin approved when opting a routine in to built-in sandbox tools.

The invariant: built-ins are enabled for a routine's run only when the run's
agent version and environment are exactly the ones recorded when the admin
opted in. Any mismatch, or a missing record, keeps them off.

An operator can instead opt in an agent (`AgentSandboxPin`). That records the
agent id and the environment name, not a version: an agent edit keeps it, a
different agent or environment does not. It covers every routine run of the agent,
direct-message routines included, and `unattended_capabilities` still caps it.

`current_pin` reads the agent and environment a fire would use, at opt-in
(an unreadable one refuses the opt-in). `run_matches_pin` compares a run's
own session with the recorded `SandboxPin`; the headless runner turns
built-ins on only when it holds, and otherwise reports the drift through
`RoutineSandbox.on_drift` so the scheduler can clear the flag.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsSession
from daimon.core.config import Settings
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, find_environment_by_daimon_tag
from daimon.core.features.registry import FEATURES
from daimon.core.features.snapshot import FeaturePlace, place_guardrail
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent, retrieve_environment
from daimon.core.rule_views import routine_destination_channel
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.domain import AgentSandboxPin, RoutineRow, SandboxPin
from daimon.core.stores.scoped_config_read import resolve
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "AgentSandboxPin",
    "RoutineSandbox",
    "SandboxPin",
    "SandboxPinUnavailable",
    "builtins_allowed",
    "current_agent_pin",
    "current_pin",
    "pin_of",
    "run_matches_pin",
]

log = structlog.get_logger(__name__)


class SandboxPinUnavailable(Exception):
    """The routine's agent or environment cannot be read, so nothing can be pinned."""


@dataclass(frozen=True)
class RoutineSandbox:
    """A routine's opt-in, handed to the headless runner for one fire."""

    pin: SandboxPin | AgentSandboxPin
    #: Called once when the run's agent or environment no longer matches `pin`.
    on_drift: Callable[[], Awaitable[None]]


def _revision(environment: BetaEnvironment) -> datetime:
    """An environment's revision: its `updated_at`, which the SDK gives as text."""
    return datetime.fromisoformat(environment.updated_at)


def pin_of(row: RoutineRow) -> SandboxPin | None:
    """The recorded pin of an opted-in routine, or None (off, or nothing recorded)."""
    if (
        not row.sandbox_tools_enabled
        or row.sandbox_agent_id is None
        or row.sandbox_agent_version is None
        or row.sandbox_environment_id is None
        or row.sandbox_environment_updated_at is None
    ):
        return None
    return SandboxPin(
        agent_id=row.sandbox_agent_id,
        agent_version=row.sandbox_agent_version,
        environment_id=row.sandbox_environment_id,
        environment_updated_at=row.sandbox_environment_updated_at,
    )


async def current_pin(
    client: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    row: RoutineRow,
    *,
    default: DeploymentDefault,
) -> SandboxPin:
    """The agent version and environment the routine would run with now.

    The agent is the routine's own; the environment is the one its channel
    resolves to, as the scheduler resolves it at fire time. Raises
    `SandboxPinUnavailable` when either is archived, missing or unreadable.
    """
    scope = resource_scope(tenant_id=str(row.tenant_id))
    try:
        agent = await retrieve_agent(client, row.agent_id, scope=scope, records=session_factory)
        async with session_factory() as session:
            scoped = await resolve(
                session,
                context=ScopeContext(
                    tenant_id=row.tenant_id,
                    channel_id=routine_destination_channel(row) or row.channel_id,
                ),
                default=default,
            )
        environment = await find_environment_by_daimon_tag(
            client,
            tenant_id=row.tenant_id,
            name=scoped.environment_name or "default",
            records=session_factory,
        )
    except Exception as exc:  # any read failure refuses the opt-in
        raise SandboxPinUnavailable(str(exc)) from exc
    if agent.archived_at is not None or environment is None:
        raise SandboxPinUnavailable("the routine's agent or environment is not live")
    return SandboxPin(
        agent_id=agent.id,
        agent_version=agent.version,
        environment_id=environment.id,
        environment_updated_at=_revision(environment),
    )


async def current_agent_pin(
    client: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    environment_name: str | None,
    default: DeploymentDefault,
) -> AgentSandboxPin:
    """The agent and environment an operator's opt-in would name now.

    Without `environment_name` it is the workspace's default environment, as a
    routine without a channel setting resolves it. Raises `SandboxPinUnavailable`
    when either is archived, missing or unreadable.
    """
    try:
        agent = await find_agent_by_daimon_tag(
            client, tenant_id=tenant_id, name=agent_name, records=session_factory
        )
        if environment_name is None:
            async with session_factory() as session:
                scoped = await resolve(
                    session,
                    context=ScopeContext(tenant_id=tenant_id, channel_id=None),
                    default=default,
                )
            environment_name = scoped.environment_name or "default"
        environment = await find_environment_by_daimon_tag(
            client, tenant_id=tenant_id, name=environment_name, records=session_factory
        )
    except Exception as exc:  # any read failure refuses the opt-in
        raise SandboxPinUnavailable(str(exc)) from exc
    if agent is None or agent.archived_at is not None or environment is None:
        raise SandboxPinUnavailable("the agent or environment is not live")
    return AgentSandboxPin(agent_id=agent.id, environment_name=environment_name)


async def run_matches_pin(
    client: AsyncAnthropic,
    pin: SandboxPin | AgentSandboxPin,
    session: BetaManagedAgentsSession,
    *,
    tenant_id: uuid.UUID | None,
    records: async_sessionmaker[AsyncSession] | None = None,
) -> bool:
    """True only when the run's own session uses exactly the pinned agent and environment.

    The agent id and version come from the session snapshot itself; the
    environment's revision is read once. Any read failure is a mismatch. An
    `AgentSandboxPin` needs only the same agent id and the environment its name
    resolves to now.
    """
    if isinstance(pin, AgentSandboxPin):
        return await _agent_pin_matches(client, pin, session, tenant_id, records)
    if (
        tenant_id is None
        or session.agent.id != pin.agent_id
        or session.agent.version != pin.agent_version
        or session.environment_id != pin.environment_id
    ):
        return False
    try:
        environment = await retrieve_environment(
            client,
            pin.environment_id,
            scope=resource_scope(tenant_id=str(tenant_id)),
            records=records,
        )
    except Exception:  # unreadable: fail closed
        log.warning("routine.sandbox_pin_unreadable", environment_id=pin.environment_id)
        return False
    return environment.archived_at is None and _revision(environment) == pin.environment_updated_at


async def _agent_pin_matches(
    client: AsyncAnthropic,
    pin: AgentSandboxPin,
    session: BetaManagedAgentsSession,
    tenant_id: uuid.UUID | None,
    records: async_sessionmaker[AsyncSession] | None,
) -> bool:
    if tenant_id is None or session.agent.id != pin.agent_id:
        return False
    try:
        environment = await find_environment_by_daimon_tag(
            client, tenant_id=tenant_id, name=pin.environment_name, records=records
        )
    except Exception:  # unreadable: fail closed
        log.warning("routine.sandbox_pin_unreadable", environment_name=pin.environment_name)
        return False
    return (
        environment is not None
        and environment.archived_at is None
        and environment.id == session.environment_id
    )


async def builtins_allowed(
    session_factory: async_sessionmaker[AsyncSession] | None,
    settings: Settings | None,
    tenant_id: uuid.UUID | None,
    platform: str | None,
    agent_id: str,
) -> bool:
    """Whether `unattended_capabilities` lets an opted-in routine keep its built-ins.

    With no store nothing is set, so the default applies; unreadable, they stay off.
    Caps both a routine's own opt-in and an agent-level one.
    """
    if session_factory is None or tenant_id is None:
        return FEATURES["unattended_capabilities"].default == "on"
    place = FeaturePlace.of(tenant_id, platform or "routine", None, agent_id=agent_id)
    found = await place_guardrail(session_factory, settings, place, "unattended_capabilities")
    return found.on
