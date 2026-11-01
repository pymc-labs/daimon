"""Remember what chat platforms call people and channels, and name them from it.

Adapters call `remember_user_names` / `remember_channel_names` with names they
already hold: an inbound message's author, a click's user, a lookup that
succeeded. A call never waits and never raises: it puts the name in a bounded
in-process queue, keyed by (tenant, platform, person or channel), where a later
sighting replaces an earlier one that has not been written yet. One consumer
task per process drains the queue in batches of at most ``BATCH_SIZE`` rows,
one session per batch, so a burst of new people never holds more than one pool
connection. A name is remembered as written only once its batch commits; a
failed batch is logged and dropped, and the next sighting tries again. With
``MAX_PENDING`` keys queued, further new keys are dropped and logged.

A person's name is only stored while they have a principal in that tenant
(`daimon.core.stores.platform_names`), and a privacy purge deletes it after the
principal, so a write racing the purge, from any process, cannot put it back.
The purge also calls `forget_users`, which drops their queued names and what
this process remembers writing for them. The billing panel reads the stored names back
(`daimon.core.stores.platform_names`) when the platform cannot answer live.

`resolve_names` is the panel's resolution order, shared by every adapter: a
live lookup, else the stored name, else a handle lookup for someone never seen,
each person concurrently under one timeout.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

import structlog
from daimon.core.stores.platform_names import KnownName, upsert_channel_names, upsert_user_names
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "BATCH_SIZE",
    "MAX_PENDING",
    "KnownName",
    "LiveLookup",
    "UserKey",
    "CHANNEL_STATUS_LABELS",
    "UNNAMED_CHANNEL",
    "ChannelLookup",
    "ChannelStatus",
    "LookedUpChannels",
    "defer_writes",
    "forget_all",
    "lookup_channel_names",
    "forget_users",
    "pending_count",
    "remember_channel_names",
    "remember_user_name",
    "remember_user_names",
    "resolve_names",
    "settle",
]

log = structlog.get_logger(__name__)

# Rows written per batch, in one session.
BATCH_SIZE = 50
# Keys waiting to be written; a new key past this is dropped.
MAX_PENDING = 5_000
# Names this process has written, so a busy channel writes each person once.
_MEMO_SIZE = 10_000
# What a failed write may raise; anything else is a bug and surfaces in the log.
_WRITE_ERRORS = (SQLAlchemyError, OSError, TimeoutError)

Kind = Literal["user", "channel"]
_Key = tuple[uuid.UUID, str, Kind, str]
UserKey = tuple[uuid.UUID, str, str]
"""(tenant id, platform, platform user id): whose name a privacy deletion forgets."""
Sessionmaker = async_sessionmaker[AsyncSession]

_memo: OrderedDict[_Key, KnownName] = OrderedDict()
# Latest unwritten value per key, oldest first, with the sessionmaker to write it with.
_pending: OrderedDict[_Key, tuple[Sessionmaker, KnownName]] = OrderedDict()
_consumer: asyncio.Task[None] | None = None
# Tests hold writes for `settle` rather than start the consumer; see `defer_writes`.
_held = False
_dropped = 0


def _clean(value: object) -> str | None:
    """One line of text, or None for a blank name or anything that is not text."""
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text or None


def _merge(new: KnownName, old: KnownName | None) -> KnownName:
    if old is None:
        return new
    return KnownName(new.display_name or old.display_name, new.handle or old.handle)


def _enqueue(
    sessionmaker: Sessionmaker,
    tenant_id: uuid.UUID,
    platform: str,
    kind: Kind,
    names: Mapping[str, KnownName],
) -> None:
    global _dropped
    added = False
    for item_id, name in names.items():
        given = KnownName(_clean(name.display_name), _clean(name.handle))
        if not item_id or given.label is None:
            continue
        key: _Key = (tenant_id, platform, kind, item_id)
        queued = _pending.get(key)
        if queued is not None:
            # Latest wins; a part this sighting lacks keeps the queued one.
            _pending[key] = (sessionmaker, _merge(given, queued[1]))
            added = True
            continue
        written = _memo.get(key)
        merged = _merge(given, written)
        if merged == written:
            _memo.move_to_end(key)
            continue
        if len(_pending) >= MAX_PENDING:
            _dropped += 1
            log.warning("platform_names.queue_full", dropped=_dropped, pending=len(_pending))
            continue
        _pending[key] = (sessionmaker, merged)
        added = True
    if added:
        _start_consumer()


def _start_consumer() -> None:
    global _consumer
    if _held or (_consumer is not None and not _consumer.done()):
        return
    _consumer = asyncio.get_running_loop().create_task(_consume(), name="platform_names.writer")


async def _consume() -> None:
    """Write queued names a batch at a time until the queue is empty."""
    while _pending:
        await _write_next_batch()


async def _write_next_batch() -> None:
    try:
        await _write_batch()
    except Exception:
        # Nobody awaits these writes for a result: log what escaped the batch's
        # own catch rather than lose the rest of the queue to it.
        log.exception("platform_names.writer_crashed")


def _take_batch() -> tuple[Sessionmaker, dict[_Key, KnownName]]:
    first = next(iter(_pending))
    sessionmaker = _pending[first][0]
    batch: dict[_Key, KnownName] = {}
    for key in list(_pending):
        if len(batch) == BATCH_SIZE:
            break
        maker, name = _pending[key]
        if maker is sessionmaker:
            batch[key] = name
            del _pending[key]
    return sessionmaker, batch


async def _write_batch() -> None:
    sessionmaker, batch = _take_batch()
    groups: dict[tuple[uuid.UUID, str, Kind], dict[str, KnownName]] = {}
    for (tenant_id, platform, kind, item_id), name in batch.items():
        groups.setdefault((tenant_id, platform, kind), {})[item_id] = name
    # A person with no principal yet (their first message, before admission
    # creates one) stores nothing; they stay unremembered so a later sighting
    # stores the name once the principal exists.
    skipped: set[_Key] = set()
    try:
        async with sessionmaker() as session, session.begin():
            for (tenant_id, platform, kind), names in groups.items():
                if kind == "user":
                    stored = await upsert_user_names(
                        session, tenant_id=tenant_id, platform=platform, names=names
                    )
                    skipped.update(
                        (tenant_id, platform, kind, user_id)
                        for user_id in names
                        if user_id not in stored
                    )
                else:
                    labels = {cid: n.label for cid, n in names.items() if n.label is not None}
                    await upsert_channel_names(
                        session, tenant_id=tenant_id, platform=platform, names=labels
                    )
    except _WRITE_ERRORS as exc:
        # Not remembered as written: the next sighting queues it again.
        log.warning("platform_names.write_failed", count=len(batch), error=type(exc).__name__)
        return
    for key, name in batch.items():
        if key not in skipped:
            _note(key, name)


def _note(key: _Key, name: KnownName) -> None:
    _memo[key] = name
    _memo.move_to_end(key)
    while len(_memo) > _MEMO_SIZE:
        _memo.popitem(last=False)


def forget_users(keys: Iterable[UserKey]) -> None:
    """Drop these people's queued names and what this process remembers writing.

    A privacy purge calls it with the rows it deletes, so a name queued before
    the purge is not written after it, and a later sighting is written afresh.
    """
    for tenant_id, platform, user_id in keys:
        key: _Key = (tenant_id, platform, "user", user_id)
        _pending.pop(key, None)
        _memo.pop(key, None)


def remember_user_names(
    sessionmaker: Sessionmaker,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    names: Mapping[str, KnownName],
) -> None:
    """Queue these people's names to be stored. Never raises, never waits.

    A part left None keeps the stored one. A name this process already wrote is
    skipped; a failed write is logged and tried again the next time it is seen.
    """
    _enqueue(sessionmaker, tenant_id, platform, "user", names)


def remember_user_name(
    sessionmaker: Sessionmaker,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    user_id: str,
    display_name: str | None = None,
    handle: str | None = None,
) -> None:
    """`remember_user_names` for one person."""
    remember_user_names(
        sessionmaker,
        tenant_id=tenant_id,
        platform=platform,
        names={user_id: KnownName(display_name, handle)},
    )


def remember_channel_names(
    sessionmaker: Sessionmaker,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    names: Mapping[str, str],
) -> None:
    """Queue these channels' names to be stored, as `remember_user_names` does."""
    _enqueue(
        sessionmaker,
        tenant_id,
        platform,
        "channel",
        {cid: KnownName(name) for cid, name in names.items()},
    )


