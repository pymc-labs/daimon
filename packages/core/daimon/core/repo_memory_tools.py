"""Turn off repository-memory tools on named MCP servers: retired readers, or all of them."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

LEGACY_MEMORY_READERS = frozenset({"repo_memory_list", "repo_memory_view"})
REPO_MEMORY_TOOLS = frozenset(
    {"repo_memory_list", "repo_memory_view", "repo_memory_save", "repo_memory_delete"}
)


def suppress_legacy_readers(
    tools: list[dict[str, Any]], server_names: tuple[str, ...]
) -> list[dict[str, Any]]:
    return disable_mcp_tools(tools, server_names, LEGACY_MEMORY_READERS)


def disable_mcp_tools(
    tools: list[dict[str, Any]], server_names: Iterable[str], names: frozenset[str]
) -> list[dict[str, Any]]:
    """`tools` with `names` turned off on the toolsets of `server_names`."""
    servers = frozenset(server_names)
    result: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "mcp_toolset" or tool.get("mcp_server_name") not in servers:
            result.append(tool)
            continue
        existing = cast(list[dict[str, Any]], tool.get("configs") or [])
        configs = [config for config in existing if config.get("name") not in names]
        configs.extend(
            {"name": name, "enabled": False, "permission_policy": {"type": "always_ask"}}
            for name in sorted(names)
        )
        result.append({**tool, "configs": configs})
    return result
