"""Compute cleanup must return control to routine dispatch during an outage."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from daimon.adapters.scheduler import main
from pydantic import SecretStr


async def test_compute_sweep_cancels_a_stalled_provider(monkeypatch):
    cancelled = asyncio.Event()
    timeout = asyncio.timeout
    budgets = []

    def short_timeout(seconds):
        budgets.append(seconds)
        return timeout(0.01)

    async def stalled():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(main.asyncio, "timeout", short_timeout)
    # Logging stays synchronous and must never receive the remote exception.
    log = Mock()
    monkeypatch.setattr(main, "log", log)
    compute = SimpleNamespace(
        settings=SimpleNamespace(deployment_id="test", api_token=SecretStr("test")),
        reconcile=stalled,
    )
    await main._sweep_compute(compute)
    assert budgets == [10]
    assert cancelled.is_set()
    log.warning.assert_called_once_with("compute.sweep_retry")


async def test_compute_sweep_skips_unconfigured_provider():
    compute = SimpleNamespace(
        settings=SimpleNamespace(deployment_id="", api_token=None), reconcile=AsyncMock()
    )
    await main._sweep_compute(compute)
    compute.reconcile.assert_not_awaited()
