"""The fake Anthropic client keeps the SDK's retries and skips its backoff."""

from __future__ import annotations

import anyio
import httpx
import pytest
from anthropic import APIConnectionError
from mux.testing.isolated_sdk import build_async_anthropic


async def test_the_fake_anthropic_client_retries_as_the_sdk_does_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    backoffs: list[float] = []
    real_sleep = anyio.sleep

    async def unreachable(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("no route", request=request)

    async def recorded_sleep(delay: float) -> None:
        backoffs.append(delay)
        await real_sleep(0)

    # The SDK sleeps between attempts through `anyio.sleep`; record what it asks for.
    monkeypatch.setattr("anthropic._base_client.anyio.sleep", recorded_sleep)
    client = build_async_anthropic(transport=httpx.MockTransport(unreachable), max_retries=2)
    try:
        with pytest.raises(APIConnectionError):
            await client.beta.agents.retrieve("agent_test")
    finally:
        await client.close()

    assert attempts == 3, "the first attempt and both retries reach the transport"
    # The SDK's own schedule would ask for about 0.5 s and then 1 s.
    assert backoffs == [0.0, 0.0]
