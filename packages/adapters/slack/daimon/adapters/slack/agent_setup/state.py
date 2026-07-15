"""AgentSetupState — identifiers-only private_metadata and pure reducers for /agent-setup.

Pure logic only: frozen dataclasses, encode/decode helpers, and reducer functions.
No I/O, no clock, no DB, no slack_sdk. Identifiers only — workspace id is re-derived
server-side per event, never serialized here.

Port of the BEHAVIOR from discord/agent_setup/state.py (apply_agent_modal rename-forbidden
invariant, apply_mcp_modal, remove_skill_at, remove_mcp_at) collapsed to stateless
identifiers-only functions (no fat PanelState self).
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

__all__ = [
    "AgentSetupState",
    "RosterEntry",
    "decode_private_metadata",
    "encode_private_metadata",
    "apply_agent_modal",
    "apply_mcp_modal",
    "remove_skill_at",
    "remove_mcp_at",
]


@dataclasses.dataclass(frozen=True)
class RosterEntry:
    """Decorated view-model for one roster row — identifiers only, no fat state."""

    agent_name: str
    model_id: str


@dataclasses.dataclass(frozen=True)
class AgentSetupState:
    """State for the /agent-setup panel — identifiers only, no fat in-memory objects."""

    rows: list[RosterEntry]
    over_cap_count: int


def encode_private_metadata(
    *,
    team_id: str,
    channel_id: str,
    selected_agent_name: str | None = None,
    agent_name: str | None = None,
    active_section: str | None = None,
    parent_section: str | None = None,
) -> str:
    """Serialize Slack modal private_metadata — identifiers ONLY (no workspace-derived UUIDs).

    Builds only the keys whose value is not None so callers can omit L2/L3 fields
    when building an L1 payload, and so the serialized string stays well within the
    Slack 3,000-character limit.

    Level mapping:
      L1 — team_id, channel_id, selected_agent_name (null when no agent is selected)
      L2 — adds agent_name, active_section
      L3 — adds parent_section
    """
    d: dict[str, Any] = {
        "team_id": team_id,
        "channel_id": channel_id,
    }
    if selected_agent_name is not None:
        d["selected_agent_name"] = selected_agent_name
    if agent_name is not None:
        d["agent_name"] = agent_name
    if active_section is not None:
        d["active_section"] = active_section
    if parent_section is not None:
        d["parent_section"] = parent_section
    return json.dumps(d, separators=(",", ":"))


def decode_private_metadata(raw: str) -> dict[str, Any]:
    """Deserialize Slack modal private_metadata — total function, never raises.

    Mirrors the parse pattern from privacy_panel/submit.py:69-82. Returns an empty
    dict on malformed or empty input so callers can safely call .get() on the result.
    """
    try:
        return json.loads(raw) if raw else {}  # type: ignore[no-any-return]
    except (json.JSONDecodeError, ValueError):
        return {}


def apply_agent_modal(
    *,
    model_id: str | None,
    system_prompt: str | None,
) -> dict[str, Any]:
    """Return the field-update dict for an agent-modal edit.

    RENAME-FORBIDDEN: this function intentionally accepts NO name/agent_name
    parameter. Rename = Fork + Delete (Structural Guarantee #6, 83-UI-SPEC.md;
    mirrors Discord apply_agent_modal's invariant — Pitfall 4).

    The returned dict is applied by the write layer (write.py) to the MA agent spec.
    """
    result: dict[str, Any] = {}
    if model_id is not None:
        result["model"] = model_id
    if system_prompt is not None:
        result["system"] = system_prompt
    return result


def apply_mcp_modal(
    *,
    name: str,
    endpoint: str,
    has_token: bool,
) -> dict[str, Any]:
    """Return the MCP-server entry dict for an add-MCP-modal submission.

    Reserved-name validation and endpoint format checking live in submit.py
    (pure format validators run pre-ack). This function only constructs the
    shape the write layer needs.
    """
    return {
        "name": name,
        "url": endpoint,
        "has_token": has_token,
    }


def remove_skill_at(skills: list[str], index: int) -> list[str]:
    """Return a new skills list with the element at index removed.

    Out-of-range index returns the list unchanged — caller does not need to
    guard bounds before calling.
    """
    if not (0 <= index < len(skills)):
        return list(skills)
    result = list(skills)
    result.pop(index)
    return result


def remove_mcp_at(mcps: list[str], index: int) -> list[str]:
    """Return a new MCP list with the element at index removed.

    Out-of-range index returns the list unchanged — caller does not need to
    guard bounds before calling.
    """
    if not (0 <= index < len(mcps)):
        return list(mcps)
    result = list(mcps)
    result.pop(index)
    return result
