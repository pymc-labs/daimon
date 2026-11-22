"""Relay a session's requests to a signed-identity MCP server.

`<app root>/mcp-identity/<name>` accepts a session's own relay credential
(`daimon.core.mcp_vault.create_relay_vault`), signs who is calling
(`daimon.core.mcp_identity`) and forwards the streamable HTTP request to the
configured server with that token as its bearer. The response, an SSE stream
included, is passed back unchanged. A person's call to one of a server's `wake_tools`
also carries a callback watch link (`_wake_link`) the server posts to when the work ends.
"""

from __future__ import annotations

import ssl
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import cast

import httpx
import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.verifier import FOREIGN_HOME_CLAIM, TOKEN_KIND_CLAIM
from daimon.adapters.mcp.tools.watches import callback_watch_terms
from daimon.core.audience_gate import read_audience_mode
from daimon.core.channel_audience import channel_has_outsiders
from daimon.core.config import SignedIdentityServer, load_watches_settings
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.defaults.builtin_agent import find_agent_or_builtin
from daimon.core.defaults.metadata import BUILTIN_AGENT_NAMES, MA_METADATA_KEY_MANAGED
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_hold import HOLD_CLAIM, parse_hold_claim
from daimon.core.mcp_identity import (
    RELAY_CLAIM,
    RELAY_PATH,
    REQUESTER_PLATFORMS,
    IdentityTokenCache,
    RelayBinding,
    RequesterBodyError,
    RequesterKind,
    called_tool,
    caller_claims,
    matching_turn,
    origin_turn_place,
    parse_relay_claim,
    read_request_body,
    relay_url,
    requester_conversation,
    routine_conversation,
    stamp_parsed,
    watch_conversation,
)
from daimon.core.mcp_server_url import same_ascii_mcp_url
from daimon.core.stores import watches
from daimon.core.stores.agent_avatars import normalize_agent_name
from daimon.core.stores.domain import ChatPlatform, RoutineRow, TurnOriginRow, WatchRow
from daimon.core.stores.routines import get_routine
from daimon.core.stores.thread_sessions import get_thread_session_by_id
from daimon.core.stores.turn_origins import get_session_origin, list_active_origins
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
#: A wake link's watch lasts this long after the latest call that asked for one, never
#: longer than `callback_watch_lifetime` allows: one live per conversation and server,
#: so the owner's watch limit is never filled by old jobs.
WAKE_LIFETIME = timedelta(days=1)


