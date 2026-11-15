"""A short process-local memory of each custom skill's concrete latest version.

A preview or turn of an agent with many skills attached at `latest` would
otherwise spend one paced Skills API request per skill on every read. A version
Daimon publishes in this process drops its skill's entry at once; one published
elsewhere (another process, the console) shows up within `LATEST_VERSION_TTL_S`.
"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic

#: How long a looked-up latest version is trusted without asking again.
LATEST_VERSION_TTL_S = 300.0
_PRUNE_AT = 4096


class LatestVersionCache:
    def __init__(self, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._entries: dict[tuple[str, str], tuple[float, str]] = {}
        self._generation = 0

    def generation(self) -> int:
        """Read before a lookup and pass to `put`: a `forget` since then drops its answer."""
        return self._generation

    def get(self, tenant_id: str, skill_id: str) -> str | None:
        entry = self._entries.get((tenant_id, skill_id))
        if entry is None or entry[0] <= self._clock():
            return None
        return entry[1]

    def put(self, tenant_id: str, skill_id: str, version: str, *, generation: int) -> None:
        if generation != self._generation:
            # A publish here overlapped the lookup, which may have read the old version.
            return
        now = self._clock()
        if len(self._entries) >= _PRUNE_AT:
            self._entries = {k: v for k, v in self._entries.items() if v[0] > now}
        self._entries[(tenant_id, skill_id)] = (now + LATEST_VERSION_TTL_S, version)

    def clear(self) -> None:
        """Forget everything; for test isolation only."""
        self._entries.clear()

    def forget(self, skill_id: str) -> None:
        """Drop `skill_id` for every tenant: its latest version just changed here."""
        self._generation += 1
        for key in [key for key in self._entries if key[1] == skill_id]:
            del self._entries[key]


#: The process's one cache: every caller shares the Skills API budget it saves.
LATEST_VERSIONS = LatestVersionCache()
