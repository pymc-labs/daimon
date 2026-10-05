"""Agent-chat-aware BM25 search transform.

The stock ``BM25SearchTransform`` collapses ``tools/list`` to
``[*pinned, search_tools, call_tool]`` so a large catalog is discovered via
search instead of listed in full. That's right for the admin/user surface, but
wrong for a per-agent token: the IdentityMiddleware narrows such a session to
agent-chat tools via ``disable_components(match_all=True)`` +
``enable_components(tags={"agent-chat"})``, and the synthetic search/call tools
carry no ``agent-chat`` tag, so the ``match_all`` disable hides them — leaving
``tools/list`` empty even though ``tools/call`` still works (issue #181).

For a narrowed agent session the catalog is small — the six ``agent_chat``
tools plus the eight tools tagged ``agent-chat`` in ``self_edit``/``vault``
— so search is pointless. This subclass detects the narrowing (the
request's ``auth`` state has a non-null ``agent_id``) and returns the
catalog unchanged, letting the visibility filter narrow it to exactly the
agent-chat-tagged tools. An operator token is narrowed the same way, to the
tools tagged with its scopes, and lists them directly too.

Tools that can wait for the person's Approve (`APPROVAL_TOOLS`) stay listed by
name and refuse the ``call_tool`` proxy. Managed Agents applies a session's
per-tool ``always_ask`` to the name the model calls; through the proxy that
name is ``call_tool``, which daimon's toolset allows. A gated tool reached that
way would run with no card while its own entry still reads ``always_ask``, the
entry ``session_card_gap`` takes as proof of the card.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated, Any, Final

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.core.tool_safety import CONFIRMED_DAIMON_TOOLS, PUBLISH_TOOLS
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.dependencies import get_context
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from fastmcp.server.transforms.search.base import SearchResultSerializer

#: Daimon's tools a session can hold on `always_ask`.
APPROVAL_TOOLS: Final[frozenset[str]] = frozenset({*CONFIRMED_DAIMON_TOOLS, *PUBLISH_TOOLS})


class AgentChatAwareBM25SearchTransform(BM25SearchTransform):
    """BM25 search collapse that yields to per-agent narrowing.

    Narrowed agent and operator sessions list their tools directly; every
    other session gets the normal search interface, with `APPROVAL_TOOLS`
    pinned beside it and refused through ``call_tool``.
    """

    def __init__(
        self,
        *,
        max_results: int = 5,
        always_visible: list[str] | None = None,
        search_result_serializer: SearchResultSerializer | None = None,
    ) -> None:
        super().__init__(
            max_results=max_results,
            always_visible=sorted({*(always_visible or ()), *APPROVAL_TOOLS}),
            search_result_serializer=search_result_serializer,
        )

    async def _get_visible_tools(self, ctx: Context) -> Sequence[Tool]:
        """The catalog search ranks: the stock one, plus `APPROVAL_TOOLS`.

        The stock search leaves pinned tools out. A model that searches for a
        gated tool still finds it, and a proxied call gets the refusal that
        names it.
        """
        pinned = self._always_visible - APPROVAL_TOOLS
        return [t for t in await self.get_tool_catalog(ctx) if t.name not in pinned]

    def _make_call_tool(self) -> Tool:
        """The stock proxy, refusing `APPROVAL_TOOLS`."""
        synthetic = {self._call_tool_name, self._search_tool_name}

        async def call_tool(
            name: Annotated[str, "The name of the tool to call"],
            arguments: Annotated[dict[str, Any] | None, "Arguments to pass to the tool"] = None,
            ctx: Context = None,  # pyright: ignore[reportArgumentType]
        ) -> ToolResult:
            """Call a tool by name with the given arguments.

            Use this to execute tools discovered via search_tools.
            """
            if name in APPROVAL_TOOLS:
                raise ToolError(
                    f"Call {name} directly, not through call_tool: it is listed under its own "
                    "name so the person can approve it. Nothing was done."
                )
            if name in synthetic:
                raise ValueError(
                    f"'{name}' is a synthetic search tool and cannot be called via the "
                    "call_tool proxy"
                )
            return await ctx.fastmcp.call_tool(name, arguments)

        return Tool.from_function(fn=call_tool, name=self._call_tool_name)

    async def transform_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        try:
            ctx = get_context()
        except RuntimeError:
            # No active request context (no narrowing info) — use search.
            return await super().transform_tools(tools)
        auth = await ctx.get_state("auth")
        if isinstance(auth, AuthIdentity) and (auth.agent_id is not None or auth.is_operator):
            # Narrowed agent or operator session: skip the search collapse so
            # the visibility filter can surface the tools it was narrowed to.
            return tools
        return await super().transform_tools(tools)
