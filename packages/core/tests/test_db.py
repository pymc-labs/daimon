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
async def test_build_engine_replaces_pooled_connection_when_killed_while_idle() -> None:
    """A connection reaped while it sits in the pool must not fail the next query.

    Long-lived adapters hold one engine for the process lifetime, so a quiet
    thread leaves a pooled connection idle long enough for the network path to
    drop it without a FIN. Without pool_pre_ping the pool hands that dead socket
    straight back and the caller sees ConnectionDoesNotExistError.
    """
    url = os.environ["DAIMON_DATABASE__TEST_URL"]
    engine = build_engine(url)
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            first_pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()

        # `terminate()` drops the transport without a clean shutdown, which is
        # what the client is left holding after a network-side idle reap: a
        # socket that is gone with no FIN to announce it. Killing the backend
        # server-side instead would not reproduce this — Postgres would send a
        # FATAL and asyncpg would abort the protocol, a different failure.
        pooled = await engine.raw_connection()
        driver = pooled.driver_connection
        assert driver is not None, "pooled connection should expose its asyncpg connection"
        driver.terminate()
        pooled.close()

        async with factory() as session:
            second_pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()

        assert second_pid != first_pid, (
            "pool should have opened a replacement backend, not reused the dead one"
        )
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


@pytest.mark.parametrize(
    ("allow", "event", "level"),
    [
        ("false", "agent_env.encryption_keys_missing", "error"),
        ("true", "agent_env.encryption_disabled", "warning"),
    ],
    ids=["fail-closed", "dev-opt-in"],
)
async def test_keyless_factory_logs_once_at_startup(
    monkeypatch: pytest.MonkeyPatch, allow: str, event: str, level: str
) -> None:
    from structlog.testing import capture_logs

    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", "[]")
    monkeypatch.setenv("DAIMON_CRYPTO__ALLOW_PLAINTEXT", allow)
    engine = build_engine(os.environ["DAIMON_DATABASE__TEST_URL"])
    try:
        with capture_logs() as logs:
            factory = build_session_factory(engine)
            async with factory() as first, factory() as second:
                assert first.info["crypto_keys"] == second.info["crypto_keys"] == ()
                assert first.info["crypto_allow_plaintext"] is (allow == "true")
        assert [(log["event"], log["log_level"]) for log in logs] == [(event, level)]
    finally:
        await engine.dispose()


def test_build_engine_hides_bound_parameters_in_errors() -> None:
    """SQL errors must not carry bound values (agent keys, tokens) into logs."""
    engine = build_engine("postgresql+asyncpg://u:p@localhost:1/d")

    assert engine.sync_engine.hide_parameters is True
