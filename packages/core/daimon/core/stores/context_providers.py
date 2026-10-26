"""Tenant-isolated context provider settings. Caller owns the transaction."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from daimon.core._models import ContextProviderBinding
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Subject, authorize
from daimon.core.context_provider import ProviderConfig
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def _scope(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    # Transaction local: pooled connections never retain another tenant's scope.
    await session.execute(select(func.set_config("daimon.tenant_id", str(tenant_id), True)))


async def load(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str | None,
    agent_id: str,
) -> ProviderConfig | None:
    """Exact channel+agent > channel > agent > tenant. Disabled overrides stop fallback."""
    await _scope(session, tenant_id)
    rows = (
        await session.scalars(
            select(ContextProviderBinding).where(
                ContextProviderBinding.tenant_id == tenant_id,
                ContextProviderBinding.channel_id.in_(("", channel_id or "")),
                ContextProviderBinding.agent_id.in_(("", agent_id)),
            )
        )
    ).all()
    if not rows:
        return None
    row = max(rows, key=lambda row: (bool(row.channel_id), bool(row.agent_id)))
    return ProviderConfig.model_validate(row.config)


async def set_provider(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    config: ProviderConfig,
    actor: Subject,
    set_by: str,
    channel_id: str = "",
    agent_id: str = "",
) -> None:
    if not authorize(TenantAccessPolicy(), subject=actor, action=Action.SET_CONTEXT_PROVIDER):
        raise PermissionError("only a human workspace admin may change context providers")
    if config.bearer_env and not config.bearer_env.startswith(
        f"DAIMON_CONTEXT_{tenant_id.hex.upper()}_"
    ):
        raise ValueError("provider credential reference belongs to another scope")
    await _scope(session, tenant_id)
    stmt = insert(ContextProviderBinding).values(
        tenant_id=tenant_id,
        channel_id=channel_id,
        agent_id=agent_id,
        config=config.model_dump(mode="json"),
        updated_by=set_by,
        updated_at=datetime.now(UTC),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["tenant_id", "channel_id", "agent_id"],
            set_={
                "config": stmt.excluded.config,
                "updated_by": stmt.excluded.updated_by,
                "updated_at": stmt.excluded.updated_at,
            },
        )
    )
    await session.flush()
    structlog.get_logger(__name__).info(
        "context_provider.config_changed",
        tenant_id=str(tenant_id),
        set_by=set_by,
        enabled=config.enabled,
    )
