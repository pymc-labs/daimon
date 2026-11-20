"""Publishes each server's room facts from the gateway cache, and keeps them honest.

Facts (`daimon.core.audience_facts`) are written to the database so the scheduler and MCP
processes read them. They are published only after a full member reload, and a server
becomes unknown again when the cache can no longer be trusted: a reload starts or fails,
the members intent is missing, or the gateway disconnects. Events that change who can see
a room (members, roles, channels, threads) recompute the server's facts after a short
wait, in one transaction. The process that holds the gateway renews `fresh_until`, so a
crashed bot cannot leave facts that look current.

A server with no internal roles gets no facts.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

import structlog
from daimon.adapters.discord.member_sync import tenant_of
from daimon.adapters.discord.outsiders import guild_rooms
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores import audience_facts as facts_store
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger(__name__)

#: How long published facts stay fresh without a renewal.
FRESH_FOR_S = 180.0
#: How often the gateway owner renews them. Well inside `FRESH_FOR_S`.
RENEW_EVERY_S = 60.0
#: Events within this wait share one recompute.
DEBOUNCE_S = 2.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AudienceSync:
    """Writes what the gateway cache says about each server's rooms."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = _utcnow,
        fresh_for_s: float = FRESH_FOR_S,
        renew_every_s: float = RENEW_EVERY_S,
        debounce_s: float = DEBOUNCE_S,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._clock = clock
        self._fresh_for = timedelta(seconds=fresh_for_s)
        self._renew_every_s = renew_every_s
        self._debounce_s = debounce_s
        # Servers whose published facts follow events; events recompute only these.
        self._live: set[uuid.UUID] = set()
        # Bumped at each disconnect, so a publish that began before it never lands after.
        self._epoch = 0
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._pending: dict[uuid.UUID, asyncio.Task[None]] = {}
        # The roles each server's facts were last computed with.
        self._roles: dict[uuid.UUID, tuple[tuple[str, ...], tuple[str, ...]]] = {}

    def _lock(self, tenant_id: uuid.UUID) -> asyncio.Lock:
        return self._locks.setdefault(tenant_id, asyncio.Lock())

    async def publish(self, guild: discord.Guild) -> None:
        """Compute the server's facts from the cache and write them; unknown if the cache is
        partial, and nothing if the server names no internal roles."""
        tenant_id = tenant_of(guild.id)
        epoch = self._epoch
        async with self._lock(tenant_id):
            if epoch != self._epoch:
                return
            try:
                await self._publish(guild, tenant_id)
            except (SQLAlchemyError, AccessPolicyUnreadable) as err:
                self._live.discard(tenant_id)
                log.warning(
                    "discord.audience.publish_failed",
                    guild_id=str(guild.id),
                    err_type=type(err).__name__,
                )
                await self._try_mark_unknown(tenant_id)

    async def _publish(self, guild: discord.Guild, tenant_id: uuid.UUID) -> None:
        now = self._clock()
        async with self._sessionmaker.begin() as session:
            tenant = await get_tenant(session, tenant_id)
            if tenant is None or tenant.archived_at is not None:
                return
            policy = await load_access_policy(session, tenant_id=tenant_id)
            roles = (policy.internal_role_ids, policy.team_role_ids)
            if not policy.internal_role_ids:
                self._live.discard(tenant_id)
                self._roles.pop(tenant_id, None)
                await facts_store.retire_facts(session, tenant_id=tenant_id, now=now)
                return
            rooms = guild_rooms(guild, internal_role_ids=roles[0], team_role_ids=roles[1])
            if rooms is None:
                self._live.discard(tenant_id)
                await facts_store.mark_unknown(session, tenant_id=tenant_id, now=now)
                return
            generation = await facts_store.publish_facts(
                session,
                tenant_id=tenant_id,
                rooms=rooms,
                now=now,
                fresh_until=now + self._fresh_for,
            )
        self._live.add(tenant_id)
        self._roles[tenant_id] = roles
        log.info(
            "discord.audience.published",
            guild_id=str(guild.id),
            rooms=len(rooms),
            generation=generation,
        )

    async def mark_unknown(self, guild: discord.Guild) -> None:
        """The cache can no longer be trusted (a reload starts, or fails): every room of
        the server is unknown until the next publish."""
        tenant_id = tenant_of(guild.id)
        self._live.discard(tenant_id)
        async with self._lock(tenant_id):
            await self._try_mark_unknown(tenant_id)

    async def _try_mark_unknown(self, tenant_id: uuid.UUID) -> None:
        try:
            async with self._sessionmaker.begin() as session:
                await facts_store.retire_facts(session, tenant_id=tenant_id, now=self._clock())
        except SQLAlchemyError as err:
            log.warning(
                "discord.audience.unknown_failed",
                tenant_id=str(tenant_id),
                err_type=type(err).__name__,
            )

    async def disconnected(self, guilds: Iterable[discord.Guild]) -> None:
        """The gateway closed: events are lost until it resumes, so every room is unknown."""
        self._epoch += 1
        self._live.clear()
        for task in self._pending.values():
            task.cancel()
        self._pending.clear()
        for guild in guilds:
            await self.mark_unknown(guild)

    async def resumed(self, guilds: Iterable[discord.Guild]) -> None:
        """The gateway resumed and replayed what was missed: the cache is current again."""
        for guild in guilds:
            await self.publish(guild)

    def schedule(self, guild: discord.Guild) -> None:
        """An event may have changed who sees a room: recompute after a short wait.

        Only for a server whose facts are published; a reload in progress or a lost
        gateway ends in a full publish of its own."""
        tenant_id = tenant_of(guild.id)
        if tenant_id not in self._live or tenant_id in self._pending:
            return
        self._pending[tenant_id] = task = asyncio.create_task(self._recompute(guild, tenant_id))
        task.add_done_callback(lambda done: self._forget(tenant_id, done))

    def _forget(self, tenant_id: uuid.UUID, done: asyncio.Task[None]) -> None:
        if self._pending.get(tenant_id) is done:
            del self._pending[tenant_id]

    async def _recompute(self, guild: discord.Guild, tenant_id: uuid.UUID) -> None:
        await asyncio.sleep(self._debounce_s)
        # Events from here on schedule the next run.
        if self._pending.get(tenant_id) is asyncio.current_task():
            del self._pending[tenant_id]
        if tenant_id in self._live:
            await self.publish(guild)

    async def settle(self) -> None:
        """Wait for scheduled recomputes. For tests and shutdown."""
        while self._pending:
            await asyncio.gather(*self._pending.values(), return_exceptions=True)

    async def renew(self, guilds: Iterable[discord.Guild]) -> None:
        """One renewal: push `fresh_until` out for the servers whose facts are live, publish
        again for one that lapsed or whose roles changed in the policy."""
        by_tenant = {tenant_of(guild.id): guild for guild in guilds}
        live = [tenant_id for tenant_id in self._live if tenant_id in by_tenant]
        now = self._clock()
        try:
            async with self._sessionmaker.begin() as session:
                renewed = await facts_store.renew_fresh_until(
                    session, tenant_ids=live, now=now, fresh_until=now + self._fresh_for
                )
                changed = {
                    tenant_id
                    for tenant_id in renewed
                    if self._roles.get(tenant_id) != await _policy_roles(session, tenant_id)
                }
        except (SQLAlchemyError, AccessPolicyUnreadable) as err:
            log.warning("discord.audience.renew_failed", err_type=type(err).__name__)
            return
        for tenant_id in {*live} - renewed | changed:
            await self.publish(by_tenant[tenant_id])

    async def run(self, guilds: Callable[[], Iterable[discord.Guild]]) -> None:
        """Renew until cancelled. Started by the process that holds the gateway."""
        while True:
            await asyncio.sleep(self._renew_every_s)
            await self.renew(guilds())


async def _policy_roles(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    policy: TenantAccessPolicy = await load_access_policy(session, tenant_id=tenant_id)
    return policy.internal_role_ids, policy.team_role_ids
