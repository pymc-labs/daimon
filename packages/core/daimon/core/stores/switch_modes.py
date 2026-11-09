"""One store for both per-place switches: thread participation and progress plans.

Both are on/off features in `feature_values` (`_scope_modes`), so the channel service
and the directory read and write either through here by naming the switch. Callers own
the transaction.
"""

from __future__ import annotations

import uuid
from typing import Literal

from daimon.core.scope_cascade import CascadeMode, CascadeModes, CascadeScope
from daimon.core.stores._scope_modes import (
    clear_scope_mode,
    get_scope_modes,
    list_scope_modes,
    set_scope_mode,
)
from sqlalchemy.ext.asyncio import AsyncSession

Switch = Literal["participation", "progress_plans"]


async def get_switch_modes(
    session: AsyncSession,
    switch: Switch,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str | None,
    thread_id: str | None,
) -> CascadeModes:
    return await get_scope_modes(
        session,
        switch,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
    )


async def set_switch_mode(
    session: AsyncSession,
    switch: Switch,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope: CascadeScope,
    scope_id: str | None,
    mode: CascadeMode | None,
    parent_id: str | None = None,
) -> None:
    """Write `mode` at the scope, or with None drop its row so it inherits."""
    if mode is None:
        await clear_scope_mode(
            session,
            switch,
            tenant_id=tenant_id,
            platform=platform,
            scope=scope,
            scope_id=scope_id,
        )
        return
    await set_scope_mode(
        session,
        switch,
        tenant_id=tenant_id,
        platform=platform,
        scope=scope,
        scope_id=scope_id,
        mode=mode,
        parent_id=parent_id,
    )


async def list_switch_modes(
    session: AsyncSession,
    switch: Switch,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope: CascadeScope,
    parent_id: str | None = None,
) -> dict[str, CascadeMode]:
    """Every `scope` row's mode by id; threads only under `parent_id` when given."""
    return await list_scope_modes(
        session,
        switch,
        tenant_id=tenant_id,
        platform=platform,
        scope=scope,
        parent_id=parent_id,
    )
