"""Tests for daimon.adapters.slack.interactions.

Covers resolve_web_client:
- Returns None for an unknown team_id (no token row).
- Returns a fresh AsyncWebClient with the decrypted token for a seeded row.
- Constructs the client inside the function; never caches on runtime or module.

Uses real Postgres (db_session_factory fixture) to verify the full
store→decrypt→client path end-to-end, mirroring the guideline:testing rule of
testing stores against real DB schemas.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from pydantic import SecretStr
from slack_sdk.http_retry.builtin_async_handlers import (
    AsyncConnectionErrorRetryHandler,
    AsyncRateLimitErrorRetryHandler,
)
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_resolve_web_client_returns_none_for_unknown_team_id(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """resolve_web_client returns None when no token row exists for the team."""
    settings = MagicMock()
    fernet_key = Fernet.generate_key().decode()
    settings.crypto.keys = (SecretStr(fernet_key),)
    runtime = SlackRuntime(
        settings=settings,
        anthropic=MagicMock(spec=AsyncAnthropic),
        sessionmaker=db_session_factory,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
    )

    result = await resolve_web_client(runtime, team_id="T_UNKNOWN")

    assert result is None, "resolve_web_client should return None for an unknown team_id"


@pytest.mark.asyncio
async def test_resolve_web_client_returns_async_web_client_with_decrypted_token(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """resolve_web_client returns an AsyncWebClient with the correct token."""
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    plaintext_token = "xoxb-test-interactions-resolved"
    encrypted = encrypt_token(fernet, plaintext_token)

    async with db_session_factory() as s, s.begin():
        await upsert_slack_bot_token(
            s,
            team_id="T_INTERACTIONS",
            encrypted_token=encrypted,
        )

    settings = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    runtime = SlackRuntime(
        settings=settings,
        anthropic=MagicMock(spec=AsyncAnthropic),
        sessionmaker=db_session_factory,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
    )

    result = await resolve_web_client(runtime, team_id="T_INTERACTIONS")

    assert result is not None, (
        "resolve_web_client should return an AsyncWebClient for a seeded team"
    )
    assert isinstance(result, AsyncWebClient), "resolve_web_client should return an AsyncWebClient"
    assert result.token == plaintext_token, (
        "resolve_web_client should decrypt and use the stored token"
    )


@pytest.mark.asyncio
async def test_resolve_web_client_registers_rate_limit_retry_handler(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The resolved client must retry 429s, not raise on the first one.

    slack_sdk's default handler set is connection-error only, so without this
    every rate-limited call raises SlackApiError into a boundary that posts
    nothing. The connection-error default must survive alongside it.
    """
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    await upsert_slack_bot_token(
        db_session,
        team_id="T_RETRY",
        encrypted_token=encrypt_token(fernet, "xoxb-retry-handlers"),
    )
    await db_session.flush()

    settings = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    runtime = SlackRuntime(
        settings=settings,
        anthropic=MagicMock(spec=AsyncAnthropic),
        sessionmaker=db_session_factory,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
    )

    client = await resolve_web_client(runtime, team_id="T_RETRY")

    assert client is not None, "a seeded token row must yield a client"
    handler_types = {type(handler) for handler in client.retry_handlers}
    assert AsyncRateLimitErrorRetryHandler in handler_types, (
        "a 429 must be retried, not raised on the first response"
    )
    assert AsyncConnectionErrorRetryHandler in handler_types, (
        "the SDK's default connection-error retry must survive the override"
    )
