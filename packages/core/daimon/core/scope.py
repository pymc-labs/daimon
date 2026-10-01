"""Pure scope types and merge logic for three-tier config resolution.

No I/O, no SQLAlchemy imports. Imported by stores and adapters alike.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

ConfigField = Literal["agent_name", "environment_name"]

ConfigTier = Literal["thread", "channel", "tenant", "deployment"]


class DeploymentDefault(BaseModel):
    """Injected deployment-level config from defaults/config.yaml.

    Parsed at startup by `parse_deployment_default` into a `DeploymentDefault`
    and injected into `resolve()` as the bottom tier of the config cascade.
    """

    model_config = ConfigDict(frozen=True)

    agent_name: str | None = None
    environment_name: str | None = None


class UserScopeRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["user"] = "user"
    account_id: uuid.UUID


class ChannelScopeRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["channel"] = "channel"
    tenant_id: uuid.UUID
    channel_id: str


class TenantScopeRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["tenant"] = "tenant"
    tenant_id: uuid.UUID


ScopeRef = UserScopeRef | ChannelScopeRef | TenantScopeRef


class ScopeContext(BaseModel):
    """Inbound dimensions for resolution."""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    channel_id: str | None = None
    account_id: uuid.UUID | None = None
    platform: str | None = None
    thread_id: str | None = None


class UserConfigRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_name: str | None = None
    environment_name: str | None = None


class ChannelConfigRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    channel_id: str
    agent_name: str | None = None
    environment_name: str | None = None
    mode: Literal["agent", "user_active"] = "agent"
    agent_name_set_by_account_id: uuid.UUID | None = None
    agent_name_set_at: datetime | None = None


class TenantConfigRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    agent_name: str | None = None
    environment_name: str | None = None
    mode: Literal["agent", "user_active"] = "agent"
    agent_name_set_by_account_id: uuid.UUID | None = None
    agent_name_set_at: datetime | None = None


class ResolvedConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_name: str | None = None
    agent_name_tier: ConfigTier | None = None
    environment_name: str | None = None
    environment_name_tier: ConfigTier | None = None
    responder_ma_agent_id: str | None = None
    configuration_target_ma_agent_id: str | None = None
    configuration_target_name: str | None = None
    thread_binding_id: uuid.UUID | None = None
    # Why this thread has its own responder: a setup conversation, or a task
    # handed to another agent. None when no binding applies. The two resolve
    # their responder differently -- a setup thread must answer as the built-in
    # Daimon, a handoff thread answers as whatever agent received the task --
    # so the kind has to survive as far as admission.
    thread_binding_kind: Literal["setup", "handoff"] | None = None


class PropagateOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: ScopeRef
    fields_written: list[ConfigField]


class PropagateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcomes: list[PropagateOutcome]


def pick_agent(
    channel: ChannelConfigRow | None,
    tenant: TenantConfigRow | None,
    default: DeploymentDefault,
) -> tuple[str | None, ConfigTier | None]:
    """Walk tiers channel→tenant, honoring mode='agent' only; fall through to deployment default.

    Returns (agent_name, tier).
    """
    tiers: tuple[
        tuple[ChannelConfigRow | None, ConfigTier],
        tuple[TenantConfigRow | None, ConfigTier],
    ] = (
        (channel, "channel"),
        (tenant, "tenant"),
    )
    for row, tier in tiers:
        if row is not None and row.mode == "agent" and row.agent_name:
            return row.agent_name, tier
    if default.agent_name:
        return default.agent_name, "deployment"
    return None, None


def _pick_environment(
    channel: ChannelConfigRow | None,
    tenant: TenantConfigRow | None,
    default: DeploymentDefault,
) -> tuple[str | None, ConfigTier | None]:
    """Walk tiers channel→tenant for environment_name; mode is ignored.

    Returns (environment_name, tier).
    """
    tiers: tuple[
        tuple[ChannelConfigRow | None, ConfigTier],
        tuple[TenantConfigRow | None, ConfigTier],
    ] = (
        (channel, "channel"),
        (tenant, "tenant"),
    )
    for row, tier in tiers:
        if row is not None and row.environment_name:
            return row.environment_name, tier
    if default.environment_name:
        return default.environment_name, "deployment"
    return None, None


def is_agent_reachable(
    agent_name: str,
    *,
    tenant: TenantConfigRow | None,
    channels: Sequence[ChannelConfigRow],
    default: DeploymentDefault,
) -> bool:
    """Answer whether any user in the tenant can currently reach the named agent.

    Reachability includes the deployment tier: on a fresh install with no
    config rows at all, the agent named by the deployment default is reachable,
    because that is what every mention resolves to via `pick_agent`'s
    fall-through. A tenant row in mode='agent' with a non-empty agent_name
    overrides that fall-through for the whole tenant; a channel row only ever
    overrides for its own channel and never suppresses the deployment tier
    for the rest.

    Deliberately blind to the agent's name, to any seeded/managed marker, and
    to ownership stamps — it answers only whether the name is reachable
    through the channel/tenant/deployment cascade, nothing about what the
    agent is or who created it.
    """
    if any(row.mode == "agent" and row.agent_name == agent_name for row in channels):
        return True
    if tenant is not None and tenant.mode == "agent" and tenant.agent_name == agent_name:
        return True
    tenant_consumes_fallthrough = (
        tenant is not None and tenant.mode == "agent" and bool(tenant.agent_name)
    )
    return default.agent_name == agent_name and not tenant_consumes_fallthrough


class AnsweringPlace(BaseModel):
    """One place the cascade currently sends mentions to a given agent.

    `channel_id` is set only on the channel tier; the tenant and deployment
    tiers describe a fall-through that has no single location.
    """

    model_config = ConfigDict(frozen=True)

    tier: Literal["channel", "tenant", "deployment"]
    channel_id: str | None = None


def answering_places(
    agent_name: str,
    *,
    tenant: TenantConfigRow | None,
    channels: Sequence[ChannelConfigRow],
    default: DeploymentDefault,
) -> tuple[AnsweringPlace, ...]:
    """Enumerate every tier that currently routes mentions to `agent_name`.

    The itemised form of `is_agent_reachable`: an empty result means exactly
    what a False from that predicate means, and a non-empty one means exactly
    what a True means. Same tier rules — a channel row counts only in
    mode='agent' with a non-empty agent_name, a tenant row in mode='agent'
    with a non-empty agent_name consumes the deployment fall-through for the
    whole tenant, and a channel row never suppresses that fall-through
    elsewhere.

    Ordered channel → tenant → deployment, channels by channel_id, so a
    renderer can walk the result without re-sorting.
    """
    places = [
        AnsweringPlace(tier="channel", channel_id=row.channel_id)
        for row in sorted(channels, key=lambda row: row.channel_id)
        if row.mode == "agent" and row.agent_name and row.agent_name == agent_name
    ]
    tenant_names_target = (
        tenant is not None and tenant.mode == "agent" and tenant.agent_name == agent_name
    )
    tenant_consumes_fallthrough = (
        tenant is not None and tenant.mode == "agent" and bool(tenant.agent_name)
    )
    if tenant_names_target:
        places.append(AnsweringPlace(tier="tenant"))
    if default.agent_name == agent_name and not tenant_consumes_fallthrough:
        places.append(AnsweringPlace(tier="deployment"))
    return tuple(places)


def merge(
    *,
    channel: ChannelConfigRow | None,
    tenant: TenantConfigRow | None,
    default: DeploymentDefault,
) -> ResolvedConfig:
    agent_name, agent_tier = pick_agent(channel, tenant, default)
    env_name, env_tier = _pick_environment(channel, tenant, default)
    return ResolvedConfig(
        agent_name=agent_name,
        agent_name_tier=agent_tier,
        environment_name=env_name,
        environment_name_tier=env_tier,
    )
