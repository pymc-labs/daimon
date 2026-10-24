"""Fatal poll configuration errors escape the background scheduler."""

import asyncio

import pytest
from daimon.adapters.sidecar.main import poll_loop
from daimon.adapters.sidecar.settings import SidecarSettings
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker


async def test_poll_loop_raises_for_misconfigured_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    # Bypass settings validation to exercise the runtime origin guard.
    settings = SidecarSettings.model_construct(
        os_executor_origin="http://outside.invalid",
        executor_secret=SecretStr("s" * 32),
    )
    async with asyncio.timeout(1):
        with pytest.raises(ValueError, match="private OS service"):
            await poll_loop(async_sessionmaker(), settings)
