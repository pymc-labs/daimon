"""Read-only tools over this release's docs and source (`daimon.core.self_knowledge`).

`CapabilityGateMiddleware` hides and refuses them where `self_knowledge` is Off
(`SELF_KNOWLEDGE_TOOLS`); web pages and the web adapter's source stay out where
`web_awareness` is Off at every place the call may run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._web_url import web_aware
from daimon.core.release import current_release
from daimon.core.self_knowledge import Library, SelfKnowledgeError
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


async def _read(ctx: Context, runtime: McpRuntime, read: Callable[[Library], str]) -> str:
    settings = runtime.settings.self_knowledge
    library = Library(
        docs=settings.docs_root,
        source=settings.source_root,
        docs_url=settings.docs_url,
        repo_url=settings.repo_url,
        release=current_release(),
        web=await web_aware(runtime, await _auth(ctx)),
    )
    try:
        return await asyncio.to_thread(read, library)
    except SelfKnowledgeError as exc:
        raise ToolError(str(exc)) from exc


def register_self_knowledge_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def read_daimon_docs(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, page: str = "", start_line: int = 1
    ) -> str:
        """Read Daimon's own documentation, for the release that is running.

        With no page: how Daimon works and the list of pages. With a page from that list
        (`slack.md`): its text from `start_line`. Use it to answer how Daimon works, how to set
        it up or why it behaves as it does.
        """
        return await _read(ctx, runtime, lambda library: library.read_docs(page, start_line))

    @mcp.tool
    async def search_daimon_docs(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, query: str
    ) -> str:
        """Search Daimon's own documentation for some plain text, ignoring case.

        Returns each matching line as `page:line: text`; read the page with read_daimon_docs.
        """
        return await _read(ctx, runtime, lambda library: library.search_docs(query))

    @mcp.tool
    async def read_daimon_source(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, path: str = "", start_line: int = 1
    ) -> str:
        """Read Daimon's own source code, for the release that is running.

        `path` is under `packages/` or `defaults/` (`packages/core/daimon/core/config.py`).
        A file comes back with line numbers from `start_line`; a folder, or no path, lists
        what is in it.
        """
        return await _read(ctx, runtime, lambda library: library.read_source(path, start_line))

    @mcp.tool
    async def search_daimon_source(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, query: str, path: str = ""
    ) -> str:
        """Search Daimon's own source code for some plain text, ignoring case.

        Searches `packages/` and `defaults/`, or only the file or folder `path` names.
        Returns each matching line as `path:line: text`.
        """
        return await _read(ctx, runtime, lambda library: library.search_source(query, path))
