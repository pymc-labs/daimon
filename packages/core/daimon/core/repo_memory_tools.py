"""Suppress retired repository-memory readers on explicitly named legacy servers."""

from __future__ import annotations

from typing import Any, cast

LEGACY_MEMORY_READERS = frozenset({"repo_memory_list", "repo_memory_view"})


def suppress_legacy_readers(
    tools: list[dict[str, Any]], server_names: tuple[str, ...]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "mcp_toolset" or tool.get("mcp_server_name") not in server_names:
            result.append(tool)
            continue
        existing = cast(list[dict[str, Any]], tool.get("configs") or [])
        configs = [config for config in existing if config.get("name") not in LEGACY_MEMORY_READERS]
        configs.extend(
            {"name": name, "enabled": False, "permission_policy": {"type": "always_ask"}}
            for name in sorted(LEGACY_MEMORY_READERS)
        )
        result.append({**tool, "configs": configs})
    return result
