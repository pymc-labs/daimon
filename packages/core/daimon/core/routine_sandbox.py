"""Built-in sandbox tools for a routine's runs: the `routine_sandbox_tools` feature.

It resolves for the routine like any feature, so a routine's own value beats its
agent's unless a tier above locks it. An On holds only with the pin kept on the row
that decided it: a routine's pins the agent version and the environment revision, an
agent's pins the agent id and the environment's name.
Any mismatch, or a missing pin, keeps built-ins off, so a provider or environment
change needs a new opt-in. `unattended_capabilities` off still wins.

`current_pin` and `current_agent_pin` read what an opt-in would pin now (an
unreadable one refuses it) for `change_sandbox`; `routine_sandbox` hands the deciding
pin to the headless runner, which checks it against the run's own session
(`run_matches_pin`).
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
from daimon.core.features.snapshot import FeaturePlace, feature_in, place_guardrail
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent, retrieve_environment
from daimon.core.rule_views import routine_destination_channel
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.domain import (
    SANDBOX_KEY,
    AgentSandboxPin,
    AnySandboxPin,
    FeatureTarget,
    FeatureValueRow,
    PinnedTier,
    RoutineRow,
    SandboxPin,
    stored_pin,
)
from daimon.core.stores.feature_values import get_feature_value, list_feature_values
from daimon.core.stores.scoped_config_read import resolve
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "DM_SANDBOX_REFUSAL",
    "PIN_UNAVAILABLE_REFUSAL",
    "SANDBOX_KEY",
    "AgentSandboxPin",
    "RoutineSandbox",
    "SandboxPin",
    "builtins_allowed",
    "change_sandbox",
    "clear_agent_opt_ins",
    "current_agent_pin",
    "current_pin",
    "opted_in_by",
    "own_opt_in",
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
    "Can't read the {what} or its environment, so sandbox tools can't be turned on for "
    "it. Nothing was changed."
)


def _unavailable(what: str, reason: str) -> ChannelChangeRefused:
    log.warning("routine.sandbox_pin_unavailable", what=what, reason=reason)
    return ChannelChangeRefused(
        PIN_UNAVAILABLE_REFUSAL.format(what=what), kind="invalid", reason="pin_unavailable"
    )


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
    """What a routine's own On would pin now: its agent's version and the environment its
    channel resolves to, as the scheduler resolves it at fire time.

    Raises `ChannelChangeRefused` for a DM routine, which runs as its owner's private
    conversation and takes no On of its own, or when either is archived, missing or
    unreadable.
    """
    if row.destination_kind == "dm":
        raise ChannelChangeRefused(DM_SANDBOX_REFUSAL, kind="invalid", reason="dm_routine")
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
        raise _unavailable("routine's agent", str(exc)) from exc
    if agent.archived_at is not None or environment is None:
        raise _unavailable("routine's agent", "not live")
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
    routine without a channel setting resolves it. Raises `ChannelChangeRefused` when
    either is archived, missing or unreadable.
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
        raise _unavailable(f"agent {agent_name}", str(exc)) from exc
    if agent is None or agent.archived_at is not None or environment is None:
        raise _unavailable(f"agent {agent_name}", "not live")
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
        routine_id=row.id,
    )


async def _deciding(
    session: AsyncSession, settings: Settings | None, row: RoutineRow, platform: str
) -> FeatureValueRow | None:
    """The agent's or the routine's own row when it decides an On for the routine."""
    place = routine_place(row, platform)
    found = await feature_in(session, settings, place, "routine_sandbox_tools")
    if not found.on or found.tier not in ("agent", "routine"):
        return None
    tier: PinnedTier = "agent" if found.tier == "agent" else "routine"
    target = FeatureTarget(tier, agent_id=row.agent_id, routine_id=row.id)
    return await get_feature_value(session, tenant_id=row.tenant_id, target=target, key=SANDBOX_KEY)


async def opted_in_by(session: AsyncSession, row: RoutineRow, platform: str) -> PinnedTier | None:
    """Whose pinned On the routine runs under, its own or its agent's; None when off.
    What every surface shows; each run also checks the pin still matches (`routine_sandbox`)."""
    found = await _deciding(session, None, row, platform)
    if found is None or found.pin is None:
        return None
    return "agent" if found.tier == "agent" else "routine"


async def own_opt_in(
    sessions: async_sessionmaker[AsyncSession], place: FeaturePlace
) -> tuple[str | None, AnySandboxPin | None]:
    """The own `routine_sandbox_tools` value (None when unset) and pin of the place's routine,
    or of its agent when it names none."""
    target = FeatureTarget(
        "routine" if place.routine_id else "agent",
        agent_id=place.agent_id,
        routine_id=place.routine_id,
    )
    async with sessions() as session:
        found = await get_feature_value(
            session, tenant_id=place.tenant_id, target=target, key=SANDBOX_KEY
        )
    return (None, None) if found is None else (found.value, stored_pin(found))


async def change_sandbox(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor: FeatureActor,
    place: FeaturePlace,
    *,
    value: str,
    pin: AnySandboxPin | None,
) -> FeatureChange | None:
    """Set the place's routine's own `routine_sandbox_tools`, or its agent's when it names no
    routine (`DEFAULT` clears it); an On carries the pin from `current_pin` or
    `current_agent_pin`. None when it already holds that value and pin."""
    if await own_opt_in(sessions, place) == (None if value == DEFAULT else value, pin):
        return None
    return await change_feature(
        sessions,
        settings,
        actor,
        key=SANDBOX_KEY,
        tier="routine" if place.routine_id else "agent",
        value=value,
        place=place,
        pin=pin,
    )


async def clear_agent_opt_ins(session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: str) -> int:
    """Clear the agent's own On and every routine On pinned to it, in the caller's
    transaction, before its rows move to another agent id: an opt-in covers that id only.
    Returns how many it cleared."""
    cleared = 0
    for found in await list_feature_values(session, tenant_id=tenant_id, keys=[SANDBOX_KEY]):
        pinned_to = found.agent_id if found.tier == "agent" else (found.pin or {}).get("agent_id")
        if found.value == "on" and found.tier in ("agent", "routine") and pinned_to == agent_id:
            target = FeatureTarget(
                found.tier, agent_id=found.agent_id or None, routine_id=found.routine_id
            )
            cleared += await clear_pinned_on(
                session,
                None,
                tenant_id=tenant_id,
                key=SANDBOX_KEY,
                target=target,
                revision=found.revision,
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
    try:
        async with sessions() as session:
            found = await _deciding(session, settings, row, platform)
        pin = None if found is None else stored_pin(found)
    except Exception:  # unreadable: no opt-in
        log.exception("routine.sandbox_pin_unreadable", routine_id=str(row.id))
        return None
    if found is None:
        return None
    if pin is None:
        log.warning("routine.sandbox_pin_missing", routine_id=str(row.id), tier=found.tier)
        return None
    decided = found

    async def on_drift() -> None:
        log.info("routine.sandbox_pin_mismatched", routine_id=str(row.id), tier=decided.tier)
        if decided.tier != "routine":
            return
        try:
            async with sessions.begin() as session:
                await clear_pinned_on(
                    session,
                    settings,
                    tenant_id=row.tenant_id,
                    key=SANDBOX_KEY,
                    target=FeatureTarget("routine", routine_id=row.id),
                    revision=decided.revision,
                )
        except Exception:  # the run already went ahead without built-ins
            log.exception("routine.sandbox_drift_clear_failed", routine_id=str(row.id))

    return RoutineSandbox(pin=pin, on_drift=on_drift)
