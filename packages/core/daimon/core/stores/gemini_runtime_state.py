"""Transactional Gemini payload persistence on the existing application state DB."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, cast
from uuid import UUID

from daimon.core._models import Account, GeminiRecord, GeminiRuntimeState
from daimon.core.session_fence_retry import FenceUnavailable, retry_fences, try_fence
from daimon.core.shared_threads import shared_owner, shared_slot
from mux.contracts.ids import Scope
from mux.drivers.gemini.storage import ACCOUNT, Keys
from mux.state.lease import Slot
from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, case, delete, func, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PRE_DISPATCH_BUSY_BOUND_S = 5.0
"""How long a pre-dispatch transaction waits for a busy Records row before it
gives up. Busy is safely retryable only before the provider POST."""


async def complete(operation: Awaitable[None]) -> None:
    """Finish an already-started commit/close before propagating cancellation."""
    task = asyncio.ensure_future(operation)
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


@dataclass(frozen=True)
class Payload:
    body: str | None
    save: Callable[[str], Awaitable[None]]
    # The locked row's open transaction (PostgreSQL), for writes beside the save.
    session: AsyncSession | None = None


def _busy(error: BaseException, deadline: float) -> FenceUnavailable | None:
    """A failed acquisition named as busy (fixed labels, never data); None: not busy."""
    if isinstance(error, FenceUnavailable):
        return FenceUnavailable(deadline, error.reason)
    if isinstance(error, PoolTimeoutError):
        return FenceUnavailable(deadline, "pool_timeout")
    if isinstance(error, TimeoutError):
        return FenceUnavailable(deadline, "deadline")
    if isinstance(error, DBAPIError) and getattr(error.orig, "sqlstate", None) == "55P03":
        return FenceUnavailable(deadline, "row_nowait")
    return None


class PayloadDatabase(Protocol):
    def transaction(
        self, *, after_dispatch: bool = False
    ) -> AbstractAsyncContextManager[Payload]: ...


class PostgresPayloadDatabase:
    """One locked payload row; records and holds use independent transactions."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        scope: Scope,
        project: str,
        kind: Literal["records", "holds"],
        shared_slot: Slot | None = None,
    ) -> None:
        if scope.is_platform or scope.is_legacy_host_authorized or not project.strip():
            raise ValueError("PostgreSQL Gemini state requires a tenant account and project")
        # A shared thread's state belongs to the thread: its owner is derived from the
        # slot and has no Account row, so the slot stands in for the account check.
        if shared_slot is not None and (
            shared_slot.thread.channel.tenant_id != scope.tenant_id
            or str(shared_owner(shared_slot)) != scope.account_id
        ):
            raise ValueError("Gemini shared state differs from its thread")
        self._shared = shared_slot is not None
        self._sessions = sessions
        self._key = {
            "tenant_id": UUID(scope.tenant_id),
            "account_id": UUID(scope.account_id),
            "project": project,
            "kind": kind,
        }

    async def _require_account(self, session: AsyncSession, *, locking: bool) -> None:
        if self._shared:
            return
        statement = select(Account.id).where(
            Account.id == self._key["account_id"], Account.tenant_id == self._key["tenant_id"]
        )
        if locking:
            statement = statement.with_for_update(read=True, key_share=True)
        if await session.scalar(statement) is None:
            raise ValueError("Gemini state account is unavailable")

    @asynccontextmanager
    async def _locked(
        self, deadline: float, *, initialize: bool, after_dispatch: bool
    ) -> AsyncIterator[Payload]:
        key = "gemini_state:" + json.dumps([str(value) for value in self._key.values()])

        async def acquire():
            stack = AsyncExitStack()
            try:
                async with asyncio.timeout_at(deadline):
                    session = await stack.enter_async_context(self._sessions())
                    transaction = await session.begin()
                    await session.execute(text("SET LOCAL lock_timeout = '100ms'"))
                    await try_fence(
                        session,
                        key,
                        timeout_s=max(0, deadline - asyncio.get_running_loop().time()),
                        schema_scoped=True,
                    )
                    await self._require_account(session, locking=initialize)
                    predicates = (
                        GeminiRuntimeState.tenant_id == self._key["tenant_id"],
                        GeminiRuntimeState.account_id == self._key["account_id"],
                        GeminiRuntimeState.project == self._key["project"],
                        GeminiRuntimeState.kind == self._key["kind"],
                    )
                    body = None
                    if initialize:
                        await session.execute(
                            insert(GeminiRuntimeState).values(**self._key).on_conflict_do_nothing()
                        )
                    else:
                        row = (
                            await session.execute(
                                select(GeminiRuntimeState.body)
                                .where(*predicates)
                                .with_for_update(nowait=True)
                            )
                        ).one_or_none()
                        if row is None:
                            raise ValueError("Gemini state is unavailable")
                        body = row.body
                    return stack, session, transaction, predicates, body
            except BaseException as error:
                await complete(stack.aclose())
                if (busy := _busy(error, deadline)) is not None:
                    raise busy from None
                raise

        stack, session, transaction, predicates, body = await retry_fences(
            acquire, backoff=not after_dispatch, label=f"gemini_state:{self._key['kind']}"
        )
        try:

            async def save(value: str) -> None:
                # Read-only retrieval/polling must not rewrite the entire blob.
                if value != body:
                    await session.execute(
                        update(GeminiRuntimeState).where(*predicates).values(body=value)
                    )

            yield Payload(body, save, session)
            await complete(transaction.commit())
        except BaseException:
            if transaction.is_active:
                await complete(transaction.rollback())
            raise
        finally:
            await complete(stack.aclose())

    async def read(self) -> str | None:
        """The last committed payload, for a pure read: no fence, no row lock.

        Every session in the account shares this row, so a usage or event listing that
        waited behind its writers (fence plus FOR UPDATE NOWAIT, 5 s before dispatch)
        failed as busy under load. The connection is released before the caller decodes.
        """
        deadline = asyncio.get_running_loop().time() + PRE_DISPATCH_BUSY_BOUND_S

        async def attempt() -> str | None:
            try:
                async with asyncio.timeout_at(deadline):
                    async with self._sessions() as session, session.begin():
                        await session.execute(text("SET TRANSACTION READ ONLY"))
                        await self._require_account(session, locking=False)
                        return await session.scalar(
                            select(GeminiRuntimeState.body).where(
                                GeminiRuntimeState.tenant_id == self._key["tenant_id"],
                                GeminiRuntimeState.account_id == self._key["account_id"],
                                GeminiRuntimeState.project == self._key["project"],
                                GeminiRuntimeState.kind == self._key["kind"],
                            )
                        )
            # No lock to wait for, but a connection may be: busy, named, as for writes.
            except PoolTimeoutError:
                raise FenceUnavailable(deadline, "pool_timeout") from None
            except TimeoutError:
                raise FenceUnavailable(deadline, "deadline") from None

        return await retry_fences(attempt, label=f"gemini_state:{self._key['kind']}:read")

    @asynccontextmanager
    async def transaction(self, *, after_dispatch: bool = False) -> AsyncIterator[Payload]:
        from daimon.core.turn.ceiling import TURN_CEILING_S

        # Before dispatch, busy is safely retryable. After dispatch, keep a long
        # acquisition fallback; the host's enclosing send timeout (120s default,
        # including pre-dispatch work/POST time) or earlier request cancellation binds.
        deadline = asyncio.get_running_loop().time() + (
            TURN_CEILING_S if after_dispatch else PRE_DISPATCH_BUSY_BOUND_S
        )
        # Initialization commits its FK/account locks before any provider I/O.
        async with self._locked(deadline, initialize=True, after_dispatch=after_dispatch):
            pass
        async with self._locked(
            deadline, initialize=False, after_dispatch=after_dispatch
        ) as payload:
            yield payload


