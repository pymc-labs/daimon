"""Route a session's GitHub MCP requests to the right short-lived App token.

A session whose GitHub grants need more than one installation token points its
`github` MCP server at `<app root>/github/mcp`. The session vault holds an opaque
router bearer (`daimon.core.stores.github_router_sessions`). Each request is
forwarded to GitHub's hosted MCP server (`GITHUB_COPILOT_MCP_URL`) with the App
token that serves it (`daimon.core.github_router_tokens`):

- a `tools/call` with the token whose repos cover every repo or owner it names;
- every other method, GET (the standing SSE stream) and DELETE with the
  owner-free choice: the token covering the working repo, else the lowest
  (installation, chunk).

A call no token may serve, or a batch that would need two tokens, gets a JSON-RPC
error and no upstream request. The client's own Authorization never goes upstream,
and no body, bearer or token is logged.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, cast

import httpx
import structlog
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.identity_relay import (
    MAX_BODY_BYTES,
    MAX_CONNECTIONS,
    POOL_TIMEOUT,
    POST_READ_TIMEOUT,
    read_capped_body,
)
from daimon.core import github_router_tokens as router_tokens
from daimon.core.mcp_server_url import GITHUB_COPILOT_MCP_URL, GITHUB_ROUTER_PATH
from daimon.core.stores import github_router_sessions as router_sessions
from daimon.core.stores.security_audit import append_event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

log = structlog.get_logger(__name__)

GITHUB_ROUTER_ROUTE = GITHUB_ROUTER_PATH
GITHUB_ROUTER_METHODS = ["GET", "POST", "DELETE"]
__all__ = [
    "GITHUB_ROUTER_METHODS",
    "GITHUB_ROUTER_ROUTE",
    "MAX_BODY_BYTES",
    "AUDIT_TOOL_NAME",
    "BATCH_NOT_SENT",
    "CoreRouterBackend",
    "Refusal",
    "RoutedToken",
    "RouterBackend",
    "RouterCaller",
    "ToolCall",
    "build_github_router_client",
    "build_github_router_route",
]

#: `security_audit_events.tool_name` of a routed call; `operation` is the MCP tool.
AUDIT_TOOL_NAME = "github_mcp"
#: The answer to a batch's other requests when one of its calls is refused.
BATCH_NOT_SENT = (
    "This batch was not sent because another call in it was refused. "
    "Send one GitHub call at a time."
)
#: JSON-RPC error code of a refused call (implementation-defined server range).
REFUSED_CODE = -32001
_INVALID_PARAMS = -32602
_INVALID_REQUEST = -32600
_PARSE_ERROR = -32700
#: Waits at most this long for an audit row, so a slow database cannot stall a call.
AUDIT_TIMEOUT = 2.0
#: The audit reason of each way the resolver refuses a request.
_REFUSAL_REASONS: Mapping[type[router_tokens.TokenRefusal], str] = {
    router_tokens.NoAccess: "no_access",
    router_tokens.NeedsOneScope: "needs_one_scope",
    router_tokens.NotUsable: "not_usable",
    router_tokens.UnsupportedCall: "unsupported_call",
}
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:/-]{1,128}")

_REQUEST_HEADERS = (
    "accept",
    "content-type",
    "last-event-id",
    "mcp-protocol-version",
    "mcp-session-id",
)
#: The request headers GitHub's hosted server documents for tool selection
#: (github/github-mcp-server docs/remote-server.md, read 2026-11-06).
_GITHUB_HEADERS = (
    "x-mcp-toolsets",
    "x-mcp-tools",
    "x-mcp-readonly",
    "x-mcp-lockdown",
    "x-mcp-insiders",
)
_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "mcp-protocol-version", "cache-control")


@dataclass(frozen=True)
class RouterCaller:
    """A verified, live router session."""

    router_session_id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    account_id: uuid.UUID | None
    ma_session_id: str | None


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class RoutedToken:
    """The App token chosen for a request, with the row metadata the audit keeps."""

    token: str = field(repr=False)
    token_id: uuid.UUID
    installation_id: int
    repo_ids: tuple[int, ...]
    permissions: Mapping[str, str]
    expires_at: datetime


@dataclass(frozen=True)
class Refusal:
    """No token may serve the request; `message` goes to the agent unchanged."""

    message: str
    reason: str
    code: int = REFUSED_CODE


type Choice = RoutedToken | Refusal


class RouterBackend(Protocol):
    """The core lookups the route needs; `CoreRouterBackend` is the real one."""

    async def authenticate(self, bearer: str) -> RouterCaller | None:
        """The live router session the bearer names, its use recorded; else None."""
        ...

    async def choose(self, caller: RouterCaller, calls: Sequence[ToolCall]) -> Choice:
        """One token serving every call; no calls means the owner-free choice.

        Reads the session's token rows each time, never a cache, so a call after
        a rotation finds the new rows.
        """
        ...


class CoreRouterBackend:
    """`RouterBackend` over the router session store and the token resolver."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        fernet: MultiFernet,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessionmaker = sessionmaker
        self._fernet = fernet
        self._clock = clock

    async def authenticate(self, bearer: str) -> RouterCaller | None:
        now = self._clock()
        async with self._sessionmaker() as session, session.begin():
            record = await router_sessions.verify(session, bearer=bearer, now=now)
            if record is None or not await router_sessions.is_session_live(
                session, record, now=now
            ):
                return None
            await router_sessions.touch_last_used(session, router_session_id=record.id, now=now)
        return RouterCaller(
            router_session_id=record.id,
            tenant_id=record.tenant_id,
            agent_id=record.agent_id,
            account_id=record.account_id,
            ma_session_id=record.ma_session_id,
        )

    async def choose(self, caller: RouterCaller, calls: Sequence[ToolCall]) -> Choice:
        now = self._clock()
        async with self._sessionmaker() as session:
            rows = await router_tokens.load_usable_rows(
                session, caller.router_session_id, fernet=self._fernet, now=now
            )
            grants = await router_tokens.load_grant_view(
                session, tenant_id=caller.tenant_id, agent_id=caller.agent_id
            )
        scopes = [router_tokens.resolve_call(call.name, call.arguments) for call in calls]
        decision = router_tokens.choose_batch_token(
            scopes or [router_tokens.OwnerFree()],
            rows,
            grants,
            grants.working_repo_id,
            now=now,
        )
        if isinstance(decision, router_tokens.TokenChoice):
            row = decision.token
            return RoutedToken(
                token=row.token,
                token_id=row.token_id,
                installation_id=row.installation_id,
                repo_ids=tuple(sorted(row.repo_ids)),
                permissions=row.permissions,
                expires_at=row.expires_at,
            )
        return Refusal(decision.message, _REFUSAL_REASONS[type(decision)])


