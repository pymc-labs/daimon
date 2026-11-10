"""`/connectors/{server_key}`: a provider turn's MCP connectors, called by Daimon.

The provider sends the turn's own Daimon credential; Daimon resolves the key in
that turn's tenant and agent, gates each call (`daimon.core.connector_relay`)
and calls the connector with a token the provider never sees. Only `initialize`,
`tools/list` and `tools/call` are served; nothing is streamed back.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import structlog
from daimon.adapters.mcp.auth.verifier import CHAT_EXECUTION_CLAIM, TOKEN_JTI_CLAIM
from daimon.adapters.mcp.identity_relay import read_capped_body
from daimon.adapters.mcp.middleware.host_call_gate import schema_problem
from daimon.core import host_calls
from daimon.core.connector_relay import (
    CACHE_TTL_S,
    RELAY_PATH,
    Connector,
    ToolsCache,
    relay_verdict,
)
from daimon.core.features.snapshot import deployment_on
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_hold import HOLD_CLAIM, held_connectors
from daimon.core.mcp_tool_call import (
    INTERACTIVE_LIMITS,
    PROTOCOL_VERSION,
    McpCallError,
    McpToolSession,
    mcp_tool_session,
    strict_json,
)
from daimon.core.stores import host_call_cards as cards
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.mcp_tokens import revoke_mcp_token
from daimon.core.stores.turn_origins import get_session_origin
from daimon.core.switch_cards import SWITCH_CARD_MAX_S
from daimon.core.tool_safety import ToolCall
from fastmcp.server.auth.auth import TokenVerifier
from pydantic import JsonValue
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

if TYPE_CHECKING:
    from cryptography.fernet import MultiFernet
    from daimon.adapters.mcp.runtime import McpRuntime
    from daimon.core.config import Settings
    from daimon.core.mcp_oauth.connector_tokens import HttpFactory
    from daimon.core.stores.domain import TurnOriginRow
    from daimon.core.stores.host_call_cards import HostCallCardRow
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

CONNECTOR_ROUTE: Final = RELAY_PATH + "/{server_key}"
FEATURE: Final = "mcp_connector_relay"
RECONNECT_TEXT: Final = "This connector needs signing in again: the person can press Reconnect."
REFUSED_TEXT: Final = "This connector tool can't run here."
FAILED_TEXT: Final = "The connector didn't answer. Try again later."
_OUTCOME_MAX: Final = 2000


@dataclass(frozen=True, slots=True)
class RelayTurn:
    """The provider turn a relay request belongs to, read from its verified credential."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID
    agent_id: uuid.UUID
    chat_session_id: str
    platform: str
    requester_account_id: uuid.UUID | None
    held: bool
    confirmed_call_id: uuid.UUID | None
    token_jti: uuid.UUID | None

    @property
    def requester(self) -> uuid.UUID:
        return self.requester_account_id or self.account_id

    @property
    def shared(self) -> bool:
        """Someone other than the credential's owner asked: only agent-wide sign-ins apply."""
        return self.requester_account_id not in (None, self.account_id)


LoadConnectors = Callable[["RelayTurn", "TurnOriginRow"], Awaitable[Mapping[str, Connector]]]
TokenFor = Callable[[RelayTurn, Connector], Awaitable[str | None]]
OpenSession = Callable[[str, str], AbstractAsyncContextManager[McpToolSession]]
PostCard = Callable[[RelayTurn, "TurnOriginRow", "HostCallCardRow"], Awaitable[bool]]
Tools = tuple[dict[str, JsonValue], ...]


def interactive_session(url: str, token: str) -> AbstractAsyncContextManager[McpToolSession]:
    return mcp_tool_session(url, token, limits=INTERACTIVE_LIMITS)


