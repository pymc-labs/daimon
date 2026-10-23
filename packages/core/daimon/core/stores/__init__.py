"""daimon-core stores — free async functions over the core tables.

Each submodule takes `session: AsyncSession` as the first arg and returns
Pydantic domain types from `daimon.core.stores.domain`. No module-level state;
callers own the transaction boundary.
"""

from __future__ import annotations

from typing import Any

from daimon.core.errors import StoreError
from daimon.core.stores import (
    domain,
    identity,
    scoped_config_read,
    scoped_config_write,
    tenants,
    thread_sessions,
    turn_card_intents,
)
from sqlalchemy import Result, Select, inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.util import find_tables

__all__ = [
    "domain",
    "identity",
    "check_persisted_read",
    "read_persisted_columns",
    "UnflushedReadState",
    "scoped_config_read",
    "scoped_config_write",
    "tenants",
    "thread_sessions",
    "turn_card_intents",
]


class UnflushedReadState(StoreError):
    """A persisted read would hide effective changes to a guarded source."""


async def read_persisted_columns(
    session: AsyncSession, statement: Select[Any], *, reject_new: bool = False
) -> Result[Any]:
    """Read database columns without flushing or reusing cached ORM entities.

    Caller owns scope, ordering and transaction. Pending new objects remain
    unflushed; persistent dirty/deleted state refuses without being overwritten.
    """
    projected = check_persisted_read(session, statement, reject_new=reject_new)
    with session.no_autoflush:
        return await session.execute(projected)


def check_persisted_read(
    session: AsyncSession, statement: Select[Any], *, reject_new: bool = False
) -> Select[Any]:
    """`read_persisted_columns`' refusal alone, without the read; returns the projection.

    For a reader answering from rows it already read in this transaction: the same
    unflushed state refuses at the same point the per-row read would have refused.
    """
    projected = statement.with_only_columns(*statement.selected_columns)
    guarded = {
        name
        for table in find_tables(
            projected, check_columns=True, include_aliases=True, include_joins=True
        )
        if (name := getattr(table, "name", None)) is not None
    }
    for instance in (*session.deleted, *session.dirty, *(session.new if reject_new else ())):
        state = inspect(instance)
        table = getattr(state.mapper.persist_selectable, "name", None)
        if state.pending and table in guarded:
            raise UnflushedReadState(f"unflushed insert on {table} before a persisted read")
        if state.persistent and table in guarded:
            operation = (
                "delete"
                if instance in session.deleted
                else "update"
                if session.is_modified(instance, include_collections=False)
                else None
            )
            if operation:
                raise UnflushedReadState(
                    f"unflushed {operation} on {table} before a persisted read"
                )
    return projected
