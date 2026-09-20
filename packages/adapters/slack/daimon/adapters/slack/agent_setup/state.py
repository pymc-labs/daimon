"""AgentSetupState — identifiers-only private_metadata and pure reducers for /agent-setup.

Pure logic only: frozen dataclasses, encode/decode helpers, and reducer functions.
No I/O, no clock, no DB, no slack_sdk. Identifiers only — workspace id is re-derived
server-side per event, never serialized here.

Port of the BEHAVIOR from discord/agent_setup/state.py (apply_agent_modal rename-forbidden
invariant) collapsed to stateless identifiers-only functions (no fat PanelState self).
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Final, Literal, cast

from daimon.adapters.slack.modal_limits import MAX_PRIVATE_METADATA_CHARS

__all__ = [
    "PANEL_PAGE_SIZE",
    "AgentSetupState",
    "PanelExpansion",
    "PanelMetadata",
    "PanelViewName",
    "RosterEntry",
    "decode_panel_metadata",
    "decode_private_metadata",
    "encode_panel_metadata",
    "encode_private_metadata",
    "apply_agent_modal",
]

#: Rows the panel shows on one page. Sized for Slack's 100-block view: a roster
#: row costs a section plus a context, so 20 rows plus the chrome and the pager
#: leaves better than half the budget spare.
PANEL_PAGE_SIZE: Final = 20

PanelViewName = Literal["agents", "details", "routing", "new_agent", "creating"]
"""Which of the panel's screens a view is showing."""

PanelExpansion = Literal["keys", "skills", "connections"]
"""A list the reader has asked to see in full rather than collapsed."""

_PANEL_VIEW_NAMES: Final[frozenset[str]] = frozenset(
    {"agents", "details", "routing", "new_agent", "creating"}
)
_PANEL_EXPANSIONS: Final[tuple[PanelExpansion, ...]] = ("keys", "skills", "connections")


@dataclasses.dataclass(frozen=True)
class PanelMetadata:
    """What a panel view carries in `private_metadata` to serve its next click.

    Identifiers and screen position only. No tenant id and no MA id: both are
    re-derived server-side from the event, so a payload replayed from another
    workspace cannot name a resource it has no claim to.

    `root_view_id` is the one piece of Slack state kept here — the New agent
    form and the Creating placeholder need to refresh the root roster after
    they finish, and by then the payload is all they have.
    """

    team_id: str
    channel_id: str
    view: PanelViewName
    page: int = 0
    agent_name: str | None = None
    root_view_id: str | None = None
    expanded: PanelExpansion | None = None

    def with_page(self, page: int) -> PanelMetadata:
        """Same view, a different page. Negative pages clamp to the first."""
        return dataclasses.replace(self, page=max(page, 0))

    def with_view(
        self,
        view: PanelViewName,
        *,
        agent_name: str | None = None,
        root_view_id: str | None = None,
        page: int | None = None,
    ) -> PanelMetadata:
        """Move to `view`, keeping the page the reader is on unless told otherwise.

        The page travels because the pushed view has to hand it back: the New
        agent form refreshes the root roster once the create lands, and a
        refresh that dropped the page would move the reader to the first page
        of a roster they were three pages into. A screen that pages over its
        own list passes `page=0` explicitly.
        """
        return dataclasses.replace(
            self,
            view=view,
            page=self.page if page is None else max(page, 0),
            agent_name=agent_name,
            root_view_id=root_view_id,
            expanded=self.expanded if agent_name == self.agent_name else None,
        )

    def toggled(self, expansion: PanelExpansion) -> PanelMetadata:
        """Flip one collapsed list open, or an open one closed."""
        return dataclasses.replace(self, expanded=None if self.expanded == expansion else expansion)


def encode_panel_metadata(meta: PanelMetadata) -> str:
    """Serialize `meta` as compact JSON, omitting every field still at default.

    Keys are one character because the budget is 3,000 characters for the
    whole string and an agent name has to fit inside it; a field at its
    default is left out entirely rather than written as null.
    """
    payload: dict[str, Any] = {"t": meta.team_id, "c": meta.channel_id, "v": meta.view}
    if meta.page:
        payload["p"] = meta.page
    if meta.agent_name is not None:
        payload["a"] = meta.agent_name
    if meta.root_view_id is not None:
        payload["r"] = meta.root_view_id
    if meta.expanded is not None:
        payload["x"] = meta.expanded
    encoded = json.dumps(payload, separators=(",", ":"))
    if len(encoded) > MAX_PRIVATE_METADATA_CHARS:
        raise ValueError(
            f"panel metadata is {len(encoded)} characters; "
            f"Slack allows {MAX_PRIVATE_METADATA_CHARS}"
        )
    return encoded


def decode_panel_metadata(raw: str) -> PanelMetadata | None:
    """Parse `raw` back into `PanelMetadata`, or None if it is not one.

    Total: malformed JSON, a JSON value that is not an object, a missing or
    wrong-typed identifier, and an unrecognised view name all give None. The
    caller treats None as "this click carries no usable panel state" rather
    than rendering a view built on half-read identifiers.
    """
    if not raw:
        return None
    try:
        decoded: object = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    payload = cast("dict[str, object]", decoded)
    team_id = payload.get("t")
    channel_id = payload.get("c")
    view = payload.get("v")
    if not isinstance(team_id, str) or not isinstance(channel_id, str):
        return None
    if not isinstance(view, str) or view not in _PANEL_VIEW_NAMES:
        return None
    page = payload.get("p", 0)
    if not isinstance(page, int) or isinstance(page, bool) or page < 0:
        return None
    agent_name = payload.get("a")
    root_view_id = payload.get("r")
    if agent_name is not None and not isinstance(agent_name, str):
        return None
    if root_view_id is not None and not isinstance(root_view_id, str):
        return None
    raw_expanded = payload.get("x")
    expanded: PanelExpansion | None = None
    if isinstance(raw_expanded, str) and raw_expanded in _PANEL_EXPANSIONS:
        expanded = raw_expanded
    elif isinstance(raw_expanded, list):
        # Older views carried a list and could have more than one open. Pick
        # the first known value so a click from an already-open modal remains usable.
        expanded = next((name for name in _PANEL_EXPANSIONS if name in raw_expanded), None)
    elif raw_expanded is not None:
        return None
    return PanelMetadata(
        team_id=team_id,
        channel_id=channel_id,
        view=cast("PanelViewName", view),
        page=page,
        agent_name=agent_name,
        root_view_id=root_view_id,
        expanded=expanded,
    )


@dataclasses.dataclass(frozen=True)
class RosterEntry:
    """Decorated view-model for one roster row — identifiers only, no fat state."""

    agent_name: str
    model_id: str
    ma_agent_id: str | None = None


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
