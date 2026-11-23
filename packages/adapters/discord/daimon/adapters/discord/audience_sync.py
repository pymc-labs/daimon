"""Publishes each server's room facts from our own REST read of Discord, and keeps them honest.

Facts (`daimon.core.audience_facts`) are written to the database so the scheduler and MCP
processes read them. Their only source is a reconcile: one full REST read of the server
(`audience_rest.DiscordRestReader`) turned into the room rule's inputs. The gateway cache
never decides a fact.

Each server has one owned reconcile task. Startup, the periodic pass, events and a resume
all ask the same scheduler, and asks that arrive while a read runs share one more read
after it. The reader's work is ours, so a disconnect can cancel it, and the server's slot
frees only once the task has ended. discord.py's HTTP client is never used or cancelled.

Facts fail closed. A change that may alter who sees a room (a member, role, channel,
overwrite or owner event, the server becoming unavailable, a disconnect) makes every room
of the server unknown at once and asks for an early reconcile. These changes are taken
from the gateway's raw events (`watch_gateway`), before discord.py parses them, so a
member missing from its cache still counts and no handler's await can come first. An
event that leaves every input of the newest published read as it was (a nickname, a
role's colour) changes nothing, even while nothing is published: that read's inputs stay
as the comparison basis across a withdrawal, so harmless events cannot keep a server
unknown. A real event is folded into the basis. Each change also advances the server's
change count (and a disconnect the epoch). A reconcile whose read was crossed by a change
publishes nothing, unless every event that came during the read states what the read
itself saw. The count is checked again inside the publishing transaction, after its last
await before the commit. Between a disconnect and the next ready or resume nothing is
read or published. Thread events only ask for a reconcile, since a thread's facts come
from its parent's.

The process that holds the gateway renews `fresh_until` so a crashed bot cannot leave
facts that look current. Renewal runs on its own, never waits for a reconcile, and treats
each server apart with a bounded database wait. It never pushes validity past the start
of the read the facts came from plus `RECONCILE_LIMIT_S`; a reconcile every
`RECONCILE_EVERY_S` keeps that limit ahead, so one failed read does not expire facts.

A server with no internal roles gets no facts.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from daimon.adapters.discord.audience_rest import (
    DiscordRestReader,
    GuildSnapshot,
    ReadFailed,
    channel_from_payload,
    member_from_payload,
    permissions_from_payload,
    rooms_from_snapshot,
)
from daimon.adapters.discord.member_sync import tenant_of
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.audience_facts import AudienceChannel, AudienceMember
from daimon.core.session_fence_retry import FenceUnavailable
from daimon.core.stores import audience_facts as facts_store
from daimon.core.stores.access_policy import (
    AccessPolicyUnreadable,
    load_access_policy,
    lock_policy_writes_shared,
)
from daimon.core.stores.tenants import get_tenant
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger(__name__)

#: How long published facts stay fresh without a renewal.
FRESH_FOR_S = 180.0
#: How often the gateway owner renews them. Well inside `FRESH_FOR_S`.
RENEW_EVERY_S = 60.0
#: An event's reconcile waits this long, so a burst of events shares one read.
DEBOUNCE_S = 1.0
#: How often each server is reconciled with Discord: half the limit, so one failure is safe.
RECONCILE_EVERY_S = 300.0
#: Facts stay trusted at most this long after the start of the read they came from.
RECONCILE_LIMIT_S = 600.0
#: The longest one database step (a publish, a renewal, an unknown mark) may wait.
DB_WAIT_S = 10.0
#: Servers read at once.
MAX_READS = 4

#: Gateway events that may change who sees a room. Thread events are not here: they only
#: ask for a reconcile (the bot's thread handlers), since a thread's facts are its parent's.
AUDIENCE_EVENTS = frozenset(
    {
        "GUILD_UPDATE",
        "GUILD_MEMBER_ADD",
        "GUILD_MEMBER_UPDATE",
        "GUILD_MEMBER_REMOVE",
        "GUILD_ROLE_CREATE",
        "GUILD_ROLE_UPDATE",
        "GUILD_ROLE_DELETE",
        "CHANNEL_CREATE",
        "CHANNEL_UPDATE",
        "CHANNEL_DELETE",
    }
)

_Roles = tuple[tuple[str, ...], tuple[str, ...]]
_Event = tuple[str, Mapping[str, Any]]
#: Events kept per read to compare with it; more than this always cross it.
MAX_LATE_EVENTS = 64


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Published:
    """The facts this process last published for a server, and what they depend on."""

    generation: int
    epoch: int
    changes: int
    read_started: datetime
    roles: _Roles


@dataclass
class _Basis:
    """The audience inputs of the newest published read, kept up to date by the raw
    events that changed them. Events are compared with it. It is dropped when events may
    have been lost (a disconnect, an outage) or one cannot be folded in."""

    owner_id: str
    role_permissions: dict[str, int]
    members: dict[str, AudienceMember]
    channels: dict[str, AudienceChannel]

    @classmethod
    def of(cls, snapshot: GuildSnapshot) -> _Basis:
        return cls(
            snapshot.owner_id,
            dict(snapshot.role_permissions),
            {m.user_id: m for m in snapshot.members},
            {c.id: c for c in snapshot.channels},
        )

    def same(self, event: str, data: Mapping[str, Any], now: datetime) -> bool:
        """Whether a raw event states what the basis already has. Anything unreadable, or
        not in the basis, counts as a change."""
        try:
            if event == "GUILD_UPDATE":
                return str(data.get("owner_id")) == self.owner_id
            if event == "GUILD_MEMBER_UPDATE":
                member = self._member(data, now)
                return self.members.get(member.user_id) == member
            if event == "GUILD_ROLE_UPDATE":
                role_id, bits = self._role(data)
                return self.role_permissions.get(role_id) == bits
            if event == "CHANNEL_UPDATE":
                channel = channel_from_payload(data)
                return self.channels.get(channel.id) == channel
        except ReadFailed:
            return False
        return False

    def apply(self, event: str, data: Mapping[str, Any], now: datetime) -> bool:
        """Fold a changed event in. False when it cannot be (a member or role added or
        removed, a new owner): the basis is then no longer a true copy."""
        try:
            if event == "GUILD_MEMBER_UPDATE":
                member = self._member(data, now)
                self.members[member.user_id] = member
                return True
            if event == "GUILD_ROLE_UPDATE":
                role_id, bits = self._role(data)
                self.role_permissions[role_id] = bits
                return True
            if event == "CHANNEL_UPDATE":
                channel = channel_from_payload(data)
                self.channels[channel.id] = channel
                return True
        except ReadFailed:
            return False
        return False

    def _member(self, data: Mapping[str, Any], now: datetime) -> AudienceMember:
        return member_from_payload(data, self.role_permissions, owner_id=self.owner_id, now=now)

    @staticmethod
    def _role(data: Mapping[str, Any]) -> tuple[str, int]:
        role: object = data.get("role")
        if not isinstance(role, dict):
            raise ReadFailed("role_missing")
        fields = cast("dict[str, object]", role)
        return str(fields.get("id")), permissions_from_payload(fields.get("permissions"))


class _Crossed(Exception):
    """A change came during the publishing transaction: roll it back."""


def watch_gateway(
    parsers: MutableMapping[str, Callable[[Any], None]],
    on_event: Callable[[str, int, Mapping[str, Any]], None],
) -> None:
    """Call `on_event(event, guild_id, data)` for each `AUDIENCE_EVENTS` payload, before
    discord.py parses it.

    `parsers` is discord.py's `ConnectionState.parsers`, which every gateway connection
    reads. Raw, so the cache cannot hide a change: discord.py dispatches no member update
    for a member it has not cached. First, so no handler's await delays the withdrawal.
    A failure here is logged and never stops the parser. A channel event with no guild
    (a direct message) is no room's."""
    for event in AUDIENCE_EVENTS:
        parse = parsers.get(event)
        if parse is None:
            continue

        def watched(
            data: Mapping[str, Any], *, event: str = event, parse: Callable[[Any], None] = parse
        ) -> None:
            try:
                raw = data.get("id") if event == "GUILD_UPDATE" else data.get("guild_id")
                if raw is not None:
                    on_event(event, int(raw), data)
            except Exception as err:
                log.exception(
                    "discord.audience.event_failed",
                    gateway_event=event,
                    err_type=type(err).__name__,
                )
            parse(data)

        parsers[event] = watched


