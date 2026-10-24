"""Who a Microsoft tool acts for, and where personal content may be shown."""

from __future__ import annotations

import uuid

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import caller_foreign_status, get_verified_origin
from daimon.adapters.mcp.tools.microsoft._access import MicrosoftGraph
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.core.stores.domain import TurnOriginRow
from fastmcp.exceptions import ToolError

NEEDS_ORIGIN = (
    "Pass the origin_context_id from the current chat and call again: it is needed on every "
    "call, to be sure the request comes from the person's own chat. If this conversation was "
    "not given one, it is not the person's own chat and this cannot be done here."
)


def _canonical(value: str | None) -> str | None:
    try:
        return str(uuid.UUID(value or ""))
    except ValueError:
        return None


def require_person(runtime: McpRuntime, auth: AuthIdentity) -> tuple[MicrosoftGraph, str]:
    """The Graph access and Entra object id of the Teams member this call acts for."""
    graph = runtime.microsoft
    if graph is None:
        raise ToolError("Microsoft 365 access is not set up on this server.")
    user_id = _canonical(auth.platform_user_id)
    if auth.platform != "teams" or auth.agent_id is not None or user_id is None:
        raise ToolError("Microsoft 365 access works only for a person in a Teams conversation.")
    if _canonical(auth.external_id) != graph.config.tenant_id:
        raise ToolError(
            "Microsoft 365 access is set up for another organisation's Teams, so it "
            "cannot be used here."
        )
    return graph, user_id


async def require_one_to_one(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str, *, what: str
) -> TurnOriginRow:
    """The verified turn origin, if it is the person's 1:1 chat with Daimon.

    The model passes the origin id, but only a live origin of this person's own
    turn with this agent verifies, and its id is random per turn: mail,
    calendar and OneNote content must not reach a channel or group chat.

    The id is required. A routine runs on the same credential as the person's chat
    turns but opens no origin, so guessing "the person's one running turn" would
    hand a routine whatever the person's live 1:1 turn may see.
    """
    if not origin_context_id:
        raise ToolError(f"Nothing was read or sent. {NEEDS_ORIGIN}")
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    if origin.sender_foreign:
        raise ToolError(
            f"{what} works only for people in the organisation that added Daimon, and this "
            "person is from another organisation, so nothing was read or sent."
        )
    if not origin.parent_channel_id.startswith("a:"):
        raise ToolError(
            f"{what} can only be used in the person's 1:1 chat with Daimon, and this "
            "is a channel or group chat, so nothing was read or sent. Tell them to ask "
            "again in a 1:1 chat."
        )
    return origin


async def origin_facts(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str
) -> tuple[bool, bool | None]:
    """(is it the person's 1:1 chat, is the sender from another organisation) for the
    verified turn origin. Without one, only a person known to be from another
    organisation is told so; for anyone else the second value is None, so the caller
    asks for the origin id and never points at a chat on a guess."""
    origin = await get_verified_origin(runtime, auth, origin_context_id)
    if origin is None:
        return False, (True if await caller_foreign_status(runtime, auth) else None)
    return origin.parent_channel_id.startswith("a:"), origin.sender_foreign


async def is_one_to_one(runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str) -> bool:
    """Whether the verified turn origin is the person's 1:1 chat; False when unverified."""
    origin = await get_verified_origin(runtime, auth, origin_context_id)
    return origin is not None and origin.parent_channel_id.startswith("a:")
