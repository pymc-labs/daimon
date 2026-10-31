"""A failed read of the cost footer setting keeps the footer."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

from daimon.core.cost_footer import cost_footer_shown
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class _Unreachable:
    def __call__(self) -> contextlib.AbstractAsyncContextManager[Any]:
        @contextlib.asynccontextmanager
        async def session() -> AsyncIterator[Any]:
            raise ConnectionRefusedError("database down")
            yield  # pragma: no cover

        return session()


async def test_an_unreadable_setting_shows_the_footer() -> None:
    sessions = cast(async_sessionmaker[AsyncSession], _Unreachable())
    assert await cost_footer_shown(sessions, tenant_id=uuid.uuid4()) is True, (
        "a display setting must never cost a turn its footer or its close"
    )
