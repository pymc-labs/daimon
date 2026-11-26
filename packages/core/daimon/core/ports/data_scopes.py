"""The data scopes an audience may read together, the data package answering: none without it."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from daimon.core import ports
from daimon.core.config import Settings
from daimon.core.data_audience import DataAudience

type HeldScopes = Callable[[Settings, uuid.UUID, DataAudience], Awaitable[frozenset[str]]]
_REGISTERED: list[HeldScopes] = []


def register_held_scopes(read: HeldScopes) -> None:
    _REGISTERED[:] = [read]


async def held_scopes(
    settings: Settings, tenant_id: uuid.UUID, audience: DataAudience
) -> frozenset[str]:
    """None held with no data package installed; raises what the store read raises."""
    ports.load()
    if not _REGISTERED:
        return frozenset()
    return await _REGISTERED[0](settings, tenant_id, audience)