def pending_count() -> int:
    """Keys waiting to be written."""
    return len(_pending)


def defer_writes(enabled: bool = True) -> None:
    """Hold queued writes until `settle` instead of starting the consumer.

    For tests, whose sessions share one connection that the consumer would use
    concurrently with the code under test.
    """
    global _held
    _held = enabled


async def settle() -> None:
    """Write everything queued and wait for the consumer; for tests and shutdown."""
    loop = asyncio.get_running_loop()
    if _consumer is not None and not _consumer.done() and _consumer.get_loop() is loop:
        await _consumer
    while _pending:
        await _write_next_batch()


def forget_all() -> None:
    """Drop the queue and what this process remembers writing; for tests."""
    global _dropped
    _memo.clear()
    _pending.clear()
    _missed.clear()
    _dropped = 0


LiveLookup = Callable[[str], Awaitable[KnownName | None]]
"""A platform lookup by user id: their name, or None when the platform can't say."""


async def resolve_names(
    user_ids: Collection[str],
    *,
    live: LiveLookup | None,
    stored: Mapping[str, str],
    handle: LiveLookup | None = None,
    timeout_s: float,
    log_event: str = "platform_names.lookup_timed_out",
) -> tuple[dict[str, str], dict[str, KnownName]]:
    """Each person's label, and the names lookups returned, for the caller to remember.

    Per person, in order: ``live`` (the platform's name for them now), else
    their ``stored`` label, else ``handle`` (an account lookup that answers for
    people who left), tried only for someone with no stored label. People are
    looked up concurrently; whoever is still waiting after ``timeout_s`` gets
    their stored label. Someone with no label at all is left out of the first
    map: the caller decides how to show them.
    """
    found: dict[str, KnownName] = {}

    async def one(user_id: str) -> str | None:
        if live is not None and (name := await live(user_id)) and name.label:
            found[user_id] = name
            return name.label
        if label := stored.get(user_id):
            return label
        if handle is not None and (name := await handle(user_id)) and name.label:
            found[user_id] = name
            return name.label
        return None

    labels: dict[str, str] = {}
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return labels, found
    tasks = {asyncio.create_task(one(user_id)): user_id for user_id in ids}
    done, pending = await asyncio.wait(tasks, timeout=timeout_s)
    for task in pending:
        task.cancel()
    if pending:
        log.info(log_event, unresolved=len(pending))
    for task, user_id in tasks.items():
        label: str | None = None
        if task in done and (exc := task.exception()) is not None:
            log.info("platform_names.lookup_failed", error=type(exc).__name__)
        elif task in done:
            label = task.result()
        if label := label or stored.get(user_id):
            labels[user_id] = label
    return labels, found


