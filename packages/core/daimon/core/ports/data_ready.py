"""Whether a workspace has a data source, the data package answering: until it does, and
while the store is not ready, no data tool is offered."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from daimon.core import ports
from daimon.core.config import Settings

type HasSources = Callable[[Settings, uuid.UUID], Awaitable[bool]]
_REGISTERED: list[HasSources] = []


def register_has_sources(check: HasSources) -> None:
    _REGISTERED[:] = [check]


async def has_sources(settings: Settings, tenant_id: uuid.UUID) -> bool:
    """False with no data package installed."""
    ports.load()
    return bool(_REGISTERED) and await _REGISTERED[0](settings, tenant_id)
