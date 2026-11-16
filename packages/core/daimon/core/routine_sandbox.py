"""Built-in sandbox tools for a routine's runs: the `routine_sandbox_tools` feature.

It resolves for the routine like any feature, so a routine's own value beats its
agent's unless a tier above locks it. An On holds only with the pin of the tier
that decided it (`sandbox_pins`): a routine's pins the agent version and the
environment revision, an agent's pins the agent id and the environment's name.
Any mismatch, or a missing pin, keeps built-ins off, so a provider or environment
change needs a new opt-in. `unattended_capabilities` off still wins.

`current_pin` and `current_agent_pin` read what an opt-in would pin now (an
unreadable one refuses it); `routine_sandbox` hands the deciding pin to the
headless runner, which checks it against the run's own session (`run_matches_pin`).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsSession
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.config import Settings
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, find_environment_by_daimon_tag
from daimon.core.features.registry import DEFAULT, FEATURES
from daimon.core.features.service import (
    FeatureActor,
    FeatureChange,
    change_feature,
    clear_pinned_on,
)
from daimon.core.features.snapshot import FeaturePlace, place_guardrail
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent, retrieve_environment
from daimon.core.rule_views import routine_destination_channel
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.domain import (
    AgentSandboxPin,
    AnySandboxPin,
    FeatureTarget,
    RoutineRow,
    SandboxPin,
)
from daimon.core.stores.feature_values import get_feature_value
from daimon.core.stores.sandbox_pins import SANDBOX_KEY, PinnedTier, list_sandbox_pins, sandbox_pin
from daimon.core.stores.scoped_config_read import resolve
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "DM_SANDBOX_REFUSAL",
    "PIN_UNAVAILABLE_REFUSAL",
    "SANDBOX_KEY",
    "AgentSandboxPin",
    "RoutineSandbox",
    "SandboxPin",
    "SandboxPinUnavailable",
    "builtins_allowed",
    "change_agent_sandbox",
    "change_routine_sandbox",
    "clear_agent_opt_ins",
    "current_agent_pin",
    "current_pin",
    "own_opt_in",
    "routine_opt_in_pin",
    "routine_place",
    "routine_sandbox",
    "run_matches_pin",
]

log = structlog.get_logger(__name__)

DM_SANDBOX_REFUSAL = (
    "DM routines can't use sandbox tools of their own: they run as the owner's private "
    "conversation. An operator can turn them on for the agent. Nothing was changed."
)
PIN_UNAVAILABLE_REFUSAL = (
    "Can't read the routine's agent version or environment, so sandbox tools can't be "
    "turned on for it. Nothing was changed."
)


class SandboxPinUnavailable(Exception):
    """The routine's agent or environment cannot be read, so nothing can be pinned."""


@dataclass(frozen=True)
class RoutineSandbox:
    """A routine's opt-in, handed to the headless runner for one fire."""

    pin: AnySandboxPin
    #: Called once when the run's agent or environment no longer matches `pin`.
    on_drift: Callable[[], Awaitable[None]]


def _revision(environment: BetaEnvironment) -> datetime:
    """An environment's revision: its `updated_at`, which the SDK gives as text."""
    return datetime.fromisoformat(environment.updated_at)


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
    pin: AnySandboxPin,
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


def routine_place(row: RoutineRow, platform: str) -> FeaturePlace:
    """Where a routine's runs resolve features: its destination, its agent and itself."""
    return FeaturePlace.of(
        row.tenant_id,
        platform,
        routine_destination_channel(row),
        agent_id=row.agent_id,
        routine_id=str(row.id),
    )


async def own_opt_in(
    sessions: async_sessionmaker[AsyncSession], place: FeaturePlace, tier: PinnedTier
) -> tuple[str | None, AnySandboxPin | None]:
    """The tier's own `routine_sandbox_tools` value (None when unset) and its pin."""
    key = place.agent_id if tier == "agent" else place.routine_id
    assert key is not None, "the place names the agent or routine"
    target = FeatureTarget(tier, agent_id=place.agent_id, routine_id=place.routine_id)
    async with sessions() as session:
        found = await get_feature_value(
            session, tenant_id=place.tenant_id, target=target, key=SANDBOX_KEY
        )
        pin = await sandbox_pin(session, tenant_id=place.tenant_id, tier=tier, key=key)
    return (found.value if found is not None else None), pin


