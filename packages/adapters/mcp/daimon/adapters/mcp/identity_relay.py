"""Relay a session's requests to a signed-identity MCP server.

`<app root>/mcp-identity/<name>` accepts a session's own relay credential
(`daimon.core.mcp_vault.create_relay_vault`), signs who is calling
(`daimon.core.mcp_identity`) and forwards the streamable HTTP request to the
configured server with that token as its bearer. The response, an SSE stream
included, is passed back unchanged.
"""

from __future__ import annotations

import ssl
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime

import httpx
import structlog
from daimon.adapters.mcp.auth.verifier import FOREIGN_HOME_CLAIM, TOKEN_KIND_CLAIM
from daimon.core.config import SignedIdentityServer
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_identity import (
    RELAY_CLAIM,
    RELAY_PATH,
    REQUESTER_PLATFORMS,
    IdentityTokenCache,
    RelayBinding,
    RequesterBodyError,
    RequesterKind,
    caller_claims,
    matching_turn,
    origin_turn_place,
    parse_relay_claim,
    requester_conversation,
    routine_conversation,
    stamp_requester,
)
from daimon.core.stores.domain import RoutineRow, TurnOriginRow
from daimon.core.stores.routines import get_routine
from daimon.core.stores.thread_sessions import get_thread_session_by_id
from daimon.core.stores.turn_origins import list_active_origins
from fastmcp.server.auth.auth import TokenVerifier
from pydantic import SecretStr
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
MAX_CONNECTIONS = 100
#: Wait for a free connection; past it the caller is told to retry (503).
POOL_TIMEOUT = 5.0
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
    clients: dict[str, httpx.AsyncClient] = {}
    for server in servers:
        verify: bool | ssl.SSLContext = True
        if server.ca_certificate is not None:
            context = ssl.create_default_context()
            try:
                context.load_verify_locations(cadata=server.ca_certificate)
            except (ssl.SSLError, ValueError) as err:
                raise ValueError("relay CA certificate is invalid") from err
            verify = context
        clients[server.name] = httpx.AsyncClient(
            verify=verify,
            timeout=httpx.Timeout(30.0, read=POST_READ_TIMEOUT, pool=POOL_TIMEOUT),
            limits=httpx.Limits(
                max_connections=MAX_CONNECTIONS, max_keepalive_connections=MAX_CONNECTIONS // 2
            ),
        )
    return clients


async def read_capped_body(request: Request) -> bytes | None:
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


async def _routine_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    binding: RelayBinding,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> RoutineRow | None:
    """The routine a scheduled run's credential names, or None.

    Only a routine of this tenant that runs this agent (by id) and is still owned by
    the user the run started for counts; a routine since deleted, handed to someone
    else or aimed at another agent names nobody.
    """
    if binding.routine_id is None:
        return None
    async with sessionmaker() as session:
        row = await get_routine(session, binding.routine_id, tenant_id=tenant_id)
    if (
        row is None
        or not row.created_by_user_id
        or row.created_by_user_id != binding.owner
        or derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=row.agent_id) != agent_id
    ):
        return None
    return row


def build_identity_relay_route(
    *,
    servers: Mapping[str, SignedIdentityServer],
    auth: TokenVerifier,
    sessionmaker: async_sessionmaker[AsyncSession],
    tokens: IdentityTokenCache | None,
    clients: Mapping[str, httpx.AsyncClient],
    clock: Callable[[], datetime],
    bearers: Mapping[str, SecretStr] | None = None,
) -> Callable[[Request], Awaitable[Response]]:
    """The relay route. `tokens` signs for servers whose auth is signed; `bearers` holds
    the key of each server whose auth is bearer. A server with neither is refused (503)."""
    keys = bearers or {}

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
        # An allowlist names the agents that may use a server (a bearer server always has one).
        if server.agents and binding.agent_name not in server.agents:
            return Response(status_code=403)
        bearer = keys.get(server.name)
        if (server.auth == "bearer" and bearer is None) or (
            server.auth == "signed" and tokens is None
        ):
            log.error("mcp_identity.relay_unconfigured", server=server.name)
            return Response(status_code=503)
        person = claims.get("platform_user_id")
        body = await read_capped_body(request)
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
        headers = {
            name: value
            for name in _REQUEST_HEADERS
            if (value := request.headers.get(name)) is not None
        }
        if server.auth == "bearer":
            assert bearer is not None
            headers["authorization"] = f"Bearer {bearer.get_secret_value()}"
        else:
            assert tokens is not None
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
            headers["authorization"] = f"Bearer {tokens.token(identity, now=now)}"
        if request.method in ("GET", "DELETE"):
            # Neither carries a message; a body on one is dropped, not forwarded.
            body = b""
        if server.requester_meta and request.method == "POST":
            # A turn a person started names that person. An unattended run names who
            # it acts for, as kind "routine": a routine's owner and delivery place, or
            # the person and place of a wake (watch, timer, continuation). A foreign
            # sender, a replaced session and two turns at once match neither and
            # carry no requester fields.
            named: str | None = None
            conversation: dict[str, str | None] | None = None
            kind: RequesterKind | None = None
            if (
                origin is not None
                and platform in REQUESTER_PLATFORMS
                and isinstance(person, str)
                and person
            ):
                kind = "person" if origin.human_invocation is True else "routine"
                named = person
                conversation = requester_conversation(origin, unattended=kind == "routine")
            elif (
                binding.routine_id is not None
                and platform in REQUESTER_PLATFORMS
                and FOREIGN_HOME_CLAIM not in claims
            ):
                routine = await _routine_run(
                    sessionmaker, binding, tenant_id=tenant_id, agent_id=agent_id
                )
                if routine is not None:
                    named, conversation, kind = (
                        routine.created_by_user_id,
                        routine_conversation(routine),
                        "routine",
                    )
            try:
                body = stamp_requester(body, person=named, conversation=conversation, kind=kind)
            except RequesterBodyError:
                return Response(status_code=400)
        http = clients[server.name]
        upstream = http.build_request(
            request.method,
            server.url,
            headers=headers,
            content=body,
            # A GET is the server's standing SSE stream, quiet between events.
            timeout=(
                httpx.Timeout(30.0, read=None, pool=POOL_TIMEOUT)
                if request.method == "GET"
                else httpx.USE_CLIENT_DEFAULT
            ),
        )
        try:
            response = await http.send(upstream, stream=True)
        except httpx.PoolTimeout:
            log.warning("mcp_identity.relay_busy", server=server.name)
            return Response(status_code=503, headers={"retry-after": "5"})
        except httpx.HTTPError as err:
            log.warning(
                "mcp_identity.relay_failed", server=server.name, error_type=type(err).__name__
            )
            return Response(status_code=502)
        log.info(
            "mcp_identity.request_relayed",
            server=server.name,
            status=response.status_code,
            person=origin is not None,
            auth=server.auth,
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
