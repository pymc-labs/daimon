from __future__ import annotations

import os

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.config import Settings


@pytest.mark.asyncio
async def test_build_runtime_yields_wired_bundle_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.environ["DAIMON_DATABASE__TEST_URL"]
    monkeypatch.setenv("DAIMON_DATABASE__URL", url)
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test-not-real")
    settings = Settings()  # pyright: ignore[reportCallIssue]
    async with build_runtime(settings) as rt:
        assert isinstance(rt, CliRuntime)
        assert isinstance(rt.anthropic, AsyncAnthropic)
        async with rt.sessionmaker() as s:
            await s.connection()
