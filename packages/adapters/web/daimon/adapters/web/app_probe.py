"""How embedded apps answered their last probe, reused briefly.

A probe waits on the app, and an admin asks often, so one answer per person and app is
reused for `STATUS_TTL_S`. It is not shared: the app is asked with the person's own token.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

AppStatus = Literal["connected", "error"]

#: How long an app's answer is reused before it is asked again.
STATUS_TTL_S = 30.0


@dataclass
class AppStatusCache:
    """The last answer per person and app."""

    _seen: dict[tuple[uuid.UUID, str], tuple[float, AppStatus]] = field(
        default_factory=dict[tuple[uuid.UUID, str], tuple[float, AppStatus]]
    )

    async def get(
        self, person_id: uuid.UUID, key: str, probe: Callable[[], Awaitable[AppStatus]]
    ) -> AppStatus:
        """The cached status, or `probe()`'s answer, which is then cached."""
        seen = self._seen.get((person_id, key))
        if seen is not None and time.monotonic() - seen[0] < STATUS_TTL_S:
            return seen[1]
        status = await probe()
        self._seen[(person_id, key)] = (time.monotonic(), status)
        return status