def build_github_router_client() -> httpx.AsyncClient:
    """The bounded client for GitHub's hosted server; the caller closes it."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=POST_READ_TIMEOUT, pool=POOL_TIMEOUT),
        limits=httpx.Limits(
            max_connections=MAX_CONNECTIONS, max_keepalive_connections=MAX_CONNECTIONS // 2
        ),
    )


# --- JSON-RPC body --------------------------------------------------------------


@dataclass(frozen=True)
class _Message:
    """One JSON-RPC message of a POST body."""

    raw: Mapping[str, object]
    call: ToolCall | Refusal | None
    """The `tools/call` it carries, a refusal when its params are malformed, else None."""

    @property
    def answers(self) -> bool:
        """A request (method and id): the client waits for an answer to it."""
        return "method" in self.raw and "id" in self.raw

    @property
    def id(self) -> object:
        return self.raw.get("id")


def _tool_call(message: Mapping[str, object]) -> ToolCall | Refusal | None:
    if message.get("method") != "tools/call":
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return Refusal("Invalid tools/call params.", "malformed_call", _INVALID_PARAMS)
    name = cast(dict[str, object], params).get("name")
    arguments = cast(dict[str, object], params).get("arguments", {})
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return Refusal("Invalid tools/call params.", "malformed_call", _INVALID_PARAMS)
    return ToolCall(name, cast(dict[str, object], arguments))


def _parse(body: bytes) -> tuple[list[_Message], bool] | Refusal:
    """The body's messages and whether it was a batch, or why it is not JSON-RPC."""
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return Refusal("Parse error.", "malformed_body", _PARSE_ERROR)
    batch = isinstance(decoded, list)
    items = cast(list[object], decoded) if batch else [decoded]
    if not items or not all(isinstance(item, dict) for item in items):
        return Refusal("Invalid request.", "malformed_body", _INVALID_REQUEST)
    messages = [cast(dict[str, object], item) for item in items]
    return [_Message(item, _tool_call(item)) for item in messages], batch


