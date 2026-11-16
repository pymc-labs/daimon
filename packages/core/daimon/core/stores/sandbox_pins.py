"""Async store for `sandbox_pins`: what a `routine_sandbox_tools` On covers.

A pin is written only with the feature row it pins (`features.service.change_feature`);
a pin without its row, or a row without its pin, covers nothing. Callers own the
transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final, Literal

import structlog
from daimon.core._models import SandboxPinRecord, Tenant
from daimon.core.stores.channel_changes import WORKSPACE_ID, record_change
from daimon.core.stores.domain import AgentSandboxPin, AnySandboxPin, FeatureTarget, SandboxPin
from daimon.core.stores.feature_values import clear_feature_value, get_feature_value
from pydantic import BaseModel, ConfigDict, JsonValue
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

__all__ = [
    "SANDBOX_KEY",
    "PinnedTier",
    "SandboxPinEntry",
    "drop_routine_opt_in",
    "list_sandbox_pins",
    "pin_json",
    "put_sandbox_pin",
    "sandbox_pin",
]

log = structlog.get_logger(__name__)

PinnedTier = Literal["agent", "routine"]
SANDBOX_KEY: Final = "routine_sandbox_tools"


class SandboxPinEntry(BaseModel):
    """One recorded pin: the agent's (`key` is its id) or the routine's."""

    model_config = ConfigDict(frozen=True)

    tier: PinnedTier
    key: str
    pin: AnySandboxPin
    set_at: datetime


def _pin(row: SandboxPinRecord) -> AnySandboxPin:
    if row.tier == "agent":
        return AgentSandboxPin(agent_id=row.agent_id, environment_name=row.environment_name or "")
    assert row.agent_version is not None and row.environment_id is not None
    assert row.environment_updated_at is not None, "ck_sandbox_pins_shape"
    return SandboxPin(
        agent_id=row.agent_id,
        agent_version=row.agent_version,
        environment_id=row.environment_id,
        environment_updated_at=row.environment_updated_at,
    )


def _where(tenant_id: uuid.UUID, tier: PinnedTier, key: str) -> ColumnElement[bool]:
    if tier == "agent":
        return (
            (SandboxPinRecord.tenant_id == tenant_id)
            & (SandboxPinRecord.tier == "agent")
            & (SandboxPinRecord.agent_id == key)
        )
    return (
        (SandboxPinRecord.tenant_id == tenant_id)
        & (SandboxPinRecord.tier == "routine")
        & (SandboxPinRecord.routine_id == uuid.UUID(key))
    )


def pin_json(pin: AnySandboxPin) -> dict[str, JsonValue]:
    """A pin as a change record keeps it."""
    if isinstance(pin, AgentSandboxPin):
        return {"agent_id": pin.agent_id, "environment_name": pin.environment_name}
    return {
        "agent_id": pin.agent_id,
        "agent_version": pin.agent_version,
        "environment_id": pin.environment_id,
        "environment_updated_at": pin.environment_updated_at.isoformat(),
    }


async def sandbox_pin(
    session: AsyncSession, *, tenant_id: uuid.UUID, tier: PinnedTier, key: str
) -> AnySandboxPin | None:
    """The agent's (`key` is its id) or the routine's (its id) pin, or None."""
    row = await session.scalar(select(SandboxPinRecord).where(_where(tenant_id, tier, key)))
    return None if row is None else _pin(row)


async def list_sandbox_pins(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[SandboxPinEntry]:
    """Every pin in the tenant, agents first, oldest first."""
    rows = await session.scalars(
        select(SandboxPinRecord)
        .where(SandboxPinRecord.tenant_id == tenant_id)
        .order_by(SandboxPinRecord.tier, SandboxPinRecord.set_at, SandboxPinRecord.id)
    )
    return [
        SandboxPinEntry(
            tier="agent" if row.tier == "agent" else "routine",
            key=row.agent_id if row.tier == "agent" else str(row.routine_id),
            pin=_pin(row),
            set_at=row.set_at,
        )
        for row in rows
    ]


async def put_sandbox_pin(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    tier: PinnedTier,
    key: str,
    pin: AnySandboxPin | None,
) -> None:
    """Replace the tier's pin with `pin`; None drops it."""
    await session.execute(delete(SandboxPinRecord).where(_where(tenant_id, tier, key)))
    if isinstance(pin, AgentSandboxPin):
        assert tier == "agent", "an agent's pin names its environment"
        session.add(
            SandboxPinRecord(
                tenant_id=tenant_id,
                tier="agent",
                agent_id=pin.agent_id,
                environment_name=pin.environment_name,
            )
        )
    elif pin is not None:
        assert tier == "routine", "a routine's pin names its agent version"
        session.add(
            SandboxPinRecord(
                tenant_id=tenant_id,
                tier="routine",
                agent_id=pin.agent_id,
                routine_id=uuid.UUID(key),
                agent_version=pin.agent_version,
                environment_id=pin.environment_id,
                environment_updated_at=pin.environment_updated_at,
            )
        )
    await session.flush()


async def drop_routine_opt_in(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    routine_id: uuid.UUID,
    only_pin: SandboxPin | None = None,
) -> bool:
    """Clear a routine's own On and its pin, recorded as Daimon's change; an Off stays.

    With `only_pin`, only while that is still the routine's pin, so a fresh opt-in made
    since the run read it is kept. Returns whether an On was cleared.
    """
    key = str(routine_id)
    target = FeatureTarget("routine", routine_id=key)
    found = await get_feature_value(session, tenant_id=tenant_id, target=target, key=SANDBOX_KEY)
    if found is None or found.value != "on":
        return False
    pin = await sandbox_pin(session, tenant_id=tenant_id, tier="routine", key=key)
    if only_pin is not None and pin != only_pin:
        return False
    await clear_feature_value(session, tenant_id=tenant_id, target=target, key=SANDBOX_KEY)
    await put_sandbox_pin(session, tenant_id=tenant_id, tier="routine", key=key, pin=None)
    before: dict[str, JsonValue] = {"routine": key, "value": "on"}
    if pin is not None:
        before["pin"] = pin_json(pin)
    platform = await session.scalar(select(Tenant.platform).where(Tenant.id == tenant_id))
    await record_change(
        session,
        tenant_id=tenant_id,
        platform=platform or "",
        channel_id=WORKSPACE_ID,
        setting=SANDBOX_KEY,
        before=before,
        after=None,
        actor_account_id=None,
        surface="system",
    )
    return True
