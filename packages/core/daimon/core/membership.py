"""Current tenant membership, independent of any platform adapter.

Resolvers are process-scoped. Callers must select a registered tenant's guild;
keys include the tenant so evidence never crosses tenant boundaries. Unknown
means unavailable evidence, never a grant. ``require_fresh`` skips completed
cache entries, sharing only concurrent lookups. Ordinary positive evidence is
valid for at most 30 seconds; absence for five. Errors are never cached.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class Membership:
    """Verified platform facts; None membership means lookup failure/unknown."""

    is_member: bool | None = None
    role_ids: tuple[str, ...] = ()
    is_admin: bool = False
    is_bot: bool = False


class MembershipResolver(Protocol):
    async def resolve(
        self, tenant_id: UUID, guild_id: str, user_id: str, *, require_fresh: bool = False
    ) -> Membership:
        """Resolve current membership and roles for exactly this tenant/guild/user."""
        ...


type MembershipKey = tuple[UUID, str, str]
type MembershipFetch = Callable[[UUID, str, str], Awaitable[Membership]]


class CachedMembershipResolver:
    """Bounded, single-flight cache with explicit gateway invalidation hooks.

    Invalidation also detaches outstanding requests: an older lookup cannot
    repopulate the cache or grant after a newer member/guild event.
    """

    def __init__(self, fetch: MembershipFetch, *, clock: Callable[[], float] = time.monotonic):
        self._fetch = fetch
        self._clock = clock
        self._next_sweep = clock() + 30
        self._cache: dict[MembershipKey, tuple[float, Membership]] = {}
        self._flights: dict[MembershipKey, asyncio.Task[Membership]] = {}

    def _sweep(self) -> None:
        now = self._clock()
        if now >= self._next_sweep:
            self._cache = {
                key: entry
                for key, entry in self._cache.items()
                if now - entry[0] < (30 if entry[1].is_member else 5)
            }
            self._next_sweep = now + 30

    def invalidate(self, tenant_id: UUID, guild_id: str, user_id: str) -> None:
        key = (tenant_id, guild_id, user_id)
        self._cache.pop(key, None)
        self._flights.pop(key, None)

    def invalidate_guild(self, guild_id: str) -> None:
        for key in self._cache.keys() | self._flights.keys():
            if key[1] == guild_id:
                self.invalidate(*key)

    def observe(self, tenant_id: UUID, guild_id: str, user_id: str, facts: Membership) -> None:
        """Record a timestamped gateway event, never an untimestamped SDK cache hit."""
        self._sweep()
        self.invalidate(tenant_id, guild_id, user_id)
        if facts.is_member is not None:
            self._cache[(tenant_id, guild_id, user_id)] = (self._clock(), facts)

    async def resolve(
        self, tenant_id: UUID, guild_id: str, user_id: str, *, require_fresh: bool = False
    ) -> Membership:
        self._sweep()
        key = (tenant_id, guild_id, user_id)
        if not require_fresh and (entry := self._cache.get(key)) is not None:
            at, facts = entry
            if self._clock() - at < (30 if facts.is_member else 5):
                return facts
        flight = self._flights.get(key)
        if flight is None:
            flight = asyncio.create_task(self._lookup(key))
            self._flights[key] = flight
        return await asyncio.shield(flight)

    async def _lookup(self, key: MembershipKey) -> Membership:
        current = asyncio.current_task()
        started = self._clock()
        try:
            try:
                async with asyncio.timeout(10):
                    facts = await self._fetch(*key)
            except Exception:
                facts = Membership()
            if self._flights.get(key) is not current:
                return Membership()
            if facts.is_member is not None:
                self._cache[key] = (started, facts)
            else:
                self._cache.pop(key, None)
            return facts
        finally:
            if self._flights.get(key) is current:
                del self._flights[key]
