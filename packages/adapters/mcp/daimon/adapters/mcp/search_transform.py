"""Agent-chat-aware BM25 search transform.

The stock ``BM25SearchTransform`` collapses ``tools/list`` to
``[*pinned, search_tools, call_tool]`` so a large catalog is discovered via
search instead of listed in full. That's right for the admin/user surface, but
wrong for a per-agent token: the IdentityMiddleware narrows such a session to
agent-chat tools via ``disable_components(match_all=True)`` +
``enable_components(tags={"agent-chat"})``, and the synthetic search/call tools
carry no ``agent-chat`` tag, so the ``match_all`` disable hides them — leaving
``tools/list`` empty even though ``tools/call`` still works (issue #181).

For a narrowed agent session there are only six tools, so search is pointless.
This subclass detects the narrowing (the request's ``auth`` state has a non-null
``agent_id``) and returns the catalog unchanged, letting the visibility filter
narrow it to exactly the agent-chat tools.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from fastmcp.server.dependencies import get_context
from fastmcp.server.transforms.search import BM25SearchTransform

if TYPE_CHECKING:
    from fastmcp.tools.base import Tool


class AgentChatAwareBM25SearchTransform(BM25SearchTransform):
    """BM25 search collapse that yields to per-agent narrowing.

    Narrowed agent sessions list their agent-chat tools directly; every other
    session gets the normal search interface.
    """

    async def transform_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        try:
            ctx = get_context()
        except RuntimeError:
            # No active request context (no narrowing info) — use search.
            return await super().transform_tools(tools)
        auth = await ctx.get_state("auth")
        if isinstance(auth, AuthIdentity) and auth.agent_id is not None:
            # Narrowed agent session: skip the search collapse so the
            # visibility filter can surface the agent-chat tools.
            return tools
        return await super().transform_tools(tools)
