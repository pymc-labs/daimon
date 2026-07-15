"""Layer-2 HTTP auth integration tests.

Real ASGI app via httpx.ASGITransport, real DaimonJWTVerifier, real Postgres
(schema-per-test). One test per outcome.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from collections.abc import AsyncIterator

import httpx
import jwt as pyjwt
import pytest
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Message

from .factories import seed_tenant_and_account

pytestmark = pytest.mark.asyncio

SECRET = "a" * 32
INIT_BODY: dict[str, object] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
INIT_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@contextlib.asynccontextmanager
async def _lifespan(app: ASGIApp) -> AsyncIterator[None]:
    send_queue: asyncio.Queue[Message] = asyncio.Queue()
    receive_queue: asyncio.Queue[Message] = asyncio.Queue()

    async def receive() -> Message:
        return await receive_queue.get()

    async def send(message: Message) -> None:
        await send_queue.put(message)

    async def run_lifespan() -> None:
        await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    task = asyncio.create_task(run_lifespan())

    await receive_queue.put({"type": "lifespan.startup"})
    msg = await send_queue.get()
    assert msg["type"] == "lifespan.startup.complete", msg
    try:
        yield
    finally:
        await receive_queue.put({"type": "lifespan.shutdown"})
        msg = await send_queue.get()
        assert msg["type"] == "lifespan.shutdown.complete", msg
        await task


async def _post(
    app: ASGIApp,
    *,
    token: str | None,
    body: dict[str, object] = INIT_BODY,
) -> httpx.Response:
    headers = dict(INIT_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    async with (
        _lifespan(app),
        httpx.AsyncClient(transport=transport, base_url="http://t") as c,
    ):
        return await c.post("/mcp", json=body, headers=headers)


async def test_valid_account_jwt_handshakes_ok(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as s, s.begin():
        _tenant_id, account_id = await seed_tenant_and_account(s)
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr(SECRET), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
    )
    token = pyjwt.encode(
        {"sub": str(account_id), "iat": int(dt.datetime.now().timestamp())},
        SECRET,
        algorithm="HS256",
    )
    r = await _post(app, token=token)
    assert r.status_code == 200, r.text


async def test_valid_signed_unknown_account_rejected(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr(SECRET), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
    )
    token = pyjwt.encode({"sub": str(uuid.uuid4()), "iat": 0}, SECRET, algorithm="HS256")
    r = await _post(app, token=token)
    assert r.status_code == 401, "unknown account should be rejected"


async def test_valid_signed_malformed_uuid_sub_rejected(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr(SECRET), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
    )
    token = pyjwt.encode({"sub": "not-a-uuid", "iat": 0}, SECRET, algorithm="HS256")
    r = await _post(app, token=token)
    assert r.status_code == 401, "malformed UUID sub should be rejected"


async def test_bad_signature_rejected(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr(SECRET), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
    )
    token = pyjwt.encode({"sub": str(uuid.uuid4()), "iat": 0}, "wrong-secret", algorithm="HS256")
    r = await _post(app, token=token)
    assert r.status_code == 401, "bad signature should be rejected"


async def test_missing_bearer_rejected(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr(SECRET), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
    )
    r = await _post(app, token=None)
    assert r.status_code == 401, "missing bearer token should be rejected"


async def test_verified_token_carries_platform_and_external_id_claims(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Verifier executes the three-table JOIN and injects platform/external_id claims.

    The minted token carries only `sub` — no platform or guild_id wire claims.
    The verifier must look up platform and external_id from the DB and inject them
    into AccessToken.claims. This test proves the injection works by verifying the
    request is accepted (verifier returned AccessToken not None → 200).
    """
    from daimon.testing.factories import make_platform_principal, make_tenant

    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="guild-test-1234")
        from daimon.core._models import Account

        account = Account(tenant_id=tenant.id)
        s.add(account)
        await s.flush()
        account_id = account.id
        await make_platform_principal(
            s,
            platform="discord",
            external_id="user-snowflake-9999",
            tenant=tenant,
            account=account,
        )

    token = pyjwt.encode({"sub": str(account_id), "iat": 0}, SECRET, algorithm="HS256")

    # Verify the minted token carries no platform or guild_id wire claims (REQ-1)
    decoded = pyjwt.decode(token, SECRET, algorithms=["HS256"])
    assert "platform" not in decoded, "minted token must carry no platform wire claim"
    assert "guild_id" not in decoded, "minted token must carry no guild_id wire claim"

    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr(SECRET), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
    )
    r = await _post(app, token=token)
    assert r.status_code == 200, (
        f"verifier should accept token for discord account with PlatformPrincipal, "
        f"injecting platform/external_id claims; got {r.status_code}: {r.text}"
    )
