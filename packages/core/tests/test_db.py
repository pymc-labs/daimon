from __future__ import annotations

import os

import pytest
from daimon.core.db import build_engine, build_session_factory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker


@pytest.mark.asyncio
async def test_build_engine_returns_live_async_engine_when_pointed_at_test_db() -> None:
    url = os.environ["DAIMON_DATABASE__TEST_URL"]
    engine = build_engine(url)
    try:
        assert isinstance(engine, AsyncEngine)
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_build_session_factory_produces_usable_sessions_when_called() -> None:
    url = os.environ["DAIMON_DATABASE__TEST_URL"]
    engine = build_engine(url)
    factory = build_session_factory(engine)
    try:
        assert isinstance(factory, async_sessionmaker)
        async with factory() as session:
            result = await session.execute(text("SELECT 42"))
            assert result.scalar_one() == 42
    finally:
        await engine.dispose()
