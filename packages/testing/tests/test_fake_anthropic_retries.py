"""The fake Anthropic client keeps the SDK's retries and skips its backoff."""

from __future__ import annotations

import time

import httpx
import pytest
from anthropic import APIConnectionError
from daimon.testing.isolated_sdk import build_async_anthropic


async def test_the_fake_anthropic_client_retries_as_the_sdk_does_without_waiting() -> None:
    attempts = 0

    async def unreachable(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("no route", request=request)

    client = build_async_anthropic(transport=httpx.MockTransport(unreachable), max_retries=2)
    started = time.perf_counter()
    try:
        with pytest.raises(APIConnectionError):
            await client.beta.agents.retrieve("agent_test")
    finally:
        await client.close()

    assert attempts == 3, "the first attempt and both retries reach the transport"
    # The SDK's own backoff would idle for at least 0.7 s before the third attempt.
    assert time.perf_counter() - started < 0.7
