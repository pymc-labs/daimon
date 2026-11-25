"""Erasure SQL for tables outside core's ORM, run in the purge's own transaction."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from daimon.core import ports
from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ErasureStatement:
    """One statement bound to `:account_id` or `:tenant_id`, by `subject`."""

    key: str
    sql: str
    subject: Literal["account", "tenant"]
    schema: str
    """The schema the statement touches: it is skipped while that schema does not exist
    (a deployment that never turned the feature on, or a migration that was a no-op)."""


_REGISTERED: Final[dict[str, ErasureStatement]] = {}


def register_erasure(statements: Iterable[ErasureStatement]) -> None:
    """Add statements, keyed so a second registration replaces the first."""
    _REGISTERED.update((s.key, s) for s in statements)


async def _run(session: AsyncSession, subject: str, params: dict[str, uuid.UUID]) -> int:
    ports.load()
    schemas = {s.schema for s in _REGISTERED.values() if s.subject == subject}
    present = {
        schema
        for schema in schemas
        if (
            await session.execute(text("select to_regnamespace(:s) is not null"), {"s": schema})
        ).scalar_one()
    }
    erased = 0
    for statement in _REGISTERED.values():
        if statement.subject == subject and statement.schema in present:
            result = await session.execute(text(statement.sql), params)
            erased += max(0, cast("CursorResult[Any]", result).rowcount)
    return erased


async def erase_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """Run every account statement; the rows deleted."""
    return await _run(session, "account", {"account_id": account_id})


async def erase_tenant(session: AsyncSession, *, tenant_id: uuid.UUID) -> int:
    """Run every tenant statement; the rows deleted."""
    return await _run(session, "tenant", {"tenant_id": tenant_id})
