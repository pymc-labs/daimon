"""Scoped config writes: set, unset, and propagate across scope tiers."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from daimon.core._models import (
    Account,
    ChannelConfig,
    TenantConfig,
    UserConfig,
)
from daimon.core.errors import StoreError
from daimon.core.scope import (
    ChannelScopeRef,
    ConfigField,
    PropagateOutcome,
    PropagateResult,
    ScopeRef,
    TenantScopeRef,
    UserScopeRef,
)
from daimon.core.stores.scoped_config_read import get_scope
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

_CONFIG_FIELDS: list[ConfigField] = ["agent_name", "environment_name"]

PropagationMode = Literal["agent", "user_active"]


async def set_fields(
    session: AsyncSession,
    *,
    scope: ScopeRef,
    tenant_id: uuid.UUID,
    agent_name: str | None = None,
    environment_name: str | None = None,
    mode: PropagationMode | None = None,
    actor_account_id: uuid.UUID | None = None,
) -> None:
    """Upsert config fields for a scope. Fields left as None are not touched."""
    updates: dict[str, str | uuid.UUID | None | ColumnElement[datetime]] = {}
    if agent_name is not None:
        if agent_name == "":
            raise StoreError("agent_name must not be empty")
        updates["agent_name"] = agent_name
        if isinstance(scope, (ChannelScopeRef, TenantScopeRef)):
            updates["agent_name_set_by_account_id"] = actor_account_id
            updates["agent_name_set_at"] = func.now()  # pyright: ignore[reportUnknownMemberType]  # sqlalchemy.func.now() is dynamically typed by dialect
    if environment_name is not None:
        if environment_name == "":
            raise StoreError("environment_name must not be empty")
        updates["environment_name"] = environment_name
    if mode is not None and isinstance(scope, (ChannelScopeRef, TenantScopeRef)):
        # User scope: silently ignore — user_config has no mode column.
        updates["mode"] = mode
    if not updates:
        raise StoreError("set_fields requires at least one field to set")

    # Scope-specific validation and upsert.
    if isinstance(scope, UserScopeRef):
        # Validate account belongs to tenant.
        acct_tenant = (
            await session.execute(select(Account.tenant_id).where(Account.id == scope.account_id))
        ).scalar_one_or_none()
        if acct_tenant is None or acct_tenant != tenant_id:
            raise StoreError("account does not belong to tenant")

        stmt = (
            pg_insert(UserConfig)
            .values(account_id=scope.account_id, **updates)
            .on_conflict_do_update(index_elements=["account_id"], set_=updates)
        )
        await session.execute(stmt)

    elif isinstance(scope, ChannelScopeRef):
        if scope.tenant_id != tenant_id:
            raise StoreError("scope.tenant_id does not match tenant_id")
        stmt = (
            pg_insert(ChannelConfig)
            .values(
                tenant_id=scope.tenant_id,
                channel_id=scope.channel_id,
                **updates,
            )
            .on_conflict_do_update(
                constraint="pk_channel_config",
                set_=updates,
            )
        )
        await session.execute(stmt)

    else:
        # TenantScopeRef is the only remaining variant.
        if scope.tenant_id != tenant_id:
            raise StoreError("scope.tenant_id does not match tenant_id")
        stmt = (
            pg_insert(TenantConfig)
            .values(tenant_id=scope.tenant_id, **updates)
            .on_conflict_do_update(constraint="pk_tenant_config", set_=updates)
        )
        await session.execute(stmt)

    await session.flush()


async def unset_fields(
    session: AsyncSession,
    *,
    scope: ScopeRef,
    fields: list[ConfigField],
    actor_account_id: uuid.UUID | None = None,
) -> None:
    """Set specified fields to NULL. Auto-deletes the row if all config fields become NULL."""
    if isinstance(scope, UserScopeRef):
        model = UserConfig
        pk_filter = UserConfig.account_id == scope.account_id
    elif isinstance(scope, ChannelScopeRef):
        model = ChannelConfig
        pk_filter = (ChannelConfig.tenant_id == scope.tenant_id) & (
            ChannelConfig.channel_id == scope.channel_id
        )
    else:
        # TenantScopeRef is the only remaining variant.
        model = TenantConfig
        pk_filter = TenantConfig.tenant_id == scope.tenant_id

    orm = (await session.execute(select(model).where(pk_filter))).scalar_one_or_none()
    if orm is None:
        return

    for f in fields:
        setattr(orm, f, None)

    # Stamp the actor on agent_name clears for channel/tenant scopes.
    # The stamp records who cleared agent_name; auto-delete below may discard
    # the row, in which case the stamp is harmless.
    if "agent_name" in fields and isinstance(orm, (ChannelConfig, TenantConfig)):
        orm.agent_name_set_by_account_id = actor_account_id
        orm.agent_name_set_at = func.now()  # pyright: ignore[reportUnknownMemberType]  # sqlalchemy.func.now() is dynamically typed by dialect

    await session.flush()

    # Auto-delete if all config fields are NULL — except preserve mode='user_active'
    # rows. A user_active row legitimately carries agent_name=NULL because the mode
    # IS the propagation (resolver substitutes the invoker's active agent at read time).
    all_fields_null = all(getattr(orm, f) is None for f in _CONFIG_FIELDS)
    is_user_active = isinstance(orm, (ChannelConfig, TenantConfig)) and orm.mode == "user_active"
    if all_fields_null and not is_user_active:
        await session.execute(delete(model).where(pk_filter))
        await session.flush()


async def clear_agent_references(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
) -> int:
    """Clear every channel- and tenant-scope row in one tenant that names an agent.

    Called when an agent goes away (deleted or archived). A scope row still
    naming a dead agent makes resolution fail for that channel — or, for the
    tenant row, for the whole install — until someone re-scopes by hand.
    Clearing the name lets the cascade fall through to the next tier.

    Every mutation is delegated to `unset_fields` rather than issued as a bulk
    UPDATE, because `unset_fields` already owns the two rules that matter here:
    a row whose config fields all become NULL is removed outright, and a row
    carrying mode='user_active' survives the clear because that mode IS the
    propagation. Agent deletion is a rare admin action, so the per-row loop
    costs nothing worth optimizing away.

    `user_config` is deliberately left alone: `resolve()` never consults the
    user tier, so clearing it would change nothing observable.

    Returns the number of scope rows cleared. The caller owns the transaction.
    """
    return await _clear_references(
        session, tenant_id=tenant_id, field="agent_name", value=agent_name
    )


async def clear_environment_references(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    environment_name: str,
) -> int:
    """Clear every channel- and tenant-scope pick of an environment in one tenant.

    The environment counterpart of `clear_agent_references`, for when an
    environment is archived or deleted: each pick falls through to the next
    tier instead of failing every turn there. Returns the number of scope rows
    cleared. The caller owns the transaction.
    """
    return await _clear_references(
        session, tenant_id=tenant_id, field="environment_name", value=environment_name
    )


async def _clear_references(
    session: AsyncSession, *, tenant_id: uuid.UUID, field: ConfigField, value: str
) -> int:
    channel_column = (
        ChannelConfig.agent_name if field == "agent_name" else ChannelConfig.environment_name
    )
    tenant_column = (
        TenantConfig.agent_name if field == "agent_name" else TenantConfig.environment_name
    )
    # Materialize the channel ids before mutating: unset_fields may remove rows,
    # so iterating a live result set while it writes would be wrong.
    channel_ids: list[str] = list(
        (
            await session.execute(
                select(ChannelConfig.channel_id).where(
                    ChannelConfig.tenant_id == tenant_id, channel_column == value
                )
            )
        )
        .scalars()
        .all()
    )
    tenant_row_matches = (
        await session.execute(
            select(TenantConfig.tenant_id).where(
                TenantConfig.tenant_id == tenant_id, tenant_column == value
            )
        )
    ).scalar_one_or_none() is not None

    for channel_id in channel_ids:
        await unset_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id),
            fields=[field],
        )
    if tenant_row_matches:
        await unset_fields(session, scope=TenantScopeRef(tenant_id=tenant_id), fields=[field])

    return len(channel_ids) + (1 if tenant_row_matches else 0)


async def delete_propagation_row(
    session: AsyncSession,
    *,
    scope: ChannelScopeRef | TenantScopeRef,
) -> bool:
    """Delete a channel_config / tenant_config row outright.

    Returns True if a row was deleted, False if none existed (idempotent).
    Used by /unpropagate when the row's mode is 'user_active' — clearing
    agent_name on a user_active row is meaningless because the row's mode
    IS the propagation. For mode='agent' rows /unpropagate continues to
    call unset_fields, which auto-deletes the row when agent_name was its
    only set field.
    """
    if isinstance(scope, ChannelScopeRef):
        pk_filter = (ChannelConfig.tenant_id == scope.tenant_id) & (
            ChannelConfig.channel_id == scope.channel_id
        )
        existing = (
            await session.execute(select(ChannelConfig).where(pk_filter))
        ).scalar_one_or_none()
        if existing is None:
            return False
        await session.execute(delete(ChannelConfig).where(pk_filter))
    else:
        # TenantScopeRef
        tenant_filter = TenantConfig.tenant_id == scope.tenant_id
        existing = (
            await session.execute(select(TenantConfig).where(tenant_filter))
        ).scalar_one_or_none()
        if existing is None:
            return False
        await session.execute(delete(TenantConfig).where(tenant_filter))
    await session.flush()
    return True


async def propagate(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    source: ScopeRef,
    target: list[ScopeRef],
    fields: list[ConfigField] | None,
    reset: bool,
    actor_account_id: uuid.UUID | None = None,
) -> PropagateResult:
    """Copy or reset config fields from source to targets."""
    source_row = await get_scope(session, scope=source)
    outcomes: list[PropagateOutcome] = []

    for t in target:
        # Guard: channel/tenant targets must belong to the propagation's tenant.
        if isinstance(t, (ChannelScopeRef, TenantScopeRef)) and t.tenant_id != tenant_id:
            raise StoreError("propagate target tenant_id does not match tenant_id")
        if reset:
            fields_to_reset = fields if fields is not None else list(_CONFIG_FIELDS)
            await unset_fields(session, scope=t, fields=fields_to_reset)
            outcomes.append(PropagateOutcome(scope=t, fields_written=[]))
        else:
            # Copy non-None source fields to target.
            fields_to_copy = fields if fields is not None else list(_CONFIG_FIELDS)
            written: list[ConfigField] = []
            agent_name_val: str | None = None
            environment_name_val: str | None = None
            for f in fields_to_copy:
                val = getattr(source_row, f, None) if source_row is not None else None
                if val is not None:
                    if f == "agent_name":
                        agent_name_val = val
                    elif f == "environment_name":
                        environment_name_val = val
                    written.append(f)
            if agent_name_val is not None or environment_name_val is not None:
                await set_fields(
                    session,
                    scope=t,
                    tenant_id=tenant_id,
                    agent_name=agent_name_val,
                    environment_name=environment_name_val,
                    actor_account_id=actor_account_id,
                )
            outcomes.append(PropagateOutcome(scope=t, fields_written=written))

    return PropagateResult(outcomes=outcomes)
