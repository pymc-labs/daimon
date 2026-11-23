"""The call a chat adapter asks for when a person confirms a reaction action.

`POST <app root>/internal/reaction-actions/call` takes the short-lived credential of
`daimon.core.reaction_credential` as its bearer. This service holds the attached
server's key; the chat process does not. The route calls the one tool a binding
allows for that workspace, server and agent. The link in the arguments comes from
the credential, and the arguments are that link alone: the request body is never read.
The requester is stamped from the credential's reactor, the way the identity relay
stamps a person's turn. A credential works once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime

import structlog
from daimon.core.config import SignedIdentityServer
from daimon.core.mcp_identity import (
    CONVERSATION_META_KEY,
    KIND_META_KEY,
    REQUESTER_META_KEY,
)
from daimon.core.mcp_tool_call import (
    INTERACTIVE_LIMITS,
    McpCallError,
    McpToolSession,
    mcp_tool_session,
)
from daimon.core.reaction_actions import (
    CALL_PATH,
    ReactionAction,
    message_url,
    outcome_of,
)
from daimon.core.reaction_credential import CredentialError, ReactionCall, SingleUse, verify
from pydantic import JsonValue, SecretStr
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

log = structlog.get_logger(__name__)

ROUTE = CALL_PATH
METHODS = ["POST"]
_UNREACHABLE = "The action could not be reached."

SessionFactory = Callable[[str, str], AbstractAsyncContextManager[McpToolSession]]


def _default_session(url: str, token: str) -> AbstractAsyncContextManager[McpToolSession]:
    return mcp_tool_session(url, token, limits=INTERACTIVE_LIMITS)


def requester_meta(call: ReactionCall) -> dict[str, JsonValue]:
    """The `_meta` of a call a person made: who, and where, as the relay stamps a turn."""
    place = "thread" if call.parent is not None else "channel"
    return {
        REQUESTER_META_KEY: call.user,
        KIND_META_KEY: "person",
        CONVERSATION_META_KEY: {
            "place_type": place,
            "channel_id": call.channel,
            "parent_channel_id": call.parent,
        },
    }


def _allowed(call: ReactionCall, actions: Sequence[ReactionAction]) -> ReactionAction | None:
    for action in actions:
        if (
            action.workspace == call.workspace
            and action.server == call.server
            and action.agent == call.agent
            and action.confirm_tool == call.tool
        ):
            return action
    return None


def build_reaction_action_route(
    *,
    actions: Sequence[ReactionAction],
    servers: Mapping[str, SignedIdentityServer],
    bearers: Mapping[str, SecretStr],
    secret: bytes,
    clock: Callable[[], datetime],
    session: SessionFactory = _default_session,
    used: SingleUse | None = None,
) -> Callable[[Request], Awaitable[Response]]:
    spent = used or SingleUse()

    async def handler(request: Request) -> Response:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return Response(status_code=401)
        try:
            call = verify(header[len("bearer ") :], secret=secret, now=clock())
        except CredentialError:
            log.info("reaction_action.refused", reason="credential")
            return Response(status_code=401)
        action = _allowed(call, actions)
        server = servers.get(call.server)
        key = bearers.get(call.server)
        if action is None or server is None or key is None:
            log.warning("reaction_action.refused", reason="not_allowed", server=call.server)
            return Response(status_code=403)
        if not spent.claim(call.jti, now=clock()):
            log.warning("reaction_action.refused", reason="replayed", server=call.server)
            return Response(status_code=401)
        arguments: dict[str, JsonValue] = {
            "message_url": message_url(call.guild, call.channel, call.message)
        }
        try:
            async with session(server.url, key.get_secret_value()) as tools:
                result = await tools.call_tool_result(
                    call.tool, arguments, meta=requester_meta(call)
                )
        except McpCallError as err:
            log.warning("reaction_action.failed", server=call.server, kind=err.kind)
            return JSONResponse({"status": "error", "message": _UNREACHABLE})
        outcome = outcome_of(result)
        log.info(
            "reaction_action.called", server=call.server, tool=call.tool, status=outcome.status
        )
        return JSONResponse({"status": outcome.status, "message": outcome.text})

    return handler
