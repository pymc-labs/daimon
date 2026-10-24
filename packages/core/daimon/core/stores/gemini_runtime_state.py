"""Transactional Gemini payload persistence on the existing application state DB."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from uuid import UUID

from daimon.core._models import Account, GeminiRuntimeState
from daimon.core.session_fence_retry import FenceUnavailable, retry_fences, try_fence
from mux.contracts.ids import Scope
from sqlalchemy import delete, func, select, text, update
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
    ) -> None:
        if scope.is_platform or scope.is_legacy_host_authorized or not project.strip():
            raise ValueError("PostgreSQL Gemini state requires a tenant account and project")
        self._sessions = sessions
        self._key = {
            "tenant_id": UUID(scope.tenant_id),
            "account_id": UUID(scope.account_id),
            "project": project,
            "kind": kind,
        }

    async def _require_account(self, session: AsyncSession, *, locking: bool) -> None:
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
                # Each acquisition that can fail is named (fixed labels, never data).
                if isinstance(error, FenceUnavailable):
                    raise FenceUnavailable(deadline, error.reason) from None
                if isinstance(error, PoolTimeoutError):
                    raise FenceUnavailable(deadline, "pool_timeout") from None
                if isinstance(error, TimeoutError):
                    raise FenceUnavailable(deadline, "deadline") from None
                if (
                    isinstance(error, DBAPIError)
                    and getattr(error.orig, "sqlstate", None) == "55P03"
                ):
                    raise FenceUnavailable(deadline, "row_nowait") from None
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

            yield Payload(body, save)
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


async def count_records_for_account(session: AsyncSession, *, account_id: UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(GeminiRuntimeState)
            .where(
                GeminiRuntimeState.account_id == account_id, GeminiRuntimeState.kind == "records"
            )
        )
        or 0
    )


async def delete_records_for_account(session: AsyncSession, *, account_id: UUID) -> int:
    result = await session.execute(
        delete(GeminiRuntimeState).where(
            GeminiRuntimeState.account_id == account_id, GeminiRuntimeState.kind == "records"
        )
    )
    return cast(CursorResult[object], result).rowcount