RowId = tuple[str, str]


class GeminiRecordRow(BaseModel):
    """One stored `gemini_records` row, as loaded."""

    model_config = ConfigDict(frozen=True)

    kind: str
    id: str
    body: str
    version: int
    touched_at: datetime
    refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecordWrite:
    kind: str
    id: str
    body: str
    binding_id: str | None = None
    session_id: str | None = None
    refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadedRows:
    """The rows one transaction loaded; `named`: every id it asked for (found or not)."""

    rows: dict[RowId, GeminiRecordRow]
    named: tuple[RowId, ...]
    save: Callable[[Sequence[RecordWrite], Sequence[RowId]], Awaitable[None]]


def ref_id(value: str) -> RowId:
    kind, _, id_ = value.partition(":")
    return kind, id_


class PostgresRecordsDatabase:
    """`gemini_records` rows of one (tenant, account, project); holds stay a blob.

    Locked rows take FOR UPDATE NOWAIT in one order (account, resources by kind
    and id, sessions) inside the same busy-retry bounds as the blob. New rows
    have fresh ids, so they need no fence; the account row is initialized
    before it is locked, as the blob is.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        scope: Scope,
        project: str,
        empty_account: str,
        shared_slot: Slot | None = None,
    ) -> None:
        self._legacy = PostgresPayloadDatabase(
            sessions, scope=scope, project=project, kind="records", shared_slot=shared_slot
        )
        self._sessions = sessions
        self._owner = (UUID(scope.tenant_id), UUID(scope.account_id), project)
        self._empty_account = empty_account

    @property
    def owner(self) -> tuple[UUID, UUID, str]:
        return self._owner

    def _where(self) -> tuple[ColumnElement[bool], ...]:
        tenant, account, project = self._owner
        return (
            GeminiRecord.tenant_id == tenant,
            GeminiRecord.account_id == account,
            GeminiRecord.project == project,
        )

    async def _select(
        self,
        session: AsyncSession,
        ids: Iterable[RowId] | None,
        *,
        kinds: Iterable[str] = (),
        lock: bool = False,
    ) -> list[GeminiRecordRow]:
        """Rows by id (None: every row) and of `kinds`; `lock` in the one lock order."""
        wanted = list(ids) if ids is not None else None
        kinds = list(kinds)
        if wanted == [] and not kinds:
            return []
        clauses: list[ColumnElement[bool]] = []
        if wanted:
            clauses.append(tuple_(GeminiRecord.kind, GeminiRecord.id).in_(wanted))
        if kinds:
            clauses.append(GeminiRecord.kind.in_(kinds))
        statement = select(
            GeminiRecord.kind,
            GeminiRecord.id,
            GeminiRecord.body,
            GeminiRecord.version,
            GeminiRecord.touched_at,
            GeminiRecord.refs,
        ).where(*self._where(), *([or_(*clauses)] if wanted is not None else []))
        if lock:
            order = case(
                (GeminiRecord.kind == "account", 0), (GeminiRecord.kind == "session", 2), else_=1
            )
            statement = statement.order_by(
                order, GeminiRecord.kind, GeminiRecord.id
            ).with_for_update(nowait=True)
        return [
            GeminiRecordRow(
                kind=row.kind,
                id=row.id,
                body=row.body,
                version=row.version,
                touched_at=row.touched_at,
                refs=tuple(row.refs),
            )
            for row in await session.execute(statement)
        ]

    async def _load(
        self, session: AsyncSession, keys: Keys, *, locking: bool
    ) -> tuple[dict[RowId, GeminiRecordRow], tuple[RowId, ...]]:
        rows: dict[RowId, GeminiRecordRow] = {}
        if keys.everything:
            found = await self._select(session, None, lock=locking)
            return {(r.kind, r.id): r for r in found}, ()
        named: list[RowId] = [*keys.lock, *keys.read]
        if locking and keys.lock:
            rows.update(
                {(r.kind, r.id): r for r in await self._select(session, keys.lock, lock=True)}
            )
        unlocked = [k for k in named if k not in rows]
        rows.update(
            {(r.kind, r.id): r for r in await self._select(session, unlocked, kinds=keys.scan)}
        )
        if keys.refs:
            refs = [
                ref_id(value)
                for key in named
                if key[0] == "session" and (row := rows.get(key)) is not None
                for value in row.refs
            ]
            named.extend(refs)
            missing = [k for k in refs if k not in rows]
            rows.update({(r.kind, r.id): r for r in await self._select(session, missing)})
        return rows, tuple(dict.fromkeys(named))

    async def _write(
        self, session: AsyncSession, writes: Sequence[RecordWrite], deletes: Sequence[RowId]
    ) -> None:
        tenant, account, project = self._owner
        if writes:
            statement = insert(GeminiRecord).values(
                [
                    {
                        "tenant_id": tenant,
                        "account_id": account,
                        "project": project,
                        "kind": w.kind,
                        "id": w.id,
                        "body": w.body,
                        "binding_id": w.binding_id,
                        "session_id": w.session_id,
                        "refs": list(w.refs),
                    }
                    for w in writes
                ]
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["tenant_id", "account_id", "project", "kind", "id"],
                    set_={
                        "body": statement.excluded.body,
                        "version": GeminiRecord.version + 1,
                        "touched_at": func.now(),
                        "binding_id": statement.excluded.binding_id,
                        "session_id": statement.excluded.session_id,
                        "refs": statement.excluded.refs,
                    },
                )
            )
        if deletes:
            await session.execute(
                delete(GeminiRecord).where(
                    *self._where(), tuple_(GeminiRecord.kind, GeminiRecord.id).in_(list(deletes))
                )
            )

    async def _initialize_account(self, deadline: float) -> None:
        """Commit the account row (FK/account locks) before it is ever locked."""
        tenant, account, project = self._owner
        try:
            async with asyncio.timeout_at(deadline):
                async with self._sessions() as session, session.begin():
                    await self._legacy._require_account(session, locking=True)  # pyright: ignore[reportPrivateUsage]  # same store module
                    await session.execute(
                        insert(GeminiRecord)
                        .values(
                            tenant_id=tenant,
                            account_id=account,
                            project=project,
                            kind="account",
                            id="",
                            body=self._empty_account,
                        )
                        .on_conflict_do_nothing()
                    )
        except BaseException as error:
            if (busy := _busy(error, deadline)) is not None:
                raise busy from None
            raise

    @asynccontextmanager
    async def transaction(
        self, keys: Keys, *, after_dispatch: bool = False
    ) -> AsyncIterator[LoadedRows]:
        from daimon.core.turn.ceiling import TURN_CEILING_S

        deadline = asyncio.get_running_loop().time() + (
            TURN_CEILING_S if after_dispatch else PRE_DISPATCH_BUSY_BOUND_S
        )
        if keys.everything or ACCOUNT in keys.lock:
            await self._initialize_account(deadline)

        async def acquire():
            stack = AsyncExitStack()
            try:
                async with asyncio.timeout_at(deadline):
                    session = await stack.enter_async_context(self._sessions())
                    transaction = await session.begin()
                    await session.execute(text("SET LOCAL lock_timeout = '100ms'"))
                    await self._legacy._require_account(session, locking=False)  # pyright: ignore[reportPrivateUsage]  # same store module
                    rows, named = await self._load(session, keys, locking=True)
                    return stack, session, transaction, rows, named
            except BaseException as error:
                await complete(stack.aclose())
                if (busy := _busy(error, deadline)) is not None:
                    raise busy from None
                raise

        stack, session, transaction, rows, named = await retry_fences(
            acquire, backoff=not after_dispatch, label="gemini_records"
        )
        try:

            async def save(writes: Sequence[RecordWrite], deletes: Sequence[RowId]) -> None:
                await self._write(session, writes, deletes)

            yield LoadedRows(rows, named, save)
            await complete(transaction.commit())
        except BaseException:
            if transaction.is_active:
                await complete(transaction.rollback())
            raise
        finally:
            await complete(stack.aclose())

    async def read(self, keys: Keys) -> LoadedRows:
        """The last committed rows, for a pure read: no lock; the connection is
        released before the caller decodes."""
        deadline = asyncio.get_running_loop().time() + PRE_DISPATCH_BUSY_BOUND_S

        async def refuse(writes: Sequence[RecordWrite], deletes: Sequence[RowId]) -> None:
            raise RuntimeError("a Gemini records read never saves")

        async def attempt() -> LoadedRows:
            try:
                async with asyncio.timeout_at(deadline):
                    async with self._sessions() as session, session.begin():
                        await session.execute(text("SET TRANSACTION READ ONLY"))
                        await self._legacy._require_account(session, locking=False)  # pyright: ignore[reportPrivateUsage]  # same store module
                        rows, named = await self._load(session, keys, locking=False)
                        return LoadedRows(rows, named, refuse)
            except PoolTimeoutError:
                raise FenceUnavailable(deadline, "pool_timeout") from None
            except TimeoutError:
                raise FenceUnavailable(deadline, "deadline") from None

        return await retry_fences(attempt, label="gemini_records:read")

    async def drain(
        self,
        tombstone: str,
        convert: Callable[[str | None], Awaitable[list[RecordWrite]]],
    ) -> None:
        """Move the legacy blob into rows once, under the blob's own fence and lock.

        Missing: a tombstone takes its place, so older code never starts a blob.
        A blob: decoded once, its rows inserted and the tombstone written in one
        transaction, so a write by older code is never lost.
        """
        deadline = asyncio.get_running_loop().time() + PRE_DISPATCH_BUSY_BOUND_S
        tenant, account, project = self._owner
        legacy = (
            GeminiRuntimeState.tenant_id == tenant,
            GeminiRuntimeState.account_id == account,
            GeminiRuntimeState.project == project,
            GeminiRuntimeState.kind == "records",
        )
        try:
            async with asyncio.timeout_at(deadline):
                async with self._sessions() as session, session.begin():
                    await self._legacy._require_account(session, locking=True)  # pyright: ignore[reportPrivateUsage]  # same store module
                    await session.execute(
                        insert(GeminiRuntimeState)
                        .values(
                            tenant_id=tenant,
                            account_id=account,
                            project=project,
                            kind="records",
                            body=tombstone,
                        )
                        .on_conflict_do_nothing()
                    )
                    body = await session.scalar(select(GeminiRuntimeState.body).where(*legacy))
        except BaseException as error:
            if (busy := _busy(error, deadline)) is not None:
                raise busy from None
            raise
        if body == tombstone:
            return
        async with self._legacy.transaction() as payload:
            if payload.body == tombstone:
                return
            assert payload.session is not None, "the PostgreSQL payload carries its session"
            await self._write(payload.session, await convert(payload.body), ())
            await payload.save(tombstone)


async def count_shared_records(
    session: AsyncSession, *, threads: Iterable[tuple[UUID, str, str, str]]
) -> int:
    """Rows a shared thread owns: (tenant, platform, channel, thread) each, every kind,
    in both the legacy blob table and `gemini_records`.

    A shared thread's rows belong to the thread, not to any one participant.
    """
    owners = {(tenant, shared_owner(shared_slot(tenant, *rest))) for tenant, *rest in threads}
    if not owners:
        return 0
    total = 0
    for table in (GeminiRuntimeState, GeminiRecord):
        total += int(
            await session.scalar(
                select(func.count())
                .select_from(table)
                .where(tuple_(table.tenant_id, table.account_id).in_(owners))
            )
            or 0
        )
    return total


def _legacy_records(account_id: UUID) -> tuple[ColumnElement[bool], ...]:
    return GeminiRuntimeState.account_id == account_id, GeminiRuntimeState.kind == "records"


async def count_records_for_account(session: AsyncSession, *, account_id: UUID) -> int:
    """The account's Gemini records: its rows plus any legacy blob (drained or not)."""
    legacy = await session.scalar(
        select(func.count()).select_from(GeminiRuntimeState).where(*_legacy_records(account_id))
    )
    rows = await session.scalar(
        select(func.count()).select_from(GeminiRecord).where(GeminiRecord.account_id == account_id)
    )
    return int(legacy or 0) + int(rows or 0)


async def delete_records_for_account(session: AsyncSession, *, account_id: UUID) -> int:
    legacy = await session.execute(delete(GeminiRuntimeState).where(*_legacy_records(account_id)))
    rows = await session.execute(delete(GeminiRecord).where(GeminiRecord.account_id == account_id))
    return cast(CursorResult[object], legacy).rowcount + cast(CursorResult[object], rows).rowcount
