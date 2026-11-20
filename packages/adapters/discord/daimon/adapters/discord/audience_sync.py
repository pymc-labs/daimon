"""Publishes each server's room facts from the gateway cache, and keeps them honest.

Facts (`daimon.core.audience_facts`) are written to the database so the scheduler and MCP
processes read them. They are published only after a full member reload, and a server
becomes unknown again when the cache can no longer be trusted: a reload starts or fails,
the members intent is missing, or the gateway disconnects. Events that change who can see
a room (members, roles, channels, threads) recompute the server's facts after a short
wait, in one transaction.

The process that holds the gateway renews `fresh_until` so a crashed bot cannot leave
facts that look current. A renewal asks Discord nothing, so it never pushes validity past
the last full reconcile plus `RECONCILE_LIMIT_S`. A reconcile reads roles and channels by
REST and re-chunks the members, then recomputes and publishes; it runs every
`RECONCILE_EVERY_S` for each server with internal roles, live or not. That also recovers
a server whose publish failed and picks up a policy change. A missed gateway event can
therefore mislead for at most the limit, and one failed reconcile does not expire facts.

Only a reconcile (or the member reload after a gateway connect) can relax what the last
one found. It publishes its own result and keeps it as the server's baseline. An event
recompute reads the gateway cache, and each room is contained or narrow if the cache or
the baseline says so (`merge_with_baseline`): a missed role, member or channel update can
therefore open a room only until the next reconcile, never undo one. A real change that
opens a room shows at the next reconcile; one that closes a room shows at once. Members
come from the cache, filled by the last chunk; our own record of that (not discord.py's
`chunked` flag, which a missed join can keep false) says whether the cache is complete.

A server with no internal roles gets no facts.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from daimon.adapters.discord.member_sync import tenant_of
from daimon.adapters.discord.outsiders import guild_rooms
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores import audience_facts as facts_store
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.audience_facts import StoredRoom
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
#: How often each server is reconciled with Discord: half the limit, so one failure is safe.
RECONCILE_EVERY_S = 300.0
#: Facts stay trusted at most this long after the last successful reconcile.
RECONCILE_LIMIT_S = 600.0
#: A server's member chunks must arrive within this.
CHUNK_TIMEOUT_S = 300.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Read:
    """What a publish computes from. None for roles or channels means the cache's."""

    roles: Sequence[discord.Role] | None
    channels: Sequence[discord.abc.GuildChannel] | None
    members: Sequence[discord.Member]


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
        reconcile_every_s: float = RECONCILE_EVERY_S,
        reconcile_limit_s: float = RECONCILE_LIMIT_S,
        chunk_timeout_s: float = CHUNK_TIMEOUT_S,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._clock = clock
        self._fresh_for = timedelta(seconds=fresh_for_s)
        self._renew_every_s = renew_every_s
        self._debounce_s = debounce_s
        self._reconcile_every = timedelta(seconds=reconcile_every_s)
        self._reconcile_limit = timedelta(seconds=reconcile_limit_s)
        self._chunk_timeout_s = chunk_timeout_s
        # When each server was last reconciled with Discord; renewals never outlive it.
        self._reconciled: dict[uuid.UUID, datetime] = {}
        # Servers whose published facts follow events; events recompute only these.
        self._live: set[uuid.UUID] = set()
        # Servers whose member cache a full load filled since the last disconnect or reload
        # start. The gateway's own "chunked" flag compares counts, so a missed join or
        # leave can keep it false for good; this record replaces it.
        self._members_loaded: set[uuid.UUID] = set()
        # Each server's rooms from its last reconcile or member reload: events never open
        # a room beyond it.
        self._baseline: dict[uuid.UUID, dict[str, StoredRoom]] = {}
        # Bumped at each disconnect, so a publish that began before it never lands after.
        self._epoch = 0
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._pending: dict[uuid.UUID, asyncio.Task[None]] = {}
        # The roles each server's facts were last computed with.
        self._roles: dict[uuid.UUID, tuple[tuple[str, ...], tuple[str, ...]]] = {}

    def _lock(self, tenant_id: uuid.UUID) -> asyncio.Lock:
        return self._locks.setdefault(tenant_id, asyncio.Lock())

    async def publish(self, guild: discord.Guild, *, reconciled: bool = False) -> None:
        """Publish the server's facts, or retire them if it names no internal roles.

        With `reconciled` the caller has just loaded every member (the member reload after
        a gateway connect, whose guild snapshot is fresh): the result becomes the baseline
        and starts the reconcile limit. Without it this is an event recompute from the
        cache, merged with the baseline. A server whose members were not loaded since the
        last disconnect is skipped: the next tick reconciles it."""
        await self._publish_guarded(guild, reconciled=reconciled, epoch=self._epoch, read=None)

    async def _publish_guarded(
        self,
        guild: discord.Guild,
        *,
        reconciled: bool,
        epoch: int,
        read: _Read | None,
    ) -> bool:
        """Whether a full set of facts was written."""
        tenant_id = tenant_of(guild.id)
        async with self._lock(tenant_id):
            if epoch != self._epoch:
                return False
            try:
                return await self._publish(guild, tenant_id, reconciled=reconciled, read=read)
            except (SQLAlchemyError, AccessPolicyUnreadable) as err:
                self._live.discard(tenant_id)
                log.warning(
                    "discord.audience.publish_failed",
                    guild_id=str(guild.id),
                    err_type=type(err).__name__,
                )
                await self._try_mark_unknown(tenant_id)
                return False

    async def _publish(
        self,
        guild: discord.Guild,
        tenant_id: uuid.UUID,
        *,
        reconciled: bool,
        read: _Read | None,
    ) -> bool:
        now = self._clock()
        async with self._sessionmaker.begin() as session:
            tenant = await get_tenant(session, tenant_id)
            if tenant is None or tenant.archived_at is not None:
                return False
            policy = await load_access_policy(session, tenant_id=tenant_id)
            policy_roles = (policy.internal_role_ids, policy.team_role_ids)
            if not policy.internal_role_ids:
                self._live.discard(tenant_id)
                self._roles.pop(tenant_id, None)
                self._reconciled.pop(tenant_id, None)
                self._baseline.pop(tenant_id, None)
                await facts_store.retire_facts(session, tenant_id=tenant_id, now=now)
                return False
            if not reconciled and (
                tenant_id not in self._members_loaded
                or self._roles.get(tenant_id) not in (None, policy_roles)
            ):
                # Not loaded, or the policy moved under the baseline: the tick reconciles.
                if tenant_id in self._members_loaded:
                    self._members_loaded.discard(tenant_id)
                    self._baseline.pop(tenant_id, None)
                return False
            seen = read or _Read(None, None, list(guild.members))
            rooms = guild_rooms(
                guild,
                internal_role_ids=policy_roles[0],
                team_role_ids=policy_roles[1],
                roles=seen.roles,
                guild_channels=seen.channels,
                members=seen.members,
            )
            if rooms is None:
                self._live.discard(tenant_id)
                await facts_store.mark_unknown(session, tenant_id=tenant_id, now=now)
                return False
            if not reconciled:
                rooms = facts_store.merge_with_baseline(rooms, self._baseline.get(tenant_id))
            reconciled_at = now if reconciled else self._reconciled.get(tenant_id)
            fresh_until = now + self._fresh_for
            if reconciled_at is not None:
                fresh_until = min(fresh_until, reconciled_at + self._reconcile_limit)
            generation = await facts_store.publish_facts(
                session,
                tenant_id=tenant_id,
                rooms=rooms,
                now=now,
                fresh_until=fresh_until,
            )
        self._live.add(tenant_id)
        self._roles[tenant_id] = policy_roles
        if reconciled:
            self._reconciled[tenant_id] = now
            self._members_loaded.add(tenant_id)
            self._baseline[tenant_id] = rooms
        log.info(
            "discord.audience.published",
            guild_id=str(guild.id),
            rooms=len(rooms),
            generation=generation,
            reconciled=reconciled,
        )
        return True

    async def reconcile(self, guild: discord.Guild) -> bool:
        """Read the server's roles and channels by REST and re-chunk its members, then
        recompute and publish. Returns whether it published from a full read.

        A failed read changes nothing: the facts stay until their limit and then lapse."""
        epoch = self._epoch
        try:
            roles = await guild.fetch_roles()
            channels = await guild.fetch_channels()
            # The chunk is the full member list: a member whose leave event was missed
            # is not in it, while the cache would keep them.
            members = await asyncio.wait_for(guild.chunk(cache=True), self._chunk_timeout_s)
        except (discord.ClientException, discord.HTTPException, TimeoutError) as err:
            log.warning(
                "discord.audience.reconcile_failed",
                guild_id=str(guild.id),
                err_type=type(err).__name__,
            )
            return False
        return await self._publish_guarded(
            guild, reconciled=True, epoch=epoch, read=_Read(roles, channels, members)
        )

    async def mark_unknown(self, guild: discord.Guild) -> None:
        """The cache can no longer be trusted (a reload starts, or fails): every room of
        the server is unknown until the next publish."""
        tenant_id = tenant_of(guild.id)
        self._live.discard(tenant_id)
        self._members_loaded.discard(tenant_id)
        self._baseline.pop(tenant_id, None)
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
        self._members_loaded.clear()
        self._baseline.clear()
        self._reconciled.clear()
        for task in self._pending.values():
            task.cancel()
        self._pending.clear()
        for guild in guilds:
            await self.mark_unknown(guild)

    async def resumed(self, guilds: Iterable[discord.Guild]) -> None:
        """The gateway resumed and replayed what was missed: reconcile each server, since
        the replay can have fallen short. One that fails is retried at each tick."""
        for guild in guilds:
            await self.reconcile(guild)

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

    async def tick(self, guilds: Iterable[discord.Guild]) -> None:
        """One maintenance pass. Reconcile each server with internal roles that is not live,
        was never reconciled, was last reconciled a full interval ago or whose policy roles
        changed; then renew the live ones, each to its last reconcile plus the limit."""
        by_tenant = {tenant_of(guild.id): guild for guild in guilds}
        try:
            async with self._sessionmaker.begin() as session:
                policy = {
                    tenant_id: await _policy_roles(session, tenant_id) for tenant_id in by_tenant
                }
        except (SQLAlchemyError, AccessPolicyUnreadable) as err:
            log.warning("discord.audience.renew_failed", err_type=type(err).__name__)
            return
        reconciled_now: set[uuid.UUID] = set()
        for tenant_id, guild in by_tenant.items():
            roles = policy[tenant_id]
            if not roles[0]:
                if tenant_id in self._live or tenant_id in self._roles:
                    await self.publish(guild)  # retires the facts
                continue
            last = self._reconciled.get(tenant_id)
            now = self._clock()
            if (
                tenant_id not in self._live
                or last is None
                or now - last >= self._reconcile_every
                or self._roles.get(tenant_id) != roles
            ) and await self.reconcile(guild):
                reconciled_now.add(tenant_id)
        await self._renew(by_tenant, reconciled_now)

    async def _renew(
        self, by_tenant: Mapping[uuid.UUID, discord.Guild], skip: set[uuid.UUID]
    ) -> None:
        now = self._clock()
        limits = {
            tenant_id: min(now + self._fresh_for, last + self._reconcile_limit)
            for tenant_id in self._live
            if tenant_id in by_tenant
            and tenant_id not in skip
            and (last := self._reconciled.get(tenant_id)) is not None
        }
        limits = {t: limit for t, limit in limits.items() if limit > now}
        try:
            async with self._sessionmaker.begin() as session:
                renewed = await facts_store.renew_fresh_until(session, fresh_until=limits, now=now)
        except SQLAlchemyError as err:
            log.warning("discord.audience.renew_failed", err_type=type(err).__name__)
            return
        for tenant_id in limits.keys() - renewed:
            self._live.discard(tenant_id)  # lapsed: the next tick reconciles it

    async def run(self, guilds: Callable[[], Iterable[discord.Guild]]) -> None:
        """Tick until cancelled. Started by the process that holds the gateway."""
        while True:
            await asyncio.sleep(self._renew_every_s)
            await self.tick(guilds())


async def _policy_roles(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    policy: TenantAccessPolicy = await load_access_policy(session, tenant_id=tenant_id)
    return policy.internal_role_ids, policy.team_role_ids