def _error(message_id: object, refusal: Refusal) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": refusal.code, "message": refusal.message},
    }


# --- token choice ---------------------------------------------------------------


@dataclass(frozen=True)
class _Decision:
    """A token for the whole request, or a refusal for each message by index."""

    token: RoutedToken | None
    refusals: Mapping[int, Refusal]


async def _decide(
    backend: RouterBackend, caller: RouterCaller, messages: Sequence[_Message]
) -> _Decision:
    calls = {i: m.call for i, m in enumerate(messages) if m.call is not None}
    if not calls:
        return _whole(messages, await backend.choose(caller, ()))
    # Each call alone first, so a refused call is named with its own reason.
    singles: dict[int, Choice] = {}
    for index, call in calls.items():
        singles[index] = call if isinstance(call, Refusal) else await backend.choose(caller, [call])
    refused = {i: c for i, c in singles.items() if isinstance(c, Refusal)}
    if refused:
        batch_refusal = Refusal(BATCH_NOT_SENT, "batch_refused")
        return _Decision(
            None,
            {i: refused.get(i, batch_refusal) for i, m in enumerate(messages) if m.answers},
        )
    if len(singles) == 1:
        (only,) = singles.values()
        return _whole(messages, only)
    tool_calls = [c for c in calls.values() if isinstance(c, ToolCall)]
    return _whole(messages, await backend.choose(caller, tool_calls))


def _whole(messages: Sequence[_Message], choice: Choice) -> _Decision:
    if isinstance(choice, RoutedToken):
        return _Decision(choice, {})
    return _Decision(None, {i: choice for i, m in enumerate(messages) if m.answers})


# --- audit ----------------------------------------------------------------------


async def _audit(
    sessionmaker: async_sessionmaker[AsyncSession],
    caller: RouterCaller,
    messages: Sequence[_Message],
    decision: _Decision,
    *,
    reason: str,
) -> None:
    """One row per `tools/call`: allowed with the token's metadata, or denied."""
    for index, message in enumerate(messages):
        call = message.call
        if call is None:
            continue
        name = call.name if isinstance(call, ToolCall) else None
        refusal = decision.refusals.get(index)
        token = decision.token if refusal is None else None
        await _write_audit_row(
            sessionmaker,
            caller,
            operation=name if name is not None and _TOOL_NAME.fullmatch(name) else "<invalid>",
            outcome="allowed" if token is not None else "denied",
            reason=reason if token is not None else (refusal.reason if refusal else "refused"),
            token=token,
        )


async def _write_audit_row(
    sessionmaker: async_sessionmaker[AsyncSession],
    caller: RouterCaller,
    *,
    operation: str,
    outcome: Literal["allowed", "denied"],
    reason: str,
    token: RoutedToken | None,
) -> None:
    try:
        async with asyncio.timeout(AUDIT_TIMEOUT), sessionmaker() as session, session.begin():
            await append_event(
                session,
                tenant_id=caller.tenant_id,
                account_id=caller.account_id,
                agent_id=caller.agent_id,
                platform=None,
                platform_user_id=None,
                tool_name=AUDIT_TOOL_NAME,
                operation=operation,
                outcome=outcome,
                reason=reason,
                github_session_id=caller.ma_session_id,
                github_token_id=token.token_id if token else None,
                github_installation_id=token.installation_id if token else None,
                github_repo_ids=list(token.repo_ids) if token else None,
                github_permissions=dict(token.permissions) if token else None,
                github_expires_at=token.expires_at if token else None,
            )
    except Exception as exc:
        log.warning(
            "github_router.audit_failed",
            tenant_id=str(caller.tenant_id),
            error_type=type(exc).__name__,
        )


