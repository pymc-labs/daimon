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

from anthropic.types.beta.sessions import BetaManagedAgentsUserToolConfirmationEventParams
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import StopReason


def pending_confirmation_ids(
    stop_reason: StopReason | None,
    *,
    confirmed: set[str],
) -> list[str]:
    """Return `stop_reason.event_ids` not already in `confirmed`.

    Takes the SDK `StopReason` union directly (not the `session.status_idle`
    event that carries it) so this ONE implementation serves both call
    sites that need it: the live consume loop, which has the idle event
    itself, and the eventless-cycle reconnect branch in `driver.py`, which
    only has the folded `TurnState.stop_reason` (the reducer stores the
    same SDK union verbatim). A second, near-identically-named function for
    the reconnect branch is precisely where a reconnect-dedup bug would
    hide — one function, two callers, zero duplicated dedup logic.

    Order is preserved from `stop_reason.event_ids`. Does NOT mutate
    `confirmed` — the caller owns that step, so "which ids did we just
    claim" stays visible at the call site instead of hidden inside this
    function.

    `None` and any non-`requires_action` member return `[]` rather than
    raising, so neither caller needs a pre-check: the live loop already
    knows `terminal_stop_reason(event) == "requires_action"` before
    calling, and the reconnect branch may have folded a `TurnState` whose
    `stop_reason` is `None` or some other member entirely.
    """
    if stop_reason is None or stop_reason.type != "requires_action":
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
