"""Who a Microsoft tool acts for, and where personal content may be shown."""

from __future__ import annotations

import uuid

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import caller_foreign_status, get_verified_origin
from daimon.adapters.mcp.tools.microsoft._access import MicrosoftGraph
from daimon.adapters.mcp.tools.setup_target import require_turn_origin, sole_running_origin_id
from daimon.core.stores.domain import TurnOriginRow
from fastmcp.exceptions import ToolError


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
    """
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


async def _live_origin(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str
) -> TurnOriginRow | None:
    """The verified turn origin, or None. As `require_turn_origin` does, a model that
    dropped the id gets the person's one running turn with this agent; with none or
    several, or an id that does not verify, there is no origin."""
    if not origin_context_id:
        sole = await sole_running_origin_id(runtime, auth)
        if sole is None:
            return None
        origin_context_id = sole
    return await get_verified_origin(runtime, auth, origin_context_id)


async def origin_facts(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str
) -> tuple[bool, bool | None]:
    """(is it the person's 1:1 chat, is the sender from another organisation) for the
    verified turn origin. Without one, the person's running turns say whether they are
    from another organisation, and None means that is not known."""
    origin = await _live_origin(runtime, auth, origin_context_id)
    if origin is None:
        return False, await caller_foreign_status(runtime, auth)
    return origin.parent_channel_id.startswith("a:"), origin.sender_foreign


async def is_one_to_one(runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str) -> bool:
    """Whether the verified turn origin is the person's 1:1 chat; False when unverified."""
    origin = await _live_origin(runtime, auth, origin_context_id)
    return origin is not None and origin.parent_channel_id.startswith("a:")
