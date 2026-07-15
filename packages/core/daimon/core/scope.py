"""Pure scope types and merge logic for three-tier config resolution.

No I/O, no SQLAlchemy imports. Imported by stores and adapters alike.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

ConfigField = Literal["agent_name", "environment_name"]

ConfigTier = Literal["channel", "tenant", "deployment"]


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


class PropagateOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: ScopeRef
    fields_written: list[ConfigField]


class PropagateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcomes: list[PropagateOutcome]


def _pick_agent(
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


def merge(
    *,
    channel: ChannelConfigRow | None,
    tenant: TenantConfigRow | None,
    default: DeploymentDefault,
) -> ResolvedConfig:
    agent_name, agent_tier = _pick_agent(channel, tenant, default)
    env_name, env_tier = _pick_environment(channel, tenant, default)
    return ResolvedConfig(
        agent_name=agent_name,
        agent_name_tier=agent_tier,
        environment_name=env_name,
        environment_name_tier=env_tier,
    )
