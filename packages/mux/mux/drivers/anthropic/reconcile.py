"""Rebuild a session's projection from the Managed Agents session record and event log.

The native status says whether the session is working; the event log says which
root turn it is working on and what it waits for. Both are read, folded and
cross-checked. A state that cannot be classified is an error, never `idle`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from anthropic import AsyncAnthropic
from pydantic import JsonValue

from mux.contracts.events import Event, RequiredAction, RequiresActionPayload
from mux.contracts.ids import ResourceRef
from mux.contracts.resources import ProjectionSnapshot
from mux.drivers.anthropic.normalize import EventNormalizer, object_json
from mux.drivers.anthropic.resources._errors import provider_call, provider_iter
from mux.errors import ProviderError

PAGE_SIZE = 100
MAX_EVENTS = 5_000
"""Newest events read per reconcile. Older history is reported as a gap."""
HISTORY_TRUNCATED = "history_truncated"
STATUS_DISAGREEMENT = "status_events_disagree"

type State = Literal["idle", "running", "requires_action", "terminated"]

_NATIVE_STATE: dict[str, State] = {
    "idle": "idle",
    "running": "running",
    # Anthropic is retrying a failed model call; the turn is still in flight.
    "rescheduling": "running",
    "terminated": "terminated",
}
_TURN_END_REASONS = {"end_turn", "budget_reached", "retries_exhausted"}


def native_state(status: object) -> State:
    if isinstance(status, str) and status in _NATIVE_STATE:
        return _NATIVE_STATE[status]
    raise ProviderError("upstream", retryable=False, native_code="unknown_session_status")


def _settles_turn(raw: dict[str, JsonValue]) -> bool:
    """True for a native record after which no earlier history changes the state."""
    if raw.get("type") == "session.status_terminated":
        return True
    stop = raw.get("stop_reason")
    return (
        raw.get("type") == "session.status_idle"
        and isinstance(stop, dict)
        and stop.get("type") in _TURN_END_REASONS
    )


@dataclass
class _Fold:
    state: State | Literal["unknown"] | None = None
    active: str | None = None
    actions: dict[str, RequiredAction] = field(default_factory=dict[str, RequiredAction])

    def apply(self, event: Event) -> None:
        if event.type == "session.status_running":
            self.state, self.active, self.actions = "running", event.turn_id, {}
        elif event.type == "session.requires_action":
            payload = event.typed_payload()
            if not isinstance(payload, RequiresActionPayload):
                raise ValueError("invalid required-action payload")
            self.state, self.active = "requires_action", event.turn_id
            self.actions = {action.id: action for action in payload.actions}
        elif event.type == "session.turn_ended":
            self.state, self.active, self.actions = "idle", None, {}
        elif event.type == "session.status_terminated":
            self.state, self.active, self.actions = "terminated", None, {}
        elif event.type == "native.session.status_idle":
            self.state, self.active, self.actions = "unknown", None, {}
        elif event.type == "agent.tool_result" and self.state == "requires_action":
            call_id = event.payload.get("call_id")
            self.actions = {k: a for k, a in self.actions.items() if a.call_id != call_id}


def _resolve(native: State, fold: _Fold) -> tuple[State, bool]:
    """Combine the native status with the folded log; the bool flags a disagreement."""
    if fold.state == "unknown":
        raise ProviderError("upstream", retryable=False, native_code="unknown_idle_stop_reason")
    if native == "terminated" or fold.state == "terminated":
        return "terminated", False
    if fold.state is None or fold.state == native:
        return native, False
    if fold.state == "requires_action" and native == "idle":
        # A session paused for input reports native `idle`.
        return "requires_action", False
    # The two reads straddled a change. Never report idle for a session that may be working.
    return "running", True


def project(
    session: ResourceRef,
    native: State,
    events: Sequence[Event],
    *,
    truncated: bool,
    taken_at: datetime,
) -> ProjectionSnapshot:
    fold = _Fold()
    for event in events:
        fold.apply(event)
    state, disagree = _resolve(native, fold)
    actions = tuple(fold.actions.values()) if state == "requires_action" else ()
    active = fold.active if state in ("running", "requires_action") else None
    gaps = (HISTORY_TRUNCATED,) if truncated else ()
    if disagree:
        gaps += (STATUS_DISAGREEMENT,)
    return ProjectionSnapshot(
        session=session,
        cursor=events[-1].id if events else "0",
        state=state,
        active_root_turn=active,
        required_actions=actions,
        gaps=gaps,
        taken_at=taken_at,
    )


async def reconcile_session(client: AsyncAnthropic, session: ResourceRef) -> ProjectionSnapshot:
    """Read the status, then the newest events, and project them. Authorize first."""
    record = await provider_call(client.beta.sessions.retrieve(session.id))
    native = native_state(record.status)
    raws: list[dict[str, JsonValue]] = []
    truncated, boundaries = False, 0
    # Newest first, so a long session costs the recent turn and not the whole log.
    async for item in provider_iter(
        client.beta.sessions.events.list(session_id=session.id, limit=PAGE_SIZE, order="desc")
    ):
        if len(raws) >= MAX_EVENTS:
            truncated = True
            break
        raw = object_json(item.model_dump(mode="json"))
        raws.append(raw)
        boundaries += _settles_turn(raw)
        if boundaries == 2:
            break
    raws.reverse()
    normalizer = EventNormalizer(session)
    try:
        taken_at = datetime.now(UTC)
        events = [normalizer.normalize(raw, observed_at=taken_at) for raw in raws]
        return project(session, native, events, truncated=truncated, taken_at=taken_at)
    except (ValueError, KeyError, TypeError) as error:
        raise ProviderError("upstream", retryable=False, native_code="malformed_event") from error
