"""Fatal poll configuration errors escape the background scheduler."""

import asyncio
import socket
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.sidecar import main
from daimon.adapters.sidecar.main import poll_loop
from daimon.adapters.sidecar.settings import SidecarSettings
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker


async def test_poll_loop_raises_for_misconfigured_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    # Bypass settings validation to exercise the runtime origin guard.
    settings = SidecarSettings.model_construct(
        os_executor_origin="http://outside.invalid",
        executor_secret=SecretStr("s" * 32),
    )
    async with asyncio.timeout(1):
        with pytest.raises(ValueError, match="private watch executor"):
            await poll_loop(async_sessionmaker(), settings)


@pytest.mark.parametrize(
    "error",
    [
        OSError("connection lost"),
        ConnectionRefusedError("database restarting"),
        socket.gaierror("DNS unavailable"),
        TimeoutError("connection timed out"),
        DBAPIError(None, None, Exception("connection lost")),
    ],
)
async def test_poll_loop_retries_transient_database_errors(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    succeeded = asyncio.Event()

    async def poll(*args: object, **kwargs: object) -> int:
        if polling.await_count > 1:
            succeeded.set()
            return 0
        raise error

    polling = AsyncMock(side_effect=poll)
    monkeypatch.setattr(main, "poll_once", polling)
    settings = SidecarSettings.model_construct(poll_interval_s=0.01)
    task = asyncio.create_task(poll_loop(async_sessionmaker(), settings))
    try:
        async with asyncio.timeout(1):
            await succeeded.wait()
        assert polling.await_count == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
