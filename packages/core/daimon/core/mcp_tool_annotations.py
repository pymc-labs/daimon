"""Read third-party MCP tools' own annotations for the tool-safety deciders.

Managed Agents runs attached MCP servers itself and does not forward their
`tools/list` annotations, so `classify_tool` only saw the tool's name: a read
named noun-first (`memory_flag_list`) counted as a write and asked for
approval. `mcp_annotation_lookup` closes that gap the way `agent_rule_lookup`
closes the rule one: the approval decider asks it for the blocked call's
`ToolAnnotations`, and it lists that server's tools once per turn.

The read uses the agent-wide credential stored for that URL
(`agent_mcp_credentials`), or no credential when none is stored. A personal
sign-in lives only in a write-only MA vault, so such a server usually refuses
the read. Any failure (refusal, timeout, malformed page, a server that sends
no hints) answers `None`, and the name decides as before: the lookup can
only make a tool a read when its own server says `readOnlyHint: true`, and
`classify_tool` still treats `destructiveHint: true` as a write.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING

import structlog
from cryptography.fernet import MultiFernet
from daimon.core.agent_mcp_credentials import resolve_agent_mcp_credentials
from daimon.core.mcp_oauth.discovery import read_mcp_catalogue
from daimon.core.mcp_server_url import same_mcp_url
from daimon.core.tool_safety import ToolAnnotations, ToolCall
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    # Type only: `daimon.core.turn` imports this module through `turn.run`.
    from daimon.core.turn.approvals import AnnotationLookup

__all__ = [
    "agent_mcp_token_for",
    "annotations_from_mcp",
    "mcp_annotation_lookup",
    "no_mcp_token",
]

log = structlog.get_logger(__name__)

CatalogueReader = Callable[[str, str | None], Awaitable[Sequence[Mapping[str, JsonValue]]]]
TokenFor = Callable[[str], Awaitable[str | None]]

#: Ceiling on one server's catalogue read, so a slow server delays the first
#: approval decision for its tools by at most this long.
CATALOGUE_TIMEOUT_S = 10.0

# Server URLs whose catalogue read already failed loudly in this process. A
# server that refuses the read (a personal sign-in, no hints served) refuses it
# every turn, so later failures log at info.
_warned_urls: set[str] = set()


def annotations_from_mcp(tool: Mapping[str, JsonValue]) -> ToolAnnotations | None:
    """The two hints `classify_tool` reads, from one `tools/list` entry.

    A hint that is absent or not a boolean is `None`; a tool with neither
    hint answers `None`, which leaves the name to decide.
    """
    raw = tool.get("annotations")
    if not isinstance(raw, dict):
        return None
    read_only = raw.get("readOnlyHint")
    destructive = raw.get("destructiveHint")
    hints = ToolAnnotations(
        read_only_hint=read_only if isinstance(read_only, bool) else None,
        destructive_hint=destructive if isinstance(destructive, bool) else None,
    )
    if hints.read_only_hint is None and hints.destructive_hint is None:
        return None
    return hints


def agent_mcp_token_for(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    fernet: MultiFernet | None,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> TokenFor:
    """The agent-wide credential for a server URL, or `None` when none is stored."""

    async def _token_for(url: str) -> str | None:
        if fernet is None:
            return None
        credentials = await resolve_agent_mcp_credentials(
            sessionmaker=sessionmaker, fernet=fernet, tenant_id=tenant_id, agent_id=agent_id
        )
        return next(
            (c.token for c in credentials if same_mcp_url(c.mcp_server_url, url)),
            None,
        )

    return _token_for


async def no_mcp_token(url: str) -> str | None:
    """Read every catalogue anonymously: no stored credential can be resolved."""
    return None


def mcp_annotation_lookup(
    *,
    server_urls: Mapping[str, str],
    token_for: TokenFor,
    read_catalogue: CatalogueReader | None = None,
) -> AnnotationLookup:
    """Capture a session's server URLs; list each server's tools at most once."""
    catalogues: dict[str, asyncio.Task[Mapping[str, ToolAnnotations]]] = {}

    async def _catalogue(server_name: str, url: str) -> Mapping[str, ToolAnnotations]:
        try:
            async with asyncio.timeout(CATALOGUE_TIMEOUT_S):
                reader = read_catalogue or read_mcp_catalogue
                tools = await reader(url, await token_for(url))
        except Exception as err:
            first = url not in _warned_urls
            _warned_urls.add(url)
            (log.warning if first else log.info)(
                "tool_safety.annotations_unavailable", server=server_name, error=str(err)
            )
            return {}
        out: dict[str, ToolAnnotations] = {}
        for tool in tools:
            name = tool.get("name")
            hints = annotations_from_mcp(tool)
            if isinstance(name, str) and hints is not None:
                out[name] = hints
        return out

    async def _lookup(call: ToolCall) -> ToolAnnotations | None:
        name = call.server_name or ""
        url = server_urls.get(name)
        if url is None:
            return None
        if name not in catalogues:
            catalogues[name] = asyncio.ensure_future(_catalogue(name, url))
        return (await asyncio.shield(catalogues[name])).get(call.tool_name)

    return _lookup