async def _wake_link(
    sessionmaker: async_sessionmaker[AsyncSession],
    origin: TurnOriginRow,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    person: str,
    server: str,
    now: datetime,
) -> str | None:
    """A fresh link to the conversation's wake watch for `server` (`watches.wake_link`);
    None when watches or callbacks are off here, or the watch cannot be made (a helper's
    turn, a Slack DM, the owner's limit). It skips the approval card, as
    `test_a_wake_link_is_made_without_the_approval_card` records. The link is a secret:
    it goes only into the forwarded call, never into a log."""
    settings = load_watches_settings()
    if not settings.enabled or settings.callback_base_url is None:
        return None
    binding = ContinuationRequest(
        tenant_id=tenant_id,
        platform=cast(ChatPlatform, origin.platform),
        parent_channel_id=origin.parent_channel_id,
        thread_id=origin.thread_id,
        requester_account_id=account_id,
        requester_external_user_id=person,
        target_ma_agent_id=origin.responder_ma_agent_id,
        target_name=origin.responder_name,
        requested_work=None,
        reason="watch_fired",
        idempotency_key=uuid.uuid4(),
    )
    try:
        allowed = await callback_watch_terms(sessionmaker, None, origin)
        async with sessionmaker.begin() as db:
            watch_id, token = await watches.wake_link(
                db,
                binding,
                server=server,
                lifetime=WAKE_LIFETIME if allowed is None else min(allowed, WAKE_LIFETIME),
                now=now,
            )
    except Exception as err:  # boundary: the call still runs, only without a wake
        log.info("mcp_identity.wake_link_unavailable", server=server, error_type=type(err).__name__)
        return None
    return f"{settings.callback_base_url}/callbacks/watches/{watch_id}/{token}"


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

    The binding names the session's `thread_sessions` row. With the session's
    execution grant, the turn is the one holding it now (`bind_origin_session`),
    so each call names the turn that made it, beside a queued turn or after the
    row was replaced. Without one (a session with no grant) a turn counts only
    while that row is the caller's live session and runs one turn in its thread.
    """
    if binding.session_key is None:
        return None
    async with sessionmaker() as session:
        row = await get_thread_session_by_id(session, id=binding.session_key)
        if row is None or (row.tenant_id, row.account_id, row.platform) != (
            tenant_id,
            account_id,
            platform,
        ):
            return None
        if binding.grant is not None:
            held = await get_session_origin(
                session,
                tenant_id=tenant_id,
                account_id=account_id,
                platform=platform,
                chat_session_id=binding.grant,
                now=now,
            )
            origins = [] if held is None else [held]
        elif row.status != "live":
            return None
        else:
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


async def _watch_poll(
    sessionmaker: async_sessionmaker[AsyncSession],
    binding: RelayBinding,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    server: SignedIdentityServer,
    public_url: str | None,
) -> WatchRow | None:
    """The active watch a poll's credential names, or None.

    Only a watch of this tenant and agent, still owned by the account and user the
    credential was minted for and still polling this relay server, counts; a paused,
    deleted or handed-over watch names nobody.
    """
    if binding.watch_id is None or public_url is None:
        return None
    async with sessionmaker() as session:
        row = await watches.get_watch(session, binding.watch_id)
    if (
        row is None
        or row.status != "active"
        or row.source != "mcp_poll"
        or (row.tenant_id, row.owner_account_id) != (tenant_id, account_id)
        or row.owner_external_id != binding.owner
        or derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=row.agent_id) != agent_id
        or row.source_state.get("credential") != "relay"
        or not same_ascii_mcp_url(
            str(row.source_state.get("server_url")), relay_url(public_url, server.name)
        )
    ):
        return None
    return row


def _is_poll_message(message: object, tool: str) -> bool:
    """Whether a relayed body is part of a watch poll: the handshake, a tool listing or
    one call of the watch's own tool. A watch's credential is for nothing else."""
    if not isinstance(message, dict):
        return False
    fields = cast("dict[str, object]", message)
    method, params = fields.get("method"), fields.get("params")
    if method in ("initialize", "notifications/initialized", "tools/list"):
        return True
    return (
        method == "tools/call"
        and isinstance(params, dict)
        and cast("dict[str, object]", params).get("name") == tool
    )


async def _listed_as_builtin(
    anthropic: AsyncAnthropic | None,
    sessionmaker: async_sessionmaker[AsyncSession],
    server: SignedIdentityServer,
    binding: RelayBinding,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> bool:
    """Whether the seeded agent signs one built-in name and the allowlist the other.

    Its rename changes the name a credential carries; a workspace's own agent
    of either name never passes this way.
    """
    if normalize_agent_name(binding.agent_name) not in BUILTIN_AGENT_NAMES or not any(
        normalize_agent_name(name) in BUILTIN_AGENT_NAMES for name in server.agents
    ):
        return False
    try:
        agent = await find_agent_or_builtin(
            anthropic,
            tenant_id=tenant_id,
            name=binding.agent_name,
            records=sessionmaker,
            surface="identity_relay",
        )
    except Exception as exc:  # boundary: an unverified agent is refused
        log.warning("mcp_identity.relay_builtin_lookup_failed", error_type=type(exc).__name__)
        return False
    return (
        agent is not None
        and agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
        and derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id) == agent_id
    )


async def _held_in_audience_mode_on(
    sessionmaker: async_sessionmaker[AsyncSession],
    raw_hold: object,
    *,
    tenant_id: uuid.UUID,
    platform: str,
) -> bool:
    """Whether a held token is one of a room outsiders may read, or nobody can vouch for, in
    mode `on`. Such a session mounts no signed-identity server there, so the token is a
    leftover. A relay-only hold of an internal room (`relay=True`) keeps working: the room's
    facts are read again here. An unreadable hold, mode or fact counts as yes: fail closed."""
    try:
        hold = parse_hold_claim(raw_hold)
        async with sessionmaker() as session:
            mode = await read_audience_mode(
                sessionmaker,
                tenant_id=tenant_id,
                platform=hold.platform,
                channel_id=hold.channel_id,
                session=session,
            )
            if mode != "on":
                return False
            audience = await channel_has_outsiders(
                session, tenant_id=tenant_id, platform=hold.platform, channel_id=hold.channel_id
            )
    except Exception:
        log.warning("mcp_identity.relay_hold_unreadable", exc_info=True)
        return True
    return audience.contained


def _refused(
    status: int,
    reason: str,
    *,
    server: SignedIdentityServer | None = None,
    tenant_id: uuid.UUID | None = None,
) -> Response:
    """A refusal that used to be silent, with a fixed reason. The tenant is named only once
    the credential resolved it, and no server name is logged for one the relay does not have."""
    log.warning(
        "mcp_identity.relay_refused",
        status=status,
        reason=reason,
        server=server.name if server is not None else None,
        tenant_id=str(tenant_id) if tenant_id is not None else None,
    )
    return Response(status_code=status)


def _tool_name(request: Request, message: object, body: bytes) -> str | None:
    """The tool a relayed `tools/call` names, clipped; never its arguments."""
    if message is None and request.method == "POST":
        try:
            message = read_request_body(body)
        except RequesterBodyError:
            return None
    name = called_tool(message)
    return name[:64] if name is not None else None


def build_identity_relay_route(
    *,
    servers: Mapping[str, SignedIdentityServer],
    auth: TokenVerifier,
    sessionmaker: async_sessionmaker[AsyncSession],
    tokens: IdentityTokenCache | None,
    clients: Mapping[str, httpx.AsyncClient],
    clock: Callable[[], datetime],
    bearers: Mapping[str, SecretStr] | None = None,
    anthropic: AsyncAnthropic | None = None,
    public_url: str | None = None,
) -> Callable[[Request], Awaitable[Response]]:
    """The relay route. `tokens` signs for servers whose auth is signed; `bearers` holds
    the key of each server whose auth is bearer. A server with neither is refused (503)."""
    keys = bearers or {}

    async def handler(request: Request) -> Response:
        server = servers.get(request.path_params["name"])
        if server is None:
            return _refused(404, "unknown_server")
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return _refused(401, "no_bearer", server=server)
        access = await auth.verify_token(header[len("bearer ") :])
        if access is None:
            return _refused(401, "token_rejected", server=server)
        claims = access.claims
        # Only a session's relay credential: it names the executing agent and the
        # session. Daimon's own chat credential, coding, operator and CLI keys do not.
        try:
            account_id = uuid.UUID(str(claims["sub"]))
            tenant_id = uuid.UUID(str(claims["tenant_id"]))
            agent_id = uuid.UUID(str(claims["chat_agent_id"]))
            binding = parse_relay_claim(claims[RELAY_CLAIM])
        except (KeyError, ValueError):
            return _refused(403, "not_session_credential", server=server)
        if claims.get(TOKEN_KIND_CLAIM) != "agent":
            return _refused(403, "not_agent_token", server=server, tenant_id=tenant_id)
        platform = str(claims.get("platform") or "")
        if HOLD_CLAIM in claims and await _held_in_audience_mode_on(
            sessionmaker, claims[HOLD_CLAIM], tenant_id=tenant_id, platform=platform
        ):
            # A held session is in a room outsiders may read, or nobody can vouch for: in
            # `on` it mounts no signed-identity server, so this token is a leftover. Refused.
            return _refused(403, "held_session", server=server, tenant_id=tenant_id)
        workspace = claims.get("external_id")
        workspace = workspace if isinstance(workspace, str) else None
        if server.workspaces is not None and f"{platform}:{workspace}" not in server.workspaces:
            return _refused(403, "workspace_not_allowed", server=server, tenant_id=tenant_id)
        # An allowlist names the agents that may use a server (a bearer server always has one).
        if (
            server.agents
            and binding.agent_name not in server.agents
            and not await _listed_as_builtin(
                anthropic, sessionmaker, server, binding, tenant_id=tenant_id, agent_id=agent_id
            )
        ):
            return _refused(403, "agent_not_allowed", server=server, tenant_id=tenant_id)
        bearer = keys.get(server.name)
        if (server.auth == "bearer" and bearer is None) or (
            server.auth == "signed" and tokens is None
        ):
            log.error(
                "mcp_identity.relay_unconfigured", server=server.name, tenant_id=str(tenant_id)
            )
            return Response(status_code=503)
        person = claims.get("platform_user_id")
        body = await read_capped_body(request)
        if body is None:
            return _refused(413, "body_too_large", server=server, tenant_id=tenant_id)
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
        watch: WatchRow | None = None
        if binding.watch_id is not None:
            # A poll's credential is for its watch alone; a gone or handed-over watch
            # names no tool to allow, so it reaches nothing.
            watch = await _watch_poll(
                sessionmaker,
                binding,
                tenant_id=tenant_id,
                account_id=account_id,
                agent_id=agent_id,
                server=server,
                public_url=public_url,
            )
            if watch is None or request.method != "POST":
                return _refused(403, "watch_poll_refused", server=server, tenant_id=tenant_id)
        message: object = None
        if watch is not None or (server.requester_meta and request.method == "POST"):
            try:
                message = read_request_body(body)
            except RequesterBodyError:
                return _refused(400, "bad_body", server=server, tenant_id=tenant_id)
        if watch is not None and not _is_poll_message(message, str(watch.source_state.get("tool"))):
            return _refused(403, "watch_poll_tool_refused", server=server, tenant_id=tenant_id)
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
            # it acts for, as kind "routine": a routine's owner and delivery place, a
            # watch poll's owner and report place, or the person and place of a wake
            # (watch, timer, continuation). A foreign sender, or a session whose calling
            # turn is unknown (`_session_turn`), matches none and carries no requester
            # fields.
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
            elif (
                watch is not None
                and platform in REQUESTER_PLATFORMS
                and FOREIGN_HOME_CLAIM not in claims
            ):
                named, conversation, kind = (
                    watch.owner_external_id,
                    watch_conversation(watch),
                    "routine",
                )
            wake_url: str | None = None
            # Only a person's call that names them and their place gets a link.
            if (
                kind == "person"
                and origin is not None
                and named is not None
                and conversation
                and called_tool(message) in server.wake_tools
            ):
                wake_url = await _wake_link(
                    sessionmaker,
                    origin,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    person=named,
                    server=server.name,
                    now=now,
                )
            try:
                body = stamp_parsed(
                    message, person=named, conversation=conversation, kind=kind, wake_url=wake_url
                )
            except RequesterBodyError:
                return _refused(400, "bad_body", server=server, tenant_id=tenant_id)
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
            log.warning(
                "mcp_identity.relay_busy",
                server=server.name,
                tenant_id=str(tenant_id),
                tool=_tool_name(request, message, body),
            )
            return Response(status_code=503, headers={"retry-after": "5"})
        except httpx.HTTPError as err:
            log.warning(
                "mcp_identity.relay_failed",
                server=server.name,
                tenant_id=str(tenant_id),
                tool=_tool_name(request, message, body),
                error_type=type(err).__name__,
            )
            return Response(status_code=502)
        # An upstream 5xx is a failed relay for alerting; the call still reaches the caller.
        (log.warning if response.status_code >= 500 else log.info)(
            "mcp_identity.relay_failed"
            if response.status_code >= 500
            else "mcp_identity.request_relayed",
            server=server.name,
            tenant_id=str(tenant_id),
            tool=_tool_name(request, message, body),
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