class ChannelStatus(StrEnum):
    """A channel the platform answered for that has no name to show."""

    GONE = "gone"  # deleted on the platform
    DIRECT = "direct"  # a direct message, not a workspace channel


UNNAMED_CHANNEL = "Unnamed channel"
CHANNEL_STATUS_LABELS: Mapping[ChannelStatus, str] = {
    ChannelStatus.GONE: "Deleted channel",
    ChannelStatus.DIRECT: "Direct message",
}

ChannelLookup = Callable[[str, str], Awaitable[str | ChannelStatus | None]]
"""A platform lookup by (workspace external id, channel id): the name, a status, or None.

None also means the channel belongs to another workspace than the one named.
"""


@dataclass(frozen=True)
class LookedUpChannels:
    """What the platform said: names it gave, and channels that are gone or direct messages."""

    names: dict[str, str] = field(default_factory=dict[str, str])
    statuses: dict[str, ChannelStatus] = field(default_factory=dict[str, ChannelStatus])

    @property
    def gone(self) -> frozenset[str]:
        return frozenset(c for c, s in self.statuses.items() if s is ChannelStatus.GONE)

    def labels(self) -> dict[str, str]:
        """Every answered channel's label: its name, else what its status says."""
        return {cid: CHANNEL_STATUS_LABELS[s] for cid, s in self.statuses.items()} | self.names


