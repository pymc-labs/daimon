"""What one provider API key can take: its rate-limit waits and its running turns.

Every replica shares both through the database. A 429 puts the key's model on a
cooldown for the wait the provider named; a turn holds a short lease on the key,
renewed until it ends, and a key or workspace at its cap makes the next turn
wait, then refuse.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from daimon.core.config import ProviderLimitsSettings
from mux.drivers.clients import key_digest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

_POLL_S = 0.5


#: How often a running turn renews its lease.
RENEW_EVERY_S = 30.0


def lease_ttl() -> timedelta:
    """A lease outlives its last renewal by this: a crashed holder's slot frees this soon."""
    return timedelta(seconds=3 * RENEW_EVERY_S)


@dataclass(frozen=True)
class ProviderKey:
    """A provider and a digest of one of its API keys; never the key."""

    provider: str
    digest: str

    @classmethod
    def from_env(cls, provider: str, env_name: str) -> ProviderKey | None:
        """The key the named variable holds now, or None when it is unset or blank."""
        value = (os.environ.get(env_name) or "").strip()
        return cls(provider, key_digest(value)) if value else None


# Read when a turn enters the gate, never earlier: a refused policy never touches the key.
ProviderKeySource = Callable[[], ProviderKey | None]


class ProviderKeyGate:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        limits: ProviderLimitsSettings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sessions, self._limits = sessionmaker, limits
        self._clock, self._sleep = clock, sleep

    async def cooling_until(self, key: ProviderKey, model: str) -> datetime | None:
        from daimon.core.stores import provider_key_gate as store

        async with self._sessions() as session:
            return await store.cooling_until(
                session,
                provider=key.provider,
                key_digest=key.digest,
                model=model,
                now=self._clock(),
            )

    async def record_cooldown(self, key: ProviderKey, model: str, wait_s: float) -> None:
        from daimon.core.stores import provider_key_gate as store

        async with self._sessions.begin() as session:
            await store.record_cooldown(
                session,
                provider=key.provider,
                key_digest=key.digest,
                model=model,
                now=self._clock(),
                wait=timedelta(seconds=wait_s),
            )

    async def acquire(self, key: ProviderKey, tenant_id: uuid.UUID) -> uuid.UUID | None:
        """A lease on the key, waiting up to `wait_s` for a slot; None when none freed."""
        from daimon.core.stores import provider_key_gate as store

        waited = 0.0
        while True:
            async with self._sessions.begin() as session:
                lease = await store.acquire_lease(
                    session,
                    tenant_id=tenant_id,
                    provider=key.provider,
                    key_digest=key.digest,
                    now=self._clock(),
                    ttl=lease_ttl(),
                    key_cap=self._limits.key_turns,
                    tenant_cap=self._limits.tenant_turns,
                )
            if lease != "full":
                return lease
            if waited >= self._limits.wait_s:
                return None
            step = min(_POLL_S, self._limits.wait_s - waited)
            await self._sleep(step)
            waited += step

    async def renew(self, lease: uuid.UUID) -> None:
        """Extend a held lease; a failure only lets it run toward its expiry."""
        from daimon.core.stores import provider_key_gate as store

        try:
            async with self._sessions.begin() as session:
                await store.renew_lease(session, lease, expires_at=self._clock() + lease_ttl())
        except Exception:
            # Boundary: a turn already running must not fail on a renewal.
            log.warning("provider_key_gate.renew_failed", exc_info=True)

    async def release(self, lease: uuid.UUID) -> None:
        """Free a slot; a failure only leaves the lease to expire."""
        from daimon.core.stores import provider_key_gate as store

        try:
            async with self._sessions.begin() as session:
                await store.release_lease(session, lease)
        except Exception:
            log.warning("provider_key_gate.release_failed", exc_info=True)
