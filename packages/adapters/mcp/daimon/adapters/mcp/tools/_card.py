"""The person's Approve, for tools that undo or change something and run only on a card.

The tool is listed in `CARDED_TOOLS`, so the session holds it on `always_ask`: MA runs the
call once the person approves it. This refuses every call that cannot show that card: an agent
key, an operator token, a routine, a held or client credential or a turn with no card.
`provider_move_applies` is the one rule for the tools that move conversations to another
provider.
"""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.confirmed_call import card_satisfied
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import get_verified_origin
from daimon.adapters.mcp.tools._session_gate import session_card_gap
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.tool_safety import CARDED_TOOLS, ProviderMoveTool
from fastmcp.exceptions import ToolError

NEEDS_CARD = "Confirm this in your chat conversation. Nothing changed."
CARDS_OFF = (
    "This needs the person to press Approve on a confirmation card, and approval cards are "
    "turned off here (tool safety is disabled); whoever runs this Daimon install can turn them "
    "on. Nothing changed. Do not retry."
)


async def require_card(
    runtime: McpRuntime, auth: AuthIdentity, *, tool_name: str, origin_context_id: str | None
) -> None:
    """Return only when a person confirmed this very call on a card; else refuse it."""
    if (
        auth.agent_id is not None
        or auth.routine_id is not None
        or auth.is_external_agent
        or auth.hold is not None
        or auth.client
        or (auth.no_confirmation_surface and not card_satisfied(auth))
        or auth.platform_user_id is None
    ):
        raise ToolError(NEEDS_CARD)
    if tool_name not in CARDED_TOOLS and not runtime.settings.tool_safety.enabled:
        # Only the always-carded tools still get a card with the policy off.
        raise ToolError(CARDS_OFF)
    origin = await get_verified_origin(runtime, auth, origin_context_id)
    if origin is None or not origin.attended:
        raise ToolError(NEEDS_CARD)
    if await session_card_gap(runtime, auth, origin, tool_name=tool_name) is not None:
        raise ToolError(NEEDS_CARD)


async def provider_move_applies(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow,
    *,
    tool_name: ProviderMoveTool,
    moves: bool,
    confirming: bool,
) -> bool:
    """Whether a pick applies now, or first previews (False): one rule for `PROVIDER_MOVE_TOOLS`.

    A move to another provider sends it the conversations and their connector data,
    so it previews, then its confirming call runs on the person's card; a session
    that can't show one is refused. A model change within the provider applies.
    """
    if not moves:
        return True
    if not confirming:
        return False
    if await session_card_gap(runtime, auth, origin, tool_name=tool_name) is not None:
        raise ToolError("This conversation cannot verify the confirmation card. Nothing changed.")
    return True


def provider_move_line(what: str, phrase: str, *, sends: str, field: str) -> str:
    """The preview's words: nothing changed, who receives what, and how to confirm."""
    return (
        f"Nothing changed yet. Moving {what} to {phrase} sends {sends}, and the connector "
        f"data they use, to that provider. Call again with the same arguments and this "
        f"{field}; the person confirms on the card."
    )
