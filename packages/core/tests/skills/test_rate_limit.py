"""The Skills API pacer must preserve the SDK's streaming connection pool."""

from anthropic import DEFAULT_CONNECTION_LIMITS, DefaultAsyncHttpxClient
from daimon.core.skills.rate_limit import SkillsRateLimitedTransport


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