# How long a channel the platform could not name is left alone, so a channel it failed to
# answer for is asked about once an hour, not on every page.
_MISS_TTL_S = 3600.0
# A deleted channel or a direct message stays that way: remembered for a day.
_STATUS_TTL_S = 86_400.0
_LOOKUP_TIMEOUT_S = 3.0
# A lookup cut off by the timeout is asked again after this long.
_TIMEOUT_RETRY_S = 60.0
# At most this many platform calls per lookup, this many at once: a page that lists many
# unnamed channels must not turn into a burst of requests to the platform.
_MAX_LOOKUPS = 20
_LOOKUP_CONCURRENCY = 5
_MISS_MEMO_SIZE = 5_000
_missed: OrderedDict[tuple[uuid.UUID, str, str], tuple[float, ChannelStatus | None]] = OrderedDict()


async def lookup_channel_names(
    sessionmaker: Sessionmaker,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    external_id: str,
    channel_ids: Collection[str],
    lookup: ChannelLookup,
) -> LookedUpChannels:
    """Ask the platform for channels the store has no name for, and remember what it says.

    Only for ids the stored names missed, at most `_MAX_LOOKUPS` per call (the rest wait for
    a later call). A channel the platform could not name is not asked again for an hour; one
    it called gone or a direct message is answered from memory for a day. One it did not
    answer in time is asked again after a minute.
    Never raises and never waits past a few seconds.
    """
    now = time.monotonic()
    statuses: dict[str, ChannelStatus] = {}
    ids: list[str] = []
    for cid in dict.fromkeys(channel_ids):
        at, status = _missed.get((tenant_id, platform, cid), (-_STATUS_TTL_S, None))
        if status is not None and now - at < _STATUS_TTL_S:
            statuses[cid] = status
        elif status is not None or now - at >= _MISS_TTL_S:
            ids.append(cid)
    ids = ids[:_MAX_LOOKUPS]
    if not ids:
        return LookedUpChannels(statuses=statuses)
    gate = asyncio.Semaphore(_LOOKUP_CONCURRENCY)

    async def one(channel_id: str) -> str | ChannelStatus | None:
        async with gate:
            try:
                answer = await lookup(external_id, channel_id)
            except Exception:
                # Boundary: the lookup is the caller's network call; a name is optional.
                return None
            return answer if isinstance(answer, ChannelStatus) else _clean(answer)

    tasks = {asyncio.create_task(one(cid)): cid for cid in ids}
    done, pending = await asyncio.wait(tasks, timeout=_LOOKUP_TIMEOUT_S)
    for task in pending:
        task.cancel()
    found: dict[str, str] = {}
    for task, channel_id in tasks.items():
        answered = task in done and not task.cancelled()
        answer = task.result() if answered else None
        if isinstance(answer, str) and not isinstance(answer, ChannelStatus):
            found[channel_id] = answer
            continue
        if answer is not None:
            statuses[channel_id] = answer
        # No answer in time: a miss that ends after `_TIMEOUT_RETRY_S`, not an hour.
        at = now if answered else now - _MISS_TTL_S + _TIMEOUT_RETRY_S
        _missed[(tenant_id, platform, channel_id)] = (at, answer)
        _missed.move_to_end((tenant_id, platform, channel_id))
        while len(_missed) > _MISS_MEMO_SIZE:
            _missed.popitem(last=False)
    if found:
        remember_channel_names(sessionmaker, tenant_id=tenant_id, platform=platform, names=found)
    return LookedUpChannels(names=found, statuses=statuses)