def _uuid(raw: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def relay_turn(claims: Mapping[str, object]) -> RelayTurn | None:
    """The turn a provider-turn chat credential names; None for any other credential."""
    account, tenant = _uuid(claims.get("sub")), _uuid(claims.get("tenant_id"))
    agent, session = _uuid(claims.get("chat_agent_id")), claims.get("chat_session_id")
    platform = claims.get("platform")
    if (
        account is None
        or tenant is None
        or agent is None
        or not isinstance(session, str)
        or not session
        or not isinstance(platform, str)
        or claims.get(CHAT_EXECUTION_CLAIM) is not True
        # Only a provider turn's credential: a card-capable turn calls its connectors natively.
        or claims.get("no_confirmation_surface") is not True
        # A malformed binding must not read as "no requester": refuse it, as identity does.
        or any(
            claims.get(name) is not None and _uuid(claims.get(name)) is None
            for name in ("requester_account_id", "confirmed_call", TOKEN_JTI_CLAIM)
        )
    ):
        return None
    return RelayTurn(
        tenant_id=tenant,
        account_id=account,
        agent_id=agent,
        chat_session_id=session,
        platform=platform,
        requester_account_id=_uuid(claims.get("requester_account_id")),
        held=claims.get(HOLD_CLAIM) is not None,
        confirmed_call_id=_uuid(claims.get("confirmed_call")),
        token_jti=_uuid(claims.get(TOKEN_JTI_CLAIM)),
    )


def _reply(request_id: JsonValue, result: Mapping[str, JsonValue]) -> Response:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": dict(result)})


def _error(request_id: JsonValue, code: int, message: str) -> Response:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


def _tool_text(text: str) -> dict[str, JsonValue]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


class ConnectorRelay:
    """Serve one deployment's relay route; every dependency that leaves the process is injected."""

    def __init__(
        self,
        *,
        auth: TokenVerifier,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        public_url: str,
        load_connectors: LoadConnectors,
        token_for: TokenFor,
        post_card: PostCard,
        open_session: OpenSession = interactive_session,
        tools: ToolsCache | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._auth, self._settings, self._sessionmaker = auth, settings, sessionmaker
        self._public_url = public_url
        self._load, self._token_for, self._post_card = load_connectors, token_for, post_card
        self._open, self._clock, self._monotonic = open_session, clock, monotonic
        self._tools = tools or ToolsCache(clock=monotonic)
        self._connectors: dict[tuple[uuid.UUID, uuid.UUID], tuple[float, Mapping[str, Connector]]]
        self._connectors = {}

    async def handle(self, request: Request) -> Response:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return Response(status_code=401)
        access = await self._auth.verify_token(header[len("bearer ") :])
        if access is None:
            return Response(status_code=401)
        turn = relay_turn(access.claims)
        if turn is None:
            return Response(status_code=403)
        if not deployment_on(self._settings, FEATURE, turn.platform):
            return Response(status_code=404)
        if request.method != "POST":
            return Response(status_code=405)
        body = await read_capped_body(request)
        if body is None:
            return Response(status_code=413)
        try:
            message = strict_json(body.decode("utf-8"))
        except (McpCallError, UnicodeDecodeError):
            return _error(None, -32700, "Parse error")
        if not isinstance(message, dict) or not isinstance(method := message.get("method"), str):
            return _error(None, -32600, "Invalid request")
        request_id = message.get("id")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if "id" not in message:
            return Response(status_code=202)  # A notification: nothing upstream needs it.
        origin = await self._origin(turn)
        if origin is None:
            return Response(status_code=403)
        key = str(request.path_params["server_key"])
        connector = (await self._turn_connectors(turn, origin)).get(key)
        if connector is None:
            return Response(status_code=404)
        if method == "initialize":
            return _reply(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": key, "version": "0"},
                },
            )
        if method == "ping":
            return _reply(request_id, {})
        if method not in ("tools/list", "tools/call"):
            return _error(request_id, -32601, "Method not found")
        if turn.held and key not in held_connectors((key,)):
            return _error(request_id, -32001, REFUSED_TEXT)
        token = await self._token_for(turn, connector)
        if token is None:
            log.info("connector_relay.reconnect_needed", server_key=key)
            return _error(request_id, -32001, RECONNECT_TEXT)
        try:
            if method == "tools/list":
                listed = await self._list(turn, connector, token)
                return _reply(request_id, {"tools": cast(JsonValue, list(listed))})
            return _reply(request_id, await self._call(turn, origin, connector, token, params))
        except McpCallError as error:
            log.warning("connector_relay.call_failed", server_key=key, kind=error.kind)
            if method == "tools/list":
                return _error(request_id, -32002, FAILED_TEXT)
            return _reply(request_id, _tool_text(FAILED_TEXT))

    async def _origin(self, turn: RelayTurn) -> TurnOriginRow | None:
        async with self._sessionmaker() as session:
            origin = await get_session_origin(
                session,
                tenant_id=turn.tenant_id,
                account_id=turn.requester,
                platform=turn.platform,
                chat_session_id=turn.chat_session_id,
                now=self._clock(),
            )
        if origin is None or (
            derive_agent_uuid(tenant_id=origin.tenant_id, ma_agent_id=origin.responder_ma_agent_id)
            != turn.agent_id
        ):
            return None
        return origin

    async def _turn_connectors(
        self, turn: RelayTurn, origin: TurnOriginRow
    ) -> Mapping[str, Connector]:
        owner = (turn.tenant_id, turn.agent_id)
        cached = self._connectors.get(owner)
        if cached is not None and self._monotonic() - cached[0] <= CACHE_TTL_S:
            return cached[1]
        found = await self._load(turn, origin)
        self._connectors[owner] = (self._monotonic(), found)
        return found

    async def _upstream_tools(self, turn: RelayTurn, connector: Connector, token: str) -> Tools:
        cached = self._tools.get(turn.tenant_id, turn.requester, connector.url, token)
        if cached is not None:
            return cached
        async with self._open(connector.url, token) as upstream:
            tools = await upstream.list_tools()
        self._tools.put(turn.tenant_id, turn.requester, connector.url, token, tools)
        return tools

    async def _list(self, turn: RelayTurn, connector: Connector, token: str) -> Tools:
        tools = await self._upstream_tools(turn, connector, token)
        return tuple(t for t in tools if connector.permission(str(t["name"])) != "disabled")

    async def _find(
        self, turn: RelayTurn, connector: Connector, token: str, name: str
    ) -> dict[str, JsonValue] | None:
        for fresh in (False, True):
            if fresh:
                # An unknown name may be a tool added since the cached list: list once more.
                self._tools.drop(turn.tenant_id, turn.requester, connector.url)
            tools = await self._list(turn, connector, token)
            found = next((t for t in tools if t.get("name") == name), None)
            if found is not None:
                return found
        return None

    def _digest(self, turn: RelayTurn, connector: Connector, name: str, args: object) -> str:
        return host_calls.canonical_call_digest(
            route=host_calls.ROUTE_CONNECTOR,
            # The relay route and the upstream server both: one press approves one place.
            server=f"{self._public_url.rstrip('/')}{RELAY_PATH}/{connector.key}|{connector.url}",
            tool=name,
            args=cast("Mapping[str, object]", args),
            executing_agent_id=turn.agent_id,
        )

    async def _call(
        self,
        turn: RelayTurn,
        origin: TurnOriginRow,
        connector: Connector,
        token: str,
        params: Mapping[str, JsonValue],
    ) -> dict[str, JsonValue]:
        name, args = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(args, dict):
            return _tool_text(REFUSED_TEXT)
        tool = await self._find(turn, connector, token, name)
        if tool is None:
            return _tool_text(REFUSED_TEXT)
        schema = tool.get("inputSchema")
        if isinstance(schema, dict) and (problem := schema_problem(schema, args)) is not None:
            return _tool_text(problem)
        if turn.confirmed_call_id is not None:
            return await self._run_confirmed(turn, connector, token, name, args)
        call = ToolCall(
            tool_use_id=f"relay-{uuid.uuid4()}",
            server_name=connector.key,
            tool_name=name,
            input=cast("dict[str, object]", args),
        )
        verdict = await relay_verdict(
            self._settings.tool_safety, connector, call, attended=origin.attended
        )
        if verdict == "refuse":
            log.info("connector_relay.call_refused", server_key=connector.key, tool=name)
            return _tool_text(REFUSED_TEXT)
        if verdict == "ask":
            return await self._ask(turn, origin, connector, name, args)
        async with self._open(connector.url, token) as upstream:
            return await upstream.call_tool_result(name, args)

    async def _ask(
        self,
        turn: RelayTurn,
        origin: TurnOriginRow,
        connector: Connector,
        name: str,
        args: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        if not deployment_on(self._settings, "host_call_gate", turn.platform):
            return _tool_text(REFUSED_TEXT)
        async with self._sessionmaker() as session:
            # Only the person who asked presses: read who that is, never a claim.
            person = await get_account_with_tenant(session, account_id=turn.requester)
        if person is None or person.tenant_id != turn.tenant_id or not person.platform_user_id:
            return _tool_text(REFUSED_TEXT)
        now = self._clock()
        timeout = min(self._settings.tool_safety.confirmation_timeout_s, SWITCH_CARD_MAX_S)
        async with self._sessionmaker.begin() as session:
            created = await cards.create_card(
                session,
                tenant_id=turn.tenant_id,
                origin_id=origin.id,
                requester_account_id=origin.account_id,
                requester_platform_user_id=person.platform_user_id,
                platform=origin.platform,
                channel_id=origin.parent_channel_id,
                thread_id=origin.thread_id,
                executing_agent_id=turn.agent_id,
                executing_ma_agent_id=origin.responder_ma_agent_id,
                agent_name=origin.responder_name,
                tool_name=name,
                args_digest=self._digest(turn, connector, name, args),
                reference={
                    "route": host_calls.ROUTE_CONNECTOR,
                    "server": connector.url,
                    "server_key": connector.key,
                    "args": args,
                    "admin": False,
                },
                expires_at=now + timedelta(seconds=timeout),
            )
        asker = "the person who asked"
        if created is None:
            return _tool_text(host_calls.TOO_MANY_TEXT.format(person=asker))
        if created.fresh and not await self._post_card(turn, origin, created.row):
            return _tool_text(host_calls.NOT_RUN_TEXT)
        log.info("connector_relay.call_recorded", card_id=str(created.row.id), tool=name)
        return {"content": [{"type": "text", "text": host_calls.waiting_text(asker)}]}

    async def _run_confirmed(
        self,
        turn: RelayTurn,
        connector: Connector,
        token: str,
        name: str,
        args: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        now = self._clock()
        assert turn.confirmed_call_id is not None
        async with self._sessionmaker.begin() as session:
            row = await cards.consume(
                session,
                card_id=turn.confirmed_call_id,
                tool_name=name,
                args_digest=self._digest(turn, connector, name, args),
                executing_agent_id=turn.agent_id,
                now=now,
            )
            if row is not None and turn.token_jti is not None:
                # One call per credential: nothing else may run under it.
                await revoke_mcp_token(session, jti=turn.token_jti, now=now)
        if row is None:
            return _tool_text(REFUSED_TEXT)
        status: cards.Finished = "failed"
        outcome = host_calls.CRASHED_TEXT
        try:
            async with self._open(connector.url, token) as upstream:
                result = await upstream.call_tool_result(name, args)
            status = "failed" if result.get("isError") is True else "applied"
            outcome = json.dumps(result.get("content", result), ensure_ascii=False)[:_OUTCOME_MAX]
            return result
        finally:
            # Anything else may have half-run: it is recorded, never retried.
            async with self._sessionmaker.begin() as session:
                await cards.finish(session, card_id=row.id, status=status, outcome=outcome)
            log.info("connector_relay.call_finished", card_id=str(row.id), status=status)


async def relay_token(
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    turn: RelayTurn,
    connector: Connector,
    *,
    http_factory: HttpFactory | None = None,
) -> str | None:
    """The requester's own token, or on a shared thread only an agent-wide one."""
    from daimon.core.mcp_oauth.connector_tokens import connector_access_token

    return await connector_access_token(
        sessionmaker,
        tenant_id=turn.tenant_id,
        agent_id=turn.agent_id,
        server_url=connector.url,
        requester_account_id=turn.requester,
        hide_personal=turn.shared,
        fernet=fernet,
        http_factory=http_factory,
    )


def build_connector_relay(
    runtime: McpRuntime, auth: TokenVerifier, public_url: str
) -> ConnectorRelay:
    """The relay over this server's runtime: the agent's source spec, R1 tokens, own cards."""
    from daimon.adapters.mcp.auth.resolver import AuthIdentity
    from daimon.adapters.mcp.tools._own_card import post_own_card
    from daimon.core.channel_backend import AgentBackendRefused, load_backend_agent
    from daimon.core.connector_relay import connectors_of
    from daimon.core.stores.domain import Role

    sessionmaker = runtime.session_factory

    async def load_connectors(turn: RelayTurn, origin: TurnOriginRow) -> Mapping[str, Connector]:
        try:
            agent = await load_backend_agent(
                runtime.client,
                sessionmaker,
                tenant_id=turn.tenant_id,
                agent_name=origin.responder_name,
            )
        except AgentBackendRefused:
            return {}
        spec = agent.source.spec if agent.source is not None else None
        if spec is None or agent.agent_id != turn.agent_id:
            return {}
        return connectors_of(spec)

    async def token_for(turn: RelayTurn, connector: Connector) -> str | None:
        if runtime.fernet is None:
            return None
        return await relay_token(sessionmaker, runtime.fernet, turn, connector)

    async def post_card(turn: RelayTurn, origin: TurnOriginRow, row: HostCallCardRow) -> bool:
        identity = AuthIdentity(
            account_id=turn.account_id,
            tenant_id=turn.tenant_id,
            role=Role.USER,
            platform=turn.platform,
            chat_agent_id=turn.agent_id,
            platform_user_id=row.requester_platform_user_id,
            chat_session_id=turn.chat_session_id,
            requester_account_id=turn.requester_account_id,
            no_confirmation_surface=True,
        )
        return await host_calls.post_call_card(
            sessionmaker,
            row,
            post=lambda card: post_own_card(runtime, identity, origin=origin, card=card),
        )

    return ConnectorRelay(
        auth=auth,
        settings=runtime.settings,
        sessionmaker=sessionmaker,
        public_url=public_url,
        load_connectors=load_connectors,
        token_for=token_for,
        post_card=post_card,
    )