@dataclass
class _Slot:
    """A server's one reconcile task."""

    guild: discord.Guild
    task: asyncio.Task[None] | None = None
    reading: bool = False
    """The task has captured the epoch and change count below and started its read."""
    read_epoch: int = 0
    read_changes: int = 0
    again: bool = False
    """A change came after the read started: read once more when it ends."""
    published: bool = False
    """Whether the last run published."""


class AudienceSync:
    """Writes what one REST read of each server says about its rooms."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        reader: DiscordRestReader | None = None,
        clock: Callable[[], datetime] = _utcnow,
        fresh_for_s: float = FRESH_FOR_S,
        renew_every_s: float = RENEW_EVERY_S,
        debounce_s: float = DEBOUNCE_S,
        reconcile_every_s: float = RECONCILE_EVERY_S,
        reconcile_limit_s: float = RECONCILE_LIMIT_S,
        db_wait_s: float = DB_WAIT_S,
        max_reads: int = MAX_READS,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._reader = reader
        self._clock = clock
        self._fresh_for = timedelta(seconds=fresh_for_s)
        self._renew_every_s = renew_every_s
        self._debounce_s = debounce_s
        self._reconcile_every = timedelta(seconds=reconcile_every_s)
        self._reconcile_limit = timedelta(seconds=reconcile_limit_s)
        self._db_wait_s = db_wait_s
        self._reads = asyncio.Semaphore(max_reads)
        # Bumped at each disconnect, so a read that began before it never lands after.
        self._epoch = 0
        # False from a disconnect to the next ready or resume: events are being lost, so
        # nothing is read or published.
        self._connected = True
        # Per server, bumped by every change that may alter who sees a room.
        self._changes: defaultdict[uuid.UUID, int] = defaultdict(int)
        # Servers whose facts this process published and still renews.
        self._published: dict[uuid.UUID, _Published] = {}
        # What each server's newest published read said, kept after a withdrawal.
        self._basis: dict[uuid.UUID, _Basis] = {}
        # Per server, each change since its read began: (count, the raw event or None).
        self._late: dict[uuid.UUID, list[tuple[int, _Event | None]]] = {}
        # Servers with internal roles at the last pass: events reconcile only these.
        self._tracked: set[uuid.UUID] = set()
        self._slots: dict[uuid.UUID, _Slot] = {}
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        # Servers whose rooms must be written unknown, and the one writer each.
        self._unknown_due: set[uuid.UUID] = set()
        self._unknown_failed: set[uuid.UUID] = set()
        self._writers: dict[uuid.UUID, asyncio.Task[None]] = {}

    # --- what the bot calls ---------------------------------------------------------

    def schedule(self, guild: discord.Guild) -> None:
        """A change may have altered who sees a room: every room of the server is unknown
        now, and a reconcile follows shortly."""
        self._changed(tenant_of(guild.id), guild)

    def gateway_event(
        self, event: str, guild_id: int, data: Mapping[str, Any], guild: discord.Guild | None
    ) -> None:
        """One raw `AUDIENCE_EVENTS` payload (see `watch_gateway`). It withdraws the
        server's facts unless it leaves every input of the basis as it was. `guild`
        is the cached server, if any; without it the facts are still withdrawn, and the
        next tick reads."""
        tenant_id = tenant_of(guild_id)
        basis = self._basis.get(tenant_id)
        if basis is not None:
            now = self._clock()
            if basis.same(event, data, now):
                return
            if not basis.apply(event, data, now):
                del self._basis[tenant_id]
        self._changed(tenant_id, guild, (event, data))

    def refresh(self, guild: discord.Guild) -> None:
        """A thread was created or changed: reconcile soon, without withdrawing the facts.
        A thread's facts are its parent's, so no room's audience moved."""
        self._request(guild, fresh=True, delay=self._debounce_s)

    def request_reconcile(self, guild: discord.Guild) -> None:
        """Reconcile now unless a read that can still publish is under way: the server was
        joined or came back, or its member list reloads."""
        self._request(guild, fresh=False)

    async def mark_unknown(self, guild: discord.Guild) -> None:
        """The server is unavailable: every room is unknown until a reconcile after it
        returns."""
        tenant_id = tenant_of(guild.id)
        self._basis.pop(tenant_id, None)  # events may have been lost
        self._invalidate(tenant_id)
        await self._wait_writers([tenant_id])

    def connected(self) -> None:
        """A ready or a resume: events arrive again, so reads may start and publish."""
        self._connected = True

    async def disconnected(self, guilds: Iterable[discord.Guild]) -> None:
        """The gateway closed: events are lost until it resumes, so every room is unknown.
        Our own reads are cancelled, and each slot frees once its task has ended. Until
        `connected`, no read starts and none publishes."""
        self._connected = False
        self._epoch += 1
        self._published.clear()
        self._basis.clear()
        running = [slot.task for slot in self._slots.values() if slot.task is not None]
        for slot in self._slots.values():
            slot.again = False
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        tenants = [tenant_of(guild.id) for guild in guilds]
        for tenant_id in tenants:
            self._invalidate(tenant_id)
        await self._wait_writers(tenants)

    async def resumed(self, guilds: Iterable[discord.Guild]) -> None:
        """The gateway resumed, and events missed meanwhile may be lost: reconcile each
        server. Returns at once; the reads run in their own tasks."""
        self.connected()
        for guild in guilds:
            self._request(guild, fresh=True)

    async def reconcile(self, guild: discord.Guild) -> bool:
        """Ask for a fresh read of the server and wait for it. Whether it published."""
        if not self._connected:
            return False
        tenant_id = tenant_of(guild.id)
        self._request(guild, fresh=True)
        slot = self._slots[tenant_id]
        while slot.task is not None:
            await asyncio.wait({slot.task})
        return slot.published

    async def settle(self) -> None:
        """Wait for every reconcile and unknown mark. For tests and shutdown."""
        while True:
            tasks = [s.task for s in self._slots.values() if s.task is not None]
            tasks += self._writers.values()
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self, guilds: Callable[[], Iterable[discord.Guild]]) -> None:
        """Tick at once, then every `RENEW_EVERY_S`, until cancelled. Started by the
        process that holds the gateway, at ready and at resume."""
        while True:
            try:
                await self.tick(guilds())
            except asyncio.CancelledError:
                raise
            except Exception as err:  # one bad tick must not end renewal: facts would lapse
                log.exception("discord.audience.tick_failed", err_type=type(err).__name__)
            await asyncio.sleep(self._renew_every_s)

    async def tick(self, guilds: Iterable[discord.Guild]) -> None:
        """One maintenance pass: renew first, then ask for the reconciles that are due.

        Due: a server with internal roles that has no published facts, whose facts were
        read a full interval ago, or whose policy roles changed. Asking never waits for
        the read."""
        by_tenant = {tenant_of(guild.id): guild for guild in guilds}
        await self._renew(by_tenant)
        for tenant_id in list(self._unknown_failed):
            self._unknown_due.add(tenant_id)
            self._start_writer(tenant_id)
        read = await asyncio.gather(*(self._read_roles(t) for t in by_tenant))
        now = self._clock()
        for (tenant_id, guild), roles in zip(by_tenant.items(), read, strict=True):
            if roles is None:
                continue  # unreadable: its facts stand to their limit; the next tick retries
            if not roles[0]:
                if tenant_id in self._tracked or tenant_id in self._published:
                    self._invalidate(tenant_id)
                self._tracked.discard(tenant_id)
                continue
            self._tracked.add(tenant_id)
            if guild.unavailable:
                continue
            published = self._published.get(tenant_id)
            if (
                published is None
                or published.roles != roles
                or now - published.read_started >= self._reconcile_every
            ):
                self._request(guild, fresh=False)

    async def _read_roles(self, tenant_id: uuid.UUID) -> _Roles | None:
        """One server's policy role lists, in its own bounded transaction; None if it
        cannot be read, which holds back no other server."""
        try:
            async with asyncio.timeout(self._db_wait_s), self._sessionmaker.begin() as session:
                return await _policy_roles(session, tenant_id)
        except (SQLAlchemyError, AccessPolicyUnreadable, TimeoutError) as err:
            log.warning(
                "discord.audience.policy_read_failed",
                tenant_id=str(tenant_id),
                err_type=type(err).__name__,
            )
            return None

    # --- the scheduler ----------------------------------------------------------------

    def _changed(
        self, tenant_id: uuid.UUID, guild: discord.Guild | None, event: _Event | None = None
    ) -> None:
        self._invalidate(tenant_id, event)
        if guild is not None and tenant_id in self._tracked:
            self._request(guild, fresh=True, delay=self._debounce_s)

    def _request(self, guild: discord.Guild, *, fresh: bool, delay: float = 0.0) -> None:
        """Start the server's reconcile, or, when `fresh` and a read is already under way,
        read once more after it. Nothing while disconnected."""
        if not self._connected:
            return
        tenant_id = tenant_of(guild.id)
        slot = self._slots.get(tenant_id)
        if slot is None:
            slot = self._slots[tenant_id] = _Slot(guild)
        slot.guild = guild
        if slot.task is not None:
            crossed = (slot.read_epoch, slot.read_changes) != (
                self._epoch,
                self._changes[tenant_id],
            )
            if slot.reading and (fresh or crossed):
                slot.again = True
            return
        self._start(tenant_id, slot, delay)

    def _start(self, tenant_id: uuid.UUID, slot: _Slot, delay: float) -> None:
        slot.reading = False
        slot.task = task = asyncio.create_task(
            self._run_slot(slot, delay), name="discord.audience_reconcile"
        )
        task.add_done_callback(lambda done: self._slot_done(tenant_id, slot, done))

    def _slot_done(self, tenant_id: uuid.UUID, slot: _Slot, done: asyncio.Task[None]) -> None:
        if slot.task is not done:
            return
        slot.task = None
        slot.reading = False
        if not done.cancelled() and (err := done.exception()) is not None:
            log.error("discord.audience.reconcile_crashed", err_type=type(err).__name__)
        if slot.again:
            slot.again = False
            self._start(tenant_id, slot, 0.0)

    async def _run_slot(self, slot: _Slot, delay: float) -> None:
        slot.published = False
        if delay:
            await asyncio.sleep(delay)
        async with self._reads:
            tenant_id = tenant_of(slot.guild.id)
            slot.read_epoch, slot.read_changes = self._epoch, self._changes[tenant_id]
            self._late.pop(tenant_id, None)
            slot.reading = True
            slot.published = await self._reconcile_once(
                slot.guild, epoch=slot.read_epoch, changes=slot.read_changes
            )

    async def _reconcile_once(self, guild: discord.Guild, *, epoch: int, changes: int) -> bool:
        tenant_id = tenant_of(guild.id)
        if guild.unavailable:
            return False
        if self._reader is None:
            log.warning(
                "discord.audience.reconcile_failed", guild_id=str(guild.id), why="no_reader"
            )
            return False
        started = self._clock()
        try:
            snapshot = await self._reader.read(guild.id, now=started)
        except ReadFailed as err:
            log.warning("discord.audience.reconcile_failed", guild_id=str(guild.id), why=err.reason)
            return False
        return await self._publish(
            guild, tenant_id, snapshot, epoch=epoch, changes=changes, started=started
        )

    # --- the database -----------------------------------------------------------------

    def _lock(self, tenant_id: uuid.UUID) -> asyncio.Lock:
        return self._locks.setdefault(tenant_id, asyncio.Lock())

    @contextlib.asynccontextmanager
    async def _step(self, tenant_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
        """One server's database step under its lock, bounded by `DB_WAIT_S` from the
        moment it starts to wait. No Discord call runs inside one."""
        async with (
            asyncio.timeout(self._db_wait_s),
            self._lock(tenant_id),
            self._sessionmaker.begin() as session,
        ):
            await session.execute(
                text(f"SET LOCAL lock_timeout = {max(1, int(self._db_wait_s * 500))}")
            )
            yield session

    def _current(
        self, tenant_id: uuid.UUID, epoch: int, changes: int, fresh: _Basis | None = None
    ) -> bool:
        """No disconnect and no change since the read began. With `fresh` (the read's own
        inputs), a change that is a raw event stating what the read saw does not count."""
        if not self._connected or epoch != self._epoch:
            return False
        count = self._changes[tenant_id]
        if changes == count:
            return True
        if fresh is None:
            return False
        late = [event for n, event in self._late.get(tenant_id, ()) if n > changes]
        if len(late) != count - changes:
            return False
        now = self._clock()
        return all(e is not None and fresh.same(e[0], e[1], now) for e in late)

    async def _publish(
        self,
        guild: discord.Guild,
        tenant_id: uuid.UUID,
        snapshot: GuildSnapshot,
        *,
        epoch: int,
        changes: int,
        started: datetime,
    ) -> bool:
        """Write the read's facts, unless a change crossed it, it is too old or the
        policy moved. Whether a full set was written.

        A change can come during any await of the transaction. So the count is checked
        again after the last one, just before the commit, and a crossed write is rolled
        back: no reader ever sees it. A change during the commit itself comes after the
        facts were decided; its unknown mark waits on this server's lock and lands next (a
        window of one commit plus one withdrawal write, not closed by a lock across
        processes)."""
        fresh = _Basis.of(snapshot)
        try:
            async with self._step(tenant_id) as session:
                if not self._current(tenant_id, epoch, changes, fresh) or guild.unavailable:
                    raise _Crossed
                now = self._clock()
                limit = started + self._reconcile_limit
                if now >= limit:
                    log.warning("discord.audience.read_too_old", guild_id=str(guild.id))
                    return False
                tenant = await get_tenant(session, tenant_id)
                if tenant is None or tenant.archived_at is not None:
                    return False
                policy = await load_access_policy(session, tenant_id=tenant_id)
                roles = (policy.internal_role_ids, policy.team_role_ids)
                if not policy.internal_role_ids:
                    self._published.pop(tenant_id, None)
                    await facts_store.retire_facts(session, tenant_id=tenant_id, now=now)
                    return False
                rooms = rooms_from_snapshot(
                    snapshot, internal_role_ids=roles[0], team_role_ids=roles[1]
                )
                # The only place the policy fence is held, and only to the end of this
                # transaction: a policy write cannot commit between the role lists
                # checked here and the facts written from them. A busy fence or changed
                # role lists retire the facts.
                try:
                    await lock_policy_writes_shared(session, tenant_id=tenant_id)
                except FenceUnavailable:
                    return await self._retire_stale(session, tenant_id, now, why="policy_busy")
                if await _policy_roles(session, tenant_id) != roles:
                    return await self._retire_stale(session, tenant_id, now, why="roles_changed")
                generation = await facts_store.publish_facts(
                    session,
                    tenant_id=tenant_id,
                    rooms=rooms,
                    now=now,
                    fresh_until=min(now + self._fresh_for, limit),
                )
                if not self._current(tenant_id, epoch, changes, fresh) or guild.unavailable:
                    raise _Crossed  # rolls the write back
        except _Crossed:
            log.info("discord.audience.read_crossed", guild_id=str(guild.id))
            return False
        except (SQLAlchemyError, AccessPolicyUnreadable, TimeoutError) as err:
            self._published.pop(tenant_id, None)
            log.warning(
                "discord.audience.publish_failed",
                guild_id=str(guild.id),
                err_type=type(err).__name__,
            )
            self._unknown_due.add(tenant_id)
            self._start_writer(tenant_id)
            return False
        # A change during the commit comes after the last check: its withdrawal waits for
        # this transaction's lock and runs the moment it is released, with no debounce.
        # Nothing renews facts a change has crossed, so they are visible for at most one
        # commit plus one withdrawal write. The read's inputs become the basis unless
        # such an event contradicts them.
        if self._current(tenant_id, epoch, changes, fresh):
            self._basis[tenant_id] = fresh
        if self._current(tenant_id, epoch, changes):  # else crossed during the commit
            self._published[tenant_id] = _Published(generation, epoch, changes, started, roles)
        log.info(
            "discord.audience.published",
            guild_id=str(guild.id),
            rooms=len(rooms),
            members=len(snapshot.members),
            member_pages=snapshot.member_pages,
            generation=generation,
        )
        return True

    async def _retire_stale(
        self, session: AsyncSession, tenant_id: uuid.UUID, now: datetime, *, why: str
    ) -> bool:
        """Facts computed from role lists that may be out of date must not stand: make every
        room unknown now (this needs no policy fence). The next tick reads again."""
        self._published.pop(tenant_id, None)
        await facts_store.mark_unknown(session, tenant_id=tenant_id, now=now)
        log.warning("discord.audience.publish_stale", tenant_id=str(tenant_id), why=why)
        return False

    def _invalidate(self, tenant_id: uuid.UUID, event: _Event | None = None) -> None:
        """A change: no renewal and every room unknown; a read that began before it
        publishes only if `event` states what that read saw."""
        self._changes[tenant_id] += 1
        slot = self._slots.get(tenant_id)
        if slot is not None and slot.reading:
            late = self._late.setdefault(tenant_id, [])
            keep = event if len(late) < MAX_LATE_EVENTS else None
            late.append((self._changes[tenant_id], keep))
        self._published.pop(tenant_id, None)
        self._unknown_due.add(tenant_id)
        self._start_writer(tenant_id)

    def _start_writer(self, tenant_id: uuid.UUID) -> None:
        if tenant_id in self._writers:
            return
        task = asyncio.create_task(self._write_unknown(tenant_id), name="discord.audience_unknown")
        self._writers[tenant_id] = task
        task.add_done_callback(lambda _: self._writers.pop(tenant_id, None))

    async def _write_unknown(self, tenant_id: uuid.UUID) -> None:
        """Write the server's rooms unknown, once more for each change that came while
        writing. A failure is retried at the next tick; until then nothing renews the
        facts, so they lapse within `FRESH_FOR_S`."""
        while tenant_id in self._unknown_due:
            self._unknown_due.discard(tenant_id)
            try:
                async with self._step(tenant_id) as session:
                    await facts_store.retire_facts(session, tenant_id=tenant_id, now=self._clock())
            except (SQLAlchemyError, TimeoutError) as err:
                self._unknown_failed.add(tenant_id)
                log.warning(
                    "discord.audience.unknown_failed",
                    tenant_id=str(tenant_id),
                    err_type=type(err).__name__,
                )
                return
            self._unknown_failed.discard(tenant_id)

    async def _wait_writers(self, tenants: Iterable[uuid.UUID]) -> None:
        writers = [w for t in tenants if (w := self._writers.get(t)) is not None]
        await asyncio.gather(*writers, return_exceptions=True)

    async def _renew(self, by_tenant: Mapping[uuid.UUID, discord.Guild]) -> None:
        """Push each published server's `fresh_until` out, each in its own bounded step,
        so one blocked server delays no other."""
        due: list[tuple[uuid.UUID, _Published]] = []
        for tenant_id, published in list(self._published.items()):
            guild = by_tenant.get(tenant_id)
            if (
                guild is None
                or guild.unavailable
                or not self._current(tenant_id, published.epoch, published.changes)
            ):
                self._published.pop(tenant_id, None)
                continue
            due.append((tenant_id, published))
        await asyncio.gather(*(self._renew_one(t, p) for t, p in due))

    async def _renew_one(self, tenant_id: uuid.UUID, published: _Published) -> None:
        try:
            async with self._step(tenant_id) as session:
                if self._published.get(tenant_id) is not published:
                    return  # replaced or withdrawn while this waited
                now = self._clock()
                limit = min(now + self._fresh_for, published.read_started + self._reconcile_limit)
                renewed = limit > now and await facts_store.renew_fresh_until(
                    session,
                    tenant_id=tenant_id,
                    generation=published.generation,
                    fresh_until=limit,
                    now=now,
                )
        except (SQLAlchemyError, TimeoutError) as err:
            # The facts stand to their current `fresh_until`; the next tick tries again.
            log.warning(
                "discord.audience.renew_failed",
                tenant_id=str(tenant_id),
                err_type=type(err).__name__,
            )
            return
        if not renewed and self._published.get(tenant_id) is published:
            del self._published[tenant_id]  # lapsed or retired: the next tick reads again


async def _policy_roles(session: AsyncSession, tenant_id: uuid.UUID) -> _Roles:
    policy: TenantAccessPolicy = await load_access_policy(session, tenant_id=tenant_id)
    return policy.internal_role_ids, policy.team_role_ids
