"""Pure decision logic for `AutoApprove` tool confirmations.

This module is pure — no `anthropic` client, no `httpx`, no DB, no clock —
so the dedup rule (which `tool_use_id`s are still fresh) is unit-testable
without a fake stream, which is exactly where a reconnect-dedup bug would
otherwise hide. The SEND itself (`anthropic.beta.sessions.events.send(...)`)
stays in `driver.py` because it is I/O; this module only decides WHAT to
send.

`build_confirmation_events` hardcodes `result="allow"` because `AutoApprove`
is the only posture that ever reaches this module — a future policy variant
(deny some tools, ask a callback) is a new `ToolConfirmation` member plus a
new function here, not a bool parameter bolted onto this one.
"""

from __future__ import annotations

from collections.abc import Sequence

from anthropic.types.beta.sessions import (
    BetaManagedAgentsSessionStatusIdleEvent,
    BetaManagedAgentsUserToolConfirmationEventParams,
)


def pending_confirmation_ids(
    event: BetaManagedAgentsSessionStatusIdleEvent,
    *,
    confirmed: set[str],
) -> list[str]:
    """Return the `stop_reason.event_ids` not already in `confirmed`.

    Order is preserved from the event's own `event_ids`. Does NOT mutate
    `confirmed` — the caller owns that step, so "which ids did we just
    claim" stays visible at the call site instead of hidden inside this
    function.

    Narrows on `stop_reason.type == "requires_action"` using the real SDK
    type; returns an empty list for any other stop reason rather than
    raising, so a caller that has already checked
    `terminal_stop_reason(event) == "requires_action"` can call this
    directly.
    """
    stop_reason = event.stop_reason
    if stop_reason.type != "requires_action":
        return []
    return [tool_use_id for tool_use_id in stop_reason.event_ids if tool_use_id not in confirmed]


def build_confirmation_events(
    ids: Sequence[str],
) -> list[BetaManagedAgentsUserToolConfirmationEventParams]:
    """Build one `user.tool_confirmation` `allow` payload per id, in order."""
    return [
        BetaManagedAgentsUserToolConfirmationEventParams(
            type="user.tool_confirmation",
            result="allow",
            tool_use_id=tool_use_id,
        )
        for tool_use_id in ids
    ]
