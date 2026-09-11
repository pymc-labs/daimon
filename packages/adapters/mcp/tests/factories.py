"""MCP-adapter-specific test factories.

Domain-row factories (agents, environments, accounts) live in
packages/core/tests/factories.py; don't duplicate them.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator

import httpx
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.core.mcp_auth import mint_jwt
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Message


def make_jwt(
    *,
    account_id: uuid.UUID,
    secret: bytes = b"a" * 32,
    now: dt.datetime | None = None,
    is_admin: bool = False,
) -> str:
    return mint_jwt(
        account_id=account_id,
        secret=secret,
        now=now or dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
        is_admin=is_admin,
    )


def make_identity(
    *,
    account_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    role: Role = Role.ADMIN,
) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id or uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        role=role,
    )


async def seed_tenant(session: AsyncSession, *, workspace_id: str | None = None) -> uuid.UUID:
    """Insert a Tenant row and return its id."""
    tenant = await make_tenant(
        session, platform="discord", workspace_id=workspace_id or str(uuid.uuid4())
    )
    return tenant.id


async def seed_tenant_and_account(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a Tenant + Account and return (tenant_id, account_id)."""
    tenant = await make_tenant(session, platform="discord")
    account = await make_account(session, tenant=tenant)
    return tenant.id, account.id


def make_ma_agent(**overrides: object) -> BetaManagedAgentsAgent:
    base: dict[str, object] = {
        "id": "ag_new",
        "type": "agent",
        "version": 1,
        "name": "demo",
        "model": {"id": "claude-opus-4-5"},
        "description": None,
        "system": None,
        "tools": [],
        "mcp_servers": [],
        "skills": [],
        "created_at": "2026-04-24T00:00:00Z",
        "updated_at": "2026-04-24T00:00:00Z",
        "metadata": {},
    }
    base.update(overrides)
    return BetaManagedAgentsAgent.model_validate(base)


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


async def mcp_session(
    app: ASGIApp,
    *,
    token: str,
    method: str,
    params: dict[str, object] | None = None,
) -> dict[str, object]:
    """Initialize MCP session then execute a JSON-RPC method.

    Returns the JSON-RPC result dict from the method response.
    Raises AssertionError on unexpected HTTP status.
    """
    headers = dict(INIT_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    async with _lifespan(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # Step 1: initialize handshake
        init_resp = await c.post("/mcp", json=INIT_BODY, headers=headers)
        assert init_resp.status_code == 200, f"initialize failed: {init_resp.text}"
        session_id = init_resp.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        # Step 2: actual method call
        body = {"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}}
        resp = await c.post("/mcp", json=body, headers=headers)
        assert resp.status_code == 200, f"{method} failed ({resp.status_code}): {resp.text}"
        return _parse_jsonrpc_response(resp)


def _parse_jsonrpc_response(resp: httpx.Response) -> dict[str, object]:
    """Parse a JSON-RPC response from either JSON or SSE format."""
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        # SSE: parse the data line
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])  # type: ignore[return-value]
        raise AssertionError(f"No data line in SSE response: {resp.text!r}")
    else:
        return resp.json()  # type: ignore[return-value]
