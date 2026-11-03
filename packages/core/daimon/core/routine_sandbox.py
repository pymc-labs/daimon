"""What an admin approved when opting a routine in to built-in sandbox tools.

The invariant: built-ins are enabled for a routine's run only when the run's
agent version and environment are exactly the ones recorded when the admin
opted in. Any mismatch, or a missing record, keeps them off.

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
from daimon.core.defaults.ma_index import find_environment_by_daimon_tag
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent, retrieve_environment
from daimon.core.rule_views import routine_destination_channel
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.domain import RoutineRow, SandboxPin
from daimon.core.stores.scoped_config_read import resolve
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "RoutineSandbox",
    "SandboxPin",
    "SandboxPinUnavailable",
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

    pin: SandboxPin
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
        agent = await retrieve_agent(client, row.agent_id, scope=scope)
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
            client, tenant_id=row.tenant_id, name=scoped.environment_name or "default"
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


async def run_matches_pin(
    client: AsyncAnthropic,
    pin: SandboxPin,
    session: BetaManagedAgentsSession,
    *,
    tenant_id: uuid.UUID | None,
) -> bool:
    """True only when the run's own session uses exactly the pinned agent and environment.

    The agent id and version come from the session snapshot itself; the
    environment's revision is read once. Any read failure is a mismatch.
    """
    if (
        tenant_id is None
        or session.agent.id != pin.agent_id
        or session.agent.version != pin.agent_version
        or session.environment_id != pin.environment_id
    ):
        return False
    try:
        environment = await retrieve_environment(
            client, pin.environment_id, scope=resource_scope(tenant_id=str(tenant_id))
        )
    except Exception:  # unreadable: fail closed
        log.warning("routine.sandbox_pin_unreadable", environment_id=pin.environment_id)
        return False
    return environment.archived_at is None and _revision(environment) == pin.environment_updated_at