# --- forwarding -----------------------------------------------------------------


def _upstream_headers(request: Request, token: str) -> dict[str, str]:
    headers = {
        name: value
        for name in (*_REQUEST_HEADERS, *_GITHUB_HEADERS)
        if (value := request.headers.get(name)) is not None
    }
    headers["authorization"] = f"Bearer {token}"
    return headers


async def _send(
    http: httpx.AsyncClient, request: Request, body: bytes, token: str
) -> httpx.Response:
    upstream = http.build_request(
        request.method,
        GITHUB_COPILOT_MCP_URL,
        headers=_upstream_headers(request, token),
        content=body,
        # A GET is the server's standing SSE stream, quiet between events.
        timeout=(
            httpx.Timeout(30.0, read=None, pool=POOL_TIMEOUT)
            if request.method == "GET"
            else httpx.USE_CLIENT_DEFAULT
        ),
    )
    return await http.send(upstream, stream=True)


def _refused_response(messages: Sequence[_Message], batch: bool, decision: _Decision) -> Response:
    errors = [
        _error(message.id, decision.refusals[index])
        for index, message in enumerate(messages)
        if index in decision.refusals
    ]
    if not errors:
        # Only notifications or responses, and no token to send them with.
        return Response(status_code=503, headers={"retry-after": "30"})
    return JSONResponse(errors if batch else errors[0])


def _bearer(request: Request) -> str | None:
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    credential = credential.strip()
    return credential if scheme.lower() == "bearer" and credential else None


def build_github_router_route(
    *,
    backend: RouterBackend,
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
) -> Callable[[Request], Awaitable[Response]]:
    async def handler(request: Request) -> Response:
        bearer = _bearer(request)
        caller = await backend.authenticate(bearer) if bearer is not None else None
        if caller is None:
            return Response(status_code=401)
        body = await read_capped_body(request)
        if body is None:
            return Response(status_code=413)
        if request.method == "POST":
            parsed = _parse(body)
            if isinstance(parsed, Refusal):
                return JSONResponse(_error(None, parsed), status_code=400)
            messages, batch = parsed
        else:
            messages, batch = [], False

        async def decide() -> _Decision | Response:
            decision = await _decide(backend, caller, messages)
            if decision.token is not None:
                return decision
            await _audit(sessionmaker, caller, messages, decision, reason="refused")
            if request.method != "POST":
                return Response(status_code=503, headers={"retry-after": "30"})
            return _refused_response(messages, batch, decision)

        decision = await decide()
        if isinstance(decision, Response):
            return decision
        assert decision.token is not None
        try:
            response = await _send(client, request, body, decision.token.token)
            if response.status_code == 401:
                # A rotation may have replaced the row since it was read: read the
                # rows again and retry once.
                await response.aclose()
                retried = await decide()
                if isinstance(retried, Response):
                    return retried
                decision = retried
                assert decision.token is not None
                response = await _send(client, request, body, decision.token.token)
        except httpx.PoolTimeout:
            log.warning("github_router.busy")
            await _audit(sessionmaker, caller, messages, decision, reason="upstream_busy")
            return Response(status_code=503, headers={"retry-after": "5"})
        except httpx.HTTPError as err:
            log.warning("github_router.upstream_failed", error_type=type(err).__name__)
            await _audit(sessionmaker, caller, messages, decision, reason="upstream_failed")
            return Response(status_code=502)
        token = decision.token
        assert token is not None
        log.info(
            "github_router.request_routed",
            method=request.method,
            status=response.status_code,
            installation_id=token.installation_id,
            calls=sum(1 for m in messages if m.call is not None),
        )
        if response.status_code == 401:
            # The retry was refused too: our caller's credential is fine, so this is
            # an upstream failure, not a reason for the client to drop its bearer.
            await response.aclose()
            await _audit(sessionmaker, caller, messages, decision, reason="upstream_unauthorized")
            return Response(status_code=502)
        await _audit(sessionmaker, caller, messages, decision, reason="routed")
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
