"""Remember what chat platforms call people and channels, and name them from it.

Adapters call `remember_user_names` / `remember_channel_names` with names they
already hold: an inbound message's author, a click's user, a lookup that
succeeded. Each call is fire-and-forget: it never blocks or fails the caller,
and a name already written by this process is not written again. The billing
panel reads the stored names back (`daimon.core.stores.platform_names`) when
the platform cannot answer live.

`resolve_names` is the panel's resolution order, shared by every adapter: a
live lookup, else the stored name, else a handle lookup for someone never seen,
each person concurrently under one timeout.
"""

from __future__ import annotations

import asyncio
import functools
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Mapping
from typing import Literal

import structlog
from daimon.core.stores.platform_names import KnownName, upsert_channel_names, upsert_user_names
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "KnownName",
    "LiveLookup",
    "defer_writes",
    "forget_all",
    "remember_channel_names",
    "remember_user_name",
    "remember_user_names",
    "resolve_names",
    "settle",
]

log = structlog.get_logger(__name__)

# Names written by this process, so a busy channel writes each person once.
_MEMO_SIZE = 10_000
# What a failed write may raise; anything else is a bug and surfaces.
_WRITE_ERRORS = (SQLAlchemyError, OSError, TimeoutError)

_Key = tuple[uuid.UUID, str, Literal["user", "channel"], str]
_memo: OrderedDict[_Key, KnownName] = OrderedDict()
_pending: set[asyncio.Task[None]] = set()
# Writes held for `settle` instead of started; see `defer_writes`.
_deferred: list[Callable[[], Awaitable[None]]] | None = None


def _remembered(key: _Key) -> KnownName | None:
    known = _memo.get(key)
    if known is not None:
        _memo.move_to_end(key)
    return known


def _note(key: _Key, name: KnownName) -> None:
    _memo[key] = name
    _memo.move_to_end(key)
    while len(_memo) > _MEMO_SIZE:
        _memo.popitem(last=False)


def _clean(value: object) -> str | None:
    """One line of text, or None for a blank name or anything that is not text."""
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text or None


def _news(
    tenant_id: uuid.UUID,
    platform: str,
    kind: Literal["user", "channel"],
    names: Mapping[str, KnownName],
) -> dict[str, KnownName]:
    """The names that would change what this process last wrote, noted as written."""
    news: dict[str, KnownName] = {}
    for item_id, name in names.items():
        given = KnownName(_clean(name.display_name), _clean(name.handle))
        if not item_id or given.label is None:
            continue
        key: _Key = (tenant_id, platform, kind, item_id)
        last = _remembered(key)
        merged = (
            given
            if last is None
            else KnownName(given.display_name or last.display_name, given.handle or last.handle)
        )
        if merged != last:
            news[item_id] = given
            _note(key, merged)
    return news


def _forget(
    tenant_id: uuid.UUID, platform: str, kind: Literal["user", "channel"], ids: Collection[str]
) -> None:
    for item_id in ids:
        _memo.pop((tenant_id, platform, kind, item_id), None)


def _spawn(write: Callable[[], Awaitable[None]]) -> None:
    async def run() -> None:
        # Nothing awaits this task, so whatever escapes the write's own catch
        # would only surface as asyncio's "never retrieved" line; log it here.
        try:
            await write()
        except Exception:
            log.exception("platform_names.write_crashed")

    if _deferred is not None:
        _deferred.append(run)
        return
    task = asyncio.get_running_loop().create_task(run())
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _write_users(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    platform: str,
    names: dict[str, KnownName],
) -> None:
    try:
        async with sessionmaker() as session, session.begin():
            await upsert_user_names(session, tenant_id=tenant_id, platform=platform, names=names)
    except _WRITE_ERRORS as exc:
        _forget(tenant_id, platform, "user", names)
        log.warning(
            "platform_names.user_write_failed",
            platform=platform,
            count=len(names),
            error=type(exc).__name__,
        )


async def _write_channels(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    platform: str,
    names: dict[str, str],
) -> None:
    try:
        async with sessionmaker() as session, session.begin():
            await upsert_channel_names(session, tenant_id=tenant_id, platform=platform, names=names)
    except _WRITE_ERRORS as exc:
        _forget(tenant_id, platform, "channel", names)
        log.warning(
            "platform_names.channel_write_failed",
            platform=platform,
            count=len(names),
            error=type(exc).__name__,
        )


def remember_user_names(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    names: Mapping[str, KnownName],
) -> None:
    """Store these people's names in the background. Never raises, never waits.

    A part left None keeps the stored one. A name this process already wrote is
    skipped; a failed write is logged and tried again the next time it is seen.
    """
    news = _news(tenant_id, platform, "user", names)
    if news:
        _spawn(functools.partial(_write_users, sessionmaker, tenant_id, platform, news))


def remember_user_name(
    sessionmaker: async_sessionmaker[AsyncSession],
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
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    names: Mapping[str, str],
) -> None:
    """Store these channels' names in the background, as `remember_user_names` does."""
    news = _news(
        tenant_id, platform, "channel", {cid: KnownName(name) for cid, name in names.items()}
    )
    if news:
        labels = {cid: name.label for cid, name in news.items() if name.label is not None}
        _spawn(functools.partial(_write_channels, sessionmaker, tenant_id, platform, labels))


def defer_writes(enabled: bool = True) -> None:
    """Hold writes until `settle` runs them one at a time, instead of starting them.

    For tests, whose sessions share one connection that a background write
    would use concurrently with the code under test.
    """
    global _deferred
    _deferred = [] if enabled else None


async def settle() -> None:
    """Run held writes, then wait for every pending one on this loop; for tests and shutdown."""
    while _deferred:
        await _deferred.pop(0)()
    loop = asyncio.get_running_loop()
    while tasks := [task for task in _pending if task.get_loop() is loop and not task.done()]:
        await asyncio.gather(*tasks, return_exceptions=True)


def forget_all() -> None:
    """Drop what this process remembers having written; for tests."""
    _memo.clear()


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
