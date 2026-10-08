"""The Skills API pacer must preserve the SDK's streaming connection pool."""

import httpx
import pytest
from anthropic import (
    DEFAULT_CONNECTION_LIMITS,
    AsyncAnthropic,
    DefaultAsyncHttpxClient,
    RateLimitError,
)
from daimon.core.skills.rate_limit import SkillsRateLimitedTransport
from daimon.testing.ma import HttpxToHttpx2Transport


async def test_skills_transport_keeps_sdk_connection_limits() -> None:
    transport = SkillsRateLimitedTransport(80)
    client = DefaultAsyncHttpxClient(transport=transport)
    try:
        pool = transport._inner._pool  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType]
        assert pool._max_connections == DEFAULT_CONNECTION_LIMITS.max_connections  # pyright: ignore[reportPrivateUsage]
        assert (  # pyright: ignore[reportPrivateUsage]
            pool._max_keepalive_connections == DEFAULT_CONNECTION_LIMITS.max_keepalive_connections
        )
        assert pool._keepalive_expiry == DEFAULT_CONNECTION_LIMITS.keepalive_expiry  # pyright: ignore[reportPrivateUsage]
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("body", "headers", "expected_attempts"),
    [
        (
            {
                "type": "error",
                "error": {
                    "type": "rate_limit_error",
                    "details": {"error_code": "enforced_spend_limit_reached"},
                },
            },
            {},
            1,
        ),
        ({"type": "error", "error": {"type": "rate_limit_error"}}, {"retry-after": "0"}, 2),
    ],
)
async def test_spend_cap_stops_sdk_retries_but_ordinary_429_retries(
    body: dict[str, object], headers: dict[str, str], expected_attempts: int
) -> None:
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json=body, headers=headers, request=request)

    transport = SkillsRateLimitedTransport(
        80, inner=HttpxToHttpx2Transport(httpx.MockTransport(respond))
    )
    async with AsyncAnthropic(
        api_key="test", max_retries=1, http_client=DefaultAsyncHttpxClient(transport=transport)
    ) as client:
        with pytest.raises(RateLimitError):
            await client.models.list()
    assert attempts == expected_attempts
