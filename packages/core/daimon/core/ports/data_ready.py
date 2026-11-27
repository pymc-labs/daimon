"""Whether a workspace has a data source, or a Google one, the data package answering: until it
does, and while the store is not ready, no data tool (or personal Drive and calendar tool) is
offered."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from daimon.core import ports
from daimon.core.config import Settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

type Sessions = async_sessionmaker[AsyncSession]
type HasSources = Callable[[Sessions, Settings, uuid.UUID, bool], Awaitable[bool]]
_REGISTERED: list[HasSources] = []
_TTL_S: Final = 30.0
_seen: dict[tuple[uuid.UUID, bool], tuple[float, bool]] = {}


def register_has_sources(check: HasSources) -> None:
    _REGISTERED[:] = [check]


async def has_sources(
    sessions: Sessions, settings: Settings, tenant_id: uuid.UUID, *, google: bool = False
) -> bool:
    """False with no data package installed; `google`: a Google (Drive or Calendar) source.
    Read through the app's own database session, at most every 30 seconds a workspace."""
    ports.load()
    if not _REGISTERED:
        return False
    now = time.monotonic()
    seen = _seen.get((tenant_id, google))
    if seen is not None and now - seen[0] < _TTL_S:
        return seen[1]
    found = await _REGISTERED[0](sessions, settings, tenant_id, google)
    _seen[(tenant_id, google)] = (now, found)
    return found