async def routine_opt_in_pin(
    client: AsyncAnthropic,
    sessions: async_sessionmaker[AsyncSession],
    row: RoutineRow,
    *,
    value: str,
    default: DeploymentDefault,
) -> SandboxPin | None:
    """The pin a routine's own `value` carries: for On, the agent version and environment
    it would run with now; nothing otherwise. A DM routine takes no On of its own: it
    runs as its owner's private conversation."""
    if value != "on":
        return None
    if row.destination_kind == "dm":
        raise ChannelChangeRefused(DM_SANDBOX_REFUSAL, kind="invalid", reason="dm_routine")
    try:
        return await current_pin(client, sessions, row, default=default)
    except SandboxPinUnavailable as exc:
        raise ChannelChangeRefused(
            PIN_UNAVAILABLE_REFUSAL, kind="invalid", reason="pin_unavailable"
        ) from exc


async def change_routine_sandbox(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor: FeatureActor,
    row: RoutineRow,
    *,
    platform: str,
    value: str,
    pin: SandboxPin | None,
) -> FeatureChange | None:
    """Set a routine's own `routine_sandbox_tools` (`DEFAULT` clears it) with the pin from
    `routine_opt_in_pin`; None when it already holds that value and pin."""
    place = routine_place(row, platform)
    if await own_opt_in(sessions, place, "routine") == (None if value == DEFAULT else value, pin):
        return None
    return await change_feature(
        sessions,
        settings,
        actor,
        key=SANDBOX_KEY,
        tier="routine",
        value=value,
        place=place,
        pin=pin,
    )


async def change_agent_sandbox(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor: FeatureActor,
    *,
    agent_id: str,
    platform: str,
    value: str,
    pin: AgentSandboxPin | None,
) -> FeatureChange | None:
    """Set an agent's own `routine_sandbox_tools` (`DEFAULT` clears it), an On with the pin
    from `current_agent_pin`; None when it already holds that value and pin."""
    place = FeaturePlace.of(actor.tenant_id, platform, None, agent_id=agent_id)
    if await own_opt_in(sessions, place, "agent") == (None if value == DEFAULT else value, pin):
        return None
    return await change_feature(
        sessions, settings, actor, key=SANDBOX_KEY, tier="agent", value=value, place=place, pin=pin
    )


async def clear_agent_opt_ins(session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: str) -> int:
    """Clear the agent's own On and every routine On pinned to it, in the caller's
    transaction, before its rows move to another agent id: an opt-in covers that id only.
    Returns how many it cleared."""
    cleared = await clear_pinned_on(
        session, None, tenant_id=tenant_id, key=SANDBOX_KEY, tier="agent", holder=agent_id
    )
    for entry in await list_sandbox_pins(session, tenant_id=tenant_id):
        if entry.tier == "routine" and entry.pin.agent_id == agent_id:
            cleared += await clear_pinned_on(
                session,
                None,
                tenant_id=tenant_id,
                key=SANDBOX_KEY,
                tier="routine",
                holder=entry.key,
                only_pin=entry.pin,
            )
    return cleared


async def routine_sandbox(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    row: RoutineRow,
    *,
    platform: str,
) -> RoutineSandbox | None:
    """The opt-in for this fire, or None when `routine_sandbox_tools` is off for it.

    When the run no longer matches the deciding pin it goes ahead without built-ins. A
    routine's own On is then cleared, so an admin approves the new agent or environment;
    an agent's is kept, as its other routines may still match.
    """
    place = routine_place(row, platform)
    found = await place_guardrail(sessions, settings, place, "routine_sandbox_tools")
    if not found.on or found.tier not in ("agent", "routine"):
        return None
    tier: PinnedTier = "agent" if found.tier == "agent" else "routine"
    key = row.agent_id if tier == "agent" else str(row.id)
    try:
        async with sessions() as session:
            pin = await sandbox_pin(session, tenant_id=row.tenant_id, tier=tier, key=key)
    except Exception:  # unreadable: no opt-in
        log.exception("routine.sandbox_pin_unreadable", routine_id=str(row.id), tier=tier)
        return None
    if pin is None:
        log.warning("routine.sandbox_pin_missing", routine_id=str(row.id), tier=tier)
        return None

    async def on_drift() -> None:
        log.info("routine.sandbox_pin_mismatched", routine_id=str(row.id), tier=tier)
        if not isinstance(pin, SandboxPin):
            return
        try:
            async with sessions.begin() as session:
                await clear_pinned_on(
                    session,
                    settings,
                    tenant_id=row.tenant_id,
                    key=SANDBOX_KEY,
                    tier="routine",
                    holder=str(row.id),
                    only_pin=pin,
                )
        except Exception:  # the run already went ahead without built-ins
            log.exception("routine.sandbox_drift_clear_failed", routine_id=str(row.id))

    return RoutineSandbox(pin=pin, on_drift=on_drift)
