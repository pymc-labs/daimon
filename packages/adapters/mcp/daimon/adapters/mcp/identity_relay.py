"""Relay a turn's requests to a signed-identity MCP server.

`<app root>/mcp-identity/<name>` accepts the turn's own Daimon chat credential,
signs who is calling (`daimon.core.mcp_identity`) and forwards the streamable
HTTP request to the configured server with that token as its bearer. The
response, an SSE stream included, is passed back unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime

import httpx
import structlog
from daimon.adapters.mcp.auth.verifier import TOKEN_KIND_CLAIM
from daimon.core.config import SignedIdentityServer
from daimon.core.mcp_identity import (
    RELAY_PATH,
    IdentityTokenCache,
    caller_claims,
    matching_turn,
    origin_turn_place,
)
from daimon.core.stores.turn_origins import list_active_origins
from fastmcp.server.auth.auth import TokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

log = structlog.get_logger(__name__)

RELAY_ROUTE = RELAY_PATH + "/{name}"
RELAY_METHODS = ["GET", "POST", "DELETE"]
_REQUEST_HEADERS = (
    "accept",
    "content-type",
    "last-event-id",
    "mcp-protocol-version",
    "mcp-session-id",
)
_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "mcp-protocol-version", "cache-control")


def build_identity_relay_route(
    *,
    servers: Mapping[str, SignedIdentityServer],
    auth: TokenVerifier,
    sessionmaker: async_sessionmaker[AsyncSession],
    tokens: IdentityTokenCache,
    http: httpx.AsyncClient,
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
        # Only a turn's chat credential: it names the executing agent. Coding,
        # operator and CLI keys run no turn, so they have no identity to sign.
        try:
            account_id = uuid.UUID(str(claims["sub"]))
            tenant_id = uuid.UUID(str(claims["tenant_id"]))
            agent_id = uuid.UUID(str(claims["chat_agent_id"]))
        except (KeyError, ValueError):
            return Response(status_code=403)
        if claims.get(TOKEN_KIND_CLAIM) != "agent":
            return Response(status_code=403)
        platform = str(claims.get("platform") or "")
        workspace = claims.get("external_id")
        person = claims.get("platform_user_id")
        now = clock()
        async with sessionmaker() as session:
            origins = await list_active_origins(
                session, tenant_id=tenant_id, account_id=account_id, platform=platform, now=now
            )
        origin = matching_turn(origins, tenant_id=tenant_id, agent_id=agent_id)
        identity = caller_claims(
            issuer=server.issuer,
            audience=server.audience,
            agent_id=str(agent_id),
            platform=platform,
            workspace_id=workspace if isinstance(workspace, str) else None,
            platform_user_id=person if isinstance(person, str) else None,
            agent_name=origin.responder_name if origin is not None else None,
            place=origin_turn_place(origin) if origin is not None else None,
        )
        headers = {
            name: value
            for name in _REQUEST_HEADERS
            if (value := request.headers.get(name)) is not None
        }
        headers["authorization"] = f"Bearer {tokens.token(identity, now=now)}"
        upstream = http.build_request(
            request.method, server.url, headers=headers, content=await request.body()
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
