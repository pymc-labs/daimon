"""The on/off/disabled view of an on/off feature's workspace, channel and thread rows.

Thread participation and progress plans live in `feature_values`; these queries keep
the cascade-mode shape the switch stores and the channel service were written
against. `disabled` is Off + Locked (plain Off on a thread). Agent rows are not part
of this view. Callers own the transaction.
"""

from __future__ import annotations

import uuid

from daimon.core.scope_cascade import CascadeMode, CascadeModes, CascadeScope
from daimon.core.stores.domain import FeatureSurface, FeatureTarget, FeatureValueRow
from daimon.core.stores.feature_values import (
    clear_feature_value,
    list_feature_values,
    place_values,
    set_feature_value,
)
from sqlalchemy.ext.asyncio import AsyncSession


def row_mode(row: FeatureValueRow) -> CascadeMode:
    return CascadeMode.DISABLED if row.locked and row.value == "off" else CascadeMode(row.value)


def _target(scope: CascadeScope, scope_id: str | None, parent_id: str | None) -> FeatureTarget:
    if scope is CascadeScope.WORKSPACE:
        return FeatureTarget("workspace")
    if scope_id is None:
        raise ValueError(f"{scope.value} scope needs a scope_id")
    if scope is CascadeScope.CHANNEL:
        return FeatureTarget("channel", channel_id=scope_id)
    return FeatureTarget("thread", channel_id=parent_id, thread_key=scope_id)


async def get_scope_modes(
    session: AsyncSession,
    key: str,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str | None,
    thread_id: str | None,
) -> CascadeModes:
    """One read for the whole cascade: the workspace row plus this channel's and thread's rows."""
    del platform  # A tenant lives on one platform.
    rows = await place_values(
        session,
        tenant_id=tenant_id,
        agent_id=None,
        channel_id=channel_id,
        thread_key=thread_id,
        keys=(key,),
    )
    found = {row.tier: row_mode(row) for row in rows}
    return CascadeModes(
        workspace=found.get("workspace"),
        channel=found.get("channel"),
        thread=found.get("thread"),
    )


async def set_scope_mode(
    session: AsyncSession,
    key: str,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope: CascadeScope,
    scope_id: str | None,
    mode: CascadeMode,
    parent_id: str | None = None,
    set_by: uuid.UUID | None = None,
    surface: FeatureSurface = "chat",
) -> None:
    """Write one scope's mode; `parent_id` is a thread's channel, kept once known."""
    del platform
    thread = scope is CascadeScope.THREAD
    await set_feature_value(
        session,
        tenant_id=tenant_id,
        target=_target(scope, scope_id, parent_id),
        key=key,
        value="off" if mode is CascadeMode.DISABLED else mode.value,
        locked=mode is CascadeMode.DISABLED and not thread,
        set_by=set_by,
        surface=surface,
    )


async def clear_scope_mode(
    session: AsyncSession,
    key: str,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope: CascadeScope,
    scope_id: str | None,
) -> bool:
    """Drop the explicit setting so the scope inherits again. Returns whether a row existed."""
    del platform
    return await clear_feature_value(
        session, tenant_id=tenant_id, target=_target(scope, scope_id, None), key=key
    )


async def list_scope_modes(
    session: AsyncSession,
    key: str,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope: CascadeScope,
    parent_id: str | None = None,
) -> dict[str, CascadeMode]:
    """Every `scope` row's mode by scope id; thread rows only under `parent_id` when given."""
    del platform
    rows = await list_feature_values(
        session,
        tenant_id=tenant_id,
        keys=(key,),
        tier=scope.value,
        channel_id=parent_id if scope is CascadeScope.THREAD else None,
        agent_id="",
    )
    return {_scope_id(row): row_mode(row) for row in rows}


def _scope_id(row: FeatureValueRow) -> str:
    return {"channel": row.channel_id, "thread": row.thread_key}.get(row.tier, "")
