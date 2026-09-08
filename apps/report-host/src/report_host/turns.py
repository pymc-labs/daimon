"""The per-thread turn driver: send, poll, stream text, terminate, settle.

Split functional-core / imperative-shell. ``read_turn_progress`` is the pure
decision: given a session's status and its events since a turn's boundary,
what new text arrived and is the turn over. ``run_turn`` (the imperative
shell around it — the only place that calls the seam, sleeps, or touches the
clock or the database) lands alongside it in this module.

**The done rule** (SPEC D-07, §1.3): a turn is finished only when a
``session.status_idle`` event survives the boundary filter, or
``get_my_session`` reports ``terminated``. A bare ``status == "idle"`` is
never trusted — right after a send the session has not started yet and
reads idle — and ``rescheduling`` counts as running. This is the exact bug
the prototype (``spikes/report-host/host/app.py``) hit: it trusted a bare
idle status and ended a follow-up question in three seconds with nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast


@dataclass(frozen=True)
class TurnProgress:
    """One poll's worth of decision: what new text arrived, and is it over."""

    new_texts: tuple[str, ...]
    is_done: bool
    terminal_reason: Literal["idle", "terminated"] | None
    new_event_ids: frozenset[str]


def _agent_message_text(event: dict[str, object]) -> str | None:
    """Fold an ``agent.message`` event's text blocks, the way the seam's own
    transcript reader does (``daimon.adapters.mcp.tools.agent_chat``)."""
    content = event.get("content")
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for raw_block in cast("list[object]", content):
        if not isinstance(raw_block, dict):
            continue
        block = cast("dict[str, object]", raw_block)
        if block.get("type") == "text" and block.get("text") is not None:
            texts.append(str(block["text"]))
    text = "\n".join(texts)
    return text or None


def read_turn_progress(
    *,
    status: str,
    events: Sequence[dict[str, object]],
    turn_event_id: str,
    seen_event_ids: frozenset[str],
) -> TurnProgress:
    """Pure: decide what is new and whether the turn is over.

    No clock, no I/O. Every event whose id is ``turn_event_id`` (the
    boundary — ``created_at_gte`` is inclusive, so it comes back on every
    poll) or is already in ``seen_event_ids`` is dropped before anything
    else is decided, so a boundary that is itself an idle event, or an
    already-reported answer, can never end or re-report a turn.

    A bare ``status == "idle"`` with no surviving ``session.status_idle``
    event is NOT done — a session that has not started yet reads idle right
    after a send. Neither is ``status == "rescheduling"``. The turn is done
    only when an idle event survives the filter, or ``status == "terminated"``.
    """
    new_texts: list[str] = []
    new_ids: set[str] = set()
    idle_event_survived = False

    for event in events:
        event_id = event.get("id")
        if not isinstance(event_id, str):
            continue
        if event_id == turn_event_id or event_id in seen_event_ids:
            continue
        new_ids.add(event_id)

        event_type = event.get("type")
        if event_type == "agent.message":
            text = _agent_message_text(event)
            if text is not None:
                new_texts.append(text)
        elif event_type == "session.status_idle":
            idle_event_survived = True

    if idle_event_survived:
        is_done, terminal_reason = True, "idle"
    elif status == "terminated":
        is_done, terminal_reason = True, "terminated"
    else:
        is_done, terminal_reason = False, None

    return TurnProgress(
        new_texts=tuple(new_texts),
        is_done=is_done,
        terminal_reason=terminal_reason,
        new_event_ids=frozenset(new_ids),
    )
