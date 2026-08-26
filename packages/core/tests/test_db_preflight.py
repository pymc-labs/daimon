from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg.exceptions
import pytest
from daimon.core.db import ensure_database_migrated
from daimon.core.errors import DatabaseNotMigratedError
from sqlalchemy.exc import ProgrammingError


class _Session:
    def __init__(self, execute: Callable[[object], object]) -> None:
        self._execute = execute

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, statement: object) -> object:
        result = self._execute(statement)
        if isinstance(result, BaseException):
            raise result
        return result


def _sessionmaker(execute: Callable[[object], object]) -> Any:
    return lambda: _Session(execute)


@pytest.mark.asyncio
async def test_ensure_database_migrated_probes_a_core_table() -> None:
    statements: list[str] = []

    await ensure_database_migrated(
        _sessionmaker(lambda statement: statements.append(str(statement)))
    )

    assert statements == ["SELECT 1 FROM tenants LIMIT 1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ProgrammingError("SELECT", {}, RuntimeError("relation does not exist")),
        asyncpg.exceptions.UndefinedTableError("relation does not exist"),
    ],
)
async def test_ensure_database_migrated_adds_the_operator_hint(error: BaseException) -> None:
    with pytest.raises(DatabaseNotMigratedError, match="uv run alembic upgrade head") as exc_info:
        await ensure_database_migrated(_sessionmaker(lambda _statement: error))

    assert exc_info.value.__cause__ is error
    assert "DAIMON_DATABASE_URL" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ensure_database_migrated_does_not_mask_other_database_failures() -> None:
    error = RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable") as exc_info:
        await ensure_database_migrated(_sessionmaker(lambda _statement: error))

    assert exc_info.value is error
