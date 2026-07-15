"""Pure helpers: merge the default daimon-mcp wiring into an AgentSpec.

Two halves, both gated on the same `public_url is not None` condition:

- `merge_default_mcp_server` — adds a `BetaManagedAgentsURLMCPServerParams`
  entry to `AgentSpec.mcp_servers` with `name='daimon-mcp'`.
- `merge_default_mcp_toolset` — adds a `BetaManagedAgentsMCPToolsetParams`
  entry to `AgentSpec.tools` with `mcp_server_name='daimon-mcp'`. Required
  because MA validates that every server in `mcp_servers` is referenced by
  some `mcp_toolset` tool. Without this, MA returns 400.

Both are idempotent and called in sequence from
`defaults/reconcile_agents.reconcile_agent` before `dump_agent_spec`.
"""

from __future__ import annotations

from typing import Final, cast

import structlog
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_mcp_toolset_params import (
    BetaManagedAgentsMCPToolsetParams,
)
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)

_log = structlog.get_logger(__name__)

DAIMON_MCP_SERVER_NAME: Final[str] = "daimon-mcp"


def get_reserved_mcp_rejection(*, server_name: str, url: str, public_url: str | None) -> str | None:
    """Return the rejection reason when (server_name, url) would collide with
    the reserved daimon-mcp entry, or None when the attach/add is allowed.

    - Name check is unconditional: fires even when public_url is None.
    - URL check is gated on public_url is not None and uses trailing-slash-insensitive
      comparison.

    Pure. Raises nothing, performs no I/O. Callers decide how to surface the
    rejection (ToolError in chat, ephemeral message in the panel).
    """
    if server_name == DAIMON_MCP_SERVER_NAME:
        return (
            f"'{DAIMON_MCP_SERVER_NAME}' is the reserved built-in daimon server and cannot be "
            "replaced or re-pointed. Pick a different server name."
        )
    if public_url is not None and url.rstrip("/") == public_url.rstrip("/"):
        return (
            f"'{url}' is the deployment's own MCP endpoint (already registered as "
            f"'{DAIMON_MCP_SERVER_NAME}'). Attaching it under another name would create a "
            "duplicate entry. Pick a different URL."
        )
    return None


def is_corrupted_daimon_mcp_entry(*, name: str | None, url: str | None, public_url: str) -> bool:
    """Return True when an MCP server entry carries the reserved daimon-mcp name
    but a URL that does not match the canonical deployment endpoint.

    Takes primitives so spec-side TypedDict callers (entry.get("name")) and
    MA-side SDK-model callers (entry.name, entry.url) both extract fields in
    their own access style before calling this predicate.

    Pure. Raises nothing, performs no I/O.
    """
    return name == DAIMON_MCP_SERVER_NAME and (url or "").rstrip("/") != public_url.rstrip("/")


def merge_default_mcp_server(
    existing: list[BetaManagedAgentsURLMCPServerParams] | None,
    public_url: str | None,
) -> list[BetaManagedAgentsURLMCPServerParams] | None:
    """Return `existing` with a default daimon-mcp entry appended iff missing.

    - `public_url is None` -> returns `existing` unchanged (no-op for Phase-1-only deployments).
    - Corrupted entries (name==daimon-mcp but url != public_url) are replaced
      with the canonical entry and a warning is emitted. Degenerate inputs
      containing multiple daimon-mcp-named entries collapse to exactly one.
    - Canonical URL already present (slash-insensitive) after heal -> returns
      `existing` unchanged (identity no-churn contract: the same object the
      caller passed, so reconcile_agents.py:69 identity check stays stable).
    - Otherwise -> returns a new list with the default entry appended.

    Does not mutate `existing`.
    """
    if public_url is None:
        return existing
    current = list(existing) if existing is not None else []

    # Heal: drop any entry that carries the reserved name but a foreign URL.
    healed: list[BetaManagedAgentsURLMCPServerParams] = []
    found_corruption = False
    for entry in current:
        if is_corrupted_daimon_mcp_entry(
            name=entry.get("name"), url=entry.get("url"), public_url=public_url
        ):
            found_corruption = True
            _log.warning(
                "mcp_merge.healed_corrupted_daimon_mcp",
                name=entry.get("name"),
                corrupted_url=entry.get("url"),
                canonical_url=public_url,
            )
        else:
            healed.append(entry)

    # After dropping corrupted entries, check if the canonical URL is already present.
    canonical_present = any(
        entry.get("url", "").rstrip("/") == public_url.rstrip("/") for entry in healed
    )
    if canonical_present:
        if not found_corruption:
            # Nothing changed: return the same object (identity no-churn contract).
            return existing
        # Heal dropped entries but canonical was already present via another name.
        return healed

    # Append the canonical entry.
    default_entry = cast(
        BetaManagedAgentsURLMCPServerParams,
        {"name": DAIMON_MCP_SERVER_NAME, "type": "url", "url": public_url},
    )
    healed.append(default_entry)
    return healed


def merge_default_mcp_toolset(
    existing: list[Tool] | None,
    public_url: str | None,
) -> list[Tool] | None:
    """Return `existing` with a default `mcp_toolset` tool appended iff missing.

    The toolset entry references the daimon-mcp server by `mcp_server_name`,
    which MA cross-checks against `AgentSpec.mcp_servers[*].name`.

    - `public_url is None` -> returns `existing` unchanged (no-op).
    - A tool with `type=='mcp_toolset'` and `mcp_server_name=='daimon-mcp'`
      already present -> returns `existing` unchanged.
    - Otherwise -> returns a new list with the default toolset appended.

    `default_config` is intentionally omitted; `dump_agent_spec` injects
    `permission_policy={'type': 'always_allow'}` at the SDK boundary.

    Does not mutate `existing`.
    """
    if public_url is None:
        return existing
    current = list(existing) if existing is not None else []
    for tool in current:
        if (
            tool.get("type") == "mcp_toolset"
            and tool.get("mcp_server_name") == DAIMON_MCP_SERVER_NAME
        ):
            return existing  # idempotent: same object the caller passed
    default_toolset = cast(
        Tool,
        cast(
            BetaManagedAgentsMCPToolsetParams,
            {"type": "mcp_toolset", "mcp_server_name": DAIMON_MCP_SERVER_NAME},
        ),
    )
    current.append(default_toolset)
    return current
