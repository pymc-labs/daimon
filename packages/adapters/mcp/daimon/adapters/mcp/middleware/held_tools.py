"""The tools a held turn may reach, and nothing else.

A held turn (`daimon.core.turn_hold.TurnHold`) is kept to one channel and its
threads and carries no administrator authority. Its credential therefore sees an
explicit allowlist of Daimon's tools: everything else is hidden from
`tools/list` and `search_tools`, and refused with a plain error if a client
calls it by name. A tool added later is closed by default until someone adds it
here, with a reason, in review. The list lives in `daimon.core.mcp_hold`, so a
provider host can narrow the catalogue it expects to the same set.

Allowlisted tools are not trusted on the strength of being listed. The channel
tools apply the hold through the channel policy themselves; the entries here are
only the ones that either do that or touch nothing outside the conversation.
"""

from __future__ import annotations

from daimon.core.mcp_hold import HELD_PROXY_TOOLS, HELD_TOOL_ALLOWLIST
from fastmcp.server.context import Context
from fastmcp.server.transforms.visibility import disable_components, enable_components

__all__ = ["HELD_PROXY_TOOLS", "HELD_TOOL_ALLOWLIST", "held_tool_refusal", "narrow_to_held_tools"]


def held_tool_refusal(tool_name: str) -> str | None:
    """Why a held credential may not call `tool_name`, or None when it may."""
    if tool_name in HELD_TOOL_ALLOWLIST or tool_name in HELD_PROXY_TOOLS:
        return None
    return (
        "This conversation is limited to its own channel, so that tool isn't "
        "available here. Tell the person it can't be done from this conversation."
    )


async def narrow_to_held_tools(ctx: Context) -> None:
    """Hide every tool, resource and prompt but the allowlist (and the search proxies).

    Rules are per request, so a tool the allowlist loses between requests is gone
    on the next one. Nothing is enabled by tag, so no tag-based visibility
    (admin, agent-chat, platform) can widen the set.
    """
    await disable_components(ctx, match_all=True)
    await enable_components(ctx, names={*HELD_TOOL_ALLOWLIST, *HELD_PROXY_TOOLS})
