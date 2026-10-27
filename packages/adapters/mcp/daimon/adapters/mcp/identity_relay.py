"""Relay a session's requests to a signed-identity MCP server.

`<app root>/mcp-identity/<name>` accepts a session's own relay credential
(`daimon.core.mcp_vault.create_relay_vault`), signs who is calling
(`daimon.core.mcp_identity`) and forwards the streamable HTTP request to the
configured server with that token as its bearer. The response, an SSE stream
included, is passed back unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime

import httpx
import structlog
from daimon.adapters.mcp.auth.verifier import FOREIGN_HOME_CLAIM, TOKEN_KIND_CLAIM
from daimon.core.config import SignedIdentityServer
from daimon.core.mcp_identity import (
    RELAY_CLAIM,
    RELAY_PATH,
    IdentityTokenCache,
    RelayBinding,
    caller_claims,
    matching_turn,
    origin_turn_place,
    parse_relay_claim,
)
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.thread_sessions import get_thread_session_by_id
from daimon.core.stores.turn_origins import list_active_origins
from fastmcp.server.auth.auth import TokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

log = structlog.get_logger(__name__)

RELAY_ROUTE = RELAY_PATH + "/{name}"
RELAY_METHODS = ["GET", "POST", "DELETE"]
#: A JSON-RPC request body; anything larger is refused before it is read in full.
MAX_BODY_BYTES = 4 * 1024 * 1024
#: Gap allowed between bytes of a POST answer, a tool call's SSE stream included.
POST_READ_TIMEOUT = 300.0
#: Open connections per server, so one slow server cannot hold every socket.
MAX_CONNECTIONS = 20
_REQUEST_HEADERS = (
    "accept",
    "content-type",
    "last-event-id",
    "mcp-protocol-version",
    "mcp-session-id",
)
_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "mcp-protocol-version", "cache-control")


def build_relay_clients(servers: Iterable[SignedIdentityServer]) -> dict[str, httpx.AsyncClient]:
    """One bounded client per server; the caller closes them on shutdown."""
    return {
        server.name: httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=POST_READ_TIMEOUT),
            limits=httpx.Limits(
                max_connections=MAX_CONNECTIONS, max_keepalive_connections=MAX_CONNECTIONS // 2
            ),
        )
        for server in servers
    }


async def _capped_body(request: Request) -> bytes | None:
    """The request body, or None once it passes `MAX_BODY_BYTES`."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


async def _session_turn(
    sessionmaker: async_sessionmaker[AsyncSession],
    binding: RelayBinding,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    platform: str,
    now: datetime,
) -> TurnOriginRow | None:
    """The person's turn the calling session is running now, or None.

    The binding names the session's `thread_sessions` row. Only while that row is
    still the caller's live session in its thread does a turn there count: a
    replaced or stopped session calling on names nobody.
    """
    if binding.session_key is None:
        return None
    async with sessionmaker() as session:
        row = await get_thread_session_by_id(session, id=binding.session_key)
        if (
            row is None
            or row.status != "live"
            or (row.tenant_id, row.account_id, row.platform) != (tenant_id, account_id, platform)
        ):
            return None
        origins = await list_active_origins(
            session, tenant_id=tenant_id, account_id=account_id, platform=platform, now=now
        )
    return matching_turn(origins, tenant_id=tenant_id, agent_id=agent_id, thread_id=row.thread_id)


def build_identity_relay_route(
    *,
    servers: Mapping[str, SignedIdentityServer],
    auth: TokenVerifier,
    sessionmaker: async_sessionmaker[AsyncSession],
    tokens: IdentityTokenCache,
    clients: Mapping[str, httpx.AsyncClient],
    clock: Callable[[], datetime],
) -> Callable[[Request], Awaitable[Response]]:
    async def handler(request: Request) -> Response:
        server = servers.get(request.path_params["name"])
        if server is None:
            return Response(status_code=404)
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return Response(status_code=401)
        access = await auth.verify_token(header[len("bearer ") :])
        if access is None:
            return Response(status_code=401)
        claims = access.claims
        # Only a session's relay credential: it names the executing agent and the
        # session. Daimon's own chat credential, coding, operator and CLI keys do not.
        try:
            account_id = uuid.UUID(str(claims["sub"]))
            tenant_id = uuid.UUID(str(claims["tenant_id"]))
            agent_id = uuid.UUID(str(claims["chat_agent_id"]))
            binding = parse_relay_claim(claims[RELAY_CLAIM])
        except (KeyError, ValueError):
            return Response(status_code=403)
        if claims.get(TOKEN_KIND_CLAIM) != "agent":
            return Response(status_code=403)
        platform = str(claims.get("platform") or "")
        workspace = claims.get("external_id")
        workspace = workspace if isinstance(workspace, str) else None
        if server.workspaces is not None and f"{platform}:{workspace}" not in server.workspaces:
            return Response(status_code=403)
        person = claims.get("platform_user_id")
        body = await _capped_body(request)
        if body is None:
            return Response(status_code=413)
        now = clock()
        origin = await _session_turn(
            sessionmaker,
            binding,
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=agent_id,
            platform=platform,
            now=now,
        )
        # A sender from another organisation is not one of the workspace's own
        # people: the server must not apply a member's permissions to them.
        if origin is not None and (origin.sender_foreign or FOREIGN_HOME_CLAIM in claims):
            origin = None
        identity = caller_claims(
            issuer=server.issuer,
            audience=server.audience,
            agent_id=str(agent_id),
            platform=platform,
            workspace_id=workspace,
            platform_user_id=person if isinstance(person, str) else None,
            agent_name=origin.responder_name if origin is not None else binding.agent_name,
            place=origin_turn_place(origin) if origin is not None else None,
        )
        headers = {
            name: value
            for name in _REQUEST_HEADERS
            if (value := request.headers.get(name)) is not None
        }
        headers["authorization"] = f"Bearer {tokens.token(identity, now=now)}"
        http = clients[server.name]
        upstream = http.build_request(
            request.method,
            server.url,
            headers=headers,
            content=body,
            # A GET is the server's standing SSE stream, quiet between events.
            timeout=(
                httpx.Timeout(30.0, read=None)
                if request.method == "GET"
                else httpx.USE_CLIENT_DEFAULT
            ),
        )
        try:
            response = await http.send(upstream, stream=True)
        except httpx.HTTPError as err:
            log.warning(
                "mcp_identity.relay_failed", server=server.name, error_type=type(err).__name__
            )
            return Response(status_code=502)
        log.info(
            "mcp_identity.request_relayed",
            server=server.name,
            status=response.status_code,
            person=identity.sub is not None,
        )
        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers={
                name: value
                for name in _RESPONSE_HEADERS
                if (value := response.headers.get(name)) is not None
            },
            background=BackgroundTask(response.aclose),
        )

    return handler
